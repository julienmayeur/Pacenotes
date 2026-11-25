"""The time budget moves the interview on (timebox.py).

A real 50-minute interview covered 2 of 20 questions: the opening question stayed on screen 14 minutes.
A question only moved when the analysis found it complete or after a pause after a sentence end of
3.5 s beyond the recogniser's measured delay; live, the final lines arrive in 11.5 s batches (often 23 s),
so that margin was ~36 s and a live conversation never pauses that long (10 pauses of 3.5 s after a sentence
end in 47 minutes). The follow-up queue was full the whole time and every new proposal was dropped. These
tests replay that shape with fictional words.
"""

import math
import time

from pacenote.copilot.contract import FastResult
from pacenote.copilot.engines import OfflineEngine
from pacenote.interview import timebox
from pacenote.interview.questionnaire import Questionnaire
from pacenote.interview.session import Session, Settings
from pacenote.interview.state import SessionState
from pacenote.transcription.capture import ReplaySource
from tests.conftest import real_client, result, upd

E = 1_800_000_000.0  # the interview starts at this epoch (any)


def fast(*updates):
    return FastResult.model_validate({"question_updates": list(updates), "slots": [], "coverage": [],
                                      "maps": {"org": [], "flows": [], "stores": [], "decisions": [], "catalog": []},
                                      "noise": []})


class _Clock:
    """The speech clock (the tests add the lines themselves)."""

    def __init__(self, t=0.0):
        self.t = t

    def elapsed(self):
        return self.t


def _state(minutes=20):
    st = SessionState.from_questionnaire(real_client().questionnaire)
    st.set_target_minutes(minutes)
    return st


def _started(minutes=20, tmp_path=None):
    """A session 20 minutes long that started at E, its clock the speech clock (as in session.start)."""
    s = Session(client=real_client(), session_dir=tmp_path / "s", engine=OfflineEngine(), source_factory=lambda d: None,
                settings=Settings(sync_interval=3600, first_sync_delay=3600, poll_interval=3600, fast_enabled=False))
    s.state.set_target_minutes(minutes)
    s.state.mode = "interview"
    s.source = _Clock(0.0)
    s.state.started_at = E
    s.state.set_clock(s.interview_clock)
    s.state.start_phase_clock(E)
    return s


def _say(s, start, end, text):
    s.transcript.add("fr", start, end, text, received_at=time.time())
    s.last_fast_seq = s.transcript.last_seq


# --- budgets -------------------------------------------------------------------------------------


def test_the_default_length_is_20_minutes_and_a_run_can_change_it():
    assert Questionnaire.model_fields["target_minutes"].default == timebox.DEFAULT_MINUTES == 20
    st = _state(20)
    assert st.target_minutes == 20 and abs(sum(st.phase_plan.values()) - 1200) < 1
    assert st.phase_plan["wealth"] == 120.0 and st.phase_plan["return"] == 80.0  # 15-minute plan scaled to 20


def test_each_step_gets_its_share_of_the_time_left_and_the_closing_keeps_its_reserve():
    st = _state(20)
    st.started_at = E
    st.set_clock(lambda: E)
    st.start_phase_clock(E)
    # the closing keeps a slot and its grace per open question, and one grace for the question on screen to
    # give way (2 here: 210 s, more than its planned 80 s), since the time being up ends the interview once
    # the closing is done; the steps before it share the rest by their planned minutes
    assert timebox.reserve(st) == 2 * (timebox.MIN_SLOT + timebox.GRACE) + timebox.GRACE == 210 > st.phase_plan["return"]
    before = {p: st.phase_plan[p] for p in st.phase_plan if p != "return"}
    assert st.phase == "wealth"
    assert math.isclose(st.phase_budgets["wealth"], (1200 - 210) * 120 / sum(before.values()), abs_tol=0.2)
    # the opening ran 10 minutes (it took 14 in the real interview): the next step only gets its share of
    # what is left once the closing's reserve is set aside
    st.set_clock(lambda: E + 600)
    st.advance("n", to="core-who-comes")
    weights = {p: st.phase_plan[p] for p in ("signal-carriers", "signal-flow", "knowledge", "decisions", "deep-dives")}
    assert st.phase == "signal-carriers"
    assert math.isclose(st.phase_budgets["signal-carriers"], (600 - 210) * 120 / sum(weights.values()), abs_tol=0.2)
    assert st.edit_log[-2].detail.endswith("(time budget spent)")


