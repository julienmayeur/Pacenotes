"""Who is in the meeting: one or several attendees, each with a persona and a prep note.

The client sheet lists the people who will be in the meeting (the CEO alone, a head of department, a head
and an operator, or three or four people). Each comes with a persona (personas.py). The session holds them
all; the live copilot knows every attendee, words each question for the person it is addressed to, and can
suggest addressing it to a specific attendee (the operator for the execution detail, the head for the
workflow).

Views, not stances. The transcript has no diarization: nobody knows who said a line, so there is no stance
per attendee. The different views heard in the room are grouped by statement instead (viewpoints.py).

Who to ask. The card can suggest the attendee a question is best put to (the consultant directing it); that
needs no speaker labels.

The lead. When an executive or a function leader is in the meeting, the most senior one is the lead: the
seed questions take that persona (state.persona), and the plan names them as the sponsor who leads by
example (pilots.py). An operator alone in the meeting is never the sponsor.
Duck-typed on the session state (no import cycle).
"""

from __future__ import annotations

import time

from .personas import DEFAULT, PERSONA_NAMES, PERSONAS, fold

SENIORITY = {"executive": 0, "function_leader": 1, "operator": 2}
LEADERS = ("executive", "function_leader")
_KEEP = ("index", "name", "role", "notes", "persona", "reason", "confidence", "origin")


def key(index: int) -> str:
    """The attendee's id in the prompts and the contract: a1, a2..."""
    return f"a{int(index) + 1}"


def build(people: list[dict]) -> list[dict]:
    """The session's attendees from the client sheet's people (personas.interviewees): each keeps its
    persona and gets an id."""
    out = []
    for i, p in enumerate(people or []):
        a = {k: p.get(k) for k in _KEEP if k in p}
        a["index"], a["key"] = i, key(i)
        a["persona"] = a.get("persona") if a.get("persona") in PERSONAS else DEFAULT
        out.append(a)
    return out


def of(state) -> list[dict]:
    """The attendees of a session. A session saved before attendees existed has its one interviewee."""
    if state.attendees:
        return state.attendees
    iv = state.interviewee or {}
    if iv and not (iv.get("origin") == "default" and not iv.get("role")):
        return build([iv])
    return []


def lead(attendees: list[dict]) -> dict | None:
    """The most senior leader in the meeting (an executive, else a function leader; the first listed when
    two are level), or None when only operators attend."""
    leaders = [a for a in attendees if a.get("persona") in LEADERS]
    return min(leaders, key=lambda a: (SENIORITY[a["persona"]], a.get("index", 0))) if leaders else None


def primary(attendees: list[dict]) -> dict | None:
    """Whose persona the seed questions take: the lead, else the first attendee (None: nobody listed)."""
    return lead(attendees) or (attendees[0] if attendees else None)


def label(a: dict) -> str:
    """"Claire Martin (order desk)", "the CFO" when no name: how the screen names an attendee."""
    name, role = (a.get("name") or "").strip(), (a.get("role") or "").strip()
    return f"{name} ({role})" if name and role else name or role or PERSONA_NAMES.get(a.get("persona"), "")


def find(state, ref: str) -> dict | None:
    """An attendee from the model's reference: its id (a2), else its name or role, accents and case
    ignored."""
    ref = (ref or "").strip()
    if not ref:
        return None
    people = of(state)
    for a in people:
        if a.get("key") == ref.lower():
            return a
    f = fold(ref)
    for a in people:
        if f and f in {fold(a.get("name", "")), fold(a.get("role", "")), fold(label(a))}:
            return a
    return None


def _clean(text, n: int = 200) -> str:
    return " ".join(str(text or "").split())[:n]


# --- who to ask: the question addressed to a specific attendee ----------------------------------------


def apply_ask_to(state, items, read: int, ok, reject) -> None:
    """Merge the model's picks: the question in focus (or another one) is best addressed to a given
    attendee. Only with two attendees or more."""
    for x in items or []:
        if len(of(state)) < 2:
            reject("ask_to", x.question_id, "one attendee: nobody else to ask")
            return
        q = state.get(_clean(x.question_id))
        a = find(state, x.attendee)
        if q is None or a is None:
            reject("ask_to", x.question_id, "unknown question or attendee")
            continue
        old = state.ask_to.get(q.id) or {}
        if old.get("read", 0) > read:
            continue
        state.ask_to[q.id] = {"attendee": a["key"], "reason": _clean(x.reason, 120), "read": read, "at": time.time()}
        ok("ask_to", q.id, f"{a['key']}: {x.reason}"[:120])


# the rule, without a model pick: the detail to the one who does the work, the outcome to the lead
_RUNG_PERSONAS = {"business": ("executive", "function_leader"), "workflow": ("function_leader", "executive"),
                  "implementation": ("operator", "function_leader"), "done": ("operator", "function_leader")}


def ask_to_for(state, qid: str | None = None) -> dict | None:
    """Who to address the question in focus to: {key, who, persona, reason, source (model | rule)}, or None
    (one attendee, or nobody fits). The model's pick wins; else the rung decides: the outcome to the
    lead, the steps to the function leader, the per-item detail to the operator."""
    people = of(state)
    if len(people) < 2:
        return None
    q = state.get(qid or state.focus_id)
    if q is None:
        return None
    pick = state.ask_to.get(q.id)
    if pick and (a := find(state, pick.get("attendee", ""))) is not None:
        return {"key": a["key"], "who": label(a), "persona": a["persona"], "reason": pick.get("reason", ""),
                "source": "model"}
    for persona in _RUNG_PERSONAS.get(q.rung, ()):
        a = next((x for x in people if x.get("persona") == persona), None)
        if a is not None:
            return {"key": a["key"], "who": label(a), "persona": persona, "reason": "", "source": "rule"}
    return None


# --- views and prompts ----------------------------------------------------------------------------------


def prep_note(a: dict, tr) -> str:
    """Before the meeting: how to approach this attendee, from their persona."""
    return tr(f"attendees.prep.{a.get('persona') if a.get('persona') in PERSONAS else DEFAULT}")


def view(state, tr) -> list[dict]:
    """Who is in the meeting, for the consent screen, the header and the console: persona, the lead, the
    prep note (a session saved with stances still loads: they are no longer read)."""
    ld = lead(of(state))
    return [{"key": a["key"], "name": a.get("name", ""), "role": a.get("role", ""), "persona": a["persona"],
             "lead": ld is not None and a["key"] == ld["key"], "prep": prep_note(a, tr)} for a in of(state)]


def prompt_block(state) -> str:
    """The attendees for the live prompts: id, who, persona, the lead."""
    people = of(state)
    if not people:
        return "- Nobody listed on the client sheet: one interviewee, asked as an executive."
    ld = lead(people)
    return "\n".join(f"- {a['key']}: {label(a)} — {PERSONA_NAMES[a['persona']]}"
                     f"{' (the lead: sponsor of the first wave)' if ld is not None and a['key'] == ld['key'] else ''}"
                     for a in people)


def snapshot(state) -> list[dict]:
    """The attendees as the sync model sees them."""
    return [{"id": a["key"], "who": label(a), "persona": a["persona"]} for a in of(state)]


def for_plan(state) -> list[dict]:
    """The attendees as the plan writer sees them: role and persona only, no name (the plan is shared with
    the client)."""
    return [{"role": (a.get("role") or "").strip(), "persona": a["persona"]} for a in of(state)]
