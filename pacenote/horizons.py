"""Horizons and the pre-study posture.

Every opportunity is tagged with a horizon:

- h1: execution now, on today's core work: a measured pilot that starts this week;
- h2: emerging: redesigning a workflow or a role, staged over the next quarter;
- h3: foresight: what the company could do in about two years, kept as a small bet.

The client never reads "H1 / H2 / H3": the plan groups opportunities under plain words
("This week / This quarter / Within two years", locale keys `plan.horizon.*`).

The posture sets the interview's pace and tone. The pre-study (research.py) gathers four public
signals and this module scores them, 0 to 10:

- A. sector adoption: share of companies of the client's sector and size using AI (0 to 3);
- B. competitors with a published result, each with its URL and date (0 to 3);
- C. market and customer pressure (0 to 2);
- D. task exposure: how much of the work is text, re-keying and back-office (0 to 2).

0-3 = explorer (gentle: one visible, low-risk improvement), 4-6 = pragmatic (one measured
improvement now, one dated option next), 7-10 = urgent (competitors already have results: move
fast, both in parallel). A CEO who already uses AI tools himself moves the posture up one step; a
regulated sector slows the pace, not the urgency. The posture is an operator heuristic: the admin
shows it with its sources and the interviewer may override it; nothing the client sees mentions it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict

HORIZONS = ("h1", "h2", "h3")
Horizon = Literal["h1", "h2", "h3"]
POSTURES = ("explorer", "pragmatic", "urgent")
Posture = Literal["explorer", "pragmatic", "urgent"]
Pressure = Literal["none", "moderate", "strong"]
Exposure = Literal["low", "medium", "high"]
YesNoUnknown = Literal["yes", "no", "unknown"]
Workforce = Literal["young", "mixed", "mature", "unknown"]

HORIZON_GUIDE = (
    "h1 = execution now: today's core work, a measured pilot that can start this week (re-keying, "
    "status, back-office); h2 = emerging: redesigning a workflow or a role, staged over the next quarter; "
    "h3 = foresight: what the company could do in about two years (a new service, a new model), kept as a "
    "small bet")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceRef(_Strict):
    title: str
    url: str


class CompetitorSignal(_Strict):
    competitor: str  # a named company of the same sector
    signal: str  # what it did with AI, as published (a tool, a service, a hire, a statement)
    result: str  # the published outcome, with its number; "" when no result was published
    url: str
    date: str  # publication date (YYYY-MM or YYYY-MM-DD); "" when unknown


class PreStudy(_Strict):
    """The structured part of the pre-study (research.py): what the posture is scored from."""

    sector: str  # the client's sector, in a few words
    size_band: str  # the size class the benchmark uses, e.g. "10-49 employees"
    sector_adoption_pct: float | None  # share of companies of that sector and size using AI; null when not found
    adoption_source: SourceRef  # the statistic's source (title "" and url "" when not found)
    adoption_note: str  # one sentence: the figure, its year, its scope
    competitors: list[CompetitorSignal]
    market_pressure: Pressure  # do customers already compare on speed, price or self-service?
    pressure_note: str
    task_exposure: Exposure  # share of the work that is text, documents, re-keying, back-office
    exposure_note: str
    regulated: bool  # strong regulation (health, finance...): slower pace, same urgency
    ceo_uses_ai: YesNoUnknown  # the CEO publicly uses AI tools himself (talks, posts)
    rationale: str  # 2 or 3 plain sentences: why this pace for this company
    # the workforce's age profile from public, company-level signals (published average age, graduate or
    # apprenticeship hiring, a recent hiring wave, a wave of retirements): sets how many pilots to expect
    # (pilots.py); never about an individual; unknown when nothing was found
    workforce: Workforce
    workforce_note: str


# --- scoring -----------------------------------------------------------------------------------


def _adoption_points(pct) -> int:
    try:
        p = float(pct)
    except (TypeError, ValueError):
        return 0
    if p != p or p < 0:  # NaN or negative: unknown
        return 0
    if 0 < p <= 1:  # a share given as a fraction (0.18)
        p *= 100
    return 0 if p < 10 else 1 if p < 20 else 2 if p <= 35 else 3


def counted_signals(competitors) -> list[dict]:
    """The competitor signals that count: a named competitor, an http(s) URL and a date."""
    out = []
    for c in competitors or []:
        c = c if isinstance(c, dict) else c.model_dump()
        name, url, date = (str(c.get(k) or "").strip() for k in ("competitor", "url", "date"))
        if name and url.lower().startswith(("http://", "https://")) and date:
            out.append(c)
    return out


def _competitor_points(competitors) -> int:
    signals = counted_signals(competitors)
    if not signals:
        return 0
    proven = {str(c.get("competitor")).strip().lower() for c in signals if str(c.get("result") or "").strip()}
    return 1 if not proven else 2 if len(proven) == 1 else 3


_PRESSURE = {"none": 0, "moderate": 1, "strong": 2}
_EXPOSURE = {"low": 0, "medium": 1, "high": 2}


def posture_for(total: int) -> Posture:
    return "explorer" if total <= 3 else "pragmatic" if total <= 6 else "urgent"


def score_prestudy(prestudy: dict | PreStudy | None) -> dict | None:
    """The posture score of a pre-study: {criteria: {a, b, c, d}, total, base, posture, modulators}.
    None when there is no pre-study."""
    if prestudy is None:
        return None
    p = prestudy.model_dump() if isinstance(prestudy, BaseModel) else dict(prestudy)
    criteria = {"a": _adoption_points(p.get("sector_adoption_pct")),
                "b": _competitor_points(p.get("competitors")),
                "c": _PRESSURE.get(str(p.get("market_pressure")), 0),
                "d": _EXPOSURE.get(str(p.get("task_exposure")), 0)}
    total = sum(criteria.values())
    base = posture_for(total)
    posture, modulators = base, []
    if p.get("ceo_uses_ai") == "yes" and base != "urgent":
        posture = POSTURES[POSTURES.index(base) + 1]
        modulators.append("ceo_uses_ai")
    if p.get("regulated"):
        modulators.append("regulated")
    return {"criteria": criteria, "total": total, "base": base, "posture": posture, "modulators": modulators}


def effective_posture(override: str | None, prestudy: dict | None) -> tuple[Posture | None, str]:
    """(posture, where it comes from): the interviewer's override wins, else the pre-study's score,
    else none ("")."""
    if override in POSTURES:
        return override, "manual"
    scored = score_prestudy((prestudy or {}).get("prestudy")) if prestudy else None
    if scored:
        return scored["posture"], "prestudy"
    return None, ""


# --- horizon tags --------------------------------------------------------------------------------


def preplan_horizon(case: dict) -> Horizon:
    """A pre-plan use case's horizon: the one the research gave, else from its kind (a quick win is
    h1; a bigger bet is h2 when feasible, h3 otherwise)."""
    h = str(case.get("horizon") or "").lower()
    if h in HORIZONS:
        return h  # type: ignore[return-value]
    if case.get("kind") == "quick_win":
        return "h1"
    try:
        feasible = int(case.get("feasibility") or 0) >= 3
    except (TypeError, ValueError):
        feasible = False
    return "h2" if feasible else "h3"


def checked_horizon(horizon: str, feasibility: int) -> Horizon:
    """An opportunity tagged h1 must be doable now: one scored hard to do (feasibility 1 or 2 out of
    5) moves to h2. Unknown tags become h2."""
    h = horizon if horizon in HORIZONS else "h2"
    if h == "h1" and 0 < int(feasibility or 0) <= 2:
        return "h2"
    return h  # type: ignore[return-value]


def offline_horizon(filled_slots: int, has_recipe: bool) -> Horizon:
    """The horizon of an opportunity in the offline plan (no model): a recipe that already worked
    elsewhere, or a workflow described in enough detail (5 of the 9 slots), can start now (h1);
    anything else is next quarter's (h2). The offline plan never claims h3."""
    return "h1" if has_recipe or filled_slots >= 5 else "h2"


# --- the posture's questions ------------------------------------------------------------------------

HORIZON_FILE = Path(__file__).parent / "core" / "horizons.yaml"


def load_horizon_file(path: Path = HORIZON_FILE) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def posture_theme(posture: str | None, path: Path = HORIZON_FILE):
    """The horizon questions the posture asks for, as a theme of the `return` phase (None without a
    posture). Each posture keeps its own questions with its own priorities (core/horizons.yaml):
    an explorer interview only touches the future if time allows; an urgent one checks what the
    competitors changed, as a must-have."""
    from .questionnaire import ThemeDef  # (avoid an import cycle)

    if posture not in POSTURES:
        return None
    data = load_horizon_file(path)
    plan = (data.get("postures") or {}).get(posture) or {}
    theme = ThemeDef.model_validate(data["theme"])
    questions = [q.model_copy(update={"priority": int(plan[q.id])}) for q in theme.questions if q.id in plan]
    questions.sort(key=lambda q: list(plan).index(q.id))
    if not questions:
        return None
    return theme.model_copy(update={"questions": questions})
