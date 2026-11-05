"""The interview thread: original question locked on screen, answer pieces and ONE follow-up at a
time appended under it, the Business > Workflow > Implementation ladder, focus that only moves
on with n / Enter or when the ladder is done and the room is quiet, calm (frozen) lists."""

import asyncio
import json
import time

from fastapi.testclient import TestClient

from pacenote.capture import ReplaySource
from pacenote.contract import FastResult, SyncResult
from pacenote.llm import OfflineEngine
from pacenote.prompts import build_fast_system, build_sync_system
from pacenote.questionnaire import load_client
from pacenote.server import create_app
from pacenote.session import Session, Settings
from pacenote.state import SessionState

from .conftest import NOISY, REAL_CLIENTS, SAMPLES, edit, real_client, result, upd


def fast(*updates, noise=None, slots=None):
    return FastResult.model_validate({"question_updates": list(updates), "slots": slots or [], "coverage": [],
                                      "maps": {"org": [], "flows": [], "stores": [], "decisions": [], "catalog": []}, "noise": noise or []})


def real_state():
    """The real questionnaire as committed (independent of any local pre-plan file)."""
    return SessionState.from_questionnaire(real_client().questionnaire)


# --- the real questionnaire -------------------------------------------------------------------


def test_real_questionnaire_follows_the_ladder():
    st = real_state()
    # 15-minute target: the must-haves fit, the follow-ups do the digging; the rest is a pool the phase
    # budgets cut (phases.py)
    assert 12 <= sum(1 for q in st.questions if q.priority == 1) <= 14
    assert len(st.questions) <= 24
    for q in st.questions:
        assert q.all_expect(), q.id
        assert set(q.expect) == {"business", "workflow", "implementation"}
    walkthroughs = [q for q in st.questions if "walkthrough" in q.id]
    assert len(walkthroughs) >= 4  # "walk me through the last…" questions a CEO can answer
    assert all(q.use_case and q.slot_map for q in walkthroughs)  # each story drills a use case's slots
    assert {q.use_case for q in st.questions if q.use_case} == {u.id for u in st.use_cases}  # every use case asked
    assert st.focus_id == "capacity-limit" and st.current_question.rung == "business"


def test_real_questionnaire_ignores_a_local_preplan(tmp_path):
    import json
    import shutil

    from .conftest import REAL_CLIENTS

    shutil.copytree(REAL_CLIENTS / "brioche-four", tmp_path / "brioche-four")
    (tmp_path / "brioche-four" / "preplan.json").write_text(json.dumps(
        {"summary": "x", "use_cases": [], "seed_questions": [{"id": "extra", "fr": "Une question de plus ?"}]}))
    assert len(load_client(tmp_path, "brioche-four").questionnaire.themes) == len(real_state().themes) + 1
    assert [t.id for t in load_client(tmp_path, "brioche-four", use_preplan=False).questionnaire.themes] == \
        [t.id for t in real_state().themes]


# --- thread ---------------------------------------------------------------------------------------


def test_thread_alternates_answer_pieces_and_one_follow_up():
    st = real_state()
    q = st.current_question
    st.apply_fast_result(fast(upd(q.id, "partial", "Aller plus vite.", rung="business",
                                  follow_up="Plus vite sur quoi, concrètement ?", vague=True)))
    assert [(t.kind, t.text) for t in q.thread] == [("answer", "Aller plus vite."),
                                                    ("followup", "Plus vite sur quoi, concrètement ?")]
    assert q.follow_up == "Plus vite sur quoi, concrètement ?" and q.vague
    # a new follow-up while one is pending and no new answer piece: kept, never swapped
    st.apply_fast_result(fast(upd(q.id, "partial", "Aller plus vite.", piece="", rung="business",
                                  follow_up="Autre question ?")))
    assert len(q.thread) == 2 and q.follow_up == "Plus vite sur quoi, concrètement ?"
    # the CEO answers the follow-up: piece appended, then the next follow-up
    st.apply_fast_result(fast(upd(q.id, "partial", "Les commandes : deux jours au lieu du jour même.",
                                  piece="Les commandes doivent partir le jour même, pas en deux jours.",
                                  covered=["le résultat attendu, chiffré (délai, coût, volume)"], rung="business",
                                  follow_up="D'ici combien de mois ?")))
    assert [t.kind for t in q.thread] == ["answer", "followup", "answer", "followup"]
    assert q.answer == "Les commandes : deux jours au lieu du jour même."  # merged summary kept separately
    # the same follow-up is never asked twice
    st.apply_fast_result(fast(upd(q.id, "partial", q.answer, piece="Six mois.", rung="business",
                                  follow_up="D'ici combien de mois ?")))
    assert [t.kind for t in q.thread][-1] == "answer"


