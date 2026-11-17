"""The executive lens: what to say and what to measure, per workflow, in a 15-minute interview.

Pacenote captures what it can in about fifteen minutes with one person, not by watching staff
for weeks: the weak signals from the floor rarely surface there. So the copilot helps the
interviewer guide the interviewee on value and feasibility, workflow by workflow (the talking points
are for an executive or a function leader, never for an operator: see personas.py):

- Value framings. When the interviewee describes manual work (someone opens emails by hand and re-keys
  them into Excel), the live sync writes a short, tactful talking point: this task brings that
  person little, it can run on its own, and their time goes to higher-level decisions. It is about
  the task, never the person, and never says AI or "automate" (the questions' wording rule). The
  screen shows it under the follow-up ("Worth saying"); why it is said now stays in the console.
  The plan reflects it per opportunity: the task that goes away, the time freed (from numbers
  said only) and the decision the person moves up to.
- Metric questions. Every opportunity needs a baseline (today, with a number) and a success
  metric (time spent, volume, error rate, delay). Once a workflow was walked through and either is
  still missing, the screen suggests the question ("Also worth asking", next to the people
  question when there is one): the model's wording for that workflow, else a template. The metrics check the work heads the right way, and prove the
  value to the rest of the team (the adoption plan's second wave starts from them).

Duck-typed on state.SessionState (no import cycle). Each suggestion shows once: when the focus
leaves a question while it was on screen, it is marked asked; back on that same question it shows
again.
"""

from __future__ import annotations

import re
import time

from pacenote.people.pilots import PROBE_BANNED, clean, workflow_discussed

KINDS = ("time", "volume", "errors", "delay")
SIDES = ("baseline", "target")
MAX_FRAMINGS = 12

# A talking point is said to the interviewee about their people's work: no AI vocabulary, and nothing that
# belittles the person or sounds like a job cut (the task brings little, never the person).
_FRAMING_BANNED = [
    *PROBE_BANNED,
    *(re.compile(p, re.I) for p in (
        r"remplac", r"\breplac", r"inutile", r"useless", r"pointless", r"worthless", r"sans valeur", r"\bbête",
        r"stupid", r"idiot", r"licenci", r"lay ?offs?", r"head ?count", r"effectifs?\b", r"supprim\w* (le |son |ce )?poste",
        r"cut (the |their |her |his )?(job|role|post)")),
]


def wording_problem(text: str) -> str:
    """The first word that makes `text` unfit to be said to the interviewee ("" when it is fine)."""
    for p in _FRAMING_BANNED:
        if m := p.search(text or ""):
            return m.group(0)
    return ""


def _focused(state, qid: str | None):
    q = state.get(qid or state.focus_id)
    if q is None or not q.use_case or q.status == "removed" or state.get_use_case(q.use_case) is None:
        return None
    return q


# --- value framings -------------------------------------------------------------------------------


def apply_framings(state, framings, read: int, ok, reject) -> None:
    """Merge the model's talking points, one per use case. Rejected: an unknown use case, an empty
    point, wording that belittles the person, sounds like a job cut or uses AI vocabulary, a point
    read from an older slice than the stored one, a change to one already said. "done" closes it."""
    for f in framings or []:
        uc = clean(f.use_case)
        if state.get_use_case(uc) is None:
            reject("framing", uc, f"unknown use case {uc!r}")
            continue
        old = state.value_framings.get(uc)
        if old is not None and old.get("read", 0) > read:
            reject("stale", uc, f"read at line {read}, kept the framing read at line {old['read']}")
            continue
        fields = {"person": clean(f.person), "task": clean(f.task), "moves_up_to": clean(f.moves_up_to)}
        if f.timing == "done":
            if old is None or old.get("timing") != "done":
                base = old or {**fields, "point": "", "reason": "", "asked": True}
                state.value_framings[uc] = {**base, "timing": "done", "read": read, "at": time.time()}
                ok("framing", uc, "done")
            continue
        point = clean(f.point)
        if not point:
            reject("framing", uc, "no talking point")
            continue
        if bad := wording_problem(" ".join((point, fields["moves_up_to"]))):
            reject("framing", uc, f"wording: {bad!r} (about the task, never the person; no AI vocabulary)")
            continue
        if old is not None and old.get("asked"):
            reject("framing", uc, "already said")
            continue
        if old is None and len(state.value_framings) >= MAX_FRAMINGS:
            reject("framing", uc, f"more than {MAX_FRAMINGS} talking points")
            continue
        new = {**fields, "point": point, "reason": clean(f.reason), "timing": f.timing, "read": read,
               "at": time.time(), "asked": False}
        if old is not None and all(old.get(k) == new[k] for k in (*fields, "point", "reason", "timing")):
            continue
        state.value_framings[uc] = new
        ok("framing", uc, f"{f.timing}: {point}"[:120])


