"""Who we interview: the interviewee's persona shapes the whole interview.

Three personas, kept small on purpose:

- executive: runs the company (CEO, managing director, founder of a company of 20 people or more).
  Knows the outcomes, the value, where the team's time goes and who does what; not the execution
  details. Asked at workflow level, with the executive lens (lens.py).
- function_leader: owns a process and a team (CIO, CFO, COO, head of a department). Knows the process,
  its tools and its figures, not every keystroke. Asked a mix of both.
- operator: does the work every day (order desk, accounting, planning, after-sales). Knows each step,
  the tools, the volumes and the exceptions; may not have the big picture. Asked precise, step-by-step
  questions with concrete numbers; no strategy.

Size matters: the CEO of a 6-person company is close to the work (asked like a function leader); the
CEO of 20+ people is not. A department head in a company under 10 people usually does the work too.

Where a contact's persona comes from (first that applies):
1. manual: the interviewer picked it in the admin (client.yaml, contact `persona`);
2. research: the pre-study's guess (research/desk.py, preplan.json `personas`), from what it found about
   the person and the company;
3. rule: infer() below, from the title, the headcount and the interviewer's notes, so a contact added
   after the research still gets one at once.

When the interviewee cannot answer something (an executive asked a per-item detail), the copilot never
pushes: the live sync records it as "to check with <role>" (contract.ToCheck, state.to_check), those
details stop counting as missing on the card, and the plan lists them for the other people to see.
Duck-typed on the client sheet and the session state (no import cycle).
"""

from __future__ import annotations

import re
import time
import unicodedata

from pacenote.copilot.contract import SLOT_KEYS

PERSONAS = ("executive", "function_leader", "operator")
DEFAULT = "executive"  # no contact, or a title the rule does not know: the interview's former default
SMALL_COMPANY = 10  # under this headcount, titles sit close to the work
LARGE_COMPANY = 20  # from this headcount, a CEO no longer knows the execution details
MAX_TO_CHECK = 24


