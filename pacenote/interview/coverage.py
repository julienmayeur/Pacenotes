"""Coverage first: the interview goes around the whole company before it goes deep on one point.

People dig in to understand and do not feel the time pass: in the first real interview, one case took
the whole hour and most of the company was never discussed. The copilot watches the clock and the
coverage for the interviewer. The model labels; the code decides:

- **The areas** (core/areas.yaml, or the questionnaire's `areas:`) are what a 360° view covers: sales,
  customer service, after-sales, purchasing, logistics, marketing, finance, people, IT. Each has a title,
  a short label and one ready question.
- **The model labels each slice** it analyses (sync `topic`, contract.TopicLabel): the main area of the new
  lines, the specific point, whether it goes on with the previous point or only repeats it, and which areas
  were discussed or only mentioned.
- **The code keeps the coverage**: the seconds each area was the conversation's point (the time from one
  label to the next goes to the area that label names). An area is *covered* once it was the point for
  `COVERED` seconds, *touched* once discussed or mentioned, else *untouched*. The screen shows it quietly.
- **Drift is measured on the conversation, not on the screen.** The current point started when a label named
  another area (and not the same point); it survives question changes on screen, a failed sync and slices
  labelled "other": nothing but a new area resets it, so a missing analysis never silences the move-on.
  Before any area was named, a slice labelled "other" (greetings, how the interview works) moves the first
  point's start to its end; with no label at all, the first point runs from the interview's start.
  Each point gets an allowance: its share of the interview (the length over the number of areas, times
  `ALLOWANCE_FACTOR`), between `MIN_ALLOWANCE` and `MAX_ALLOWANCE`, so every area can be reached in the
  planned length (20 minutes, 9 areas: 133 s each); twice that once no area is left
  untouched (then depth is welcome). Time guarantees the move-on; a point that repeats itself (two
  slices in a row with nothing new) brings it earlier (`REPEAT_FACTOR`).
- **The move-on.** Once the point has had its allowance and an area is still untouched, the next one
  (the areas' order, untouched first, then touched but not covered, then once more an untouched area whose
  question went up unanswered) is queued as the next question with a transition line: the interviewer can
  say it as is. It goes through the queued-next slot (state.next_qid) and moves up at the next boundary
  (session.py), on its own clock: on screen `SHOW` seconds at least, then a sure pause, or a sentence end once
  it has been queued `timebox.GRACE` seconds; the transition line stays above the question `SAY_FOR` seconds.
  The interviewer can keep going instead (`x`: `EXTEND` more seconds on this point). No action means rotate. After a move-on
  (taken or set aside) the next one waits `COOLDOWN` seconds; none while the closing questions have the
  floor (timebox.py).

Duck-typed on state.SessionState (no import cycle). Every time is on the interview's clock (state.now()).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import yaml
from pydantic import BaseModel, Field

from pacenote.interview import timebox

if TYPE_CHECKING:  # pragma: no cover
    from pacenote.copilot.contract import TopicLabel
    from pacenote.interview.state import QuestionState, SessionState

CATALOG = Path(__file__).parent / "core" / "areas.yaml"
OTHER = "other"
THEME = "areas"  # the theme of the area questions (added to the session when the first one is queued)
QID_PREFIX = "area-"

COVERED = 60.0  # s as the conversation's point: the area is covered, not only touched
ALLOWANCE_FACTOR = 1.0  # a point's allowance: its share of the interview (length / areas) times this
MIN_ALLOWANCE = 120.0
MAX_ALLOWANCE = 300.0
DEPTH_FACTOR = 2.0  # no area left untouched: depth is welcome, the allowance doubles
REPEAT_FACTOR = 0.6  # the point repeats itself: the move-on comes at this share of the allowance
REPEATS = 2  # slices in a row the model found repeating before REPEAT_FACTOR applies
EXTEND = 180.0  # the interviewer keeps going (x): this much more on the current point
COOLDOWN = 90.0  # after a move-on (taken or set aside), the next one waits at least this long
SHOW = 10.0  # s the queued move-on stays on screen before it can move up (read the line, or keep going with x)
SAY_FOR = 45.0  # s the transition line stays above the area's question once it went up
GAP_CAP = 120.0  # at most this much time goes to one label (a failed sync never inflates an area)

Status = Literal["untouched", "touched", "covered"]


class Area(BaseModel):
    id: str
    title: dict[str, str] = Field(default_factory=dict)
    short: dict[str, str] = Field(default_factory=dict)
    texts: dict[str, str] = Field(default_factory=dict)  # the ready question, per language


class AreaCoverage(BaseModel):
    """The session's coverage (SessionState.cov)."""

    areas: list[Area] = Field(default_factory=list)
    seconds: dict[str, float] = Field(default_factory=dict)  # area -> seconds as the conversation's point
    seen: dict[str, str] = Field(default_factory=dict)  # area -> "mentioned" | "discussed"
    # the conversation's current point: area (None = not labelled yet), point, since (when it started),
    # at (the last label), repeats (slices in a row found repeating), extra (seconds the interviewer added),
    # proposed (areas already suggested while on this point)
    topic: dict = Field(default_factory=dict)
    move_on: dict = Field(default_factory=dict)  # the pending move-on: {from, to, qid, at}; {} = none
    last_move: float | None = None  # when the last move-on was taken or set aside (cooldown)
    asked: dict[str, float] = Field(default_factory=dict)  # area -> when its move-on question went up
    log: list[dict] = Field(default_factory=list)  # every label and move-on, for the replay and the plan
    labels: int = 0


