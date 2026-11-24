"""The interview thread: original question locked on screen, answer pieces under it, follow-ups queued
under the question being asked (never in its place) until its answer is over, the Business > Workflow >
Implementation ladder, focus that only moves on with n / Enter or once the answer is over, calm (frozen)
lists."""

import asyncio
import json
import time

from fastapi.testclient import TestClient

from pacenote.copilot.contract import FastResult, SyncResult
from pacenote.copilot.engines import OfflineEngine
from pacenote.copilot.prompts import build_fast_system, build_sync_system
from pacenote.interview.preparation import load_client
from pacenote.interview.session import Session, Settings
from pacenote.interview.state import SessionState
from pacenote.transcription.capture import ReplaySource
from pacenote.web.server import create_app
from tests.conftest import NOISY, REAL_CLIENTS, SAMPLES, edit, real_client, result, upd


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


    shutil.copytree(REAL_CLIENTS / "brioche-four", tmp_path / "brioche-four")
    (tmp_path / "brioche-four" / "preplan.json").write_text(json.dumps(
        {"summary": "x", "use_cases": [], "seed_questions": [{"id": "extra", "fr": "Une question de plus ?"}]}))
    assert len(load_client(tmp_path, "brioche-four").questionnaire.themes) == len(real_state().themes) + 1
    assert [t.id for t in load_client(tmp_path, "brioche-four", use_preplan=False).questionnaire.themes] == \
        [t.id for t in real_state().themes]


# --- thread ---------------------------------------------------------------------------------------


def test_follow_ups_queue_under_the_question_being_asked_never_in_its_place():
    st = real_state()
    q = st.current_question
    st.apply_fast_result(fast(upd(q.id, "partial", "Aller plus vite.", rung="business",
                                  follow_up="Plus vite sur quoi, concrètement ?", vague=True)))
    assert [(t.kind, t.text) for t in q.thread] == [("answer", "Aller plus vite.")]  # the thread = what was asked
    assert st.ask_pin is None and [e.text for e in st.ask_next] == ["Plus vite sur quoi, concrètement ?"]
    assert q.vague
    # understanding grows: the next proposal is queued after it, the first one stays where it is
    st.apply_fast_result(fast(upd(q.id, "partial", "Aller plus vite.", piece="Les commandes surtout.",
                                  rung="business", follow_up="Les commandes de qui ?")))
    assert [e.text for e in st.ask_next] == ["Plus vite sur quoi, concrètement ?", "Les commandes de qui ?"]
    # the queue is capped: a third proposal, drawn from the latest answer, replaces the oldest queued one
    # (a full queue used to reject every new proposal: 14 minutes of stale follow-ups in a real interview)
    rep = st.apply_fast_result(fast(upd(q.id, "partial", "x", piece="Et les devis.", rung="business",
                                        follow_up="Et les devis ?")))
    assert [e.text for e in st.ask_next] == ["Les commandes de qui ?", "Et les devis ?"]
    assert not any(r.kind == "followup" for r in rep.rejected)
    # the answer is over: the first queued follow-up is asked now and joins the thread
    st.promote()
    assert st.ask_pin.text == "Les commandes de qui ?" and q.follow_up == st.ask_pin.text
    assert [e.text for e in st.ask_next] == ["Et les devis ?"]
    assert [t.kind for t in q.thread] == ["answer", "answer", "answer", "followup"]
    # the same follow-up is never asked or queued twice
    rep = st.apply_fast_result(fast(upd(q.id, "partial", "x", piece="Six mois.", rung="business",
                                        follow_up="Les commandes de qui ?")))
    assert [e.text for e in st.ask_next] == ["Et les devis ?"]
    # a focus change resets the ask: the new question is asked first, with nothing queued
    st.advance("n")
    st.ensure_ask()
    assert st.ask_for == st.focus_id and st.ask_pin is None and st.ask_next == []


def test_full_sync_does_not_duplicate_pieces_when_the_fast_pass_owns_the_thread():
    st = real_state()
    q = st.current_question
    st.apply_fast_result(fast(upd(q.id, "partial", "Aller plus vite.", rung="business")))
    st.apply_sync_result(result(question_updates=[upd(q.id, "partial", "Aller plus vite (résumé).",
                                                      piece="Aller plus vite.", rung="business",
                                                      follow_up="Sur quel processus ?")]), thread=False)
    assert [(t.kind, t.by) for t in q.thread] == [("answer", "fast")]
    assert [(e.text, e.by) for e in st.ask_next] == [("Sur quel processus ?", "sync")]
    assert q.answer == "Aller plus vite (résumé)."


