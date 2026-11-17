"""The views heard in the meeting: distinct positions on AI and on the change, grouped by statement, not by person.

Several people may be in the room and they may not agree (one keen, one worried about reliability, one
against it). The transcript has no speaker labels, so the copilot cannot tell who said a line, and a stance
per person would be guesswork. Instead the live sync groups the statements themselves into viewpoints: keen,
open, cautious, opposed, or practical (a concrete condition: reliability, data, who keeps the decision), each
with short verbatim quotes and the workflow or topic it bears on.

Attribution. A quote names an attendee only when the statement itself names who speaks ("en tant que
directeur commercial, je...") and the words that name them are in the quote (named_by). Otherwise it has no
speaker: never a guess.

Where it lives. Interviewer-side only: the console shows it, the session data keeps it (sessions/, never in
git). The plan writer gets the positions and topics, never a quote or a speaker, so the plan can list the
points to address in wave 2 ("the reliability of the extraction"), with no name and no label on anyone.

After the meeting (optional). When the consultant pastes a final transcript whose lines carry speaker
labels ("Claire: ...", "Speaker 2: ...", the format Wispr Flow's meeting notetaker copies), refine() maps each
quote to the voice that said it, by matching the text. Labels the transcript does not give are never
invented: a plain transcript leaves the speakers "unavailable". Duck-typed on the session state.
"""

from __future__ import annotations

import re
import time
from difflib import SequenceMatcher

from pacenote.people import attendees as attendees_lib
from pacenote.people.personas import fold

KINDS = ("keen", "open", "cautious", "opposed", "practical")
CONCERNS = ("cautious", "opposed", "practical")  # the kinds that raise a point to address in wave 2
MAX_QUOTES = 4
MAX_VIEWPOINTS = 8


def _clean(text, n: int = 200) -> str:
    return " ".join(str(text or "").split())[:n]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", fold(text or "")).strip("-")[:48]


# --- the live sync: merge the model's viewpoints -----------------------------------------------------------


def _speaker(state, q, reject) -> str:
    """The attendee a quote names, or "": the quote must carry the words that name them (named_by)."""
    ref = _clean(q.speaker, 60)
    if not ref:
        return ""
    a = attendees_lib.find(state, ref)
    named_by = _clean(q.named_by, 120)
    if a is None or not named_by or fold(named_by) not in fold(q.text):
        reject("viewpoint", ref, "a speaker only when the statement itself names them")
        return ""
    return a["key"]


def apply(state, items, read: int, ok, reject) -> None:
    """Merge the model's viewpoints. A new one needs a position and a quote; an existing id gets its kind,
    position and topic updated and its new quotes added (verbatim, at most four, newest first). A read older
    than the one kept is ignored."""
    for v in items or []:
        vid = _slug(v.id) or _slug(v.position)
        if not vid:
            reject("viewpoint", v.id, "no id and no position")
            continue
        old = state.viewpoints.get(vid) or {}
        if old.get("read", 0) > read:
            reject("stale", vid, f"read at line {read}, kept the viewpoint read at line {old['read']}")
            continue
        quotes = list(old.get("quotes") or [])
        texts = {fold(x["text"]) for x in quotes}
        for q in v.quotes or []:
            text = _clean(q.text)
            if not text or fold(text) in texts:
                continue
            texts.add(fold(text))
            quotes.insert(0, {"text": text, "speaker": _speaker(state, q, reject), "read": read})
        position = _clean(v.position, 160) or old.get("position", "")
        if not quotes or not position:
            reject("viewpoint", vid, "a viewpoint needs a position and the words it comes from")
            continue
        if not old and len(state.viewpoints) >= MAX_VIEWPOINTS:
            reject("viewpoint", vid, f"at most {MAX_VIEWPOINTS} viewpoints: add to an existing one")
            continue
        uc = v.use_case if v.use_case and any(u.id == v.use_case for u in state.use_cases) else None
        state.viewpoints[vid] = {"kind": v.kind, "position": position, "topic": _clean(v.topic, 80),
                                 "use_case": uc or old.get("use_case"), "quotes": quotes[:MAX_QUOTES],
                                 "read": read, "at": time.time()}
        ok("viewpoint", vid, f"{v.kind}: {position}"[:120])


# --- views and prompts ---------------------------------------------------------------------------------------


def _who(state, key: str) -> str:
    a = attendees_lib.find(state, key) if key else None
    return attendees_lib.label(a) if a is not None else ""


def view(state, lang: str = "fr") -> dict:
    """The console's viewpoints: each with kind, position, topic and its quotes (with the speaker only when
    the statement named them, or when a labelled transcript mapped it after the meeting), and where the
    after-meeting speakers stand."""
    uc_titles = {u.id: u.title(lang) for u in state.use_cases}
    out = []
    for vid, v in state.viewpoints.items():
        quotes = []
        for q in v.get("quotes") or []:
            voice = q.get("voice") or {}
            quotes.append({"text": q["text"], "speaker": _who(state, q.get("speaker", "")),
                           "voice": voice.get("label", ""), "voice_who": _who(state, voice.get("attendee", ""))})
        out.append({"id": vid, "kind": v.get("kind", "open"), "position": v.get("position", ""),
                    "topic": v.get("topic") or uc_titles.get(v.get("use_case") or "", ""), "quotes": quotes})
    order = {k: i for i, k in enumerate(KINDS)}
    out.sort(key=lambda x: order.get(x["kind"], 9))
    return {"items": out, "speakers": dict(state.viewpoint_speakers or {})}


