"""The recipe library: strict schema, the tracked seeds load, wording rules, catalogue for the models."""

from __future__ import annotations

import shutil

import pytest
import yaml

from pacenote import recipes as R
from pacenote.contract import PATTERNS

from .test_questions import jargon

SEED_IDS = {"customer-order-intake", "stock-availability-answers", "supplier-update-distil", "carrier-quote-outreach",
            "ceo-status-digest", "weekly-reporting-assembly", "meeting-decision-brief", "warranty-claim-precheck"}


@pytest.fixture
def library_dir(tmp_path, monkeypatch):
    """A private copy of the tracked library (tests never rewrite the real one)."""
    d = tmp_path / "recipes"
    shutil.copytree(R.RECIPES_DIR, d)
    monkeypatch.setenv("PACENOTE_RECIPES_DIR", str(d))
    return d


def _seed(name: str = "customer-order-intake") -> dict:
    return yaml.safe_load((R.RECIPES_DIR / f"{name}.yaml").read_text(encoding="utf-8"))


def test_the_eight_seeds_load_as_seeds_without_evidence():
    lib = R.load_library(R.RECIPES_DIR)
    assert set(lib) == SEED_IDS
    for r in lib.values():
        assert r.status == "seed" and r.evidence == []
        assert r.type in PATTERNS
        assert r.flow.human_decision.strip()
        assert all(m.typical_baseline.startswith("typical") and m.typical_target.startswith("typical")
                   for m in r.metrics), r.id  # ranges are marked typical, never presented as facts
        assert r.proof.first_win_cases >= 1 and r.proof.kill_week4 and r.proof.kill_week8


def test_seed_files_are_in_the_canonical_layout():
    """`pacenote recipes record` rewrites a file with dump_recipe: the seeds are already in that layout,
    so recording evidence only shows the evidence in a diff."""
    for p in R.RECIPES_DIR.glob("*.yaml"):
        assert R.dump_recipe(R.load_recipe(p)) == p.read_text(encoding="utf-8"), p.name


def test_question_like_fields_have_no_ai_vocabulary():
    for r in R.load_library(R.RECIPES_DIR).values():
        texts = [r.title.fr, r.title.en, *r.signals, *(p.fr for p in r.probes), *(p.en for p in r.probes)]
        for t in texts:
            assert jargon(t) == [], (r.id, t)


@pytest.mark.parametrize("mutate, message", [
    (lambda d: d.update(extra=1), "extra"),
    (lambda d: d["flow"].update(owner="x"), "extra"),
    (lambda d: d.update(type="magic"), "type"),
    (lambda d: d.update(status="draft"), "status"),
    (lambda d: d.update(signals=["only one"]), "two signals"),
    (lambda d: d.update(preconditions=[]), "precondition"),
    (lambda d: d.update(metrics=[]), "metric"),
    (lambda d: d["metrics"].append(dict(d["metrics"][0])), "unique"),
    (lambda d: d.update(status="proven"), "evidence"),
    (lambda d: d["evidence"].append({"client": "x", "date": "2026-11-04", "metric": "m", "baseline": "b",
                                     "result": "r", "source": "manual"}), "proven, not seed"),
    (lambda d: d.update(evidence=[{"client": "x", "date": "yesterday", "metric": "m", "baseline": "b",
                                   "result": "r", "source": "manual"}], status="proven"), "date"),
    (lambda d: d["title"].update(fr="  "), "empty"),
    (lambda d: d["proof"].update(first_win_cases=0), "first_win_cases"),
    (lambda d: d.pop("probes"), "probes"),
])
def test_invalid_recipes_are_rejected_with_the_file_name(tmp_path, mutate, message):
    data = _seed()
    mutate(data)
    p = tmp_path / "customer-order-intake.yaml"
    p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    with pytest.raises(R.RecipeError, match="customer-order-intake.yaml") as exc:
        R.load_recipe(p)
    assert message in str(exc.value)


def test_id_must_match_the_file_name(tmp_path):
    p = tmp_path / "other-name.yaml"
    p.write_text(yaml.safe_dump(_seed(), allow_unicode=True), encoding="utf-8")
    with pytest.raises(R.RecipeError, match="must match the file name"):
        R.load_recipe(p)


def test_one_invalid_file_fails_the_library_but_not_the_live_prompts(library_dir):
    (library_dir / "broken.yaml").write_text("id: broken\n", encoding="utf-8")
    with pytest.raises(R.RecipeError, match="broken.yaml"):
        R.load_library()
    assert R.try_library() == {}


def test_save_round_trips_and_refuses_an_invalid_recipe(library_dir):
    r = R.load_library()["ceo-status-digest"]
    r.signals.append("a new signal heard at another client")
    R.save_recipe(r)
    assert R.load_library()["ceo-status-digest"].signals[-1] == "a new signal heard at another client"
    bad = r.model_copy(update={"status": "proven"})
    with pytest.raises(ValueError):
        R.save_recipe(bad)
    assert R.load_library()["ceo-status-digest"].status == "seed"


def test_compact_catalogue_and_full_definitions():
    lib = R.load_library(R.RECIPES_DIR)
    cat = R.compact_catalogue(lib)
    assert {c["id"] for c in cat} == SEED_IDS
    order = next(c for c in cat if c["id"] == "customer-order-intake")
    assert set(order) == {"id", "title", "type", "status", "signals", "preconditions", "baseline"}
    assert "Time to key in one order (minutes per order)" in order["baseline"]
    full = R.full_definitions(lib, ["supplier-update-distil", "unknown"])
    assert [d["id"] for d in full] == ["supplier-update-distil"]
    assert full[0]["evidence_count"] == 0 and "evidence" not in full[0] and full[0]["metrics"]