def test_a_follow_up_proposed_for_another_question_waits_in_the_queue_when_it_becomes_the_focus():
    st = real_state()
    other = st.next_questions[0]
    st.apply_fast_result(fast(upd(other, "partial", "Les devis.", rung="business", follow_up="Combien par semaine ?")))
    assert st.get(other).thread[-1].kind == "followup"  # not the focus: in its thread, as before
    st.advance("n", to=other)
    st.ensure_ask()
    assert st.focus_id == other and [e.text for e in st.ask_next] == ["Combien par semaine ?"]
    assert st.get(other).thread[-1].kind == "answer"  # not asked yet: out of the thread


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
    assert st.ask_next[-1].text == "Concrètement, plus vite sur quoi ?"  # the rewording is queued under it
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


class _Clock:
    """A source that only has a speech clock (the tests add the lines themselves)."""

    def __init__(self, t=0.0):
        self.t = t

    def elapsed(self):
        return self.t


def _talking(tmp_path, **kw):
    """A session in the interview, its speech clock at 100 s, the fast pass caught up."""
    s = _session(tmp_path, Settings(sync_interval=3600, first_sync_delay=3600, poll_interval=3600, **kw))
    s.state.mode = "interview"
    s.source = _Clock(100.0)
    return s


def _say(s, start, end, text):
    seg = s.transcript.add("fr", start, end, text, received_at=time.time())
    s.last_fast_seq = s.transcript.last_seq  # analysed
    return seg


def test_the_question_being_answered_stays_while_the_answer_goes_on(tmp_path):
    s = _talking(tmp_path)
    st = s.state
    first = st.focus_id
    s.view()  # the question goes up
    st.ask_seq = s.transcript.last_seq
    asked = s.view()["current"]["ask"]
    assert asked == {"key": f"q:{first}", "kind": "question", "text": st.get(first).text("fr")}
    for i, follow in enumerate(["Plus vite sur quoi ?", "Les commandes de qui ?", "Et les devis ?"]):
        _say(s, 90 + i * 3, 92 + i * 3, "on parle des commandes et")
        st.apply_fast_result(fast(upd(first, "partial", "x", piece=f"morceau {i}", rung="business", follow_up=follow)))
        s.source.t = 92 + i * 3 + 1  # one second later the answer goes on
        assert not s._maybe_retire()
        v = s.view()["current"]
        assert v["ask"] == asked  # never swapped under the interviewee
        assert [x["text"] for x in v["ask_next"]] == [["Plus vite sur quoi ?"], ["Plus vite sur quoi ?", "Les commandes de qui ?"],
                                                      ["Les commandes de qui ?", "Et les devis ?"]][i]
    # a pause mid-sentence (no final mark) is not the end of the answer…
    s.source.t = 98 + 5
    assert not s._maybe_retire()
    # …the end of a sentence and a real pause are
    _say(s, 104, 106, "Voilà, c'est surtout les commandes.")
    s.source.t = 106 + 3.0
    assert not s._maybe_retire()
    s.source.t = 106 + 3.6
    assert s._maybe_retire()
    v = s.view()["current"]
    assert v["id"] == first and v["ask"]["text"] == "Les commandes de qui ?" and v["ask"]["kind"] == "followup"
    assert [x["text"] for x in v["ask_next"]] == ["Et les devis ?"]
    assert s.ask_changes[-1]["reason"].startswith("answer over")
    # the answer to the new one has not started: nothing moves until something is said
    s.source.t = 200
    assert not s._maybe_retire()


def test_a_line_without_a_final_mark_needs_a_longer_pause(tmp_path):
    s = _talking(tmp_path)
    st = s.state
    s.view()
    st.ask_seq = 0
    _say(s, 90, 93, "il faut compter trois heures parce que")
    st.apply_fast_result(fast(upd(st.focus_id, "partial", "x", piece="trois heures", follow_up="Pourquoi trois ?")))
    s.source.t = 93 + 5
    assert not s._maybe_retire()
    s.source.t = 93 + 7.1
    assert s._maybe_retire()


