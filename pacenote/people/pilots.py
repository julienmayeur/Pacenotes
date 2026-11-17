"""Potential pilots (internally "ambassadors"): people inside the client company who could carry the
first use case. Nobody knows in advance how many: none, a few, or a whole team keen to try. They come
only from what the interviewees say, as named people or as a group ("the whole sales team is up for it"),
never with invented names. The leader in the meeting always sponsors the first wave (pilots.py): an
indirect sponsor question finds out what they would do themself, by trying it first on their own work.

A pilot spreads through a respected colleague, not through the consultant or the management. During the
interview the engine notices when a use case maps to a named person or role (the interviewee puts them on
that workflow: the use case's `actors` slot, or its answers, names someone of the org map), and the
live sync keeps a light read of the interviewee (role, stance on new tools, worries, pride in the
team, sensitivities, tone, rapport) and writes, per workflow, an indirect people question phrased
for that person ("when the team picks up a new Excel trick, does it come from Claire?"), never
head-on, never as an assessment, softer in a sector that has not taken the turn. The interview
screen shows it under the follow-up once the workflow was walked through (the reason, the
ambassador angle, stays in the interviewer console). The interviewee's answer, and what they say when asked who tries new tools first and who the others listen to (a
signal-carriers core question), land in the org map's `adoption_signal`, quoted.

Only a light label is kept per person: name or role, the use case, the supporting quote. It is
stored locally in clients/<slug>/ambassadors.json (clients/ is git-ignored, never committed) and
is editable in the admin, with the same rules as the client sheet and the profile cards: the
engine fills, a hand edit wins (the entry becomes manual and is never overwritten), and an entry
the interviewer removed never comes back.
"""

from __future__ import annotations

import json
import re
import threading
import time
import unicodedata
from datetime import datetime
from pathlib import Path

from pacenote.shared import fsutil
from pacenote.interview import phases as P
from pacenote.people import sponsor as pilots_lib

FILE = "ambassadors.json"
MAX_LEN = 300
MAX_ENTRIES = 40
_SHORT_ACTORS_WORDS = 4  # an actors value this short is itself a person or a role ("l'assistante ADV")


