"""Potential ambassadors: people inside the client company who could carry a first pilot.

A pilot spreads through a respected colleague, not through the consultant or the CEO. During the
interview the engine notices when a use case maps to a named person or role (the CEO puts them on
that workflow: the use case's `actors` slot, or its answers, names someone of the org map) and the
interview screen suggests asking "Could <person> be keen to try this kind of tool?". The CEO's
answer, and what he says when asked who tries new tools first and who the others listen to (a
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
import unicodedata
from datetime import datetime
from pathlib import Path

from . import fsutil

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


def _clean(v) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()[:MAX_LEN]


# --- live detection (duck-typed on state.SessionState: no import cycle) --------------------------------


def _people(state) -> list[dict]:
    out = []
    for r in state.maps.get("org", []):
        person, role = _clean(r.fields.get("person")), _clean(r.fields.get("role"))
        if person or role:
            out.append({"id": r.id, "person": person, "role": role, "label": person or role,
                        "signal": _clean(r.fields.get("adoption_signal")), "evidence": _clean(r.evidence)})
    return out


def links(state) -> list[dict]:
    """(person, use case) pairs the CEO's words establish: the use case's actors slot, or an answer
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
                quote = _clean(actors.evidence or actors.value)
            elif any(_mentions(x, answers) for x in labels):
                quote = p["evidence"]
            else:
                continue
            found = True
            out.append({"person": p["label"], "role": p["role"] if p["person"] else "", "use_case": u.id,
                        "title": u.title(lang), "quote": quote})
        if not found and actors is not None and actors.value.strip():
            value = _clean(actors.value)
            if len(value.split()) <= _SHORT_ACTORS_WORDS and not re.search(r"[,;/+&]| et | and ", value):
                out.append({"person": value, "role": "", "use_case": u.id, "title": u.title(lang),
                            "quote": _clean(actors.evidence or actors.value)})
    return out


def prompt_for(state, qid: str | None = None) -> dict | None:
    """The ambassador question to suggest under question `qid` (default: the one in focus), or None:
    the first person its use case maps to who was not suggested under another question.
    {person, use_case (title)}."""
    q = state.get(qid or state.focus_id)
    if q is None or not q.use_case or q.status == "removed":
        return None
    asked_elsewhere = {_fold(p) for k, p in state.ambassador_prompted.items() if k != q.id}
    already = state.ambassador_prompted.get(q.id)
    for link in links(state):
        if link["use_case"] != q.use_case:
            continue
        if already and _fold(link["person"]) != _fold(already):
            continue
        if _fold(link["person"]) in asked_elsewhere:
            continue
        return {"person": link["person"], "use_case": link["title"]}
    return None


def for_use_case(state, use_case: str) -> dict | None:
    """The person to propose as the ambassador of a use case: one the interview put on that work,
    preferring one the CEO described as keen (adoption_signal, quoted). {person, role, quote} or None."""
    linked = [x for x in links(state) if x["use_case"] == use_case]
    signals = {person_id(p["label"]): p["signal"] for p in _people(state) if p["signal"]}
    for x in linked:
        if signals.get(person_id(x["person"])):
            return {"person": x["person"], "role": x["role"], "quote": signals[person_id(x["person"])]}
    for x in linked:
        if x["quote"]:
            return {"person": x["person"], "role": x["role"], "quote": x["quote"]}
    return None


def candidates(state) -> list[dict]:
    """The potential ambassadors heard so far, one per person: a quote saying they would be keen,
    try new tools first or are listened to (adoption_signal) wins over a use-case link (they do
    the work). {id, person, role, use_case, quote}."""
    by_id: dict[str, dict] = {}
    for p in _people(state):
        if p["signal"]:
            pid = person_id(p["label"])
            by_id[pid] = {"id": pid, "person": p["label"], "role": p["role"] if p["person"] else "",
                          "use_case": "", "quote": p["signal"]}
    for link in links(state):
        pid = person_id(link["person"])
        if not pid:
            continue
        if pid in by_id:
            by_id[pid]["use_case"] = by_id[pid]["use_case"] or link["title"]
            continue
        by_id[pid] = {"id": pid, "person": link["person"], "role": link["role"], "use_case": link["title"],
                      "quote": link["quote"]}
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
        entry = {"id": pid, "person": _clean(f.get("person")), "role": _clean(f.get("role")),
                 "use_case": _clean(f.get("use_case")), "quote": _clean(f.get("quote")), "label": "potential",
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
            person = _clean(e.get("person"))
            eid = str(e.get("id") or "")
            if not person:
                cleared.add(eid)
                continue
            fields = {"person": person, "role": _clean(e.get("role")), "use_case": _clean(e.get("use_case")),
                      "quote": _clean(e.get("quote"))}
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