def test_live_batches_raise_the_pause_by_the_recogniser_delay(tmp_path):
    """Live, Apple's final results arrive about every 11.5 s: a line-less stretch shorter than that says
    nothing about silence. The pause counts beyond the measured delay."""
    s = _talking(tmp_path)
    st = s.state
    s.view()
    st.ask_seq = 0
    s._lags = [0.5, 3.0, 6.0, 9.0, 11.0] * 4  # a batch holds the last ~11 s
    _say(s, 80, 88, "Voilà pour les commandes.")
    st.apply_fast_result(fast(upd(st.focus_id, "partial", "x", piece="commandes", follow_up="Et les devis ?")))
    s.source.t = 88 + 3.6 + 5  # silent for all we know, but the next batch may still bring words
    assert not s._maybe_retire()
    s.source.t = 88 + 3.6 + 11.1  # beyond the longest delay, but not its margin (a batch can be skipped)
    assert not s._maybe_retire()
    s.source.t = 88 + 3.6 + 1.25 * 11
    assert s._maybe_retire()


def test_the_interviewer_reading_a_queued_question_asks_it_at_once(tmp_path):
    s = _talking(tmp_path)
    st = s.state
    s.view()
    st.ask_seq = 0
    _say(s, 90, 92, "La saisie des commandes prend trois heures à Lucie et")
    st.apply_fast_result(fast(upd(st.focus_id, "partial", "x", piece="trois heures",
                                  follow_up="Combien de commandes saisit-elle par jour ?")))
    st.apply_fast_result(fast(upd(st.focus_id, "partial", "x", piece="Lucie", follow_up="Qui corrige les erreurs ?")))
    for e in st.ask_next:  # on screen since 92 s on the speech clock (5 s before 97 s)
        e.at = time.time() - 5
    s.source.t = 92.1
    assert not s._maybe_retire()
    _say(s, 92.2, 94, "et les erreurs, Marc les corrige")  # the interviewee's words, mid-turn: not a question read
    s.source.t = 94.5
    assert not s._maybe_retire()
    _say(s, 95, 97, "Et qui corrige les erreurs, du coup")  # read aloud after a gap, in their own words
    s.source.t = 97.2
    assert s._maybe_retire()
    assert st.ask_pin.text == "Qui corrige les erreurs ?"  # the one read, not the first one queued
    assert st.ask_next == [] and s.ask_changes[-1]["reason"] == "the interviewer asked it"


def test_a_follow_up_drawn_from_the_answer_is_not_taken_as_read_aloud(tmp_path):
    """The model writes a follow-up from what was just said, so its words echo that line: only a line said
    after the follow-up appeared can be the interviewer reading it."""
    s = _talking(tmp_path)
    st = s.state
    s.view()
    st.ask_seq = 0
    _say(s, 80, 81, "Bien.")
    _say(s, 82, 86, "Les relances de devis la semaine dernière, Paul")  # a turn start, before the follow-up
    s.source.t = 86.5
    st.apply_fast_result(fast(upd(st.focus_id, "partial", "x", piece="relances",
                                  follow_up="Ces relances, combien par semaine ?")))
    assert not s._maybe_retire()


def test_once_answered_the_next_question_is_queued_and_taken_when_the_answer_is_over(tmp_path):
    s = _talking(tmp_path)
    st = s.state
    first = st.focus_id
    s.view()
    st.ask_seq = 0
    q = st.current_question
    _say(s, 90, 95, "Le service commandes, sans hésiter.")
    st.apply_fast_result(fast(upd(first, "answered", "x", piece="commandes", covered=q.all_expect())))
    assert q.status == "answered"
    v = s.view()["current"]
    nxt = v["ask_next"][-1]
    assert nxt["kind"] == "question" and nxt["qid"] != first
    assert v["ask"]["text"] == q.text("fr")  # same text: answered never swaps it
    s.source.t = 95 + 2
    assert not s._maybe_retire()  # still within the pause: the answer may go on
    s.source.t = 95 + 3.6
    assert s._maybe_retire()
    assert st.focus_id == nxt["qid"] and s.focus_changes[-1]["reason"].startswith("answered")


