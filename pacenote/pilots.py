"""Pilots: how many people could carry the first use case, and who opens the way when nobody does.

A first use case spreads through pilots: colleagues keen to go first, who prove it on real cases and
show their own numbers to the others. There can be several (ambassadors.py finds them during the
interview, from the interviewee's words). How many to expect depends on the company: its size, how
fast its sector moves (the pre-study's pace, horizons.py) and, when public sources say so, how young
its workforce is. A small team rarely has more than one; a dynamic mid-size company can have several;
a large company that moves slowly may show almost none in a first interview, and then the leader we
interviewed carries the first wave themself, leading by example.

expected() turns those signals into a light hint: a range and whether the leader will likely have to
open the way. It is a hint about the company, never a label on a person. The admin shows it with its
reasons (operator-only: the pace and the workforce signal never reach the client); the plan only says,
in neutral words, how many pilots a company of this size usually finds and how many the interview named.
"""

from __future__ import annotations

from .personas import headcount_number

WORKFORCES = ("young", "mixed", "mature", "unknown")

# (low, high) pilots to expect, from fewest to most; the size picks a band, the dynamism moves it
BANDS = ((0, 1), (1, 2), (1, 3), (2, 4), (3, 6))
SMALL, MID, LARGE = 10, 50, 250  # headcount thresholds: under 10, 10-49, 50-249, 250 and more
LEADER_PERSONAS = ("executive", "function_leader")  # who can sponsor the first wave themself


def _size_band(n: int) -> int:
    return 0 if n < SMALL else 1 if n < MID else 3 if n < LARGE else 4


def dynamism(posture: str | None, workforce: str | None) -> tuple[int, list[str]]:
    """-2..+2 and its factors: an urgent pace or a young workforce moves it up; an explorer pace (the
    sector is behind) or a mature workforce moves it down. Unknown signals count for nothing."""
    d, factors = 0, []
    if posture == "urgent":
        d, factors = d + 1, factors + ["pace_urgent"]
    elif posture == "explorer":
        d, factors = d - 1, factors + ["pace_explorer"]
    if workforce == "young":
        d, factors = d + 1, factors + ["workforce_young"]
    elif workforce == "mature":
        d, factors = d - 1, factors + ["workforce_mature"]
    return d, factors


def expected(headcount, posture: str | None = None, workforce: str | None = None) -> dict | None:
    """The hint: {low, high, leader_likely, size, factors} or None when the headcount is unknown.

    - under 10 people: 0 to 1, and the leader is close to the work: they will likely open the way;
    - from 10 people, the size sets a band (10-49: 1-2, 50-249: 2-4, 250+: 3-6) and the dynamism moves it
      one band up or down (a slow 10-49 company: 0 to 1, the leader will likely open the way);
    - 250 people or more that move slowly (dynamism below 0): volunteers rarely surface in a first
      interview: 0 to 2, and the leader will likely have to carry it."""
    n = headcount_number(headcount)
    if n is None:
        return None
    d, factors = dynamism(posture, workforce)
    if n < SMALL:
        low, high = BANDS[0]
        return {"low": low, "high": high, "leader_likely": True, "size": n, "factors": ["small_team", *factors]}
    if n >= LARGE and d < 0:
        return {"low": 0, "high": 2, "leader_likely": True, "size": n, "factors": ["large_slow", *factors]}
    band = max(0, min(len(BANDS) - 1, _size_band(n) + (d > 0) - (d < 0)))
    low, high = BANDS[band]
    return {"low": low, "high": high, "leader_likely": low == 0, "size": n, "factors": factors}


def for_client(info, preplan: dict | None, posture: str | None) -> dict | None:
    """The hint for a client: the sheet's headcount (else the pre-study's size band), the pace in force
    and the pre-study's workforce signal."""
    pre = (preplan or {}).get("prestudy") or {}
    headcount = (getattr(info, "headcount", "") if info is not None else "") or pre.get("size_band") or ""
    workforce = pre.get("workforce") if pre.get("workforce") in WORKFORCES else "unknown"
    return expected(headcount, posture, workforce)


# --- the leader as sponsor, when no pilot emerged ------------------------------------------------------


def can_sponsor(state) -> bool:
    """The person interviewed leads the company or a function (not an operator): they can carry the
    first wave themself when nobody else was named."""
    return state.persona in LEADER_PERSONAS


def sponsor_label(state, tr) -> str:
    """The sponsor as the plan names them: the interviewed leader's role, never their name (the plan is
    shared); the company's leader when no role was given."""
    role = ((state.interviewee or {}).get("role") or "").strip()
    return role or tr("plan.adoption.leaderDefault")


def hint_line(hint: dict | None, named: int, tr) -> str:
    """The plan's neutral line: how many pilots a company of this size usually finds, and how many the
    interview named. No pace, no workforce, no label on anyone. "" without a hint."""
    if not hint:
        return ""
    low, high = int(hint.get("low", 0)), int(hint.get("high", 0))
    usual = tr("plan.adoption.hint.range", low=low, high=high) if low != high else str(high)
    if named == 0 and hint.get("leader_likely"):
        key = "small" if "small_team" in (hint.get("factors") or []) else "leader"
    else:
        key = "none" if named == 0 else "one" if named == 1 else "line"
    return tr(f"plan.adoption.hint.{key}", usual=usual, named=named)


def prompt_line(hint: dict | None) -> str:
    """One line for the prompts (English, interviewer-side): the expected range and why."""
    if not hint:
        return "Expected pilots: unknown (no headcount on the client sheet)."
    why = ", ".join(hint.get("factors") or []) or "size only"
    lead = " The leader will likely have to open the way themself." if hint.get("leader_likely") else ""
    return (f"Expected pilots for a company of {hint.get('size')} people: {hint.get('low')} to {hint.get('high')} "
            f"({why}).{lead} A hint about the company, never a label on anyone.")