def test_full_sync_does_not_duplicate_pieces_when_the_fast_pass_owns_the_thread():
    st = real_state()
    q = st.current_question
    st.apply_fast_result(fast(upd(q.id, "partial", "Aller plus vite.", rung="business")))
    st.apply_sync_result(result(question_updates=[upd(q.id, "partial", "Aller plus vite (résumé).",
                                                      piece="Aller plus vite.", rung="business",
                                                      follow_up="Sur quel processus ?")]), thread=False)
    assert [(t.kind, t.by) for t in q.thread] == [("answer", "fast"), ("followup", "sync")]
    assert q.answer == "Aller plus vite (résumé)."


# --- ladder ---------------------------------------------------------------------------------------


def test_ladder_only_moves_down_and_climbs_when_a_rung_is_covered():
    st = real_state()
    q = st.get("capacity-time")
    st.apply_fast_result(fast(upd(q.id, "partial", "x", rung="workflow")))
    assert q.rung == "workflow"
    st.apply_fast_result(fast(upd(q.id, "partial", "x", rung="business")))
    assert q.rung == "workflow"  # never back up
    st.apply_fast_result(fast(upd(q.id, "partial", "x", rung="workflow", covered=q.rung_items("workflow"))))
    assert q.rung == "implementation"  # the workflow rung is fully covered: climb by itself
    assert q.missing() == q.rung_items("implementation")


def test_ladder_done_means_answered_and_is_gated_on_concrete_details():
    st = real_state()
    q = st.get("capacity-time")  # 2 + 3 + 2 = 7 details, needs 4
    rep = st.apply_fast_result(fast(upd(q.id, "answered", "On a un ERP.", rung="done",
                                        covered=q.rung_items("business"))))
    assert q.status == "partial" and q.rung == "implementation"
    assert any(r.kind == "gate" for r in rep.rejected)
    st.apply_fast_result(fast(upd(q.id, "partial", "…", rung="implementation",
                                  covered=q.rung_items("business") + q.rung_items("implementation"),
                                  follow_up="Et où ça coince ?")))
    assert q.status == "answered" and q.rung == "done" and q.locked  # implementation covered + 4/7
    assert q.thread[-1].kind == "answer" or q.follow_up == ""  # no new follow-up once done


# --- focus lock -------------------------------------------------------------------------------------


def test_current_question_is_never_replaced_reordered_or_removed():
    st = real_state()
    cur = st.focus_id
    st.apply_fast_result(fast(upd(cur, "partial", "Plus vite.", rung="business")))
    rep = st.apply_sync_result(result(
        queue_edits=[edit("reword", cur, fr="Concrètement, plus vite sur quoi ?", en="Faster at what?"),
                     edit("remove", cur), edit("reorder", cur, after="capacity-growth")],
        next_questions=[cur, "capacity-growth", "capacity-time"]))
    q = st.get(cur)
    assert st.focus_id == cur and q.text_fr.startswith("Si demain")  # original question kept
    assert q.follow_up == "Concrètement, plus vite sur quoi ?"  # the rewording became the follow-up
    assert cur not in st.next_questions and st.next_questions[:2] == ["capacity-growth", "capacity-time"]
    assert {r.kind for r in rep.rejected} >= {"remove", "reorder"}
    st.mark_answered("capacity-growth")
    assert st.focus_id == cur  # answering another question never moves the focus


def _session(tmp_path, settings=None, engine=None):
    client = real_client()
    return Session(client=client, session_dir=tmp_path / "s", engine=engine or OfflineEngine(),
                   source_factory=lambda d: ReplaySource({"fr": SAMPLES / "fr.srt", "en": SAMPLES / "en.srt"}, speed=0),
                   settings=settings or Settings(sync_interval=3600, first_sync_delay=3600, poll_interval=3600,
                                                 fast_enabled=False))


def test_auto_advance_only_when_done_and_quiet(tmp_path):
    s = _session(tmp_path, Settings(advance_silence=4.0))
    cur = s.state.focus_id
    s.transcript.add("fr", 0, 1, "la réponse continue…", received_at=time.time())
    assert not s._maybe_auto_advance()  # not answered
    s.state.get(cur).status = "answered"
    assert not s._maybe_auto_advance()  # answered, but people are still talking
    s.transcript.segments[-1].received_at = time.time() - 5
    assert s._maybe_auto_advance()
    assert s.state.focus_id != cur and s.focus_changes[-1]["reason"].startswith("answered")