def test_a_question_the_analysis_finds_complete_retires_at_once_even_while_speech_goes_on(tmp_path):
    """Completion: once the analysis has enough (the "answered" gate: every slot, half the other details),
    the question is marked answered and the next one comes up, without waiting for the end of the speech."""
    s = _talking(tmp_path)
    st = s.state
    first = st.focus_id
    s.view()
    st.ask_seq = 0
    q = st.current_question
    assert not s._maybe_retire()  # seen open
    _say(s, 90, 93, "Le service commandes, Lucie et Marc, et")
    st.apply_fast_result(fast(upd(first, "partial", "x", piece="commandes", covered=q.all_expect()[:1],
                                  follow_up="Combien de commandes par jour ?")))
    s.source.t = 93.5
    assert not s._maybe_retire()  # partly covered, mid-sentence: it stays
    _say(s, 93.5, 96, "quatre-vingts commandes par jour, c'est la saisie qui coince et")
    st.apply_fast_result(fast(upd(first, "answered", "x", piece="saisie", covered=q.all_expect())), read=s.transcript.last_seq)
    s.transcript.add("fr", 96.1, 98, "on a aussi les devis", received_at=time.time())  # not analysed yet: no matter
    s.source.t = 96.3
    nxt = st.next_qid()
    assert s._maybe_retire()
    assert st.focus_id == nxt and s.focus_changes[-1]["reason"].startswith("complete")
    assert s.view()["current"]["ask"]["kind"] == "question"


def test_a_question_completed_by_the_tail_of_the_previous_answer_waits_for_the_pause(tmp_path):
    """One completion move per stretch of speech: if the interviewee goes on and the tail completes the new
    question too, it waits for the pause after it. A completion after a pause moves at once."""
    s = _talking(tmp_path)
    st = s.state
    first = st.focus_id
    s.view()
    st.ask_seq = 0
    assert not s._maybe_retire()
    _say(s, 90, 93, "C'est la saisie des commandes, voilà.")
    st.apply_fast_result(fast(upd(first, "answered", "x", piece="saisie", covered=st.get(first).all_expect())), read=s.transcript.last_seq)
    s.source.t = 93.2
    assert s._maybe_retire()
    second = st.focus_id
    assert not s._maybe_retire()  # the new focus is seen open
    _say(s, 93.4, 97, "Et puis en période de fêtes c'est pire, on fait des heures sup.")
    st.apply_fast_result(fast(upd(second, "answered", "x", piece="heures sup",
                                  covered=st.get(second).all_expect())), read=s.transcript.last_seq)
    s.source.t = 97.5
    assert not s._maybe_retire() and st.focus_id == second
    s.source.t = 97 + 3.6
    assert s._maybe_retire()  # the pause after the tail
    third = st.focus_id
    assert third not in (first, second) and s.focus_changes[-1]["reason"].startswith("complete")
    assert not s._maybe_retire()
    _say(s, 101, 104, "Ah et aussi les devis, Paul les relance à la main.")  # the answer to the third
    st.apply_fast_result(fast(upd(third, "answered", "x", piece="devis", covered=st.get(third).all_expect())), read=s.transcript.last_seq)
    s.source.t = 104.2
    assert s._maybe_retire() and st.focus_id not in (first, second, third)


def test_live_batches_the_tail_ends_at_a_gap_in_the_speech_timestamps(tmp_path):
    """Live, batches hide the last ~11 s, so a pause is sure only late; the gap between the tail and the
    next turn is exact in the line timestamps, so the new question's answer counts from there."""
    s = _talking(tmp_path)
    st = s.state
    first = st.focus_id
    s.view()
    st.ask_seq = 0
    s._lags = [11.0] * 10
    assert not s._maybe_retire()
    _say(s, 90, 93, "C'est la saisie des commandes, voilà.")
    st.apply_fast_result(fast(upd(first, "answered", "x", piece="saisie", covered=st.get(first).all_expect())),
                         read=s.transcript.last_seq)
    s.source.t = 93.2
    assert s._maybe_retire()
    second = st.focus_id
    assert not s._maybe_retire()
    _say(s, 93.4, 96, "Et en période de fêtes c'est pire.")  # the tail
    _say(s, 100, 103, "Sur quelle tâche avez-vous passé le plus de temps ?")  # 4 s later: the next turn
    _say(s, 104, 107, "Les relances de devis, deux jours pour Paul.")
    st.apply_fast_result(fast(upd(second, "answered", "x", piece="relances", covered=st.get(second).all_expect())),
                         read=s.transcript.last_seq)
    s.source.t = 107.5  # no pause can be sure yet (11 s batches)
    assert s._maybe_retire() and st.focus_id not in (first, second)


def _paraphrase(text):
    """A short spoken version of a long question: five of its content words."""
    from pacenote.interview.session import _content_words
    return " ".join(sorted(_content_words(text))[:5]) + " ?"


