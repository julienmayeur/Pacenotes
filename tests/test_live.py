"""Live-mode behaviour: fast incremental pass (debounce, persistent `claude` session), first
full sync shortly after the first speech, live draft answers and the readable transcript strip.
(listening start-up and the watchdog are in test_listen_capture.py.)"""

import asyncio
import json
import sys
import time

import pytest

from pacenote.capture import ReplaySource
from pacenote.contract import FastResult, empty_maps
from pacenote.llm import ClaudeCliEngine, ClaudeStreamSession, SyncFailed
from pacenote.session import Session, Settings
from pacenote.srt import load_srt
from pacenote.transcript import Transcript

from .conftest import SAMPLES, edit, result, upd

FILES = {"fr": SAMPLES / "fr.srt", "en": SAMPLES / "en.srt"}

# --- fast pass: state rules ----------------------------------------------------------------


def test_fast_result_rules(state):
    state.apply_sync_result(result(question_updates=[upd("roi-budget", "answered", "50-80 k€")]))
    cov_before = dict(state.coverage)
    cur = state.next_questions[0]
    rep = state.apply_fast_result(FastResult.model_validate({
        "question_updates": [upd(cur, "partial", "Fin de la ressaisie", "arrêtent la ressaisie"),
                             upd("roi-budget", "partial", "autre"),  # locked: ignored
                             upd("stock-tools", "answered", "Tout est dans Excel")],  # another question
        "coverage": [{"theme": "strategy", "score": 0.4, "note": ""}, {"theme": "roi", "score": 0.0, "note": ""}],
        "maps": {"org": [], "flows": [], "stores": [], "decisions": [], "catalog": []},
        "noise": [],
        "slots": [],
    }))
    q = state.get(cur)
    assert q.status == "partial" and q.answer == "Fin de la ressaisie" and q.updated_by == "fast" and q.updated_at
    assert state.get("roi-budget").answer == "50-80 k€"
    assert state.get("stock-tools").locked and "stock-tools" not in state.next_questions
    assert state.coverage["strategy"] >= 0.4 and state.coverage["roi"] == cov_before["roi"]  # raise only
    assert [r.target for r in rep.rejected] == ["roi-budget"]
    assert state.fast_count == 1 and state.sync_count == 1  # fast passes don't count as syncs
    assert [x.id for x in state.recent_updates(2)][0] in (cur, "stock-tools")


def test_reworded_questions_show_reworded_origin(state):
    state.apply_sync_result(result(queue_edits=[edit("reword", "stock-tools", fr="Le stock est-il dans l'ERP ?",
                                                     en="Is stock in the ERP?")]))
    assert state.get("stock-tools").origin == "reworded"
    state.apply_sync_result(result(queue_edits=[edit("add", "x", "stock", "Nouvelle ?", "New?")]))
    state.apply_sync_result(result(queue_edits=[edit("reword", "x", fr="Nouvelle v2 ?", en="New v2?")]))
    assert state.get("x").origin == "added"  # an added question stays "added"


# --- fast pass: timing ---------------------------------------------------------------------


class FastEngine:
    name = "fast-fake"

    def __init__(self):
        self.fast_calls = []
        self.sync_calls = []

    def fast(self, system, user, *, state, lines):
        self.fast_calls.append((time.time(), lines))
        cur = state.current_question
        return FastResult(question_updates=[upd(cur.id, "partial", " ".join(s.text for s in lines)[:120])], slots=[], maps=empty_maps(),
                          coverage=[], noise=[])

    def sync(self, system, user, *, state, delta):
        self.sync_calls.append((time.time(), delta))
        return result()

    def write_plan(self, system, user, *, state):
        return "# Plan\n"


class _NoFast:
    name = "nofast"

    def sync(self, system, user, *, state, delta):
        return result()

    def write_plan(self, system, user, *, state):
        return "# Plan\n"


def test_fast_pass_is_debounced_then_fills_the_current_answer(tmp_path, client_cfg):
    async def run():
        clock = [1000.0]
        engine = FastEngine()
        session = Session(
            client=client_cfg, session_dir=tmp_path / "f", engine=engine,
            source_factory=lambda d: ReplaySource(FILES, speed=1.0, clock=lambda: clock[0]),
            settings=Settings(sync_interval=3600, first_sync_delay=3600, poll_interval=0.02,
                              fast_debounce=0.4, fast_max_wait=5.0),
        )
        await session.start()
        clock[0] += 12  # two cues become available at once
        await asyncio.sleep(0.25)
        assert engine.fast_calls == []  # still inside the debounce window
        await asyncio.sleep(0.6)
        assert len(engine.fast_calls) == 1
        view = session.view()
        await session.shutdown()
        return session, engine, view

    session, engine, view = asyncio.run(run())
    _, lines = engine.fast_calls[0]
    assert {s.stream for s in lines} == {"fr", "en"}
    assert view["current"]["answer"].startswith("Bonjour")  # draft answer shown on the card
    assert all(r["id"] != view["current"]["id"] for r in view["history"])  # the thread excludes the focus
    assert view["fast"]["count"] == 1 and view["fast"]["latency"] is not None
    assert 0.3 <= view["fast"]["latency"] < 2.0  # debounce + call, measured from line arrival
    log = [json.loads(x) for x in (tmp_path / "f" / "sync_log.jsonl").read_text().splitlines()]
    assert log[-1]["kind"] == "fast" and log[-1]["latency"]["newest"] is not None


