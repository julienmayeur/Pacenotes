"""The interview's time budget: the clock moves the interview on, not only the answers.

A real 50-minute interview covered 2 questions of 20: the opening question stayed on screen for 14
minutes, because a question only moved on when the analysis found it complete or after a pause that a
live conversation almost never makes. Time is now a mover, with three rules (all on the interview's
own clock, `SessionState.now()`, so a replay at any speed keeps the same pace):

1. **Each step gets its share of the time left.** When a step (phase) starts, it gets the time still
   left, minus the closing step's reserve, split across the steps still to come by their planned
   minutes (phases.DEFAULT_MINUTES or the questionnaire's `phase_minutes`). A step that ran short
   gives its time to the next ones; one that ran long takes it from them, never from the closing.
2. **Each question gets a slot**: its share of its step's time, priority 1 counting double, at least
   `MIN_SLOT`. Once the slot is over the copilot stops digging: no new follow-up is queued and the
   next question is queued under the one being answered instead. It moves up at the first boundary
   (session.py: the interviewer reads it, the interviewee answers it, a short sure pause, or once the slot
   is `GRACE` seconds over, the next line that ends a sentence), never on a line cut mid-sentence. Live,
   the lines come in batches every ~11.5 s: that sentence end is seen 10-20 s after it was said, so the
   interviewee may already have gone on.
   A step with less than a slot left starts no new question but a priority-1 one: an overrun eats the
   priority 2 and 3 questions first.
3. **The closing is protected.** Its reserve is its planned time, and at least `MIN_SLOT + GRACE` per
   closing question still to ask or on screen, plus one `GRACE` for the question on screen to give way
   (the time being up ends the interview once the closing is done, so the closing must fit before it).
   Inside the closing a question moves on at the first sentence end after its slot, with no grace. When the time left reaches the closing step's reserve, the question
   on screen gives way at its next boundary and the next question is the closing step's, wherever the
   interview is: what matters for the plan (the return, the horizon) is never lost to time. Past the
   time, the closing's questions still go first.

4. **The closing is the end.** Nothing is queued after the closing's last question: no left-over follows it.
   The closing is *done* once none of its questions is left to put up and the last one's answer is over
   (`closing_done`: its slot is over or it is answered, then a boundary, as for any question; or the
   interviewer moved off it). When the time is up and the closing is done, the interview ends at the next
   boundary (interview/lifecycle.py): listening stops and the plan is written, as with End. The closing's
   reserve (rule 3) sizes its last slot to end about `GRACE` before the time, so the stop comes near the
   planned length, never in the middle of the closing; a closing still running at the time goes on to its
   end, at most `lifecycle.OVERRUN` past it.
5. **"+5 min".** From `EXTEND_SHOWN` seconds before the end the screen offers +5 min (`extend`): each click adds
   `EXTEND_BY` to the planned length and re-plans: the current step and the question on screen get their new
   share, the coverage allowance grows with the length, and the closing's reserve moves to the new end. A
   click once the closing has started gives the floor back to what is still unasked (`closing_reopened`);
   the closing comes back when the time left is its reserve again.

A question's slot never holds it for good: `HARD` seconds past it, it moves on whatever is being said (an
answer with no sentence end, nothing heard, or the tail of an earlier answer would otherwise keep it up).
The model's "enough data" is only a hint: the End button lights up, nothing stops.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pacenote.interview import phases as P

if TYPE_CHECKING:  # pragma: no cover
    from pacenote.interview.state import QuestionState, SessionState

DEFAULT_MINUTES = 20  # the interview's length when the questionnaire or the command line says nothing
CLOSING = "return"
MIN_SLOT = 60.0  # s: a question always gets at least this before time moves it on
GRACE = 30.0  # s after the slot: with no sure pause, the next line that ends a sentence is the boundary
HARD = 90.0  # s after the slot: the question moves on at once, whatever is being said
EXTEND_BY = 300.0  # s one "+5 min" adds to the planned length
EXTEND_SHOWN = 300.0  # s left from which the screen offers "+5 min"
PRIORITY_WEIGHT = {1: 2.0, 2: 1.0, 3: 0.5}


def total(state: SessionState) -> float:
    return float(state.target_minutes) * 60.0


def elapsed(state: SessionState, now: float | None = None) -> float:
    if state.started_at is None:
        return 0.0
    return max(0.0, (state.now() if now is None else now) - state.started_at)


def left(state: SessionState, now: float | None = None) -> float:
    return total(state) - elapsed(state, now)


def planned(state: SessionState) -> dict[str, float]:
    """The planned seconds per step (the weights the time left is split by)."""
    return state.phase_plan or state.phase_budgets


def _has_open(state: SessionState, phase: str) -> bool:
    return any(q.open and q.phase == phase for q in state.questions)


def reserve(state: SessionState) -> float:
    """Seconds kept for the closing step (0 once it has nothing left to ask): its planned time, and at
    least a slot plus its grace per question it still has open, plus one grace for the question on screen
    to give way while the closing has not started. The time being up ends the interview once the closing
    is done (lifecycle.py): a closing of three questions planned 1:20 ran 19:12 to 22:16 on the replay of a
    real interview, and with 90 s per question and no switch time it still ended 79 s past the time."""
    cur = state.get(state.focus_id)
    # the questions still to put up, and the one on screen (one already asked keeps no time: a "+5 min"
    # replay brought the closing back 3 minutes early when they still counted)
    n = len(closing_left(state)) + (1 if cur is not None and cur.open and cur.phase == CLOSING else 0)
    if not n:
        return 0.0
    switch = GRACE if state.phase != CLOSING else 0.0
    return max(planned(state).get(CLOSING, 0.0), n * (MIN_SLOT + GRACE) + switch)


def phase_share(state: SessionState, phase: str, now: float | None = None) -> float:
    """The budget of `phase` when it starts: its share of the time left (rule 1)."""
    if state.started_at is None:
        return planned(state).get(phase, 0.0)
    rest = left(state, now)
    if phase == CLOSING:
        return max(0.0, rest)
    idx = P.PHASE_INDEX.get(phase, 0)
    weights = planned(state)
    ahead = [p for p in P.PHASE_IDS if p != CLOSING and P.PHASE_INDEX[p] >= idx
             and (p == phase or _has_open(state, p))]
    share = weights.get(phase, 0.0) / (sum(weights.get(p, 0.0) for p in ahead) or 1.0)
    return max(0.0, rest - reserve(state)) * share


def phase_short(state: SessionState, phase: str | None, now: float | None = None) -> bool:
    """The current step has less than a question's slot left: only a priority-1 question still starts in it."""
    if phase is None or phase != state.phase or state.started_at is None or state.phase_since is None:
        return False
    return state.phase_budgets.get(phase, 0.0) - state.phase_elapsed(now) < MIN_SLOT


