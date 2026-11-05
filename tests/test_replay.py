"""End-to-end replay of the two synthetic sample SRTs from tests/srt_samples.py (fr-FR readable, en-US
gibberish, with a short English passage where it flips) through the session: ingest -> sync deltas -> state -> plan."""

import asyncio
import json

from pacenote.capture import ReplaySource
from pacenote.contract import PLAN_SECTIONS
from pacenote.llm import OfflineEngine
from pacenote.prompts import PLAN_HEADINGS
from pacenote.session import Session, Settings

from .conftest import SAMPLES, edit, result, upd

FILES = {"fr": SAMPLES / "fr.srt", "en": SAMPLES / "en.srt"}


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class ScriptedEngine:
    """Stands in for Claude: inspects each delta like the model would and returns contract-valid results."""

    name = "scripted"

    def __init__(self):
        self.calls = []
        self.plan_prompts = []

    def sync(self, system, user, *, state, delta):
        self.calls.append({"system": system, "user": user, "delta": delta})
        text = " ".join(s.text for s in delta)
        updates, edits, enough = [], [], False
        if "dix à douze semaines" in text:
            updates.append(upd("import-flow", "answered", "Usine en Italie, 10-12 semaines jusqu'à l'entrepôt"))
            edits.append(edit("add", "import-delays", "import", "Qu'est-ce qui rallonge ces délais ?",
                              "What makes those lead times longer?", "import-flow"))
            edits.append(edit("reword", "import-flow", fr="(tentative de reformulation)", en="(attempt)"))
        if "payback within twelve months" in text:  # readable only in the EN stream
            updates.append(upd("roi-expectation", "answered", "Retour sur investissement en moins de 12 mois"))
        if "merci beaucoup" in text:
            enough = True
        return result(question_updates=updates, queue_edits=edits, next_questions=[], enough_data=enough,
                      enough_data_reason="fin de l'entretien" if enough else "")

    def write_plan(self, system, user, *, state):
        self.plan_prompts.append((system, user))
        h = PLAN_HEADINGS[state.language]
        sections = ["context", "priorities", "workflows", "quick_wins", "plan_30_60_90", "risks", "budget"]
        body = "\n\n".join(f"## {h[k]}\n\n- contenu" for k in sections)
        # a model-written interview log must be replaced by ours
        return f"# {h['title']} — {state.client}\n\n{body}\n\n## {h['log']}\n\n- à supprimer\n"


def test_replay_source_emits_both_streams_by_time():
    clock = FakeClock()
    src = ReplaySource(FILES, speed=1.0, clock=clock)
    src.start()
    assert src.poll() == []
    clock.t += 20
    first = src.poll()
    assert {c.stream for c in first} == {"fr", "en"}
    assert all(c.end <= 20 for c in first)
    rest = src.poll(final=True)
    assert len(first) + len(rest) == 2 * 27
    assert src.exhausted


def test_replay_session_end_to_end(tmp_path, client_cfg):
    asyncio.run(_run_session(tmp_path, client_cfg))


async def _run_session(tmp_path, client_cfg):
    clock = FakeClock()
    engine = ScriptedEngine()
    session = Session(
        client=client_cfg,
        session_dir=tmp_path / "s1",
        engine=engine,
        source_factory=lambda d: ReplaySource(FILES, speed=1.0, clock=clock),
        settings=Settings(sync_interval=3600, poll_interval=3600),  # we drive syncs by hand
    )
    await session.start()
    assert session.state.mode == "interview"

    # Every 40 s of "audio", one sync round with only the new delta.
    seen_seqs = set()
    for _ in range(6):
        clock.t += 40
        await session.sync_once()
    await session.shutdown()

    calls = engine.calls
    assert len(calls) >= 4
    for c in calls:
        seqs = {s.seq for s in c["delta"]}
        assert not (seqs & seen_seqs), "a segment was sent twice"
        seen_seqs |= seqs
        assert {s.stream for s in c["delta"]} == {"fr", "en"}  # merged FR+EN delta
        assert "FR:" in c["user"] and "EN:" in c["user"]
        assert "<state>" in c["user"] and "<new_transcript>" in c["user"]
    assert len(seen_seqs) == 2 * 27  # everything delivered exactly once
    # glossary + rules are in the (cached, stable) system prompt
    assert "5D" in calls[0]["system"] and "Fornaxa" in calls[0]["system"]
    assert calls[0]["system"] == calls[-1]["system"]
    # later syncs carry the previous context for continuity
    assert "(start of interview)" in calls[0]["user"]
    assert "(start of interview)" not in calls[1]["user"]

    st = session.state
    assert st.get("import-flow").status == "answered"
    assert st.get("import-flow").text_fr.startswith("Comment se passe")  # reword after lock was rejected
    assert st.get("roi-expectation").status == "answered"
    assert st.get("import-delays").origin == "added"
    assert st.enough_data and st.mode == "finalize"

    # Wispr Flow transcript merged before generating the plan
    await session.set_wispr("Transcript Wispr final : dix à douze semaines entre commande et arrivée.")
    files = await session.finalize()
    assert st.mode == "done"
    system, user = engine.plan_prompts[0]
    assert "<wispr_flow_transcript>" in user and "Transcript Wispr final" in user
    assert "<raw_transcript>" in user

    plan_md = (tmp_path / "s1" / "plan.md").read_text(encoding="utf-8")
    h = PLAN_HEADINGS["fr"]
    for key in ("context", "priorities", "workflows", "quick_wins", "plan_30_60_90", "risks", "budget", "log"):
        assert f"## {h[key]}" in plan_md
    assert plan_md.count(f"## {h['log']}") == 1 and "à supprimer" not in plan_md
    assert "Usine en Italie" in plan_md  # extracted answer in the interview log
    assert not (tmp_path / "s1" / "plan.html").exists()  # the app shows plan.md; PDF on demand
    assert files["md"].endswith("plan.md")

    # persisted artefacts
    sdir = tmp_path / "s1"
    for name in ("state.json", "transcript.jsonl", "sync_log.jsonl", "wispr.txt"):
        assert (sdir / name).exists(), name
    log = [json.loads(line) for line in (sdir / "sync_log.jsonl").read_text().splitlines()]
    assert any(e.get("rejected") for e in log)  # the rejected reword is logged