def test_fast_pass_max_wait_during_continuous_speech(tmp_path, client_cfg):
    async def run():
        clock = [1000.0]
        engine = FastEngine()
        session = Session(
            client=client_cfg, session_dir=tmp_path / "m", engine=engine,
            source_factory=lambda d: ReplaySource(FILES, speed=1.0, clock=lambda: clock[0]),
            settings=Settings(sync_interval=3600, first_sync_delay=3600, poll_interval=0.02,
                              fast_debounce=0.5, fast_max_wait=0.8),
        )
        await session.start()
        t0 = time.time()
        while time.time() - t0 < 1.4:  # a new cue every 0.15 s: debounce never expires
            clock[0] += 5
            await asyncio.sleep(0.15)
        await session.shutdown()
        return engine

    engine = asyncio.run(run())
    assert len(engine.fast_calls) >= 1  # max_wait forced a pass despite continuous speech


def test_first_full_sync_runs_shortly_after_first_speech(tmp_path, client_cfg):
    async def run():
        clock = [1000.0]
        engine = FastEngine()
        session = Session(
            client=client_cfg, session_dir=tmp_path / "e", engine=engine,
            source_factory=lambda d: ReplaySource(FILES, speed=1.0, clock=lambda: clock[0]),
            settings=Settings(sync_interval=3600, first_sync_delay=0.5, poll_interval=0.02, fast_enabled=False),
        )
        await session.start()
        await asyncio.sleep(0.8)
        assert engine.sync_calls == []  # no speech yet: no full sync
        clock[0] += 40
        first_line = time.time()
        await asyncio.sleep(1.2)
        await session.shutdown()
        return engine, first_line

    engine, first_line = asyncio.run(run())
    assert len(engine.sync_calls) == 1  # the next one waits for the 3600 s interval
    delay = engine.sync_calls[0][0] - first_line
    assert 0.4 <= delay < 1.1


# --- persistent claude session (stream-json) -----------------------------------------------

FAKE_STREAM = r'''#!{python}
import json, os, sys, time
log = {log!r}
with open(log, "a") as fh:
    fh.write(json.dumps({{"pid": os.getpid(), "argv": sys.argv[1:],
                         "env": {{k: v for k, v in os.environ.items() if k.startswith("ANTHROPIC")}}}}) + "\n")
mode = {mode!r}
print(json.dumps({{"type": "system", "subtype": "init"}}), flush=True)
for line in sys.stdin:
    msg = json.loads(line)
    text = msg["message"]["content"]
    if mode == "hang":
        time.sleep(30)
    if mode == "die":
        sys.exit(1)
    out = {{"question_updates": [{{"question_id": "strategy-success", "status": "partial",
           "answer": "draft: " + text[-20:], "answer_piece": "", "evidence": "", "covered": [],
           "rung": "business", "follow_up": "", "vague": False}}],
           "slots": [], "maps": {{"org": [], "flows": [], "stores": [], "decisions": [], "catalog": []}}, "coverage": [], "noise": []}}
    print(json.dumps({{"type": "assistant", "message": {{}}}}), flush=True)
    print(json.dumps({{"type": "result", "subtype": "success", "is_error": False,
                      "result": json.dumps(out), "structured_output": out}}), flush=True)
'''


def fake_stream_claude(tmp_path, mode="ok"):
    log = tmp_path / "stream.jsonl"
    script = tmp_path / "claude"
    script.write_text(FAKE_STREAM.format(python=sys.executable, log=str(log), mode=mode))
    script.chmod(0o755)
    return script, log