def test_next_key_moves_on_and_logs_the_reason(tmp_path):
    s = _session(tmp_path)
    with TestClient(create_app(s)) as http:
        http.post("/api/start")
        first = http.get("/api/state").json()["current"]["id"]
        upcoming = http.get("/api/state").json()["next"][0]["id"]
        view = http.post("/api/next").json()
        assert view["current"]["id"] == upcoming != first
    assert s.focus_changes[-1] == {**s.focus_changes[-1], "from": first, "to": upcoming, "reason": "FDE pressed next"}


def test_next_key_after_an_auto_advance_does_not_skip_a_question(tmp_path):
    async def run():
        s = _session(tmp_path)
        await s.start()
        first = s.state.focus_id
        s.state.get(first).status = "answered"
        s._advance("answered, 4 s of silence")  # auto-advance…
        second = s.state.focus_id
        await s.next_question(seen=first)  # …and the FDE presses n, still looking at the first one
        assert s.state.focus_id == second
        await s.next_question(seen=second)
        assert s.state.focus_id not in (first, second)
        await s.shutdown()

    asyncio.run(run())


def test_advance_prefers_questions_not_asked_yet():
    st = real_state()
    first = st.focus_id
    st.apply_fast_result(fast(upd(first, "partial", "Plus vite.", rung="business")))  # still open
    st.next_questions = [first, "capacity-growth"]  # the model lists the visited question first
    assert st.advance("n") == "capacity-growth"
    for _ in range(len(st.questions)):
        if st.advance("n") == first:
            break
    assert all(st.get(i).status != "pending" or i in st.focus_history for i in st.focus_history)
    assert st.focus_history.index(first) == 0 and st.focus_history.count(first) == 2  # revisited last


def test_upcoming_list_is_frozen_while_people_talk(tmp_path):
    s = _session(tmp_path, Settings(freeze_silence=2.5))
    st = s.state
    a, b, c = st.next_questions
    s.transcript.add("fr", 0, 1, "on parle", received_at=time.time())
    assert [q["id"] for q in s.view()["next"]] == [a, b, c]
    st.next_questions = [c, a, b]  # the model re-ordered while the CEO is talking
    assert [q["id"] for q in s.view()["next"]] == [a, b, c]
    st.get(b).status = "answered"  # an item that no longer applies just disappears, in place
    assert [q["id"] for q in s.view()["next"]] == [a, c]
    s.transcript.segments[-1].received_at = time.time() - 3  # silence: now the new order shows
    st.next_questions = [c, a]
    # the list is re-derived; c opens the next phase, so it cannot jump ahead of a (phases.py)
    assert st.get(c).phase != st.get(a).phase
    assert [q["id"] for q in s.view()["next"]][:2] == [a, c]


def test_view_shows_thread_ladder_and_current_rung_checklist(tmp_path):
    s = _session(tmp_path)
    q = s.state.current_question
    s.state.apply_fast_result(fast(upd(q.id, "partial", "Plus vite.", rung="business",
                                       covered=["le temps qu'elle prend aujourd'hui (heures par jour ou par semaine)"],
                                       follow_up="Plus vite sur quoi ?")))
    cur = s.view()["current"]
    assert cur["text"].startswith("Si demain") and cur["rung"] == "business"
    assert [r["state"] for r in cur["rungs"]] == ["current", "todo", "todo"]
    assert cur["checklist"] == [{"item": "la tâche qui coincerait en premier", "done": False},
                                {"item": "le temps qu'elle prend aujourd'hui (heures par jour ou par semaine)", "done": True},
                                {"item": "ce que vous feriez du temps gagné (boulangeries ou commandes en plus)",
                                 "done": False}]
    assert cur["follow_up"] == "Plus vite sur quoi ?"
    assert [t["kind"] for t in cur["thread"]] == ["answer", "followup"]


# --- noise and the live strip --------------------------------------------------------------------


class _FastNoise:
    name = "noise"

    def fast(self, system, user, *, state, lines):
        tv = next(s for s in lines if "Bretagne" in s.text and s.stream == "fr")
        # also flags a gibberish EN line that shares its second with a real FR answer: only that
        # exact line may disappear, never the readable FR line next to it
        en_28 = next(s for s in lines if s.text.startswith("A kiness"))
        return fast(noise=[str(tv.seq), f"#{en_28.seq}"])

    def sync(self, system, user, *, state, delta):
        return result()

    def write_plan(self, system, user, *, state):
        return "# Plan\n"


