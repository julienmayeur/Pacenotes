"""Pilots and the sponsor: who carries the first use case.

A first use case spreads through pilots: colleagues keen to go first, who prove it on real cases and
show their own numbers to the others. Nobody can say in advance how many there are (none, a few, or a whole
team): they come only from the interview's answers (ambassadors.py), as named people or as a group the
interviewee describes ("the whole sales team is up for it"), recorded as a group, never with invented names.

The boss is always involved. When the leader of the company or of a function is in the meeting
(attendees.lead), the plan always names them as the sponsor who leads by example, alongside any pilots:
they cannot ask their teams to adopt it and do nothing themself. What they will do themself is found out
during the interview (the sponsor question, ambassadors.py) and goes into the first wave. An operator alone
in the meeting is never made the sponsor: the plan then says the sponsor is to be confirmed with the
leadership.
"""

from __future__ import annotations

from . import attendees as attendees_lib

LEADER_PERSONAS = attendees_lib.LEADERS  # who can sponsor the first wave themself


def sponsor(state) -> dict | None:
    """The leader in the meeting who sponsors the first wave (attendees.lead), or None."""
    return attendees_lib.lead(attendees_lib.of(state))


def can_sponsor(state) -> bool:
    """A leader of the company or of a function is in the meeting: they sponsor the first wave."""
    return sponsor(state) is not None


def sponsor_label(state, tr) -> str:
    """The sponsor as the plan names them: the leader's role, never their name (the plan is shared); the
    company's leader when no role was given; "to be confirmed with the leadership" when no leader attends."""
    lead = sponsor(state)
    if lead is None:
        return ""
    return (lead.get("role") or "").strip() or tr("plan.adoption.leaderDefault")