def test_when_the_time_left_is_the_closings_the_next_question_is_the_closings():
    st = _state(20)
    st.started_at = E
    st.set_clock(lambda: E)
    st.start_phase_clock(E)
    assert not timebox.closing_due(st)
    st.set_clock(lambda: E + 1200 - 211)
    assert not timebox.closing_due(st)
    st.set_clock(lambda: E + 1200 - 209)  # 209 s left: within the closing's reserve
    assert timebox.closing_due(st) and st.upcoming_order()[0] == "roi-expectation"
    st.advance("n")
    assert st.focus_id == "roi-expectation" and st.phase == "return"
    assert "(closing time)" in st.edit_log[-2].detail
    assert not timebox.closing_due(st)  # in the closing now


def test_a_question_gets_a_slot_by_priority_and_at_least_a_minute():
    st = _state(20)
    st.started_at = E
    st.set_clock(lambda: E)
    st.start_phase_clock(E)
    # wealth: 120 s for capacity-limit (priority 1), capacity-time (1) and capacity-growth (2): 2/5 of it,
    # never less than a minute
    assert st.focus_id == "capacity-limit" and st.focus_slot == 60.0
    st.set_target_minutes(60)  # too late: the interview started
    assert st.target_minutes == 20
    long = _state(60)
    long.started_at = E
    long.set_clock(lambda: E)
    long.start_phase_clock(E)
    assert math.isclose(long.focus_slot, 360 * 2 / 5, abs_tol=0.5)


def test_once_the_slot_is_over_no_follow_up_is_queued_and_the_next_question_is():
    st = _state(20)
    st.started_at = E
    now = [E]
    st.set_clock(lambda: now[0])
    st.start_phase_clock(E)
    q = st.current_question
    st.apply_fast_result(fast(upd(q.id, "partial", "Les commandes.", follow_up="Combien par jour ?")))
    assert [e.text for e in st.ask_next] == ["Combien par jour ?"] and st.next_qid() is None
    now[0] = E + 61
    rep = st.apply_fast_result(fast(upd(q.id, "partial", "x", piece="Et les appels.", follow_up="Qui répond ?")))
    assert [e.text for e in st.ask_next] == ["Combien par jour ?"]
    assert any("time slot over" in r.detail for r in rep.rejected)
    assert st.next_qid() == "capacity-time"  # queued under the question being answered, visible


# --- moving on, live -----------------------------------------------------------------------------


def _talk_continuously(s, t0, t1, text="et donc on reprend les commandes une par une"):
    """Lines of ~3 s with 0.5 s gaps, none ending a sentence: a lively answer, cut mid-sentence."""
    t = t0
    while t + 3 <= t1:
        _say(s, t, t + 3, text)
        s.source.t = t + 3.2
        assert not s._maybe_retire(), f"moved mid-answer at {s.source.t}"
        t += 3.5
    return t


def test_the_real_shape_a_long_answer_moves_on_at_the_first_sentence_end_after_the_grace(tmp_path):
    """Live: lines in 11.5 s batches (often 23 s), so no pause is ever sure; an answer that goes on. The
    question used to stay for good; now, once its slot and the grace are over, the next line that ends a
    sentence is the boundary, never a line cut mid-sentence."""
    s = _started(tmp_path=tmp_path)
    st = s.state
    first = st.focus_id
    s.view()
    st.ask_seq = 0
    s._lags = [11.5, 23.0] * 10
    st.apply_fast_result(fast(upd(first, "partial", "x", piece="les commandes", follow_up="Combien par jour ?")))
    end = _talk_continuously(s, 1.0, 60 + timebox.GRACE + 5)  # open lines only: never mid-sentence
    assert st.focus_id == first and timebox.slot_over(st)
    assert [i["kind"] for i in s.ask_queue()] == ["followup", "question"]  # the next question is on screen
    _say(s, end, end + 3, "et c'est là que ça bloque.")
    s.source.t = end + 3.5
    assert s._maybe_retire() and st.focus_id == "capacity-time"
    assert s.ask_changes[-1]["reason"] == "time slot over, sentence end"


def test_within_the_grace_a_sure_pause_moves_it(tmp_path):
    """Lines delivered one by one (a replay, or a short delivery delay): a sure pause of 1.5 s after the slot."""
    s = _started(tmp_path=tmp_path)
    st = s.state
    s.view()
    st.ask_seq = 0
    s._lags = [2.0] * 10  # each line arrives ~2 s after it starts
    end = _talk_continuously(s, 1.0, 62.0)
    assert st.focus_id == "capacity-limit" and timebox.slot_over(st)
    _say(s, end, end + 2.0, "voilà")
    s.source.t = end + 2.0 + 2.5 + 1.4  # beyond the delivery margin (1.25 x 2 s), not 1.5 s yet
    assert not s._maybe_retire()
    s.source.t = end + 2.0 + 2.5 + 1.6
    assert s._maybe_retire() and s.ask_changes[-1]["reason"] == "time slot over, 1.5 s pause"