def test_fast_pass_reuses_one_persistent_claude_process(tmp_path, state, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    script, log = fake_stream_claude(tmp_path)
    engine = ClaudeCliEngine(claude_bin=str(script), fast_model="claude-haiku-5-5")
    try:
        engine.warm_fast("FAST SYSTEM")
        r1 = engine.fast("FAST SYSTEM", "lines one", state=state, lines=[])
        r2 = engine.fast("FAST SYSTEM", "lines two", state=state, lines=[])
    finally:
        engine.close()
    assert r1.question_updates[0].answer.endswith("lines one")
    assert r2.question_updates[0].answer.endswith("lines two")
    spawns = [json.loads(x) for x in log.read_text().splitlines()]
    assert len(spawns) == 1  # one warm process served both calls
    argv = spawns[0]["argv"]
    for flag in ("--input-format", "--output-format"):
        assert argv[argv.index(flag) + 1] == "stream-json"
    assert "--verbose" in argv and argv[argv.index("--model") + 1] == "claude-haiku-5-5"
    assert argv[argv.index("--tools") + 1] == "" and "--no-session-persistence" in argv
    assert set(json.loads(argv[argv.index("--json-schema") + 1])["required"]) == {"question_updates", "slots",
                                                                                 "maps", "coverage", "noise"}
    env = spawns[0]["env"]  # API credentials stripped (other ANTHROPIC_* settings may pass through)
    assert "ANTHROPIC_API_KEY" not in env and "ANTHROPIC_AUTH_TOKEN" not in env


def test_stream_session_recycles_after_max_turns(tmp_path):
    script, log = fake_stream_claude(tmp_path)
    s = ClaudeStreamSession([str(script)], str(tmp_path), max_turns=2)
    try:
        for i in range(5):
            s.ask(f"m{i}", timeout=10)
    finally:
        s.close()
    pids = [json.loads(x)["pid"] for x in log.read_text().splitlines()]
    assert len(set(pids)) == 3  # 2 + 2 + 1 turns (the replacement is started right away)


def test_stream_session_timeout_and_death_raise_sync_failed(tmp_path):
    script, _ = fake_stream_claude(tmp_path, mode="hang")
    s = ClaudeStreamSession([str(script)], str(tmp_path))
    t = time.monotonic()
    with pytest.raises(SyncFailed, match="timed out"):
        s.ask("x", timeout=0.5)
    assert time.monotonic() - t < 5 and not s.alive

    script, _ = fake_stream_claude(tmp_path, mode="die")
    s = ClaudeStreamSession([str(script)], str(tmp_path))
    with pytest.raises(SyncFailed, match="ended"):
        s.ask("x", timeout=5)


def test_fast_falls_back_to_one_shot_after_two_stream_failures(tmp_path, state):
    script, _ = fake_stream_claude(tmp_path, mode="die")
    engine = ClaudeCliEngine(claude_bin=str(script))
    for _ in range(2):
        with pytest.raises(SyncFailed):
            engine.fast("S", "U", state=state, lines=[])
    assert engine.persistent_fast is False


# --- readable live transcript ----------------------------------------------------------------


def test_readable_strip_picks_the_language_being_spoken():
    t = Transcript()
    for stream in ("fr", "en"):
        for c in load_srt(FILES[stream]):
            t.add(stream, c.start, c.end, c.text)
    kept = t.readable(primary="fr")
    assert len(kept) == 27  # one line per moment
    en = [s.text for s in kept if s.stream == "en"]
    assert en[0].startswith("Let me switch to English") and len(en) == 5
    assert all("5D" in s.text or s.stream == "fr" for s in kept[:1])
    # if one recogniser died, the other one's lines are always shown
    only_en = Transcript()
    for c in load_srt(FILES["en"])[:3]:
        only_en.add("en", c.start, c.end, c.text)
    assert len(only_en.readable(primary="fr")) == 3


# --- fast-pass warm-up (review F11, F12) ------------------------------------------------------------


class WarmEngine(FastEngine):
    def __init__(self):
        super().__init__()
        self.warmed = []
        self.fast_systems = []

    def warm_fast(self, system):
        self.warmed.append(system)

    def fast(self, system, user, *, state, lines):
        self.fast_systems.append(system)
        return super().fast(system, user, state=state, lines=lines)


def test_rewarm_after_a_language_switch_uses_the_same_system_prompt_as_the_calls(tmp_path, client_cfg):
    """The warm `claude` process is keyed by model + system prompt: the re-warm after an interview
    language switch used to leave the client context out, so the next call started a cold process."""
    engine = WarmEngine()
    session = Session(client=client_cfg, session_dir=tmp_path / "s", engine=engine,
                      source_factory=lambda d: ReplaySource({}), settings=Settings(poll_interval=3600))

    async def run():
        session.state.mode = "interview"
        await session.set_languages(interview="en")
        await asyncio.gather(*session._tasks)
        session.transcript.add("fr", 0, 1, "On saisit les commandes à la main")
        await session.fast_once()

    asyncio.run(run())
    assert engine.warmed and engine.warmed[-1] == engine.fast_systems[-1]


def test_fast_conversation_is_recycled_after_ten_turns(tmp_path):
    assert ClaudeStreamSession(["true"], str(tmp_path)).max_turns == 10
