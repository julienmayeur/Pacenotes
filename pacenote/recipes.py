"""The recipe library: workflows that already work, so each engagement can reuse one instead of
starting from a blank page, and each result makes the next engagement better.

pacenote/knowledge/recipes/<id>.yaml, one tracked file per recipe, strict schema (unknown keys are
an error). A recipe says what fits (signals heard in the interview), what it needs (preconditions,
integrations), what it does (trigger -> what the agent does -> where it writes / whom it notifies ->
the decision the human keeps), how it is measured (metric definitions in the plan's shape, with
TYPICAL ranges that are never client facts), how it is proven (first verified win, shadow mode,
kill criteria) and the evidence of past engagements. `status: seed` until one engagement records a
result (`pacenote recipes record`), then `proven`.

The live sync gets the catalogue in compact form (compact_catalogue) and returns recipe matches;
the plan writer gets the full definitions of the matched recipes (full_definitions).
"""

from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from . import fsutil
from .contract import Pattern, RecipeFlow, RecipeMetricDef

RECIPES_DIR = Path(__file__).parent / "knowledge" / "recipes"
RecipeStatus = Literal["seed", "proven"]
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")


class RecipeError(ValueError):
    pass


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Wording(_Strict):
    fr: str
    en: str

    @field_validator("fr", "en")
    @classmethod
    def _filled(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("empty wording")
        return v.strip()


class Proof(_Strict):
    first_win_cases: int  # about how many real cases the first verified win is run on
    first_win: str
    shadow_mode: str  # how it runs next to the person before it writes anything
    kill_week4: str
    kill_week8: str


class SourcingNote(_Strict):
    choice: Literal["buy", "configure", "build"]
    note: str


class Evidence(_Strict):
    """One past engagement: what the metric was before and what it became."""

    client: str  # anonymised label by default, the client's name only when asked
    date: str  # when the result was recorded (ISO)
    metric: str  # name and unit
    baseline: str  # value and date
    result: str  # latest value and date
    source: str  # where the numbers come from (manual count, csv, a system)

    @field_validator("date")
    @classmethod
    def _iso(cls, v: str) -> str:
        date.fromisoformat(v)
        return v


class LibraryRecipe(_Strict):
    id: str
    title: Wording
    type: Pattern  # the opportunity type, same enum as the plan
    status: RecipeStatus
    flow: RecipeFlow
    signals: list[str]  # facts that show it fits, phrased against the live maps and slots
    preconditions: list[str]  # what must be true or granted (mailbox access, ERP export...)
    integrations: list[str]  # the systems it reads or writes
    probes: list[Wording]  # story-form follow-ups that bring out a missing precondition or the baseline
    metrics: list[RecipeMetricDef]
    proof: Proof
    sourcing: SourcingNote
    evidence: list[Evidence]

    @field_validator("id")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not _SLUG.match(v):
            raise ValueError(f"recipe id {v!r}: lowercase letters, digits and dashes")
        return v

    @model_validator(mode="after")
    def _consistent(self) -> "LibraryRecipe":
        if len([s for s in self.signals if s.strip()]) < 2:
            raise ValueError("at least two signals")
        if not any(p.strip() for p in self.preconditions):
            raise ValueError("at least one precondition")
        if not self.metrics:
            raise ValueError("at least one metric")
        ids = [m.id for m in self.metrics]
        if len(set(ids)) != len(ids) or not all(_SLUG.match(i) for i in ids):
            raise ValueError("metric ids must be unique slugs")
        if self.proof.first_win_cases < 1:
            raise ValueError("proof.first_win_cases must be at least 1")
        if self.status == "proven" and not self.evidence:
            raise ValueError("a proven recipe needs at least one evidence entry")
        if self.status == "seed" and self.evidence:
            raise ValueError("a recipe with evidence is proven, not seed")
        return self

    def title_in(self, lang: str) -> str:
        return self.title.fr if lang.split("-")[0] == "fr" else self.title.en

    def metric(self, metric_id: str) -> RecipeMetricDef | None:
        return next((m for m in self.metrics if m.id == metric_id), None)


def recipes_dir() -> Path:
    return Path(os.environ.get("PACENOTE_RECIPES_DIR") or RECIPES_DIR)


def load_recipe(path: Path) -> LibraryRecipe:
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        recipe = LibraryRecipe.model_validate(data)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise RecipeError(f"{path.name}: {exc}") from exc
    if recipe.id != path.stem:
        raise RecipeError(f"{path.name}: id {recipe.id!r} must match the file name")
    return recipe


def load_library(directory: Path | None = None) -> dict[str, LibraryRecipe]:
    """Every recipe of the library, by id (sorted). Raises RecipeError on the first invalid file."""
    d = Path(directory or recipes_dir())
    if not d.is_dir():
        return {}
    return {r.id: r for r in (load_recipe(p) for p in sorted(d.glob("*.yaml")))}


def try_library(directory: Path | None = None) -> dict[str, LibraryRecipe]:
    """The library for the live prompts: an invalid file never stops an interview (empty then;
    `pacenote recipes list` and the tests report it)."""
    try:
        return load_library(directory)
    except RecipeError:
        return {}


def dump_recipe(recipe: LibraryRecipe) -> str:
    return yaml.safe_dump(recipe.model_dump(), sort_keys=False, allow_unicode=True, width=100)


def save_recipe(recipe: LibraryRecipe, directory: Path | None = None) -> Path:
    LibraryRecipe.model_validate(recipe.model_dump())  # never write an invalid recipe
    path = Path(directory or recipes_dir()) / f"{recipe.id}.yaml"
    fsutil.atomic_write_text(path, dump_recipe(recipe))
    return path


def record_evidence(recipe_id: str, entries: list[Evidence], directory: Path | None = None) -> LibraryRecipe:
    """Append past-engagement results to a recipe and mark it proven (the improvement loop: each
    engagement makes the next one better). An entry for the same client and metric replaces the
    older one, so recording again updates the result instead of counting the engagement twice."""
    if not entries:
        raise RecipeError("no evidence to record")
    lib = load_library(directory)
    if recipe_id not in lib:
        raise RecipeError(f"unknown recipe {recipe_id!r} (known: {', '.join(lib) or 'none'})")
    r = lib[recipe_id].model_copy(deep=True)
    for e in entries:
        r.evidence = [x for x in r.evidence if (x.client, x.metric) != (e.client, e.metric)] + [e]
    r.status = "proven"
    save_recipe(r, directory)
    return r


# --- what the models receive ---------------------------------------------------------------------


def compact_catalogue(library: dict[str, LibraryRecipe]) -> list[dict]:
    """One short entry per recipe for the live sync: enough to recognise it and to know what to
    listen for next (preconditions, the baseline number), nothing more."""
    return [{"id": r.id, "title": r.title.en, "type": r.type, "status": r.status,
             "signals": r.signals, "preconditions": r.preconditions,
             "baseline": [f"{m.name} ({m.unit})" for m in r.metrics]}
            for r in library.values()]


def full_definitions(library: dict[str, LibraryRecipe], ids) -> list[dict]:
    """The matched recipes in full for the plan writer (evidence summarised as a count)."""
    out = []
    for rid in ids:
        r = library.get(rid)
        if r is None:
            continue
        d = r.model_dump(exclude={"evidence"})
        d["evidence_count"] = len(r.evidence)
        out.append(d)
    return out
