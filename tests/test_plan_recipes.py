"""Recipe-first plan: each opportunity names the library recipe it reuses (or is a new candidate with
a draft), metrics reuse the recipe's definitions but never its typical ranges as client facts, and
the ranking puts recipe-backed work first."""

from __future__ import annotations

import shutil

import pytest

from pacenote import recipes as R
from pacenote.contract import ContractError, PlanDoc, parse_plan_doc
from pacenote.finalize import apply_recipes, is_typical, ranked, recipe_rank, render_plan_md
from pacenote.llm import offline_plan_doc
from pacenote.prompts import build_plan_system
from pacenote.questionnaire import load_client
from pacenote.state import SessionState

from .conftest import CLIENTS, plan_doc, plan_use_case, result


def _state() -> SessionState:
    return SessionState.from_questionnaire(load_client(CLIENTS, "brioche-four-slots").questionnaire)


def measure(value="", evidence="", status=None, question=""):
    return {"value": value, "date": "", "status": status or ("stated" if value else "to_confirm"),
            "evidence": evidence, "question": question, "ask": ""}


def metric(mid, use_case, name="", unit="", baseline=None, target=None):
    return {"id": mid, "use_case": use_case, "name": name, "unit": unit, "how_measured": "", "cadence": "",
            "owner": "", "baseline": baseline or measure(), "target": target or measure()}


def case(uid, recipe=None, fit="strong", gaps=(), back_office=False, metric_id=""):
    u = plan_use_case(uid=uid, title=uid)
    u["recipe"] = {"id": recipe, "fit": fit, "gaps": list(gaps)} if recipe else None
    u["back_office"] = back_office
    u["metric"] = metric_id
    return u


CANDIDATE = {
    "id": "spare-parts-quotes", "use_case": "parts", "title": "Devis pièces détachées", "type": "act_on_behalf",
    "flow": {"trigger": "Une boulangerie demande un devis pièces", "agent_does": "Retrouve les références et prix",
             "writes_to": "Brouillon de devis", "notifies": "Le magasinier", "human_decision": "Le magasinier envoie"},
    "signals": ["les devis pièces sont faits à la main à partir d'un catalogue PDF"],
    "preconditions": ["catalogue pièces exportable"],
    "metrics": [{"id": "parts-quote-delay", "name": "Délai de devis", "unit": "heures", "how_measured": "",
                 "cadence": "hebdomadaire", "owner": "", "typical_baseline": "", "typical_target": ""}],
    "proof": "30 devis passés rejoués", "sourcing": "configure",
}


@pytest.fixture
def library(tmp_path, monkeypatch):
    """A private library where carrier-quote-outreach is proven (one evidence entry)."""
    d = tmp_path / "recipes"
    shutil.copytree(R.RECIPES_DIR, d)
    monkeypatch.setenv("PACENOTE_RECIPES_DIR", str(d))
    r = R.load_recipe(d / "carrier-quote-outreach.yaml")
    r.evidence.append(R.Evidence(client="Importer · a1b2c3", date="2026-10-01", metric="Time from request to booking",
                                 baseline="48 h (2026-08-01)", result="6 h (2026-09-30)", source="manual"))
    r.status = "proven"
    R.save_recipe(r, d)
    return R.load_library(d)


def test_plan_schema_requires_recipe_candidates_and_keeps_them_strict():
    d = plan_doc(case("parts"), recipe_candidates=[CANDIDATE])
    assert parse_plan_doc(d).recipe_candidates[0].flow.human_decision == "Le magasinier envoie"
    for bad in ({k: v for k, v in d.items() if k != "recipe_candidates"},
                plan_doc(case("parts"), recipe_candidates=[{**CANDIDATE, "evidence": []}]),
                plan_doc(case("parts"), recipe_candidates=[{**CANDIDATE, "type": "magic"}])):
        with pytest.raises(ContractError):
            parse_plan_doc(bad)
    assert "recipe_candidates" in PlanDoc.model_json_schema()["required"]


def test_ranking_strong_with_evidence_then_strong_then_partial_then_new(library):
    d = parse_plan_doc(plan_doc(
        case("new-backoffice", back_office=True),  # back-office alone no longer beats a recipe
        case("partial", "customer-order-intake", "partial"),
        case("strong-seed", "ceo-status-digest", "strong"),
        case("strong-proven", "carrier-quote-outreach", "strong"),
        case("unknown-recipe", "not-in-library", "strong"),
        case("ruled-out", "weekly-reporting-assembly", "none", back_office=True),
    ))
    assert [recipe_rank(u, library) for u in d.use_cases] == [0, 1, 2, 3, 0, 0]
    assert [u.id for u in ranked(d, library)] == ["strong-proven", "strong-seed", "partial", "new-backoffice",
                                                   "ruled-out", "unknown-recipe"]