def framing_for(state, qid: str | None = None, lang: str | None = None) -> dict | None:
    """The talking point to show under question `qid` (default: the one in focus), or None:
    {use_case (title), point (to say), moves_up_to, reason (interviewer only)}. Only on a question
    about a use case whose workflow was walked through, when the model says "now"; once (back on the
    question where it showed, it shows again)."""
    q = _focused(state, qid)
    if q is None or not workflow_discussed(state, q.use_case):
        return None
    if getattr(state, "persona", "") == "operator":  # never tell someone their own task brings little
        return None
    f = state.value_framings.get(q.use_case)
    if f is None or f.get("timing") == "done" or not f.get("point"):
        return None
    shown_here = state.lens_prompted.get(q.id, {}).get("framing") == q.use_case
    if not shown_here and (f.get("timing") != "now" or f.get("asked")):
        return None
    title = state.get_use_case(q.use_case).title(lang or state.language)
    return {"use_case": title, "point": f["point"], "moves_up_to": f.get("moves_up_to", ""),
            "reason": f.get("reason", "")}


def framings_view(state, lang: str | None = None) -> list[dict]:
    """The console's talking points: per workflow, the point, the decision the person moves up to,
    why now, whether it is held for later or was said (closed ones left out)."""
    out = []
    for uc, f in state.value_framings.items():
        if f.get("timing") == "done" or not f.get("point"):
            continue
        u = state.get_use_case(uc)
        out.append({"use_case": u.title(lang or state.language) if u else uc, "point": f["point"],
                    "moves_up_to": f.get("moves_up_to", ""), "reason": f.get("reason", ""),
                    "timing": f.get("timing", ""), "asked": bool(f.get("asked"))})
    return out


# --- metric questions -------------------------------------------------------------------------------


def apply_metric_questions(state, items, read: int, ok, reject) -> None:
    """Keep the model's question per (use case, side): the newest wins. Rejected: an unknown use
    case, an empty question or AI vocabulary."""
    for m in items or []:
        uc = clean(m.use_case)
        if state.get_use_case(uc) is None:
            reject("metric", uc, f"unknown use case {uc!r}")
            continue
        question = clean(m.question)
        if not question:
            reject("metric", uc, "no question")
            continue
        if bad := wording_problem(question):
            reject("metric", uc, f"wording: {bad!r}")
            continue
        key = f"{uc}:{m.side}"
        old = state.metric_questions.get(key)
        if old is not None and old.get("read", 0) > read:
            continue
        new = {"kind": m.kind, "question": question, "read": read}
        if old is None or any(old.get(k) != new[k] for k in ("kind", "question")):
            state.metric_questions[key] = new
            ok("metric", key, f"{m.kind}: {question}"[:120])


def missing_side(state, use_case: str) -> str | None:
    """The metric side still missing on a use case: its baseline first, then its target."""
    u = state.get_use_case(use_case)
    if u is None:
        return None
    return next((s for s in SIDES if not u.filled(s)), None)


def metric_ask_for(state, qid: str | None = None, lang: str | None = None) -> dict | None:
    """The metric question to suggest under question `qid` (default: the one in focus), or None:
    {use_case (title), side, kind, question, source (model | template)}. Only on a question about a
    use case whose workflow was walked through and whose baseline or target is still missing; once
    per (use case, side), except back on the question where it showed."""
    q = _focused(state, qid)
    if q is None or not workflow_discussed(state, q.use_case):
        return None
    side = missing_side(state, q.use_case)
    if side is None:
        return None
    key = f"{q.use_case}:{side}"
    if state.lens_prompted.get(q.id, {}).get("metric") != key and key in state.metric_asked:
        return None
    m = state.metric_questions.get(key) or {}
    kind = m.get("kind") if m.get("kind") in KINDS else "time"
    title = state.get_use_case(q.use_case).title(lang or state.language)
    if m.get("question"):
        return {"use_case": title, "side": side, "kind": kind, "question": m["question"], "source": "model"}
    return {"use_case": title, "side": side, "kind": kind,
            "question": state.tr()(f"focus.metric.{side}.{kind}"), "source": "template"}


def metric_kind(state, use_case: str) -> str:
    """The kind of metric the interview aimed at for a use case ("" when none)."""
    for side in SIDES:
        if (m := state.metric_questions.get(f"{use_case}:{side}")) and m.get("kind") in KINDS:
            return m["kind"]
    return ""


# --- what the screen showed, recorded when the focus leaves a question ----------------------------------


def mark_shown(state, qid: str | None) -> None:
    """The focus leaves question `qid`: what was on screen under it is now asked (or said)."""
    q = state.get(qid)
    if q is None:
        return
    shown = {}
    if (m := metric_ask_for(state, qid)) is not None:
        shown["metric"] = f"{q.use_case}:{m['side']}"
        if shown["metric"] not in state.metric_asked:
            state.metric_asked.append(shown["metric"])
    if framing_for(state, qid) is not None:
        shown["framing"] = q.use_case
        state.value_framings[q.use_case]["asked"] = True
    if shown:
        state.lens_prompted[q.id] = shown


def for_plan(state) -> list[dict]:
    """What the plan may use: per workflow, the task, who does it and the decision they move up to
    (never the reason, which is interviewer-side)."""
    return [{"use_case": uc, "person": f.get("person", ""), "task": f.get("task", ""),
             "moves_up_to": f.get("moves_up_to", "")}
            for uc, f in state.value_framings.items() if f.get("task") or f.get("moves_up_to")]