# --- the catalog ------------------------------------------------------------------------------------


def _texts(v) -> dict[str, str]:
    if isinstance(v, str):
        return {"fr": v, "en": v}
    return {str(k): str(t).strip() for k, t in (v or {}).items() if t}


def _area(d: dict) -> Area:
    texts = {k: str(d[k]).strip() for k in d if k not in ("id", "title", "short") and isinstance(d[k], str)}
    title = _texts(d.get("title"))
    return Area(id=str(d["id"]), title=title, short=_texts(d.get("short")) or title, texts=texts)


@lru_cache(maxsize=4)
def load_catalog(path: Path = CATALOG) -> tuple[Area, ...]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return tuple(_area(d) for d in data.get("areas", []))


def resolve(spec: list | None, catalog: tuple[Area, ...] | None = None) -> list[Area]:
    """The interview's areas: the catalog, or the questionnaire's `areas:` (catalog ids and/or entries of
    their own with the same fields: id, title, short, one question per language)."""
    catalog = load_catalog() if catalog is None else catalog
    if not spec:
        return [a.model_copy(deep=True) for a in catalog]
    by_id = {a.id: a for a in catalog}
    out: list[Area] = []
    for item in spec:
        if isinstance(item, str):
            if item not in by_id:
                raise ValueError(f"areas: unknown area {item!r} (known: {', '.join(by_id)})")
            out.append(by_id[item].model_copy(deep=True))
        else:
            base = by_id.get(str(item.get("id")))
            own = _area(item)
            out.append(Area(id=own.id, title=own.title or (base.title if base else {}),
                            short=own.short or (base.short if base else {}), texts=own.texts or (base.texts if base else {})))
    return out


def _pick(texts: dict[str, str], lang: str) -> str:
    return texts.get(lang) or texts.get(lang.split("-")[0]) or texts.get("en") or texts.get("fr") or next(iter(texts.values()), "")


def get(state: SessionState, aid: str | None) -> Area | None:
    return next((a for a in state.cov.areas if a.id == aid), None)


def ids(state: SessionState) -> list[str]:
    return [a.id for a in state.cov.areas]


# --- coverage ---------------------------------------------------------------------------------------


def status(state: SessionState, aid: str) -> Status:
    cov = state.cov
    if cov.seconds.get(aid, 0.0) >= COVERED:
        return "covered"
    if aid in cov.seen or cov.seconds.get(aid, 0.0) > 0:
        return "touched"
    return "untouched"


def counts(state: SessionState) -> dict[str, int]:
    out = {"untouched": 0, "touched": 0, "covered": 0}
    for aid in ids(state):
        out[status(state, aid)] += 1
    return out


def start(state: SessionState, now: float | None = None) -> None:
    """The interview starts: the first point starts with it (not labelled yet)."""
    now = state.now() if now is None else now
    state.cov.topic = {"area": None, "point": "", "since": now, "at": now, "repeats": 0, "extra": 0.0, "proposed": []}


