"""Run-time writes to the knowledge never land in the repository: a refreshed brief and evidence recorded on a
recipe go to the local knowledge folder (outside the repo), and win over the shipped file of the same name."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from pacenote import knowledge
from pacenote.knowledge import briefs, refresh
from pacenote.knowledge import recipes as R

from .conftest import ROOT


def _shipped() -> dict[Path, str]:
    files = [*briefs.KNOWLEDGE_DIR.glob("*.md"), *R.RECIPES_DIR.glob("*.yaml")]
    return {p: p.read_text(encoding="utf-8") for p in files}


def test_the_local_folder_defaults_outside_the_repository(monkeypatch):
    monkeypatch.delenv("PACENOTE_KNOWLEDGE_DIR", raising=False)
    local = knowledge.local_dir()
    assert local == Path.home() / ".pacenote" / "knowledge"
    assert ROOT.resolve() not in local.resolve().parents


def test_a_refreshed_brief_goes_to_the_local_folder_and_wins(tmp_path):
    before = _shipped()

    def runner(job, cmd, prompt, timeout, prefix):
        return {"body_md": "# What agents can do today\n\n- Refreshed item\n",
                "sources": [{"title": "Notes", "url": "https://example.com/notes"}]}

    b = refresh.refresh_brief("capabilities", claude_bin="/bin/echo", today=date(2026, 12, 1), runner=runner)
    assert b.path == knowledge.local_dir() / "capabilities.md" and b.path.exists()
    assert "Refreshed item" in briefs.load_brief("capabilities").body
    assert briefs.load_brief("delivery-practice").path.parent == briefs.KNOWLEDGE_DIR  # not refreshed: shipped
    assert _shipped() == before  # the tracked files are untouched


def test_recorded_evidence_goes_to_the_local_folder_and_wins():
    before = _shipped()
    evidence = R.Evidence(client="Client A", date="2026-12-01", metric="order entry (min/order)",
                          baseline="12 (2026-11-01)", result="3 (2026-12-01)", source="manual count")
    r = R.record_evidence("customer-order-intake", [evidence])
    assert r.status == "proven"
    assert (R.recipes_dir() / "customer-order-intake.yaml").exists()
    lib = R.load_library()
    assert lib["customer-order-intake"].status == "proven" and lib["customer-order-intake"].evidence == [evidence]
    assert set(lib) == set(R.load_library(R.RECIPES_DIR))  # the other recipes still come from the shipped library
    assert _shipped() == before  # the tracked files are untouched


def test_an_explicit_folder_reads_only_that_folder(tmp_path):
    assert R.load_library(tmp_path) == {}
    assert briefs.load_brief("capabilities", tmp_path) is None