def test_the_tail_ends_when_the_interviewer_asks_the_new_question_in_their_own_words(tmp_path):
    s = _talking(tmp_path)
    st = s.state
    first = st.focus_id
    s.view()
    st.ask_seq = 0
    assert not s._maybe_retire()
    _say(s, 90, 93, "C'est la saisie des commandes, voilà.")
    st.apply_fast_result(fast(upd(first, "answered", "x", piece="saisie", covered=st.get(first).all_expect())),
                         read=s.transcript.last_seq)
    s.source.t = 93.2
    assert s._maybe_retire()
    second = st.focus_id
    assert not s._maybe_retire()
    _say(s, 93.4, 96, "Et en période de fêtes c'est encore pire.")  # the tail
    st.apply_fast_result(fast(upd(second, "partial", "x", piece="fêtes", follow_up="Combien d'heures sup ?")))
    # 1.2 s later: neither a pause nor a gap, a turn that is not the new question: still the tail
    _say(s, 97.2, 99, "Bon, et les livraisons aussi.")
    s.source.t = 99.2
    assert not s._maybe_retire() and s._tail
    _say(s, 100.2, 103, _paraphrase(st.get(second).text("fr")))  # 1.2 s later: the new question, paraphrased
    asked_at = s.transcript.last_seq
    s.source.t = 103.2
    assert not s._maybe_retire() and not s._tail and st.ask_seq == asked_at
    s.source.t = 103 + 3.6  # a thinking pause after the question: the tail's follow-up is not promoted
    assert not s._maybe_retire()
    assert st.ask_pin is None and [e.text for e in st.ask_next] == ["Combien d'heures sup ?"]
    _say(s, 107, 110, "Les relances de devis, deux jours pour Paul.")
    st.apply_fast_result(fast(upd(second, "answered", "x", piece="relances", covered=st.get(second).all_expect())),
                         read=s.transcript.last_seq)
    s.source.t = 110.2
    assert s._maybe_retire() and st.focus_id not in (first, second)


def test_the_tail_of_a_completed_answer_is_not_the_answer_to_the_new_question(tmp_path):
    """A follow-up drawn from the tail is queued under the new question; the pause that ends the tail does
    not promote it (the new question was not asked yet)."""
    s = _talking(tmp_path)
    st = s.state
    first = st.focus_id
    s.view()
    st.ask_seq = 0
    assert not s._maybe_retire()
    _say(s, 90, 93, "C'est la saisie des commandes, voilà.")
    st.apply_fast_result(fast(upd(first, "answered", "x", piece="saisie", covered=st.get(first).all_expect())), read=s.transcript.last_seq)
    s.source.t = 93.2
    assert s._maybe_retire()
    second = st.focus_id
    _say(s, 93.4, 97, "Et puis en période de fêtes c'est pire, on fait des heures sup.")
    st.apply_fast_result(fast(upd(second, "partial", "x", piece="heures sup", follow_up="Combien d'heures sup ?")))
    for t in (97.5, 97 + 3.6, 97 + 8):
        s.source.t = t
        assert not s._maybe_retire()
    v = s.view()["current"]
    assert v["id"] == second and v["ask"]["kind"] == "question"
    assert [x["text"] for x in v["ask_next"]] == ["Combien d'heures sup ?"]


def test_a_late_completion_on_lines_said_before_the_follow_up_was_asked_does_not_yank_it(tmp_path):
    """Live, one batch can hold the end of the answer and the interviewer reading a queued follow-up: the
    follow-up goes up at once, and the result that completes the question from the earlier lines then
    comes late. It waits for the pause, as an answered question does."""
    s = _talking(tmp_path)
    st = s.state
    first = st.focus_id
    s.view()
    st.ask_seq = 0
    assert not s._maybe_retire()
    _say(s, 80, 83, "Voilà, c'est surtout les devis.")
    st.apply_fast_result(fast(upd(first, "partial", "x", piece="devis", follow_up="Et les devis, qui les prépare ?")))
    st.ask_next[0].at = time.time() - 30
    _say(s, 85, 87, "Et les devis, qui les prépare aujourd'hui ?")
    s.source.t = 87.2
    assert s._maybe_retire() and st.ask_pin is not None  # the follow-up read aloud is asked now
    read = s.transcript.last_seq
    assert not s._maybe_retire()
    st.apply_fast_result(fast(upd(first, "answered", "x", piece="devis", covered=st.get(first).all_expect())),
                         read=read)  # read nothing said after the follow-up
    _say(s, 88, 91, "C'est Paul, notre commercial, et")
    s.source.t = 91.2
    assert not s._maybe_retire() and st.focus_id == first


