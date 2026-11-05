"""The improvement loop: a client's measured result becomes evidence on the recipe it used
(`pacenote recipes record`), which flips the recipe from seed to proven and ranks it first next time."""

from __future__ import annotations

import shutil

import pytest

from pacenote import metrics as M
from pacenote import recipes as R
from pacenote.cli import main

from .conftest import REAL_CLIENTS
from .test_metrics import plan_metric, write_plan


@pytest.fixture
def env(tmp_path, monkeypatch):
    lib = tmp_path / "recipes"
    shutil.copytree(R.RECIPES_DIR, lib)
    monkeypatch.setenv("PACENOTE_RECIPES_DIR", str(lib))
    clients, sessions = tmp_path / "clients", tmp_path / "sessions"
    shutil.copytree(REAL_CLIENTS / "brioche-four", clients / "brioche-four")
    write_plan(sessions, "20261104-100000", plan_metric())
    M.seed(clients, sessions, "brioche-four")
    return lib, clients, ["--clients-dir", str(clients)]


def test_record_appends_anonymised_evidence_and_flips_seed_to_proven(env, capsys):
    lib, clients, common = env
    M.add_measurement(clients, "brioche-four", "order-entry-minutes", 9, when="2026-11-12")
    M.add_measurement(clients, "brioche-four", "order-entry-minutes", 4, when="2026-11-26", source="erp")
    assert main(["recipes", "record", "brioche-four", "customer-order-intake", *common]) == 0
    out = capsys.readouterr().out
    r = R.load_library()["customer-order-intake"]
    assert r.status == "proven" and len(r.evidence) == 1
    e = r.evidence[0]
    assert e.client == M.anonymised_label("brioche-four") and "brioche-four" not in e.client.lower()
    assert e.metric == "Temps de saisie d'une commande (minutes)"
    assert e.baseline == "12 minutes (2026-11-04)"
    assert e.result == "4 minutes (2026-11-26), 2 measurement(s) since 2026-11-12; target 2 minutes, 80 % of the way"
    assert e.source == "erp, manual"
    assert "customer-order-intake: proven, 1 evidence entry" in out
    # recording again updates this client's entry instead of counting the engagement twice
    M.add_measurement(clients, "brioche-four", "order-entry-minutes", 2, when="2026-12-03")
    assert main(["recipes", "record", "brioche-four", "customer-order-intake", "--client-name", *common]) == 0
    assert main(["recipes", "record", "brioche-four", "customer-order-intake", "--client-name", *common]) == 0
    clients_seen = [x.client for x in R.load_library()["customer-order-intake"].evidence]
    assert clients_seen == [M.anonymised_label("brioche-four"), "Brioche & Four"]
    assert R.load_library()["customer-order-intake"].evidence[-1].result.startswith("2 minutes (2026-12-03)")
    # the tracked file stays valid and in the canonical layout
    p = lib / "customer-order-intake.yaml"
    assert R.dump_recipe(R.load_recipe(p)) == p.read_text(encoding="utf-8")


def test_record_refuses_without_data_and_changes_nothing(env, capsys):
    lib, clients, common = env
    before = (lib / "customer-order-intake.yaml").read_text()
    assert main(["recipes", "record", "brioche-four", "customer-order-intake", *common]) == 2
    assert "no measurement yet" in capsys.readouterr().err
    assert main(["recipes", "record", "brioche-four", "ceo-status-digest", *common]) == 2  # no metric tied to it
    assert "pass --metric" in capsys.readouterr().err
    M.add_measurement(clients, "brioche-four", "order-entry-minutes", 9, when="2026-11-12")
    assert main(["recipes", "record", "brioche-four", "not-a-recipe", "--metric", "order-entry-minutes", *common]) == 2
    assert "unknown recipe" in capsys.readouterr().err
    assert main(["recipes", "record", "acme", "customer-order-intake", *common]) == 2
    assert (lib / "customer-order-intake.yaml").read_text() == before
    # --metric ties any metric to a recipe explicitly
    assert main(["recipes", "record", "brioche-four", "ceo-status-digest", "--metric", "order-entry-minutes", *common]) == 0
    assert R.load_library()["ceo-status-digest"].status == "proven"


def test_recipes_list_shows_status_and_evidence(env, capsys):
    _, clients, common = env
    assert main(["recipes", "list"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 8 and all(" seed " in line and "evidence 0" in line for line in out)
    M.add_measurement(clients, "brioche-four", "order-entry-minutes", 4, when="2026-11-26")
    main(["recipes", "record", "brioche-four", "customer-order-intake", *common])
    capsys.readouterr()
    main(["recipes", "list"])
    line = next(x for x in capsys.readouterr().out.splitlines() if x.startswith("customer-order-intake"))
    assert " proven " in line and "evidence 1" in line and "Order intake from email or PDF into the ERP" in line


def test_record_evidence_validates_inputs(env):
    with pytest.raises(R.RecipeError, match="no evidence"):
        R.record_evidence("customer-order-intake", [])