def test_strip_shows_readable_lines_hides_noise_and_marks_pending(tmp_path):
    noisy = NOISY

    async def run():
        client = real_client()
        s = Session(client=client, session_dir=tmp_path / "s", engine=_FastNoise(),
                    source_factory=lambda d: ReplaySource({"fr": noisy / "fr.srt", "en": noisy / "en.srt"}, speed=1.0,
                                                          clock=lambda: clock[0]),
                    settings=Settings(sync_interval=3600, first_sync_delay=3600, poll_interval=3600))
        await s.start()
        clock[0] += 64  # up to and including the TV line and "On a un ERP"
        s.ingest_once()
        assert await s.fast_once()
        clock[0] += 17  # "Dans Sage…" arrives, not analysed yet
        s.ingest_once()
        v = s.view()
        strip_all = [l["text"] for l in s.strip_lines(40)]
        await s.shutdown()
        v["_all"] = strip_all
        return v

    clock = [1000.0]
    v = asyncio.run(run())
    texts = [(l["text"], l["pending"]) for l in v["tail"]]
    assert any(t.startswith("Et on a un ERP") and not p for t, p in texts)
    assert not any("Bretagne" in t or "bread tan" in t for t, _ in texts)
    assert v["noise_ignored"] == 2
    assert texts[-1][0].startswith("Dans Sage. Lundi") and texts[-1][1]  # newest, not analysed yet
    assert not any(t.startswith(("A on a an ERP", "Dawn Sage")) for t, _ in texts)  # gibberish stream hidden
    assert any(t.startswith("Et qu'Inès arrête") for t in v["_all"])  # its real FR twin is kept


# --- prompts & contract ---------------------------------------------------------------------------


def test_prompts_carry_the_ladder_vagueness_and_noise_rules():
    st = real_state()
    glossary = real_client().glossary
    for system in (build_fast_system(st, glossary), build_sync_system(st, glossary)):
        assert "business" in system and "workflow" in system and "implementation" in system
        assert "ONE" in system and "follow" in system
        assert "concrete recent example, a number, a name or a tool" in system
        assert "TV" in system
    assert "Never reword, reorder or remove it" in build_sync_system(st, glossary)
    fields = FastResult.model_json_schema()["$defs"]["QuestionUpdate"]["required"]
    assert {"answer_piece", "rung", "follow_up", "covered", "vague"} <= set(fields)
    assert set(FastResult.model_json_schema()["required"]) == {"question_updates", "slots", "maps", "coverage", "noise"}
    assert "expect" in SyncResult.model_json_schema()["$defs"]["QueueEdit"]["required"]
    assert json.dumps(SyncResult.model_json_schema()).count("implementation") >= 1


def test_strip_picks_the_spoken_language_on_the_noisy_sample():
    from pacenote.srt import load_srt
    from pacenote.transcript import Transcript

    noisy = NOISY
    t = Transcript()
    for stream in ("fr", "en"):
        for c in load_srt(noisy / f"{stream}.srt"):
            t.add(stream, c.start, c.end, c.text)
    kept = t.readable(primary="fr")
    assert len(kept) == 20  # exactly one line per moment
    assert [s.text[:22] for s in kept if s.stream == "en"] == ["Let me switch to Engli", "Let me see, the wareho"]
    assert any(s.text.startswith("On est dix-huit") for s in kept)  # not "On a dees wheat…"


def test_missing_block_sits_right_under_the_question_before_the_captured_thread():
    from pacenote.server import STATIC_DIR

    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    # one panel: the thread of past questions, then the current card (inline controls, the theme header
    # with its own ladder, THE question to ask now, then the quiet "Ce qui manque" and live summary +
    # follow-ups), then the upcoming questions; no separate recent block
    order = [html.index(x) for x in ('id="interview"', 'id="history"', 'id="current-card"', 'id="controls"',
                                     'id="theme-head"', 'id="current"', 'id="ladder"', 'id="ask-now"',
                                     'id="missing-card"', 'id="got-row"', 'id="summary-card"', 'id="thread"',
                                     'id="next"')]
    assert order == sorted(order)
    assert 'id="recent"' not in html
    # "ask now" is not nested in "Ce qui manque": it closes before the missing block opens
    assert html.index("</section>", html.index('id="ask-now"')) < html.index('id="missing-card"')
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    for rule in (".missing-box", ".missing-chips li.filled", ".missing-chips li.leaving", ".missing-chips li.fresh"):
        assert rule in css, rule


def test_view_lists_every_detail_heard_for_the_captured_row(tmp_path):
    s = _session(tmp_path)
    q = s.state.current_question
    s.state.apply_fast_result(fast(upd(q.id, "partial", "x", rung="business", covered=q.rung_items("business"))))
    cur = s.view()["current"]
    assert cur["rung"] == "workflow"  # business fully covered: climbed
    assert cur["covered"] == q.rung_items("business")  # still listed ("Déjà capté") on the new rung
    assert all(not c["done"] for c in cur["checklist"])  # the checklist is the new rung's