def test_apply_recipes_nulls_unknown_recipes_and_reuses_definitions_never_typical_values(library):
    d = parse_plan_doc(plan_doc(
        case("orders", "customer-order-intake", metric_id="order-entry-minutes"),
        case("ghost", "not-in-library"),
        case("status", "ceo-status-digest", metric_id="status-q"),
        metrics=[
            # the model copied the typical ranges as values: blanked, the question kept
            metric("order-entry-minutes", "orders", baseline=measure("5 to 15 minutes per order", status="partial",
                                                                      question="Combien de minutes par commande ?"),
                   target=measure("typical: under 2 minutes of checking")),
            # the CEO really said it (quoted): a client fact stays, even if it looks like a range
            metric("status-q", "status", name="Questions de statut", unit="par semaine",
                   baseline=measure("10 to 30 per week", evidence="je demande dix à trente fois par semaine"),
                   target=measure("fewer than 5", status="partial")),
        ]))
    out = apply_recipes(d, library)
    orders, ghost, status = out.use_cases
    assert ghost.recipe is None and orders.recipe.id == "customer-order-intake"
    m = out.metrics[0]
    assert (m.name, m.unit, m.cadence) == ("Time to key in one order", "minutes per order", "weekly")  # reused
    assert m.baseline.status == "to_confirm" and m.baseline.value == "" and m.baseline.question
    assert m.target.status == "to_confirm" and m.target.value == ""
    s = out.metrics[1]
    assert s.name == "Questions de statut"  # the plan's own definition is kept
    assert s.baseline.value == "10 to 30 per week" and s.baseline.status == "stated"
    assert s.target.value == "" and s.target.status == "to_confirm"  # unquoted and typical: blanked
    assert d.metrics[0].baseline.value  # the input doc is not changed in place


def test_is_typical():
    m = R.load_library(R.RECIPES_DIR)["customer-order-intake"].metric("order-entry-minutes")
    assert is_typical("5 to 15 minutes per order", m) and is_typical("typical: anything", None)
    assert not is_typical("12 minutes", m) and not is_typical("", m)


def test_render_shows_the_recipe_per_opportunity_and_the_candidates(library):
    st = _state()
    d = parse_plan_doc(plan_doc(case("carriers", "carrier-quote-outreach", gaps=["liste des transporteurs"]),
                                case("parts"), recipe_candidates=[CANDIDATE]))
    md = render_plan_md(d, st)
    table = md.split("## Opportunités classées")[1].split("## Opportunités en détail")[0]
    assert "| Recette |" in table
    assert "| Demandes de prix aux transporteurs habituels, réponses comparées (adéquation forte) |" in table
    assert "| Nouvelle recette candidate |" in table
    assert ("**Recette** : Demandes de prix aux transporteurs habituels, réponses comparées — adéquation forte, "
            "prouvée sur 1 engagement(s) — manque : liste des transporteurs") in md
    assert "**Recette** : Nouvelle recette candidate" in md
    cand = md.split("## Recettes candidates (brouillons)")[1].split("## Calendrier")[0]
    assert "### Devis pièces détachées (`spare-parts-quotes`)" in cand and "relecture" in cand
    assert "- catalogue pièces exportable" in cand and "- Délai de devis (heures)" in cand
    assert "Une boulangerie demande un devis pièces → Retrouve les références et prix" in cand


def test_plan_system_receives_the_matched_recipes_in_full():
    st = _state()
    st.apply_sync_result(result(recipe_matches=[
        {"recipe_id": "warranty-claim-precheck", "use_case": "warranty-claims", "fit": "strong",
         "heard_signals": [], "missing": []}]))
    system = build_plan_system(st, glossary="")
    matched = system.split("<matched_recipes")[1].split("</matched_recipes>")[0]
    assert '"id": "warranty-claim-precheck"' in matched and '"kill_week4"' in matched and '"evidence_count": 0' in matched
    others = system.split("<other_recipes")[1].split("</other_recipes>")[0]
    assert "warranty-claim-precheck" not in others and "customer-order-intake" in others
    assert "NEVER a client value" in system and "recipe: null (reserved)" not in system
    assert "(no recipe matched live)" in build_plan_system(_state(), glossary="")


def test_offline_doc_takes_the_strongest_live_match_per_use_case():
    st = _state()
    st.apply_sync_result(result(
        slots=[{"use_case": "bakery-orders", "slot": "volume", "value": "40 par jour", "evidence": "40 par jour"}],
        recipe_matches=[
            {"recipe_id": "stock-availability-answers", "use_case": "bakery-orders", "fit": "partial",
             "heard_signals": [], "missing": []},
            {"recipe_id": "customer-order-intake", "use_case": "bakery-orders", "fit": "strong",
             "heard_signals": ["orders arrive by email or PDF and someone re-keys them into the ERP"],
             "missing": ["read access to the mailbox the orders arrive in"]}]))
    d = offline_plan_doc(st)
    u = next(x for x in d.use_cases if x.id == "bakery-orders")
    assert u.recipe.model_dump() == {"id": "customer-order-intake", "fit": "strong",
                                     "gaps": ["read access to the mailbox the orders arrive in"]}
    assert d.recipe_candidates == []