def test_the_interviewer_reading_a_queued_follow_up_still_wins_after_the_slot(tmp_path):
    s = _started(tmp_path=tmp_path)
    st = s.state
    first = st.focus_id
    s.view()
    st.ask_seq = 0
    st.apply_fast_result(fast(upd(first, "partial", "x", piece="les commandes", follow_up="Combien de commandes par jour ?")))
    st.ask_next[0].at -= 70  # on screen since the start of the answer (wall time does not run in a test)
    s._lags = [11.5] * 10
    end = _talk_continuously(s, 1.0, 70.0)
    _say(s, end + 1.0, end + 3.0, "Combien de commandes par jour ?")  # read aloud after a turn gap
    s.source.t = end + 3.2
    assert s._maybe_retire()
    assert st.focus_id == first and st.ask_pin.text == "Combien de commandes par jour ?"


def test_enough_data_keeps_the_interview_going(tmp_path):
    s = _started(tmp_path=tmp_path)
    s.state.apply_sync_result(result(enough_data=True, enough_data_reason="le temps est écoulé"))
    assert s.state.mode == "interview" and s.view()["listening"]["stopped"] is False
    s.state.apply_fast_result(fast(upd(s.state.focus_id, "partial", "suite", follow_up="Et ensuite ?")))
    assert [e.text for e in s.state.ask_next] == ["Et ensuite ?"]  # the copilot still proposes


def test_the_screen_gets_the_time_on_the_interview_clock(tmp_path):
    s = _started(tmp_path=tmp_path)
    s.source.t = 1260  # 21 minutes in a 20-minute interview
    t = s.view()["time"]
    assert t["elapsed"] == 1260 and t["total"] == 1200 and t["left"] == -60 and t["over"]


# --- replays: live-like delivery -------------------------------------------------------------------


def test_replay_batches_the_lines_like_live(tmp_path):
    srt = tmp_path / "fr.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:03,000\nBonjour.\n\n2\n00:00:12,000 --> 00:00:14,000\nOui.\n",
                   encoding="utf-8")
    now = [0.0]
    src = ReplaySource({"fr": srt}, speed=1.0, clock=lambda: now[0], batch=11.5)
    src.start()
    now[0] = 5.0
    assert src.poll() == []  # said, not delivered yet: the batch comes at 11.5 s
    now[0] = 11.5
    assert [c.text for c in src.poll()] == ["Bonjour."]
    now[0] = 13.0
    assert src.poll() == []
    now[0] = 23.0
    assert [c.text for c in src.poll()] == ["Oui."]


def test_replay_of_a_recorded_transcript_keeps_its_live_delivery(tmp_path):
    rows = [{"seq": 1, "stream": "fr", "start": 1.0, "end": 3.0, "text": "un", "received_at": 100.0},
            {"seq": 2, "stream": "fr", "start": 3.5, "end": 5.0, "text": "deux", "received_at": 120.0},
            {"seq": 3, "stream": "fr", "start": 30.0, "end": 32.0, "text": "trois", "received_at": 140.0}]
    path = tmp_path / "transcript.jsonl"
    path.write_text("\n".join(map(__import__("json").dumps, rows)), encoding="utf-8")
    now = [0.0]
    src = ReplaySource.from_transcript(path, clock=lambda: now[0], until=20)
    src.start()
    now[0] = 3.0
    assert [c.text for c in src.poll()] == ["un"]  # the quickest delivery counts as no delay
    now[0] = 22.9
    assert src.poll() == []  # "deux" arrived 20 s later live
    now[0] = 23.0
    assert [c.text for c in src.poll()] == ["deux"] and src.exhausted  # "trois" is past `until`


def test_a_step_with_less_than_a_slot_left_starts_only_a_priority_1_question():
    st = _state(20)
    st.started_at = E
    now = [E]
    st.set_clock(lambda: now[0])
    st.start_phase_clock(E)
    now[0] = E + 125  # the opening took its 120 s
    st.advance("n", to="core-who-comes")  # signal-carriers: who-comes (1), key-people (2), first-to-try (2)
    budget = st.phase_budgets["signal-carriers"]
    now[0] = E + 125 + budget - timebox.MIN_SLOT + 5  # less than a slot left in the step
    assert timebox.phase_short(st, "signal-carriers") and not timebox.must_left(st, "signal-carriers")
    assert st.phase_done("signal-carriers")  # only priority-2 questions left: the step hands over
    assert st.upcoming_order()[0] == "core-waited-on-you"  # the next step's priority-1 question