def test_replay_with_offline_engine(tmp_path, client_cfg):
    async def run():
        session = Session(
            client=client_cfg,
            session_dir=tmp_path / "s2",
            engine=OfflineEngine(),
            source_factory=lambda d: ReplaySource(FILES, speed=0),
            settings=Settings(sync_interval=3600, poll_interval=3600),
        )
        await session.start()
        assert await session.sync_once()
        files = await session.finalize()
        await session.shutdown()
        return session, files

    session, files = asyncio.run(run())
    st = session.state
    assert st.get("import-flow").status == "answered"
    assert st.get("it-outage").status in ("partial", "answered")
    assert st.sync_count >= 1
    assert sum(st.coverage.values()) > 0
    plan = (tmp_path / "s2" / "plan.md").read_text(encoding="utf-8")
    assert "Proposition d'intégration — Brioche & Four" in plan
    assert set(PLAN_SECTIONS)  # sanity


def test_sync_failure_keeps_delta_for_next_round(tmp_path, client_cfg):
    from pacenote.llm import SyncFailed

    class Flaky(ScriptedEngine):
        def __init__(self):
            super().__init__()
            self.fail = True

        def sync(self, system, user, *, state, delta):
            if self.fail:
                self.fail = False
                raise SyncFailed("boom")
            return super().sync(system, user, state=state, delta=delta)

    async def run():
        clock = FakeClock()
        engine = Flaky()
        session = Session(client=client_cfg, session_dir=tmp_path / "s3", engine=engine,
                          source_factory=lambda d: ReplaySource(FILES, speed=1.0, clock=clock),
                          settings=Settings(sync_interval=3600, poll_interval=3600))
        await session.start()
        clock.t += 30
        assert not await session.sync_once()
        assert session.state.last_sync_error == "boom"
        clock.t += 30
        assert await session.sync_once()
        assert session.state.last_sync_error is None
        await session.shutdown()
        first_delta = engine.calls[0]["delta"]
        assert first_delta[0].start < 2  # the failed slice was re-sent
    asyncio.run(run())


# --- the noisy sample (vague answers, a TV, en-US gibberish) against the example client ---------------


def test_replay_noisy_sample_with_offline_engine(tmp_path):
    from .conftest import NOISY, real_client

    async def run():
        session = Session(
            client=real_client(),
            session_dir=tmp_path / "noisy",
            engine=OfflineEngine(),
            source_factory=lambda d: ReplaySource({"fr": NOISY / "fr.srt", "en": NOISY / "en.srt"}, speed=0),
            settings=Settings(sync_interval=3600, poll_interval=3600),
        )
        await session.start()
        assert await session.sync_once()
        files = await session.finalize()
        await session.shutdown()
        return session, files

    session, files = asyncio.run(run())
    st = session.state
    assert st.get("capacity-time").status in ("partial", "answered")
    assert st.get("orders-walkthrough").status in ("partial", "answered")
    assert sum(st.coverage.values()) > 0
    assert "Proposition d'intégration — Brioche & Four" in (tmp_path / "noisy" / "plan.md").read_text(encoding="utf-8")