def test_a_completion_landing_with_the_follow_up_read_aloud_lets_the_follow_up_stand(tmp_path):
    """Same batch: the result completing the question and the interviewer reading a follow-up land on the
    same check. The follow-up read aloud wins; the completion does not move the question a moment later."""
    s = _talking(tmp_path)
    st = s.state
    first = st.focus_id
    s.view()
    st.ask_seq = 0
    assert not s._maybe_retire()
    _say(s, 80, 83, "Voilà, c'est surtout les devis.")
    st.apply_fast_result(fast(upd(first, "partial", "x", piece="devis", follow_up="Et les devis, qui les prépare ?")))
    st.ask_next[0].at = time.time() - 30
    _say(s, 85, 87, "Et les devis, qui les prépare aujourd'hui ?")
    st.apply_fast_result(fast(upd(first, "answered", "x", piece="devis", covered=st.get(first).all_expect())),
                         read=s.transcript.last_seq)
    s.source.t = 87.2
    assert s._maybe_retire() and st.ask_pin is not None and st.focus_id == first
    s.source.t = 87.6
    assert not s._maybe_retire() and st.focus_id == first


def test_a_long_next_question_read_aloud_is_recognised(tmp_path):
    """Theme questions run to ~300 characters: read aloud they come as several ~60-character lines."""
    s = _talking(tmp_path)
    st = s.state
    first = st.focus_id
    s.view()
    st.ask_seq = 0
    q = st.current_question
    _say(s, 60, 64, "Le service commandes, sans hésiter.")
    st.apply_fast_result(fast(upd(first, "answered", "x", piece="commandes", covered=q.all_expect())))
    nxt = st.next_qid()
    text = st.get(nxt).text("fr")
    assert len(text) > 200
    words, chunks, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > 60:
            chunks.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    chunks.append(cur)
    t = 66.0
    for c in chunks:
        _say(s, t, t + 3.5, c)
        t += 3.6
    s.source.t = t
    assert s._maybe_retire()
    assert st.focus_id == nxt and s.focus_changes[-1]["reason"] == "the interviewer asked it"


def test_the_interviewee_answering_the_next_question_moves_the_ask_there(tmp_path):
    async def run():
        s = _talking(tmp_path, fast_enabled=True)
        st = s.state
        first = st.focus_id
        s.view()
        st.ask_seq = 0
        q = st.current_question
        st.apply_fast_result(fast(upd(first, "answered", "x", piece="commandes", covered=q.all_expect())))
        nxt = st.next_qid()

        class _Next:
            name = "next"

            def fast(self, system, user, *, state, lines):
                return fast(upd(nxt, "partial", "", piece="Les relances de devis."))

        s.engine = _Next()
        s.transcript.add("fr", 96, 99, "Les relances de devis, deux jours", received_at=time.time())
        await s.fast_once()
        assert s._maybe_retire()  # no pause needed: the conversation is already there
        assert st.focus_id == nxt and s.focus_changes[-1]["reason"] == "the interviewee answers the next question"

    asyncio.run(run())


def test_clicking_a_queued_question_asks_it_now(tmp_path):
    s = _session(tmp_path)
    with TestClient(create_app(s)) as http:
        http.post("/api/start")
        st = s.state
        st.apply_fast_result(fast(upd(st.focus_id, "partial", "x", piece="x", follow_up="Et les devis ?")))
        v = http.get("/api/state").json()
        key = v["current"]["ask_next"][0]["key"]
        v = http.post("/api/ask", json={"key": key}).json()
        assert v["current"]["ask"]["text"] == "Et les devis ?" and v["current"]["ask_next"] == []
        assert http.post("/api/ask", json={"key": key}).status_code == 409  # no longer queued


def test_next_key_moves_on_and_logs_the_reason(tmp_path):
    s = _session(tmp_path)
    with TestClient(create_app(s)) as http:
        http.post("/api/start")
        first = http.get("/api/state").json()["current"]["id"]
        upcoming = http.get("/api/state").json()["next"][0]["id"]
        view = http.post("/api/next").json()
        assert view["current"]["id"] == upcoming != first
    assert s.focus_changes[-1] == {**s.focus_changes[-1], "from": first, "to": upcoming, "reason": "interviewer pressed next"}