def test_when_the_closing_is_due_the_question_on_screen_gives_way_at_its_next_boundary():
    st = _state(20)
    st.started_at = E
    now = [E]
    st.set_clock(lambda: now[0])
    st.start_phase_clock(E)
    now[0] = E + 1200 - 220
    st.advance("n", to="core-who-comes")  # goes up with 220 s left: its slot is a minute
    assert not timebox.slot_over(st)
    now[0] = E + 1200 - 179  # the closing's 180 s: the slot is not over, the time is
    assert timebox.slot_over(st) and st.next_qid() == "roi-expectation"



def test_when_the_closing_is_due_the_next_sentence_end_moves_to_it_without_grace(tmp_path):
    s = _started(tmp_path=tmp_path)
    st = s.state
    s.view()
    st.ask_seq = 0
    s._lags = [11.5] * 10
    s.source.t = 1200 - 220
    st.advance("n", to="core-who-comes")  # its 60 s slot would run past the closing's 180 s
    st.ask_seq = s.transcript.last_seq
    end = _talk_continuously(s, 981.0, 1024.0)  # the closing is due from 1020
    assert timebox.closing_due(st) and st.focus_id == "core-who-comes"
    _say(s, end, end + 3, "c'est comme ça.")
    s.source.t = end + 3.5
    assert s._maybe_retire() and st.focus_id == "roi-expectation"



def test_past_the_time_the_closing_questions_come_first_then_priority_1_leftovers():
    st = _state(20)
    st.started_at = E
    now = [E]
    st.set_clock(lambda: now[0])
    st.start_phase_clock(E)
    now[0] = E + 1150
    st.advance("n")  # closing time
    assert st.focus_id == "roi-expectation"
    now[0] = E + 1300  # past the 20 minutes
    st.advance("n")
    assert st.focus_id == "roi-budget"  # the other closing question, before any left-over
    now[0] = E + 1400
    st.advance("n")
    left = st.get(st.focus_id)
    assert left.phase != "return" and left.priority == 1  # then the left-overs, priority 1 first



def test_time_never_brings_back_a_question_already_asked():
    """Past the time, with every question asked once, the time moves used to ping-pong between the two
    opening questions every 1.5 minutes (real-model replay, 33-46 min). Revisiting is the interviewer's call."""
    st = _state(20)
    st.started_at = E
    now = [E]
    st.set_clock(lambda: now[0])
    st.start_phase_clock(E)
    for q in st.questions:
        if q.id != st.focus_id:
            st.focus_history.append(q.id)  # every question went up once
    now[0] = E + 1500
    assert timebox.slot_over(st) and st.next_qid() is None
    st.get(st.focus_id).status = "answered"  # complete: the next one may be any open question
    st.ask_next_qid = None
    assert st.next_qid() is not None



def test_once_the_closing_was_asked_left_overs_get_their_normal_slot():
    st = _state(20)
    st.started_at = E
    now = [E]
    st.set_clock(lambda: now[0])
    st.start_phase_clock(E)
    now[0] = E + 1150
    st.advance("n")
    now[0] = E + 1250
    st.advance("n")
    assert st.focus_id == "roi-budget"
    now[0] = E + 1350
    st.advance("n")  # both closing questions asked: a left-over
    assert st.phase != "return" and not timebox.closing_due(st)
    assert not timebox.slot_over(st) and st.focus_slot >= timebox.MIN_SLOT  # not rushed



def test_with_nothing_left_to_queue_the_copilot_proposes_follow_ups_again():
    st = _state(20)
    st.started_at = E
    now = [E]
    st.set_clock(lambda: now[0])
    st.start_phase_clock(E)
    for q in st.questions:
        if q.id != st.focus_id:
            st.focus_history.append(q.id)
    now[0] = E + 1500
    assert timebox.slot_over(st) and st.next_qid() is None
    st.apply_fast_result(fast(upd(st.focus_id, "partial", "x", piece="encore", follow_up="Et ensuite ?")))
    assert [e.text for e in st.ask_next] == ["Et ensuite ?"]  # not silent for the rest of the meeting