def must_left(state: SessionState, phase: str) -> bool:
    """A priority-1 question of `phase` is still to ask (not the one being asked, not asked before)."""
    seen = set(state.focus_history)
    return any(q.open and q.phase == phase and q.priority == 1 and q.id not in seen for q in state.questions)


def closing_left(state: SessionState) -> list[str]:
    """The closing's questions still to put up (open, never on screen)."""
    seen = set(state.focus_history)
    return [q.id for q in state.questions if q.open and q.phase == CLOSING and q.id not in seen]


def on_last_closing(state: SessionState) -> bool:
    """The question on screen is the closing's last one to ask: nothing is queued after it (rule 4)."""
    q = state.get(state.focus_id)
    return q is not None and q.phase == CLOSING and not closing_left(state)


def closing_done(state: SessionState) -> bool:
    """Rule 4: none of the closing's questions is left to put up, and the last one's answer is over (the
    session sets `closing_done_at` at its boundary) or the interviewer moved off it. True with no closing."""
    if closing_left(state):
        return False
    q = state.get(state.focus_id)
    return q is None or q.phase != CLOSING or state.closing_done_at is not None


def closing_reopened(state: SessionState, now: float | None = None) -> bool:
    """Rule 5: "+5 min" was clicked in the closing and there is room again before its reserve: the closing
    gives the floor back to what is still unasked, and comes back when it is due."""
    return (state.phase == CLOSING and state.closing_reopened
            and left(state, now) > reserve(state) + MIN_SLOT + GRACE)


