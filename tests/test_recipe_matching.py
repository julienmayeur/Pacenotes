"""Live recipe matching: the full sync returns recipe_matches, merged per (recipe, use case) with the
same optimistic versioning as the slots and maps; the prompts carry the catalogue; the FDE console
(never the question card) shows "Possible recipes"."""

from __future__ import annotations

from pacenote.capture import ReplaySource
from pacenote.llm import OfflineEngine
from pacenote.prompts import build_fast_system, build_sync_system, build_sync_user, state_snapshot
from pacenote.questionnaire import load_client
from pacenote.session import Session, Settings
from pacenote.state import SessionState

from .conftest import CLIENTS, SAMPLES, edit, result

ORDERS = "orders arrive by email or PDF and someone re-keys them into the ERP"
SWAMPED = "the order desk is the first team swamped when volume grows"
MAILBOX = "read access to the mailbox the orders arrive in"
ERP = "an ERP that accepts orders by API, import file or an existing connector"


def _state() -> SessionState:
    return SessionState.from_questionnaire(load_client(CLIENTS, "brioche-four-slots").questionnaire)


def match(recipe="customer-order-intake", use_case="bakery-orders", fit="partial", heard=(), missing=()):
    return {"recipe_id": recipe, "use_case": use_case, "fit": fit, "heard_signals": list(heard),
            "missing": list(missing)}


def test_matches_merge_per_recipe_and_use_case():
    st = _state()
    rep = st.apply_sync_result(result(recipe_matches=[match(heard=[ORDERS], missing=[MAILBOX, ERP])]), read=10)
    assert [e.kind for e in rep.applied if e.kind == "recipe"] == ["recipe"]
    m = st.recipe_matches[0]
    assert (m.recipe_id, m.use_case, m.fit, m.read, m.by) == ("customer-order-intake", "bakery-orders", "partial", 10, "sync")
    # a later sync: one more signal, the mailbox heard; heard signals only grow, missing is the newest view
    st.apply_sync_result(result(recipe_matches=[match(fit="strong", heard=[SWAMPED], missing=[ERP, SWAMPED])]), read=20)
    m = st.recipe_matches[0]
    assert m.fit == "strong" and m.heard_signals == [ORDERS, SWAMPED] and m.missing == [ERP]  # heard never missing
    # the same recipe for no use case yet is another record
    st.apply_sync_result(result(recipe_matches=[match(use_case=None, heard=[ORDERS])]), read=21)
    assert [(x.recipe_id, x.use_case) for x in st.recipe_matches] == [("customer-order-intake", "bakery-orders"),
                                                                       ("customer-order-intake", None)]


def test_a_match_read_by_an_older_analysis_is_kept():
    st = _state()
    st.apply_sync_result(result(recipe_matches=[match(fit="strong", heard=[ORDERS])]), read=30)
    rep = st.apply_sync_result(result(recipe_matches=[match(fit="none")]), read=12)  # a slow sync read less
    assert st.recipe_matches[0].fit == "strong"
    assert [(e.kind, e.target) for e in rep.rejected] == [("stale", "customer-order-intake@bakery-orders")]
    st.apply_sync_result(result(recipe_matches=[match(fit="none")]), read=30)  # equal versions apply
    assert st.recipe_matches[0].fit == "none" and st.active_recipe_matches() == []


def test_unknown_recipes_and_use_cases_are_rejected_and_none_never_creates():
    st = _state()
    rep = st.apply_sync_result(result(recipe_matches=[match(recipe="made-up"), match(use_case="nope"),
                                                      match(recipe="ceo-status-digest", use_case=None, fit="none")]))
    assert st.recipe_matches == []
    assert sorted(e.detail for e in rep.rejected) == ["unknown recipe", "unknown use case 'nope'"]
    assert all(e.kind == "recipe" for e in rep.rejected)


def test_a_use_case_added_in_the_same_sync_can_hold_a_match():
    st = _state()
    add = edit("add", "carriers-walkthrough", "import", text="Pour le dernier camion, qui a appelé les transporteurs ?",
               use_case="carrier-quotes", use_case_title="Demandes de prix transporteurs")
    st.apply_sync_result(result(queue_edits=[add], recipe_matches=[match("carrier-quote-outreach", "carrier-quotes")]))
    assert [(m.recipe_id, m.use_case) for m in st.recipe_matches] == [("carrier-quote-outreach", "carrier-quotes")]


def test_matches_survive_save_and_load(tmp_path):
    st = _state()
    st.apply_sync_result(result(recipe_matches=[match(heard=[ORDERS], missing=[MAILBOX])]), read=5)
    st.save(tmp_path / "state.json")
    back = SessionState.load(tmp_path / "state.json")
    assert back.recipe_matches == st.recipe_matches


def test_sync_prompt_carries_the_catalogue_and_the_matches_the_fast_pass_does_not():
    st = _state()
    st.apply_sync_result(result(recipe_matches=[match(heard=[ORDERS], missing=[MAILBOX])]), read=5)
    system = build_sync_system(st, glossary="")
    assert "<recipe_catalogue>" in system and '"id": "warranty-claim-precheck"' in system
    assert "Never name a recipe" in system and "CURRENT phase" in system
    assert state_snapshot(st)["recipe_matches"] == [match(heard=[ORDERS], missing=[MAILBOX])]
    assert '"recipe_matches"' in build_sync_user(st, [], [], 0.0)
    assert "recipe_catalogue" not in build_fast_system(st, glossary="")


def test_console_view_lists_possible_recipes_strong_first(tmp_path):
    cfg = load_client(CLIENTS, "brioche-four-slots")
    s = Session(client=cfg, session_dir=tmp_path / "s", engine=OfflineEngine(),
                source_factory=lambda d: ReplaySource({"fr": SAMPLES / "fr.srt"}, speed=0),
                settings=Settings(sync_interval=3600, first_sync_delay=3600, poll_interval=3600, fast_enabled=False))
    assert s.view()["recipes"] == []
    s.state.apply_sync_result(result(recipe_matches=[
        match(heard=[ORDERS], missing=[MAILBOX, ERP]),
        match("ceo-status-digest", None, "strong", heard=["the CEO asks a coworker where things stand several times a week"]),
        match("weekly-reporting-assembly", "weekly-reporting", "none"),
    ]))
    s.state.ui_language = "fr"
    view = s.view()["recipes"]
    assert [(r["id"], r["fit"]) for r in view] == [("ceo-status-digest", "strong"), ("customer-order-intake", "partial")]
    assert view[0]["title"] == "Où en sont les dossiers, sans avoir à demander" and view[0]["use_case"] is None
    assert view[1]["missing"] == [MAILBOX, ERP] and view[1]["use_case"] == "Saisie des commandes des boulangeries"
    assert view[1]["status"] == "seed" and view[1]["evidence"] == 0
    # the question card carries no recipe: only the console block reads S.recipes
    assert "recipes" not in (s.view()["current"] or {})
