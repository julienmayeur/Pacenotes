"""The plan's title and section headings in the interview language (locale file, else Claude's overlay,
else English). Shared by the plan writer's prompt and the plan renderer."""

from __future__ import annotations

from collections.abc import Mapping

from . import i18n
from .state import SessionState


PLAN_HEADING_KEYS = ("context", "priorities", "workflows", "quick_wins", "plan_30_60_90", "risks", "budget", "log",
                     "current_state", "bottlenecks", "metrics", "timeline",
                     "use_cases", "details", "recipe_candidates", "open_items", "adoption", "to_check")


def plan_headings(lang: str, overlay: Mapping[str, str] | None = None) -> dict[str, str]:
    """Plan title + section headings in `lang` (locale file, else Claude's overlay, else English)."""
    tr = i18n.Translator(lang, overlay)
    return {"title": tr("plan.title"), **{k: tr(f"plan.headings.{k}") for k in PLAN_HEADING_KEYS}}


def state_headings(state: SessionState) -> dict[str, str]:
    return plan_headings(state.language, state.i18n_overlay.get(state.language))