def closing_due(state: SessionState, now: float | None = None) -> bool:
    """Time to move to the closing step (rule 3): the time left is down to its reserve, and one of its
    questions is still to ask."""
    seen = set(state.focus_history)
    if state.started_at is None or state.phase == CLOSING or not any(
            q.open and q.phase == CLOSING and q.id not in seen for q in state.questions):
        return False
    return left(state, now) <= reserve(state)


def _weight(q: QuestionState) -> float:
    return PRIORITY_WEIGHT.get(q.priority, 1.0)


def slot_for(state: SessionState, qid: str | None, now: float | None = None) -> float | None:
    """The slot of question `qid` as it goes up (rule 2): its share of what is left of its step, by
    priority, among the step's questions still to ask; at least MIN_SLOT."""
    q = state.get(qid)
    if q is None or state.started_at is None:
        return None
    phase = q.phase
    budget = state.phase_budgets.get(phase, 0.0)
    spent = state.phase_elapsed(now) if phase == state.phase else 0.0
    phase_left = max(0.0, budget - spent)
    seen = set(state.focus_history[:-1])  # the ones already asked (the last entry is `qid` itself)
    todo = [x for x in state.questions if x.open and x.phase == phase and x.id not in seen and x.id != qid]
    if phase == CLOSING:  # the interview ends at the time: each question's grace comes out of the closing's time
        phase_left = max(0.0, phase_left - GRACE * (1 + len(todo)))
    share = _weight(q) / (_weight(q) + sum(_weight(x) for x in todo))
    return max(MIN_SLOT, phase_left * share)


def slot_left(state: SessionState, now: float | None = None) -> float | None:
    """Seconds left in the slot of the question being asked (negative once over; None = no slot)."""
    if state.focus_slot is None or state.focus_since is None or state.started_at is None:
        return None
    return state.focus_slot - ((state.now() if now is None else now) - state.focus_since)


def slot_over(state: SessionState, now: float | None = None) -> bool:
    """The question being asked has had its time: its slot is over, or the closing is due (rule 3: the
    question on screen gives way at its next boundary, whatever its slot said)."""
    rest = slot_left(state, now)
    return (rest is not None and rest <= 0) or closing_due(state, now)


def replan(state: SessionState, now: float | None = None) -> None:
    """Rule 5, after the length changed mid-interview: the current step's budget is what it spent plus its
    new share of the time left, and the question on screen gets its new slot (the coverage allowance and
    the closing's reserve read the length directly)."""
    now = state.now() if now is None else now
    if state.started_at is None or state.phase is None or state.phase_since is None:
        return
    state.phase_budgets[state.phase] = round(state.phase_elapsed(now) + phase_share(state, state.phase, now), 1)
    if state.focus_id is not None and state.focus_since is not None:
        rest = slot_for(state, state.focus_id, now)
        if rest is not None:
            state.focus_slot = round(max(0.0, now - state.focus_since) + rest, 1)


def view(state: SessionState, now: float | None = None) -> dict:
    """What the screen shows (quiet: elapsed / total) and what the interviewer details show. `extend`: the
    "+5 min" button is offered (rule 5)."""
    rest = slot_left(state, now)
    remaining = left(state, now)
    return {"elapsed": round(elapsed(state, now)), "total": round(total(state)),
            "left": round(remaining), "over": remaining <= 0,
            "slot_left": None if rest is None else round(rest), "slot_over": rest is not None and rest <= 0,
            "closing": state.phase == CLOSING or closing_due(state, now),
            "closing_done": state.started_at is not None and closing_done(state),
            "extend": state.mode == "interview" and state.started_at is not None and remaining <= EXTEND_SHOWN,
            "extended": len(state.extensions)}
