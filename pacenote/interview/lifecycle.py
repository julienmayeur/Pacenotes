"""Listening belongs to the interview: the microphone is open ONLY while the interview runs.

The rule: Pacenotes never listens outside interview mode. Capture starts when the interview starts
(the Start button: `Session.start`, consent -> interview) and stops the moment the interview ends,
whatever ends it:

- **ended**: the interviewer ends it (End / e, "enough", /api/finalize);
- **time_up**: the interview's planned length (`timebox.total`, 20 min by default, plus every "+5 min") is
  over and the closing is done, at the next boundary (timebox.py rule 4); a closing still running at the
  time goes on to its end, but never more than `OVERRUN` past it;
- **page_left**: no interview screen has been open for `PAGE_GRACE` s (closed, navigated away); a reload
  reconnects well within that;
- **idle**: nobody has been heard for `IDLE_AFTER` s (on the speech clock);
- **shutdown**: the server stops (Ctrl-C).

What happened once: the copilot said "time is up", the screen switched to its end mode, and the server
went on transcribing the room for another 32 minutes, after the call. Every way out now goes through
one door, enforced in `session.py`:

1. `Session._set_mode` is the only writer of `state.mode`. Leaving "interview" stops capture FIRST,
   before the last analysis or the plan (both can take tens of seconds); nothing but `Session.start`
   enters "interview", and only from the consent screen.
2. `Session.persist` re-checks the rule on every save: capture open in any other mode is stopped and
   logged as a violation (`listening-invariant` in sync_log.jsonl).
3. Outside the interview nothing reads the recogniser and the speech watchdog never restarts one; an
   interview that ends while the speech helper is still starting stops it as soon as it is up.

Ending the interview (`Session.end_interview`) is synchronous and cannot fail; writing the plan comes
after it and never holds the microphone open.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from pacenote.interview import timebox

if TYPE_CHECKING:  # pragma: no cover
    from pacenote.interview.state import SessionState

OVERRUN = 120.0  # s past the planned length at most: the time ends the interview then, closing done or not
PAGE_GRACE = 10.0  # s with no interview screen open before the interview ends (a reload takes ~1 s)
IDLE_AFTER = 300.0  # s with no speech heard (speech clock) before the interview ends

REASONS = ("ended", "time_up", "page_left", "idle", "shutdown")


def end_reason(state: SessionState, *, speech_now: float, last_heard: float,
               page_seen: bool, page_gone_for: float | None, at_boundary: bool = True) -> str | None:
    """Why the running interview must end now, or None. `speech_now` / `last_heard`: the speech clock
    and the end of the last line heard on it (0 = the start); `page_seen`: an interview screen was open
    during the interview; `page_gone_for`: seconds since the last one closed (None = one is open);
    `at_boundary`: now is a boundary to stop at (a pause, a sentence end: session.py)."""
    if state.mode != "interview" or state.started_at is None:
        return None
    rest = timebox.left(state)
    if rest <= -OVERRUN or (rest <= 0 and at_boundary and timebox.closing_done(state)):
        return "time_up"
    if page_seen and page_gone_for is not None and page_gone_for >= PAGE_GRACE:
        return "page_left"
    if math.isfinite(speech_now) and speech_now - last_heard >= IDLE_AFTER:  # (a replay at speed 0 has no clock)
        return "idle"
    return None