def _fold(text: str) -> str:
    s = unicodedata.normalize("NFKD", str(text or "").lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def person_id(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", _fold(label)).strip("-")[:48]


def _mentions(label: str, text: str) -> bool:
    """`label` (a name or a role) appears in `text` as whole words, accents and case ignored."""
    lab, txt = _fold(label).strip(), _fold(text)
    if len(lab) < 3 or not txt:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(lab)}(?![a-z0-9])", txt) is not None


def clean(v) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()[:MAX_LEN]


# --- live detection (duck-typed on state.SessionState: no import cycle) --------------------------------


def _people(state) -> list[dict]:
    out = []
    for r in state.maps.get("org", []):
        person, role = clean(r.fields.get("person")), clean(r.fields.get("role"))
        if person or role:
            out.append({"id": r.id, "person": person, "role": role, "label": person or role,
                        "signal": clean(r.fields.get("adoption_signal")), "evidence": clean(r.evidence)})
    return out


def links(state) -> list[dict]:
    """(person, use case) pairs the interviewee's words establish: the use case's actors slot, or an answer
    to one of its questions, names a person (or role) of the org map. Without a match, a short
    actors value is itself the person or role. Each: {person, role, use_case, title, quote}."""
    people = _people(state)
    lang = state.language
    out: list[dict] = []
    for u in state.use_cases:
        actors = u.slots.get("actors")
        actor_text = f"{actors.value} {actors.evidence}" if actors is not None else ""
        answers = " ".join(q.answer for q in state.questions if q.use_case == u.id and q.answer)
        found = False
        for p in people:
            labels = [x for x in (p["person"], p["role"]) if x]
            if any(_mentions(x, actor_text) for x in labels):
                quote = clean(actors.evidence or actors.value)
            elif any(_mentions(x, answers) for x in labels):
                quote = p["evidence"]
            else:
                continue
            found = True
            out.append({"person": p["label"], "role": p["role"] if p["person"] else "", "use_case": u.id,
                        "title": u.title(lang), "quote": quote})
        if not found and actors is not None and actors.value.strip():
            value = clean(actors.value)
            if len(value.split()) <= _SHORT_ACTORS_WORDS and not re.search(r"[,;/+&]| et | and ", value):
                out.append({"person": value, "role": "", "use_case": u.id, "title": u.title(lang),
                            "quote": clean(actors.evidence or actors.value)})
    return out


# --- the people question on screen: indirect, phrased for this interviewee, well timed -------------------

TONES = ("explorer", "pragmatic", "urgent")
WORKFLOW_SLOTS = ("trigger", "actors", "processing", "distribution")  # the workflow rung (contract.SLOT_RUNG)
MAX_PROBES = 12

# Never in a people question: AI vocabulary (the questions' wording rule) and anything that sounds like
# an assessment of a person or names the hidden purpose.
PROBE_BANNED = [
    re.compile(r"\b(IA|AI)\b"),  # case-sensitive: "j'ai", "mai" are fine
    *(re.compile(p, re.I) for p in (
        r"intelligence artificielle", r"artificial intelligence", r"\bagents?\b", r"automat", r"\bLLMs?\b",
        r"chat ?gpt", r"\bGPT", r"\b(chat)?bots?\b", r"\brobots?\b", r"algorithm", r"machine learning",
        r"\bprompts?\b", r"\bcopilot", r"ambassad", r"champion", r"r[ée]sist", r"r[ée]ticen", r"reluctan",
        r"[ée]valu", r"assess", r"\bscore", r"\bjuge[rz]?\b", r"\bjudge")),
]


def probe_problem(text: str) -> str:
    """The first word that makes `text` unfit as a people question ("" when it is fine)."""
    for p in PROBE_BANNED:
        if m := p.search(text or ""):
            return m.group(0)
    return ""


def workflow_discussed(state, use_case: str) -> bool:
    """The workflow was walked through: one of its workflow-rung slots (trigger, actors, processing,
    distribution) holds what the interviewee said."""
    u = state.get_use_case(use_case)
    return u is not None and any(u.filled(k) for k in WORKFLOW_SLOTS)


def tone(state) -> str:
    """The sector's pace sets how direct a people question may be: explorer (laggard sector) = softest,
    urgent (strong adoption) = more direct. No pre-study: pragmatic."""
    return state.posture if state.posture in TONES else "pragmatic"


def _rapport_ok(state) -> bool:
    """In a laggard sector the question waits for an established rapport; elsewhere the model's timing
    (and the workflow guard) decides."""
    return tone(state) != "explorer" or state.interviewee_read.get("rapport") == "established"


def _suggestion(person: str, title: str, question: str, reason: str, source: str) -> dict:
    return {"person": person, "use_case": title, "question": question, "reason": reason, "source": source}


def keen(state) -> list[dict]:
    """The people the interviewee described as keen (an adoption_signal quote in the org map)."""
    return [p for p in _people(state) if p["signal"]]


def sponsor_needed(state) -> bool:
    """A leader is in the meeting (pilots.can_sponsor): they sponsor the first wave whatever the pilots,
    and what they would do themself is still to find out (the sponsor question is not closed)."""
    return pilots_lib.can_sponsor(state) and state.sponsor_probe.get("timing") != "done"


FIRST_TO_TRY = "core-first-to-try"  # the core question: who tried the last new software first


def _sponsor_template_ready(state, q) -> bool:
    """The template sponsor question waits for the deep dives: the workflows were told, so the leader's own
    work can be asked about in context."""
    return P.PHASE_INDEX.get(q.phase, 0) >= P.PHASE_INDEX[P.DEEP_DIVES]


def _sponsor_person(state) -> str:
    lead = pilots_lib.sponsor(state)
    return ((lead or {}).get("role") or (lead or {}).get("name") or "").strip()


def _sponsor_for(state, q) -> dict | None:
    """The sponsor question under question `q`, or None: once (back on the same question, it shows
    again), while a leader attends and their own part is not known, and in a laggard sector only once
    rapport is established."""
    if not sponsor_needed(state) or not _rapport_ok(state):
        return None
    sp = state.sponsor_probe
    shown_on = sp.get("shown_on")
    if (shown_on and shown_on != q.id) or sp.get("timing") == "done":
        return None
    if shown_on is None and state.ambassador_prompted.get(q.id) is not None:
        return None  # another people question already showed under this one
    person = _sponsor_person(state)
    if sp.get("question"):
        if shown_on is None and sp.get("timing") != "now":
            return None
        return _suggestion(person, "", sp["question"], sp.get("reason", ""), "model") | {"kind": "sponsor"}
    if shown_on is None and not _sponsor_template_ready(state, q):
        return None
    question = state.tr()(f"focus.sponsorProbe.{tone(state)}")
    return _suggestion(person, "", question, "", "template") | {"kind": "sponsor"}


def mark_shown(state, qid: str, use_case: str | None, suggested: dict) -> None:
    """The suggestion was on screen under question `qid` when the focus left it: it is not suggested
    again under another question."""
    state.ambassador_prompted[qid] = suggested.get("person", "")
    if suggested.get("kind") == "sponsor":
        state.sponsor_probe = {**state.sponsor_probe, "shown_on": qid, "asked": True}
    else:
        mark_asked(state, use_case)


def prompt_for(state, qid: str | None = None, lang: str | None = None) -> dict | None:
    """The people question to suggest under question `qid` (default: the one in focus), or None:
    {person, use_case (title), question (to say, interview language), reason (interviewer only; ""
    for a template: the screen shows its own), source (model | template)}.

    Only on a question about a use case whose workflow was walked through, and in a laggard sector
    only once rapport is established. The model's probe for that use case wins (when it says "now";
    "later" and "done" hold it back); without one (no model, or not yet), an indirect template in the
    tone of the sector. Once per person: a person already suggested under another question is not
    suggested again; back on the same question, the same suggestion shows. `lang`: the language of
    the use case title (default: the interview language). When no workflow question applies, the
    sponsor question (_sponsor_for, kind "sponsor"), while a leader attends and their part is unknown."""
    q = state.get(qid or state.focus_id)
    if q is None or q.status == "removed":
        return None
    return _workflow_probe(state, q, lang) or _sponsor_for(state, q)


def _workflow_probe(state, q, lang: str | None) -> dict | None:
    if not q.use_case or not workflow_discussed(state, q.use_case) or not _rapport_ok(state):
        return None
    uc = state.get_use_case(q.use_case)
    title = uc.title(lang or state.language)
    already = state.ambassador_prompted.get(q.id)
    asked_elsewhere = {_fold(p) for k, p in state.ambassador_prompted.items() if k != q.id and p}
    probe = state.ambassador_probes.get(q.use_case)
    if probe is not None:
        same = already is not None and _fold(probe.get("person")) == _fold(already)
        if not probe.get("question") or probe.get("timing") == "done":
            return None
        if not same and (already is not None or probe.get("timing") != "now" or probe.get("asked")
                         or _fold(probe.get("person")) in asked_elsewhere):
            return None
        return _suggestion(probe.get("person", ""), title, probe["question"], probe.get("reason", ""), "model")
    for link in links(state):
        if link["use_case"] != q.use_case:
            continue
        if already is not None and _fold(link["person"]) != _fold(already):
            continue
        if _fold(link["person"]) in asked_elsewhere:
            continue
        question = state.tr()(f"focus.probe.{tone(state)}", person=link["person"])
        return _suggestion(link["person"], title, question, "", "template")
    return None


def mark_asked(state, use_case: str | None) -> None:
    """The suggestion under a question of `use_case` was on screen when the focus left it."""
    if use_case and (probe := state.ambassador_probes.get(use_case)) is not None:
        probe["asked"] = True


def apply_read(state, r, read: int, ok) -> None:
    """Merge the running read of the interviewee: a field the model leaves empty (or unknown) keeps
    its value; rapport only grows. A read written from an older slice than the stored one is ignored."""
    if r is None or read < state.interviewee_read_at:
        return
    new = r.model_dump() if hasattr(r, "model_dump") else dict(r)
    old = dict(state.interviewee_read)
    merged = dict(old)
    for k, v in new.items():
        v = clean(v)
        if k == "rapport":
            merged[k] = "established" if "established" in (v, old.get(k)) else v or "building"
        elif k == "ai_stance":
            merged[k] = v if v and v != "unknown" else old.get(k) or "unknown"
        elif v:
            merged[k] = v
    if merged != old:
        state.interviewee_read, state.interviewee_read_at = merged, read
        ok("read", None, f"{merged.get('rapport', '')}; {merged.get('approach', '')}"[:120])


def apply_probes(state, probes, read: int, ok, reject) -> None:
    """Merge the model's people questions, one per use case. Rejected: an unknown use case, an empty
    question, wording that sounds like an assessment or uses AI vocabulary (probe_problem), a probe
    read from an older slice than the stored one. A probe already asked is never reworded; "done"
    closes it (no template either)."""
    for p in probes or []:
        uc = clean(p.use_case)
        if state.get_use_case(uc) is None:
            reject("probe", uc, f"unknown use case {uc!r}")
            continue
        old = state.ambassador_probes.get(uc)
        if old is not None and old.get("read", 0) > read:
            reject("stale", uc, f"read at line {read}, kept the probe read at line {old['read']}")
            continue
        if p.timing == "done":
            if old is None or old.get("timing") != "done":
                state.ambassador_probes[uc] = {**(old or {"person": clean(p.person), "question": "", "reason": ""}),
                                               "timing": "done", "read": read, "at": time.time(),
                                               "asked": True if old is None else old.get("asked", False)}
                ok("probe", uc, "done")
            continue
        question = clean(p.question)
        if not question:
            reject("probe", uc, "no question")
            continue
        if bad := probe_problem(question):
            reject("probe", uc, f"wording: {bad!r} (indirect, never an assessment, no AI vocabulary)")
            continue
        if old is not None and old.get("asked"):
            reject("probe", uc, "already asked")
            continue
        if old is None and len(state.ambassador_probes) >= MAX_PROBES:
            reject("probe", uc, f"more than {MAX_PROBES} people questions")
            continue
        new = {"person": clean(p.person), "question": question, "reason": clean(p.reason), "timing": p.timing,
               "read": read, "at": time.time(), "asked": False}
        if old is not None and all(old.get(k) == new[k] for k in ("person", "question", "reason", "timing")):
            continue
        state.ambassador_probes[uc] = new
        ok("probe", uc, f"{p.timing}: {question}"[:120])


def apply_sponsor(state, p, read: int, ok, reject) -> None:
    """Merge the model's sponsor question. Rejected: no leader in the meeting (operators only),
    an empty question, wording that sounds like an assessment or uses AI vocabulary, a question read
    from an older slice, a question already asked (never reworded). "done" closes it."""
    if p is None:
        return
    old = state.sponsor_probe
    if old.get("read", 0) > read:
        reject("stale", "sponsor", f"read at line {read}, kept the sponsor question read at line {old['read']}")
        return
    if not pilots_lib.can_sponsor(state):
        reject("sponsor", None, "no leader in the meeting: no sponsor question")
        return
    if p.timing == "done":
        if old.get("timing") != "done":
            state.sponsor_probe = {**old, "timing": "done", "read": read, "at": time.time()}
            ok("sponsor", None, "done")
        return
    question = clean(p.question)
    if not question:
        reject("sponsor", None, "no question")
        return
    if bad := probe_problem(question):
        reject("sponsor", None, f"wording: {bad!r} (indirect, never an assessment, no AI vocabulary)")
        return
    if old.get("asked"):
        reject("sponsor", None, "already asked")
        return
    new = {"question": question, "reason": clean(p.reason), "timing": p.timing}
    if all(old.get(k) == v for k, v in new.items()):
        return
    state.sponsor_probe = {**old, **new, "read": read, "at": time.time()}
    ok("sponsor", None, f"{p.timing}: {question}"[:120])


def probes_view(state, lang: str | None = None) -> list[dict]:
    """The console's people questions: per workflow, the question, the reason, whether it is held for
    later or was asked (closed ones left out)."""
    out = []
    for uc, p in state.ambassador_probes.items():
        if p.get("timing") == "done" or not p.get("question"):
            continue
        u = state.get_use_case(uc)
        out.append({"use_case": u.title(lang or state.language) if u else uc, "person": p.get("person", ""),
                    "question": p["question"], "reason": p.get("reason", ""), "timing": p.get("timing", ""),
                    "asked": bool(p.get("asked"))})
    sp = state.sponsor_probe
    if sp.get("question") and sp.get("timing") != "done" and sponsor_needed(state):
        out.append({"use_case": "", "kind": "sponsor", "person": _sponsor_person(state),
                    "question": sp["question"], "reason": sp.get("reason", ""), "timing": sp.get("timing", ""),
                    "asked": bool(sp.get("asked"))})
    return out


def read_view(state) -> dict | None:
    """The console's one line on the interviewee: tone and approach (None before anything shows)."""
    r = state.interviewee_read
    if not (r.get("tone") or r.get("approach")):
        return None
    return {"tone": r.get("tone", ""), "approach": r.get("approach", ""), "rapport": r.get("rapport", "building")}


def for_use_case(state, use_case: str) -> dict | None:
    """The person to propose as the ambassador of a use case: one the interview put on that work,
    preferring one the interviewee described as keen (adoption_signal, quoted). {person, role, quote} or None."""
    linked = [x for x in links(state) if x["use_case"] == use_case]
    signals = {person_id(p["label"]): p["signal"] for p in _people(state) if p["signal"]}
    for x in linked:
        if signals.get(person_id(x["person"])):
            return {"person": x["person"], "role": x["role"], "quote": signals[person_id(x["person"])]}
    for x in linked:
        if x["quote"]:
            return {"person": x["person"], "role": x["role"], "quote": x["quote"]}
    return None


MAX_PILOTS = 6

# a label that names a group of people, not one person ("toute l'équipe ventes", "the sales team"); a
# title that leads a team ("chef d'équipe", "team lead") is one person
_ALL = re.compile(r"\b(tous|toutes|tout le monde|everyone|everybody|the whole|the entire|all the|l ensemble)\b")
_TEAM = re.compile(r"\b(equipe|equipes|team|teams|staff|personnel|collaborateurs|salaries|"
                   r"les (commerciaux|vendeurs|comptables|techniciens|assistantes|assistants|jeunes|nouveaux))\b")
_ONE = re.compile(r"\b(chef|cheffe|responsable|head|lead|leader|manager|directeur|directrice|director)\b")


def is_group(label: str) -> bool:
    """`label` names a group ("toute l'équipe ventes", "the sales team"), not one person ("chef d'équipe")."""
    f = re.sub(r"[^a-z0-9]+", " ", _fold(label))
    return bool(_ALL.search(f)) or (bool(_TEAM.search(f)) and not _ONE.search(f))


def pilots_for_plan(state, lighthouse: str) -> list[dict]:
    """The pilots the interview named, for the plan's first wave: everyone the interviewee described as
    keen (adoption_signal, quoted), people or groups, those on the lighthouse's work first; when nobody was
    described as keen, the person the interview put on the lighthouse's work, quoted.
    [{person, evidence, group}]."""
    on_light = {person_id(x["person"]) for x in links(state) if x["use_case"] == lighthouse}
    found = sorted(keen(state), key=lambda p: person_id(p["label"]) not in on_light)
    if found:
        return [{"person": p["label"], "evidence": p["signal"], "group": is_group(p["label"])}
                for p in found][:MAX_PILOTS]
    one = for_use_case(state, lighthouse) if lighthouse else None
    return [{"person": one["person"], "evidence": one["quote"], "group": is_group(one["person"])}] if one else []


def candidates(state) -> list[dict]:
    """The potential ambassadors heard so far, one per person: a quote saying they would be keen,
    try new tools first or are listened to (adoption_signal) wins over a use-case link (they do
    the work). {id, person, role, use_case, quote, keen}."""
    by_id: dict[str, dict] = {}
    for p in _people(state):
        if p["signal"]:
            pid = person_id(p["label"])
            by_id[pid] = {"id": pid, "person": p["label"], "role": p["role"] if p["person"] else "",
                          "use_case": "", "quote": p["signal"], "keen": True}
    for link in links(state):
        pid = person_id(link["person"])
        if not pid:
            continue
        if pid in by_id:
            by_id[pid]["use_case"] = by_id[pid]["use_case"] or link["title"]
            continue
        by_id[pid] = {"id": pid, "person": link["person"], "role": link["role"], "use_case": link["title"],
                      "quote": link["quote"], "keen": False}
    return [e for e in by_id.values() if e["quote"]][:MAX_ENTRIES]


# --- the local file: clients/<slug>/ambassadors.json --------------------------------------------------

_LOCKS: dict[Path, threading.Lock] = {}
_GUARD = threading.Lock()


def _lock(path: Path) -> threading.Lock:
    with _GUARD:
        return _LOCKS.setdefault(Path(path).resolve(), threading.Lock())


def path_of(client_path: Path) -> Path:
    return Path(client_path) / FILE


def load(client_path: Path) -> dict:
    p = path_of(client_path)
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, json.JSONDecodeError):
        data = {}
    return {"entries": [e for e in data.get("entries") or [] if isinstance(e, dict) and e.get("id")],
            "removed": [str(x) for x in data.get("removed") or []]}