def test_next_key_after_an_auto_advance_does_not_skip_a_question(tmp_path):
    async def run():
        s = _session(tmp_path)
        await s.start()
        first = s.state.focus_id
        s.state.get(first).status = "answered"
        s._advance("answered, 4 s of silence")  # auto-advance…
        second = s.state.focus_id
        await s.next_question(seen=first)  # …and the interviewer presses n, still looking at the first one
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
    s.source = _Clock(1.5)  # the speech clock: the line below ended half a second ago
    st = s.state
    a, b, c = st.next_questions
    s.transcript.add("fr", 0, 1, "on parle", received_at=time.time())
    assert [q["id"] for q in s.view()["next"]] == [a, b, c]
    st.next_questions = [c, a, b]  # the model re-ordered while the CEO is talking
    assert [q["id"] for q in s.view()["next"]] == [a, b, c]
    st.get(b).status = "answered"  # an item that no longer applies just disappears, in place
    assert [q["id"] for q in s.view()["next"]] == [a, c]
    s.source.t = 4.0  # three seconds of silence on the speech clock: now the new order shows
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
    assert cur["ask"]["text"].startswith("Si demain") and cur["ask_next"][0]["text"] == "Plus vite sur quoi ?"
    assert [t["kind"] for t in cur["thread"]] == ["answer"]


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
        strip_all = [line["text"] for line in s.strip_lines(40)]
        await s.shutdown()
        v["_all"] = strip_all
        return v

    clock = [1000.0]
    v = asyncio.run(run())
    texts = [(line["text"], line["pending"]) for line in v["tail"]]
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
    from pacenote.transcription.srt import load_srt
    from pacenote.transcription.transcript import Transcript

    noisy = NOISY
    t = Transcript()
    for stream in ("fr", "en"):
        for c in load_srt(noisy / f"{stream}.srt"):
            t.add(stream, c.start, c.end, c.text)
    kept = t.readable(primary="fr")
    assert len(kept) == 20  # exactly one line per moment
    assert [s.text[:22] for s in kept if s.stream == "en"] == ["Let me switch to Engli", "Let me see, the wareho"]
    assert any(s.text.startswith("On est dix-huit") for s in kept)  # not "On a dees wheat…"


def test_what_we_heard_and_what_is_missing_hang_under_the_question_and_details_stay_apart():
    from pacenote.web.server import STATIC_DIR

    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    # one panel: the thread of past questions, then the current card (the step, the theme question as small
    # context with its own ladder, THE question to ask now, what we heard, what is still missing, then the
    # interviewer details and the discreet controls), then the upcoming questions; no separate recent block
    order = [html.index(x) for x in ('id="interview"', 'id="history"', 'id="current-card"', 'id="phase-label"',
                                     'id="theme-head"', 'id="current"', 'id="ladder"', 'id="ask-now"',
                                     'id="summary-card"', 'id="missing-card"', 'id="got-row"', 'id="thread"',
                                     'id="controls"', 'id="next"')]
    assert order == sorted(order)
    assert 'id="recent"' not in html
    # "ask now" is not nested in "Ce qui manque": it closes before the answer block opens
    assert html.index("</section>", html.index('id="ask-now"')) < html.index('id="summary-card"')
    # the follow-ups already asked and how fresh the answer is are interviewer details (hidden until d)
    details = html.index('class="debug card-debug"')
    assert details < html.index('id="thread"') and details < html.index('id="summary-meta"')
    assert details > html.index('id="missing-card"')
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    for rule in (".missing-box", ".missing-chips li.filled", ".missing-chips li.leaving", ".missing-chips li.fresh",
                 ".debug { display: none !important; }", "body.details-open .debug"):
        assert rule in css, rule


def test_view_lists_every_detail_heard_for_the_captured_row(tmp_path):
    s = _session(tmp_path)
    q = s.state.current_question
    s.state.apply_fast_result(fast(upd(q.id, "partial", "x", rung="business", covered=q.rung_items("business"))))
    cur = s.view()["current"]
    assert cur["rung"] == "workflow"  # business fully covered: climbed
    assert cur["covered"] == q.rung_items("business")  # still listed ("Déjà capté") on the new rung
    assert all(not c["done"] for c in cur["checklist"])  # the checklist is the new rung's