def snapshot(state) -> list[dict]:
    """The viewpoints as the sync model sees them, to keep ids stable and add quotes to the right one."""
    return [{"id": vid, "kind": v.get("kind"), "position": v.get("position", ""), "topic": v.get("topic", ""),
             "use_case": v.get("use_case"), "quotes": [q["text"] for q in (v.get("quotes") or [])[:2]]}
            for vid, v in state.viewpoints.items()]


def for_plan(state) -> list[dict]:
    """The viewpoints as the plan writer sees them: the position and the topic, never a quote or a speaker
    (the plan is shared with the client)."""
    return [{"kind": v.get("kind"), "position": v.get("position", ""), "topic": v.get("topic", "")}
            for v in state.viewpoints.values()]


def concerns(state) -> list[str]:
    """The points the cautious, opposed and practical views raise, as topics (the plan's fallback when the
    writer listed none): "position (topic)"."""
    out = []
    for v in state.viewpoints.values():
        if v.get("kind") in CONCERNS and v.get("position"):
            topic = (v.get("topic") or "").strip()
            out.append(f"{v['position']} ({topic})" if topic else v["position"])
    return out


# --- after the meeting: speakers from a transcript that has them ---------------------------------------------

# "Claire Martin: ...", "Speaker 2: ...", "You (Speaker 1): ...": a short label, then the words
_LABELLED = re.compile(r"^\s*(?:\[[\d:.,\s>-]+\]\s*)?(?P<label>[^\W\d][\w .'’()-]{0,38}?)\s*[:：]\s+(?P<text>\S.*)$")
_NOT_SPEAKERS = {"note", "notes", "summary", "resume", "action items", "next steps", "transcript", "date", "titre",
                 "title", "participants", "attendees", "sujet", "subject", "http", "https"}
MIN_MATCH = 0.72


def labelled_lines(text: str) -> list[tuple[str, str]]:
    """The (speaker label, words) lines of a transcript, when it has speaker labels: at least two lines, one
    label used twice, and most lines labelled. [] when the transcript is plain (the live backends and Wispr Flow dictation give
    no speakers)."""
    lines, current, rows = [], None, [r for r in (text or "").splitlines() if r.strip()]
    for raw in rows:
        m = _LABELLED.match(raw)
        if m and fold(m["label"]) not in _NOT_SPEAKERS and len(m["label"].split()) <= 5:
            current = m["label"].strip()
            lines.append([current, m["text"].strip()])
        elif raw.strip() and current is not None and lines:
            lines[-1][1] += " " + raw.strip()  # a turn continued on the next line
    labels = [lb for lb, _ in lines]
    if len(lines) < 2 or max((labels.count(lb) for lb in set(labels)), default=0) < 2 or len(lines) < len(rows) / 2:
        return []  # a few "Note: ..." lines in a plain transcript are not speakers
    return [(lb, " ".join(t.split())) for lb, t in lines]


def _score(quote: str, line: str) -> float:
    """How well a live quote (Apple speech, mishearings included) matches a line of the final transcript:
    the best window of the line, by characters."""
    q, s = fold(quote), fold(line)
    if not q or not s:
        return 0.0
    if q in s:
        return 1.0
    m = SequenceMatcher(None, q, s, autojunk=False)
    return sum(b.size for b in m.get_matching_blocks()) / len(q)


def refine(state, text: str, source: str = "transcript") -> dict:
    """Map each viewpoint quote to the voice that said it, from a final transcript with speaker labels.
    Without labels: the speakers are unavailable, and nothing is guessed. A label that is an attendee's name
    (or first name) maps to that attendee. Returns and keeps the status (state.viewpoint_speakers)."""
    lines = labelled_lines(text)
    for v in state.viewpoints.values():
        for q in v.get("quotes") or []:
            q.pop("voice", None)
    if not lines:
        state.viewpoint_speakers = {"status": "unavailable", "source": source, "labels": [], "mapped": 0,
                                    "at": time.time()}
        return state.viewpoint_speakers
    labels = sorted({lb for lb, _ in lines})
    who = {}
    for lb in labels:
        f = fold(lb)
        for a in attendees_lib.of(state):
            name = fold(a.get("name", ""))
            if name and (f == name or f == name.split()[0] or name in f):
                who[lb] = a["key"]
                break
    mapped = 0
    for v in state.viewpoints.values():
        for q in v.get("quotes") or []:
            best = max(lines, key=lambda x: _score(q["text"], x[1]))
            if _score(q["text"], best[1]) >= MIN_MATCH:
                q["voice"] = {"label": best[0], "attendee": who.get(best[0], "")}
                mapped += 1
    state.viewpoint_speakers = {"status": "mapped", "source": source, "labels": labels, "mapped": mapped,
                                "at": time.time()}
    return state.viewpoint_speakers