def _write(client_path: Path, data: dict) -> dict:
    fsutil.atomic_write_text(path_of(client_path), json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return data


def merge(old: dict, found: list[dict], session: str = "") -> dict:
    """The engine's entries: a person found again is refreshed unless the interviewer edited it
    (manual); a person the interviewer removed is not brought back; earlier agent entries stay."""
    entries = [dict(e) for e in old.get("entries") or []]
    removed = set(old.get("removed") or [])
    index = {e["id"]: i for i, e in enumerate(entries)}
    stamp = datetime.now().isoformat(timespec="seconds")
    for f in found:
        pid = person_id(f.get("id") or f.get("person") or "")
        if not pid or pid in removed:
            continue
        entry = {"id": pid, "person": clean(f.get("person")), "role": clean(f.get("role")),
                 "use_case": clean(f.get("use_case")), "quote": clean(f.get("quote")), "label": "potential",
                 "by": "agent", "session": session, "updated_at": stamp}
        if pid in index:
            if entries[index[pid]].get("by") == "manual":
                continue
            entries[index[pid]] = entry
        elif len(entries) < MAX_ENTRIES:
            index[pid] = len(entries)
            entries.append(entry)
    return {"entries": entries, "removed": sorted(removed)}


def record(client_path: Path, state, session: str = "") -> dict:
    """Merge the potential ambassadors this interview surfaced into the client's file."""
    found = candidates(state)
    with _lock(path_of(client_path)):
        old = load(client_path)
        if not found and not path_of(client_path).exists():
            return old
        return _write(client_path, merge(old, found, session))


def save_edit(client_path: Path, entries: list[dict], removed: list[str]) -> dict:
    """The interviewer edited the list in the admin: a changed or new entry becomes manual; an entry
    removed with × is remembered so the engine never brings it back; an entry whose person was
    cleared is dropped but not remembered (the next interview may find it again)."""
    with _lock(path_of(client_path)):
        old = load(client_path)
        before = {e["id"]: e for e in old["entries"]}
        out, cleared = [], set()
        for e in entries:
            if not isinstance(e, dict):
                raise ValueError("an entry must be an object")
            person = clean(e.get("person"))
            eid = str(e.get("id") or "")
            if not person:
                cleared.add(eid)
                continue
            fields = {"person": person, "role": clean(e.get("role")), "use_case": clean(e.get("use_case")),
                      "quote": clean(e.get("quote"))}
            prev = before.get(eid)
            if prev is not None and all(prev.get(k, "") == v for k, v in fields.items()):
                out.append(prev)
                continue
            new_id = eid if prev is not None else person_id(person)
            if not new_id or any(x["id"] == new_id for x in out):
                raise ValueError(f"duplicate person {person!r}")
            out.append({"id": new_id, **fields, "label": "potential", "by": "manual",
                        "session": prev.get("session", "") if prev else "",
                        "updated_at": datetime.now().isoformat(timespec="seconds")})
        kept = {e["id"] for e in out}
        gone = ({r for r in removed if r} | (set(before) - kept)) - (cleared - set(removed))
        removed_ids = set(old["removed"]) | {g for g in gone if g in before or g in removed}
        return _write(client_path, {"entries": out[:MAX_ENTRIES], "removed": sorted(removed_ids - kept)})