def apply_topic(state: SessionState, label: TopicLabel | None, now: float | None = None) -> None:
    """A sync's label for the slice it read. The slice's time goes to the area it names; a new area (not the
    same point) starts a new point; "other", an unknown id or no label change nothing but the clock."""
    cov = state.cov
    if not cov.areas or state.started_at is None:
        return
    now = state.now() if now is None else now
    if not cov.topic:
        start(state, state.started_at)
    t = cov.topic
    known = set(ids(state))
    area = label.area if label is not None and label.area in known else None
    target = area or t.get("area")
    gap = max(0.0, min(GAP_CAP, now - (t.get("at") or now)))
    if target:
        cov.seconds[target] = round(cov.seconds.get(target, 0.0) + gap, 1)
    t["at"] = now
    if label is None:
        return
    cov.labels += 1
    for aid in label.mentioned:
        if aid in known:
            cov.seen.setdefault(aid, "mentioned")
    for aid in label.discussed:
        if aid in known:
            cov.seen[aid] = "discussed"
    if area:
        cov.seen[area] = "discussed"
    entry = {"at": round(now - state.started_at, 1), "area": label.area, "point": label.point,
             "same_point": label.same_point, "repeating": label.repeating}
    if area is None and t.get("area") is None:
        # no area named yet (greetings, how the interview works): the first point has not started. With no
        # label at all (every sync failed), it runs from the interview's start
        t["since"] = now
    if area and t.get("area") not in (None, area) and not label.same_point:
        # a new point: its clock starts at the beginning of this slice (the previous label), or when this
        # area's move-on question went up during the slice (the conversation followed it from then)
        since = max(now - gap, min(now, cov.asked.get(area, -1e18)))
        cov.topic = {"area": area, "point": label.point, "since": since, "at": now,
                     "repeats": 1 if label.repeating else 0, "extra": 0.0, "proposed": []}
        entry["new_point"] = True
        mv = cov.move_on
        if mv and area != mv.get("from"):
            _resolve(state, "the conversation moved on by itself", drop=not _asked(state, mv.get("qid")))
    else:
        if area:
            t["area"] = area
        if label.point:
            t["point"] = label.point
        t["repeats"] = t.get("repeats", 0) + 1 if label.repeating else 0
    cov.log.append(entry)
    cov.log = cov.log[-400:]


# --- drift ------------------------------------------------------------------------------------------


def dwell(state: SessionState, now: float | None = None) -> float:
    """Seconds the conversation has been on its current point."""
    t = state.cov.topic
    if not t or t.get("since") is None:
        return 0.0
    return max(0.0, (state.now() if now is None else now) - t["since"])


def allowance(state: SessionState) -> float:
    """Seconds the current point gets before the move-on (the interviewer's extensions included)."""
    cov = state.cov
    n = max(1, len(cov.areas))
    base = min(MAX_ALLOWANCE, max(MIN_ALLOWANCE, timebox.total(state) / n * ALLOWANCE_FACTOR))
    cur = cov.topic.get("area")
    if not any(status(state, a) == "untouched" for a in ids(state) if a != cur):
        base *= DEPTH_FACTOR
    if cov.topic.get("repeats", 0) >= REPEATS:
        base = max(MIN_ALLOWANCE * REPEAT_FACTOR, base * REPEAT_FACTOR)
    return base + float(cov.topic.get("extra", 0.0))


def next_area(state: SessionState) -> str | None:
    """The area to move to: the first untouched one whose question was not asked yet, else the first touched
    but not covered one; else, once more, an area whose question went up but that is still untouched (shown is
    not covered). Never the current point's area or one already suggested on this point."""
    cov = state.cov
    cur = cov.topic.get("area")
    skip = {cur, *cov.topic.get("proposed", [])}
    asked = set(state.focus_history)
    cands = [a for a in ids(state) if a not in skip]
    fresh = [a for a in cands if QID_PREFIX + a not in asked]
    for pool, want in ((fresh, "untouched"), (fresh, "touched"), (cands, "untouched")):
        hit = next((a for a in pool if status(state, a) == want), None)
        if hit:
            return hit
    return None


def _closing(state: SessionState, now: float | None = None) -> bool:
    return state.phase == timebox.CLOSING or timebox.closing_due(state, now)


