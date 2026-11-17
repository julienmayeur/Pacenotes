"""Who is in the meeting: one or several attendees, each with a persona and a live stance toward AI.

The client sheet lists the people who will be in the meeting (the CEO alone, a head of department, a head
and an operator, or three or four people). Each comes with a persona (personas.py). The session holds them
all; the live copilot knows every attendee, words each question for the person it is addressed to, and can
suggest addressing it to a specific attendee (the operator for the execution detail, the head for the
workflow).

Stance. For each attendee the live sync keeps a stance toward AI, discovered through the conversation:
driver (pushes for it, already does things), open, hesitant, opposed, or unknown, with short evidence quotes
and a confidence. It starts unknown. It is interviewer-side only: shown in the console, stored in the
session data (sessions/, never in git), never in the plan or the PDF.

Speaker attribution. The transcript has no diarization: nobody knows who said a line. A statement counts for
an attendee only when the conversation makes it clear: they introduce themself ("as head of sales I..."),
someone names them, or the interviewer's question was addressed to them. Otherwise it is "unknown" and no
stance moves. Simple and honest.

The lead. When an executive or a function leader is in the meeting, the most senior one is the lead: the
seed questions take that persona (state.persona), and the plan names them as the sponsor who leads by
example (pilots.py). An operator alone in the meeting is never the sponsor.
Duck-typed on the session state (no import cycle).
"""

from __future__ import annotations

import time

from .personas import DEFAULT, PERSONA_NAMES, PERSONAS, fold

STANCES = ("driver", "open", "hesitant", "opposed", "unknown")
ATTRIBUTIONS = ("self", "named", "addressed")  # how the transcript shows who spoke
SENIORITY = {"executive": 0, "function_leader": 1, "operator": 2}
LEADERS = ("executive", "function_leader")
MAX_EVIDENCE = 3
_KEEP = ("index", "name", "role", "notes", "persona", "reason", "confidence", "origin")


def key(index: int) -> str:
    """The attendee's id in the prompts and the contract: a1, a2..."""
    return f"a{int(index) + 1}"


def build(people: list[dict]) -> list[dict]:
    """The session's attendees from the client sheet's people (personas.interviewees): each keeps its
    persona and gets an id and an unknown stance."""
    out = []
    for i, p in enumerate(people or []):
        a = {k: p.get(k) for k in _KEEP if k in p}
        a["index"], a["key"] = i, key(i)
        a["persona"] = a.get("persona") if a.get("persona") in PERSONAS else DEFAULT
        a["stance"] = {"label": "unknown", "confidence": 0.0, "evidence": [], "read": 0, "at": None}
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


# --- the live stance ----------------------------------------------------------------------------------


def _clean(text, n: int = 200) -> str:
    return " ".join(str(text or "").split())[:n]


def apply_stances(state, items, read: int, ok, reject) -> None:
    """Merge the model's stance reads. Rejected: an unknown attendee, a line not attributed by what was
    said (self, named, addressed), a stance other than unknown without a quote, a read older than the
    one kept. Evidence quotes accumulate (newest first, at most three)."""
    if not state.attendees:
        state.attendees = of(state)
    for s in items or []:
        a = find(state, s.attendee)
        if a is None:
            reject("stance", s.attendee, "unknown attendee")
            continue
        if s.attributed not in ATTRIBUTIONS:
            reject("stance", a["key"], "not attributed: the transcript has no speakers")
            continue
        quote = _clean(s.evidence)
        if s.stance != "unknown" and not quote:
            reject("stance", a["key"], "a stance needs the words it comes from")
            continue
        old = a.get("stance") or {}
        if old.get("read", 0) > read:
            reject("stale", a["key"], f"read at line {read}, kept the stance read at line {old['read']}")
            continue
        evidence = list(old.get("evidence") or [])
        if quote and quote not in evidence:
            evidence = [quote, *evidence][:MAX_EVIDENCE]
        try:
            conf = round(max(0.0, min(1.0, float(s.confidence))), 2)
        except (TypeError, ValueError):
            conf = 0.0
        a["stance"] = {"label": s.stance, "confidence": conf, "evidence": evidence, "read": read,
                       "attributed": s.attributed, "at": time.time()}
        ok("stance", a["key"], f"{s.stance} ({conf}): {quote}"[:120])


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
    """Before the meeting: how to approach this attendee, from their persona (the stance starts unknown)."""
    return tr(f"attendees.prep.{a.get('persona') if a.get('persona') in PERSONAS else DEFAULT}")


def view(state, tr) -> list[dict]:
    """The console's stance view: each attendee with persona, stance, evidence, confidence and prep note."""
    out = []
    for a in of(state):
        s = a.get("stance") or {}
        out.append({"key": a["key"], "name": a.get("name", ""), "role": a.get("role", ""), "persona": a["persona"],
                    "lead": False, "stance": s.get("label", "unknown"), "confidence": s.get("confidence", 0.0),
                    "evidence": list(s.get("evidence") or []), "prep": prep_note(a, tr)})
    if (ld := lead(of(state))) is not None:
        for v in out:
            v["lead"] = v["key"] == ld["key"]
    return out


STANCE_RULES = {
    "driver": "explore what they already do with these tools and what they would take further; they can carry it",
    "open": "give them a concrete case to picture it on their own work",
    "hesitant": "stay on their own work and the time it would give back; one concrete case, no promise",
    "opposed": "stay concrete on the time given back and the decision they keep; never push, never argue",
    "unknown": "listen first: how they talk about their work and the last change of tools",
}


def prompt_block(state) -> str:
    """The attendees for the live prompts: id, who, persona, stance so far (interviewer-side)."""
    people = of(state)
    if not people:
        return "- Nobody listed on the client sheet: one interviewee, asked as an executive."
    ld = lead(people)
    lines = []
    for a in people:
        s = a.get("stance") or {}
        stance = s.get("label", "unknown")
        lead_note = " (the lead: sponsor of the first wave)" if ld is not None and a["key"] == ld["key"] else ""
        lines.append(f"- {a['key']}: {label(a)} — {PERSONA_NAMES[a['persona']]}{lead_note}; stance so far: {stance}"
                     f"{' (' + str(s.get('confidence')) + ')' if stance != 'unknown' else ''}; "
                     f"with them: {STANCE_RULES[stance]}.")
    return "\n".join(lines)


def snapshot(state) -> list[dict]:
    """The attendees as the sync model sees them (stance included: interviewer-side only)."""
    return [{"id": a["key"], "who": label(a), "persona": a["persona"],
             "stance": (a.get("stance") or {}).get("label", "unknown"),
             "evidence": (a.get("stance") or {}).get("evidence", [])[:2]} for a in of(state)]


def for_plan(state) -> list[dict]:
    """The attendees as the plan writer sees them: role and persona only. No name, no stance (the plan is
    shared with the client)."""
    return [{"role": (a.get("role") or "").strip(), "persona": a["persona"]} for a in of(state)]
