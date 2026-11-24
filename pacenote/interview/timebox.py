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
   is `GRACE` seconds over, the next line that ends a sentence), never mid-sentence.
   A step with less than a slot left starts no new question but a priority-1 one: an overrun eats the
   priority 2 and 3 questions first.
3. **The closing is protected.** When the time left reaches the closing step's reserve, the next
   question is the closing step's, wherever the interview is: what matters for the plan (the return,
   the horizon) is never lost to time.

When the time is up nothing stops: the copilot keeps listening, analysing and proposing the closing
questions, and the interviewer ends the interview (which writes the plan) when they choose.
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
    """Seconds kept for the closing step (0 once it has nothing left to ask)."""
    return planned(state).get(CLOSING, 0.0) if _has_open(state, CLOSING) else 0.0


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


def closing_due(state: SessionState, now: float | None = None) -> bool:
    """Time to move to the closing step (rule 3): the time left is down to its reserve."""
    if state.started_at is None or state.phase == CLOSING or not _has_open(state, CLOSING):
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


def view(state: SessionState, now: float | None = None) -> dict:
    """What the screen shows (quiet: elapsed / total) and what the interviewer details show."""
    rest = slot_left(state, now)
    return {"elapsed": round(elapsed(state, now)), "total": round(total(state)),
            "left": round(left(state, now)), "over": left(state, now) <= 0,
            "slot_left": None if rest is None else round(rest), "slot_over": rest is not None and rest <= 0,
            "closing": state.phase == CLOSING or closing_due(state, now)}