def due(state: SessionState, now: float | None = None) -> bool:
    """Time to suggest moving on: the point has had its allowance, an area is left to go to, no move-on is
    pending or just taken, and the closing questions do not have the floor."""
    cov = state.cov
    if not cov.areas or state.started_at is None or not cov.topic or cov.move_on or state.mode != "interview":
        return False
    now = state.now() if now is None else now
    if cov.last_move is not None and now - cov.last_move < COOLDOWN:
        return False
    if _closing(state, now) or dwell(state, now) < allowance(state):
        return False
    return next_area(state) is not None


def _question(state: SessionState, area: Area) -> QuestionState:
    """The area's question, in the current step (it never changes the step order); reused when it exists."""
    from pacenote.interview.state import QuestionState, ThemeState, _empty_ladder

    qid = QID_PREFIX + area.id
    q = state.get(qid)
    if THEME not in state.theme_ids():
        state.themes.append(ThemeState(id=THEME, titles={"fr": "Tour de l'entreprise", "en": "Company tour"},
                                       title_fr="Tour de l'entreprise", title_en="Company tour"))
    phase = state.phase or state.current_phase or "deep-dives"
    if q is None:
        fr = (state.source_language or state.language) == "fr"  # expect items are kept in the source language
        expect = _empty_ladder()
        expect.update({k: [v if fr else _EXPECT_EN[v]] for k, v in _EXPECT.items()})
        other = {"en": _EXPECT_EN} if fr else {"fr": {e: f for f, e in _EXPECT_EN.items()}}
        q = QuestionState(id=qid, theme=THEME, texts=dict(area.texts), text_fr=area.texts.get("fr", ""),
                          text_en=area.texts.get("en", ""), priority=1, origin="added", expect=expect,
                          expect_i18n=other, phase=phase)
        cur = state.get(state.focus_id)
        if cur is not None and cur in state.questions:
            state.questions.insert(state.questions.index(cur) + 1, q)
        else:
            state.questions.append(q)
    else:
        q.status, q.phase = "pending", phase
    return q


# the details an area question is dug for (source language fr; en for the interviewer screen)
_EXPECT = {"business": "ce qui coince ou prend du temps dans ce domaine",
           "workflow": "qui s'en occupe, sur un cas réel récent",
           "implementation": "le logiciel ou le fichier utilisé, par son nom"}
_EXPECT_EN = {_EXPECT["business"]: "what gets stuck or takes time in this area",
              _EXPECT["workflow"]: "who handles it, on a recent real case",
              _EXPECT["implementation"]: "the software or file used, by name"}


def raise_move_on(state: SessionState, now: float | None = None) -> str | None:
    """Queue the next area's question with a transition line. Returns the question id."""
    aid = next_area(state)
    area = get(state, aid)
    if area is None:
        return None
    now = state.now() if now is None else now
    cov = state.cov
    q = _question(state, area)
    cov.move_on = {"from": cov.topic.get("area"), "to": area.id, "qid": q.id, "at": now}
    cov.topic.setdefault("proposed", []).append(area.id)
    cov.log.append({"at": round(now - (state.started_at or now), 1), "move_on": area.id,
                    "from": cov.topic.get("area"), "dwell": round(dwell(state, now)), "allowance": round(allowance(state))})
    state.ask_next_qid = q.id
    return q.id


def tick(state: SessionState, now: float | None = None) -> bool:
    """Called on every poll of the interview: raise the move-on when it is due. True when it was raised."""
    mv = state.cov.move_on
    if mv and (q := state.get(mv.get("qid"))) is not None and not q.open:
        _resolve(state, "its question was closed", drop=False)
    if due(state, now):
        return raise_move_on(state, now) is not None
    return False


def pending_qid(state: SessionState) -> str | None:
    """The move-on question while it waits for its boundary (None once asked or set aside)."""
    mv = state.cov.move_on
    q = state.get(mv.get("qid")) if mv else None
    if q is None or not q.open or q.id == state.focus_id:
        return None
    return q.id


def _asked(state: SessionState, qid: str | None) -> bool:
    return bool(qid) and qid in state.focus_history