def fold(text: str) -> str:
    s = unicodedata.normalize("NFKD", str(text or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


# accent-folded title patterns (EN, FR, DE, ES), matched on whole words of the folded title
_EXECUTIVE = re.compile(
    r"\b(ceo|pdg|p d g|president directeur general|directeur general|directrice generale|dg|dga|gerante?|"
    r"co ?gerante?|founder|co ?founder|fondat(eur|rice)|co ?fondat(eur|rice)|presidente?|president|"
    r"managing director|md|owner|proprietaire|dirigeante?|chief executive|general manager|"
    r"geschaftsfuhrer(in)?|inhaber(in)?|director general|directora general|administrat(eur|rice) delegue)\b")
_NOT_EXECUTIVE = re.compile(r"\b(vice|vp|adjointe? (au|a la|du)|assistante? (de|du|of|to))\b")
_LEADER = re.compile(
    r"\b(cio|cfo|coo|cto|cmo|cso|cpo|chro|cdo|chief|dsi|daf|drh|dsi|directeur|directrice|director|directora|"
    r"head|vp|vice president|responsable|manager|chef de service|chef d equipe|team lead|lead|superviseur|"
    r"supervisor|leiter(in)?|jefe|jefa|controller|controleur de gestion)\b")
_OPERATOR = re.compile(
    r"\b(assistante?|assistant|adv|administration des ventes|gestionnaire|comptable|accountant|bookkeeper|"
    r"technicien(ne)?|technician|acheteuse|acheteur|buyer|approvisionneu(r|se)|magasinier|warehouse|"
    r"preparat(eur|rice)|operat(eur|rice)|operator|clerk|agent|chargee? de|charge d|sedentaire|"
    r"service client|customer service|secretaire|secretary|planner|planificat(eur|rice)|dispatcher|"
    r"employee?|employe|analyste?|analyst|coordinat(eur|rice)|coordinator|conseill(er|ere)|"
    r"sachbearbeiter(in)?|administrati(f|ve)|administrative|back office|opera(dor|dora))\b")
# the interviewer's notes say the person does the work themself
_HANDS_ON = re.compile(
    r"\b(hands on|fait (lui|elle) meme|fait tout|saisit|ressaisit|does (it|the work|everything) (him|her|them)sel(f|ves)|"
    r"does it all|in the weeds|dans le detail|sur le terrain|on the floor|close to the work|proche du terrain)\b")


def headcount_number(text) -> int | None:
    """A headcount from the sheet ("35", "about 35 employees", "20-49", "10 à 19 salariés", "1 200"):
    a single number, or the middle of a range. None when no number is given."""
    s = str(text or "").replace(" ", " ").replace("\xa0", " ")
    nums = [int(re.sub(r"[ ,.]", "", n)) for n in re.findall(r"\d{1,3}(?:[ ,.]\d{3})+(?!\d)|\d+", s)]
    nums = [n for n in nums if 0 < n < 10_000_000]
    if not nums:
        return None
    if len(nums) >= 2 and re.search(r"\d\s*(-|–|—|à|a|to|bis)\s*\d", s) and nums[1] > nums[0]:
        return (nums[0] + nums[1]) // 2
    return nums[0]


def title_kind(role: str) -> str | None:
    """executive, function_leader or operator from a job title alone (None: not recognised)."""
    f = fold(role)
    if not f:
        return None
    if _EXECUTIVE.search(f) and not _NOT_EXECUTIVE.search(f):
        return "executive"
    if _OPERATOR.search(f) and not re.search(
            r"\b(head|chief|directeur|directrice|director|responsable|chef de service|chef d equipe|team lead|"
            r"superviseur|supervisor|leiter(in)?)\b", f):
        return "operator"
    if _LEADER.search(f):
        return "function_leader"
    return None


def infer(role: str, headcount="", notes: str = "") -> dict:
    """The rule: {persona, reason (EN, short), confidence, origin: "rule"}. Title first, then size,
    then the interviewer's notes (a hands-on note moves one step towards the work)."""
    kind = title_kind(role)
    n = headcount_number(headcount)
    size = f"{n} people" if n else "headcount unknown"
    title = " ".join(str(role or "").split()) or "no title"
    if kind is None:
        persona, conf, why = DEFAULT, 0.3, f"{title}: title not recognised, the default"
    elif kind == "executive":
        if n is not None and n < SMALL_COMPANY:
            persona, conf, why = ("function_leader", 0.7,
                                  f"{title} of a {n}-person company: close to the work, knows both the outcomes and the details")
        elif n is not None and n < LARGE_COMPANY:
            persona, conf, why = "executive", 0.6, f"{title} of a {n}-person company: big picture first, may still know some details"
        else:
            persona, conf, why = ("executive", 0.85 if n else 0.65,
                                  f"{title} ({size}): the big picture, not the execution details")
    elif kind == "function_leader":
        if n is not None and n < SMALL_COMPANY:
            persona, conf, why = "operator", 0.55, f"{title} in a {n}-person company: most likely does the work too"
        else:
            persona, conf, why = ("function_leader", 0.8 if n else 0.7,
                                  f"{title} ({size}): owns a process and a team, knows its figures")
    else:
        persona, conf, why = "operator", 0.85, f"{title}: does the work every day"
    if _HANDS_ON.search(fold(notes)) and persona != "operator":
        persona = "function_leader" if persona == "executive" else "operator"
        conf, why = min(conf, 0.7), why + "; the notes say they do the work themself"
    return {"persona": persona, "reason": why, "confidence": round(conf, 2), "origin": "rule"}


def research_guess(preplan: dict | None, name: str) -> dict | None:
    """The pre-study's guess for this contact (preplan.json `personas`, matched on the folded name)."""
    key = fold(name)
    if not key:
        return None
    for g in (preplan or {}).get("personas") or []:
        if isinstance(g, dict) and fold(g.get("name", "")) == key and g.get("persona") in PERSONAS:
            try:
                conf = max(0.0, min(1.0, float(g.get("confidence") or 0)))
            except (TypeError, ValueError):
                conf = 0.0
            return {"persona": g["persona"], "reason": " ".join(str(g.get("reason") or "").split())[:240],
                    "confidence": round(conf, 2), "origin": "research"}
    return None


def for_contact(contact, info=None, preplan: dict | None = None) -> dict:
    """A contact's persona: {persona, reason, confidence, origin (manual | research | rule), inferred}
    where `inferred` is what research or the rule would give without the manual pick."""
    role = getattr(contact, "role", "") or ""
    guess = research_guess(preplan, getattr(contact, "name", "") or "") or infer(
        role, getattr(info, "headcount", "") if info is not None else "", getattr(contact, "notes", "") or "")
    manual = getattr(contact, "persona", "") or ""
    if manual in PERSONAS:
        return {"persona": manual, "reason": "", "confidence": 1.0, "origin": "manual", "inferred": guess}
    return {**guess, "inferred": guess}


def interviewees(info, preplan: dict | None = None) -> list[dict]:
    """The people we will interview (the sheet's contacts with a name or a role), each with their
    persona: [{index, name, role, notes, persona, reason, confidence, origin, inferred}]."""
    out = []
    for c in getattr(info, "contacts", None) or []:
        if not ((c.name or "").strip() or (c.role or "").strip()):
            continue
        out.append({"index": len(out), "name": c.name.strip(), "role": c.role.strip(),
                    "notes": (getattr(c, "notes", "") or "").strip(), **for_contact(c, info, preplan)})
    return out


def default_interviewee() -> dict:
    return {"index": -1, "name": "", "role": "", "notes": "", "persona": DEFAULT, "reason": "", "confidence": 0.0,
            "origin": "default", "inferred": None}


# --- prompts ------------------------------------------------------------------------------------------

PERSONA_RULES = {
    "executive": (
        "an executive (big picture). Ask at workflow level: outcomes, value, where the team's time goes, the "
        "decisions, who does what, on a real recent case told as a story. Do not dig into execution details "
        "they cannot know (field-by-field steps, screens, per-item times or volumes per person): when a detail "
        "is beyond them, record who would know in to_check and move on. Keep the executive lens: the value of "
        "the manual work and the decisions people would move up to."),
    "function_leader": (
        "a function leader (owns a process and a team). Mix the outcomes and how the process runs: who owns each "
        "step, the tools by name, the figures they follow (KPIs), the team's load and its handoffs. They know the "
        "process and its numbers, not every keystroke: per-item details they cannot give go to to_check with the "
        "person doing the work."),
    "operator": (
        "a hands-on operator (does the work every day). Ask precise, step-by-step questions on the last real "
        "case: what comes in and from where, which tool and screen, how many per day, how long per item, the "
        "exceptions and error cases and what they do then. Metric questions get concrete numbers from their own "
        "week. Do not ask about strategy, company goals, budgets or competitors: they may not have the big "
        "picture; those go to to_check with the executive."),
}

PERSONA_NAMES = {"executive": "executive", "function_leader": "function leader", "operator": "hands-on operator"}


def describe(interviewee: dict | None) -> str:
    """One line for the prompts: who we interview (role and persona, never needed by name)."""
    iv = interviewee or {}
    persona = iv.get("persona") if iv.get("persona") in PERSONAS else DEFAULT
    role = (iv.get("role") or "").strip()
    who = f"{iv.get('name')}, {role}" if iv.get("name") and role else (iv.get("name") or role or "role not given")
    return f"{who} — {PERSONA_NAMES[persona]}"


# --- questions adapted to the persona -------------------------------------------------------------------


def apply_to_questions(state, persona: str) -> list[str]:
    """Swap each seed question's wording and priority for the persona's variant (questions.yaml
    `personas:`), restoring the base first; priority 0 = not asked to this persona (removed, never shown
    as skipped). Only questions nobody touched yet. Returns the ids changed."""
    changed = []
    for q in state.questions:
        if not q.variants or q.origin != "seed" or q.answer or q.thread:
            continue
        if not q.base:
            q.base = {"texts": dict(q.texts), "priority": q.priority}
        before = (dict(q.texts), q.priority, q.status)
        v = q.variants.get(persona) or {}
        # a variant's wording replaces every language (a translation of the base wording would be wrong)
        texts = dict(v["texts"]) if v.get("texts") else dict(q.base["texts"])
        priority = int(v["priority"]) if v.get("priority") is not None else int(q.base["priority"])
        q.persona_skip = priority == 0
        q.priority = priority or int(q.base["priority"])
        q.status = "removed" if q.persona_skip else ("pending" if q.status == "removed" else q.status)
        if texts != q.texts:
            q.texts, q.machine = {}, []
            q.text_fr, q.text_en = "", ""
            for lang, t in texts.items():
                q.set_text(lang, t)
        if (dict(q.texts), q.priority, q.status) != before:
            changed.append(q.id)
    return changed


# --- to check with another person ------------------------------------------------------------------------


def _clean(text) -> str:
    return " ".join(str(text or "").split())


def apply_to_check(state, items, read: int, ok, reject) -> None:
    """Record what the interviewee could not answer, with who would know. Each new item defers the
    expect items behind its slots on the questions of that use case (or on that question): the card
    stops listing them as missing and the follow-ups move on."""
    for t in items or []:
        question, ask = _clean(t.question), _clean(t.ask)
        if not question or not ask:
            reject("to_check", t.use_case or t.question_id, "a question and who to ask are both needed")
            continue
        uc = _clean(t.use_case) or None
        if uc and state.get_use_case(uc) is None:
            reject("to_check", uc, f"unknown use case {uc!r}")
            continue
        qid = _clean(t.question_id) or None
        if qid and state.get(qid) is None:
            qid = None
        slots = [k for k in SLOT_KEYS if k in {_clean(s) for s in t.slots or []}]
        key = f"{fold(ask)}|{fold(question)[:80]}"
        if any(x["key"] == key for x in state.to_check):
            continue
        if len(state.to_check) >= MAX_TO_CHECK:
            reject("to_check", uc or qid, f"more than {MAX_TO_CHECK} items to check")
            continue
        state.to_check.append({"key": key, "use_case": uc, "question_id": qid, "slots": slots, "question": question,
                               "ask": ask, "evidence": _clean(t.evidence)[:200], "read": read, "at": time.time()})
        defer(state, uc, qid, slots, ask)
        ok("to_check", uc or qid, f"{ask}: {question}"[:120])


def defer(state, use_case: str | None, qid: str | None, slots: list[str], ask: str) -> None:
    for q in state.questions:
        if not ((qid and q.id == qid) or (use_case and q.use_case == use_case)):
            continue
        for e in q.all_expect():
            if q.slot_map.get(e) in slots and e not in q.covered:
                q.deferred.setdefault(e, ask)


def for_question(state, qid: str | None = None) -> list[dict]:
    """The "to check with" lines to show under question `qid` (default: the one in focus): the ones
    recorded on that question or its use case, newest first, at most two."""
    q = state.get(qid or state.focus_id)
    if q is None:
        return []
    mine = [t for t in state.to_check if t.get("question_id") == q.id or (q.use_case and t.get("use_case") == q.use_case)]
    return [{"ask": t["ask"], "question": t["question"]} for t in reversed(mine)][:2]


def view(state, lang: str | None = None) -> list[dict]:
    """Every item to check, for the console and the plan: {ask, question, use_case (title), evidence}."""
    out = []
    for t in state.to_check:
        u = state.get_use_case(t.get("use_case"))
        out.append({"ask": t["ask"], "question": t["question"], "evidence": t.get("evidence", ""),
                    "use_case": u.title(lang or state.language) if u else ""})
    return out


def by_person(state, lang: str | None = None) -> list[tuple[str, list[dict]]]:
    """The items to check grouped by who to ask, in the order they came up."""
    groups: dict[str, tuple[str, list[dict]]] = {}
    for item in view(state, lang):
        groups.setdefault(fold(item["ask"]), (item["ask"], []))[1].append(item)
    return list(groups.values())