def _resolve(state: SessionState, outcome: str, drop: bool, now: float | None = None) -> None:
    cov = state.cov
    mv = cov.move_on
    if not mv:
        return
    now = state.now() if now is None else now
    if drop and (q := state.get(mv.get("qid"))) is not None and q.id != state.focus_id and q.id not in state.focus_history:
        state.questions.remove(q)  # never asked: it leaves the queue (it comes back if suggested again)
        if state.ask_next_qid == q.id:
            state.ask_next_qid = None
        state.next_questions = [i for i in state.next_questions if i != q.id]
    cov.log.append({"at": round(now - (state.started_at or now), 1), "resolved": mv.get("to"), "outcome": outcome})
    cov.move_on, cov.last_move = {}, now


def on_focus(state: SessionState) -> None:
    """The focus changed (state.advance): the move-on question went up, or another question did."""
    mv = state.cov.move_on
    if mv and state.focus_id == mv.get("qid"):
        state.cov.asked[mv["to"]] = state.now()
        _resolve(state, "asked", drop=False)


def extend(state: SessionState, now: float | None = None) -> bool:
    """The interviewer keeps going on this point (x): `EXTEND` more seconds, and a pending move-on is set
    aside (its question leaves the queue)."""
    cov = state.cov
    if not cov.areas or not cov.topic:
        return False
    now = state.now() if now is None else now
    cov.topic["extra"] = float(cov.topic.get("extra", 0.0)) + EXTEND
    if cov.move_on:
        _resolve(state, "the interviewer kept going", drop=True, now=now)
    else:
        cov.last_move = now
    cov.log.append({"at": round(now - (state.started_at or now), 1), "extended": cov.topic.get("area"), "by": EXTEND})
    return True


# --- what the screen and the model see -----------------------------------------------------------------


def view(state: SessionState, lang: str, tr=None, say: str | None = None, now: float | None = None) -> dict:
    """The quiet coverage strip (short labels in the screen language `lang`, untouched / touched / covered, the
    current point) and the pending move-on: its transition line, in the interview language `say` (it is said
    aloud; `tr` translates in that language)."""
    cov = state.cov
    if not cov.areas:
        return {}
    cur = cov.topic.get("area")
    areas = [{"id": a.id, "short": _pick(a.short, lang), "title": _pick(a.title, lang), "status": status(state, a.id),
              "current": a.id == cur} for a in cov.areas]
    mv = None
    if (qid := pending_qid(state)) is not None and (to := get(state, cov.move_on.get("to"))) is not None:
        say = say or state.language
        title = _pick(to.title, say)
        title = title[:1].lower() + title[1:] if say in ("fr", "en") else title  # mid-sentence
        mv = {"to": to.id, "qid": qid, "transition": tr("coverage.transition", {"to": title}) if tr else title}
    said = None  # the area's question just went up from a move-on: its transition line stays above it a moment
    q = state.get(state.focus_id)
    if q is not None and q.id.startswith(QID_PREFIX) and (to := get(state, q.id[len(QID_PREFIX):])) is not None:
        at = cov.asked.get(to.id)
        if at is not None and (state.now() if now is None else now) - at < SAY_FOR:
            say = say or state.language
            title = _pick(to.title, say)
            title = title[:1].lower() + title[1:] if say in ("fr", "en") else title
            said = tr("coverage.transition", {"to": title}) if tr else title
    return {"areas": areas, **counts(state), "total": len(areas), "move_on": mv, "said": said,
            "dwell": round(dwell(state, now)), "allowance": round(allowance(state)),
            "point": cov.topic.get("point", ""), "extendable": bool(cov.topic)}


def snapshot(state: SessionState) -> dict:
    """For the model: the areas with their status and the current point (what the topic labels name)."""
    cov = state.cov
    if not cov.areas:
        return {}
    return {"areas": {a.id: status(state, a.id) for a in cov.areas}, "current_point": cov.topic.get("point", ""),
            "current_area": cov.topic.get("area"), "minutes_on_point": round(dwell(state) / 60, 1),
            "move_on_queued": cov.move_on.get("to") if cov.move_on else None}


def catalog_lines(state: SessionState) -> str:
    """The area ids and titles for the sync prompt."""
    return "\n".join(f"- {a.id}: {_pick(a.title, 'en')}" for a in state.cov.areas) or "(none)"
