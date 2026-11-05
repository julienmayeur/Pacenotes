"""Integration slots: every use case is drilled through the same slots (contract.SLOT_KEYS), the
schema the plan is written from. A question attached to a use case shows its unfilled slots as
"Ce qui manque", and its answer is sufficient (it can be answered / auto-advance) only when all its
required slots hold what the CEO actually said."""

import json

import pytest

from pacenote import i18n
from pacenote.contract import SLOT_KEYS, SLOT_RUNG, FastResult
from pacenote.llm import OfflineEngine
from pacenote.prompts import build_fast_system, build_fast_user, build_sync_system, build_sync_user
from pacenote.questionnaire import load_client, load_questionnaire
from pacenote.capture import ReplaySource
from pacenote.session import Session, Settings
from pacenote.state import SessionState

from .conftest import CLIENTS, edit, fill, result, upd

SLOTS_CLIENT = "brioche-four-slots"  # frozen copy of the slot-driven questionnaire


def slots_client():
    return load_client(CLIENTS, SLOTS_CLIENT)


def slots_state() -> SessionState:
    return SessionState.from_questionnaire(slots_client().questionnaire)


def fast(*updates, slots=()):
    return FastResult.model_validate({"question_updates": list(updates), "slots": list(slots), "coverage": [],
                                      "maps": {"org": [], "flows": [], "stores": [], "decisions": [], "catalog": []}, "noise": []})


def label(key: str, lang: str = "fr") -> str:
    return i18n.t(f"slots.{key}", lang)


ORDER_FACTS = {
    "trigger": "Mail de la boulangerie sur commandes@brioche-four",
    "access": "Boîte partagée Outlook, accès donné par le prestataire informatique",
    "tool": "ERP Sage 100, export CSV possible",
    "volume": "Environ 40 commandes par semaine, pic au printemps",
    "actors": "Claire saisit, le magasin prépare",
    "processing": "Recopier modèles, quantités et adresse dans l'ERP",
    "distribution": "Confirmation à la boulangerie par mail",
    "baseline": "10 minutes de saisie par commande, deux erreurs par semaine",
    "target": "Saisie en moins d'une minute d'ici trois mois",
}


# --- the schema ---------------------------------------------------------------------------------


def test_every_slot_has_a_rung_and_a_label_in_every_locale():
    assert set(SLOT_RUNG) == set(SLOT_KEYS)
    for lang in i18n.available():
        cat = i18n.flatten(i18n.catalog(lang))
        for k in SLOT_KEYS:
            for key in (f"slots.{k}", f"plan.fields.{k}", f"plan.slotQuestion.{k}"):
                assert cat.get(key), f"{lang}: missing {key}"
        assert len({cat[f"slots.{k}"] for k in SLOT_KEYS}) == len(SLOT_KEYS)  # labels are distinct


def test_questions_on_a_use_case_get_its_slots_as_ce_qui_manque():
    st = slots_state()
    q = st.get("orders-walkthrough")
    assert q.use_case == "bakery-orders" and q.missing_slots() == list(SLOT_KEYS)  # default: all nine
    assert sorted(q.slot_map.values()) == sorted(SLOT_KEYS)
    for item, key in q.slot_map.items():
        assert item == label(key) and item in q.expect[SLOT_RUNG[key]]
        assert q.expect_text(item, "en") == label(key, "en")  # shown in the interview language
    partial = st.get("logistics-delivery")  # `slots:` narrows what this question needs
    assert sorted(partial.missing_slots()) == sorted(["trigger", "actors", "tool", "volume", "distribution", "baseline"])
    plain = st.get("capacity-limit")  # no use case: the expect ladder as before, plus the slots it feeds
    assert plain.slot_map == {} and plain.missing_slots() == []
    assert plain.gathers == ["tool", "volume", "actors", "processing", "baseline", "target"]


def test_questionnaire_rejects_unknown_slots_and_use_cases(tmp_path):
    base = (CLIENTS / SLOTS_CLIENT / "questionnaire.yaml").read_text(encoding="utf-8")
    bad_slot = tmp_path / "a.yaml"
    bad_slot.write_text(base.replace("slots: [trigger, actors, tool, volume, distribution, baseline]",
                                     "slots: [trigger, budget]"), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown slots"):
        load_questionnaire(bad_slot)
    bad_uc = tmp_path / "b.yaml"
    bad_uc.write_text(base.replace("use_case: warranty-claims", "use_case: nope"), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown use case"):
        load_questionnaire(bad_uc)


# --- sufficiency ----------------------------------------------------------------------------------


def test_answer_with_missing_slots_is_not_sufficient_and_names_them():
    st = slots_state()
    q = st.get("orders-walkthrough")
    filled = {k: ORDER_FACTS[k] for k in ("trigger", "tool", "volume", "actors", "baseline", "target")}
    rep = st.apply_sync_result(result(
        slots=[fill("bakery-orders", k, v) for k, v in filled.items()],
        question_updates=[upd(q.id, "answered", "Commandes par mail, saisies dans Sage.", rung="done",
                              covered=[label(k) for k in SLOT_KEYS])]))  # "covered" alone never fills a slot
    assert q.status == "partial" and not q.sufficient()
    assert q.missing_slots() == ["access", "processing", "distribution"]
    assert q.missing_all() == [label("processing"), label("distribution"), label("access")]  # ladder order
    gate = next(r for r in rep.rejected if r.kind == "gate")
    assert "access" in gate.detail and "processing" in gate.detail and "distribution" in gate.detail


def test_all_required_slots_filled_is_sufficient_and_answers_it():
    st = slots_state()
    q = st.get("orders-walkthrough")
    st.apply_sync_result(result(slots=[fill("bakery-orders", k, v) for k, v in ORDER_FACTS.items()],
                                question_updates=[upd(q.id, "partial", "Tout est décrit.", rung="implementation")]))
    assert q.sufficient() and q.missing_slots() == [] and q.status == "answered" and q.rung == "done"
    uc = st.get_use_case("bakery-orders")
    assert uc.slots["tool"].value == ORDER_FACTS["tool"] and uc.slots["tool"].by == "sync"


def test_an_empty_or_vague_value_fills_nothing_and_values_only_grow():
    st = slots_state()
    q = st.get("orders-walkthrough")
    st.apply_fast_result(fast(slots=[fill("bakery-orders", "tool", "  ")]))
    assert "tool" in q.missing_slots()
    st.apply_fast_result(fast(slots=[fill("bakery-orders", "tool", "ERP Sage", "on saisit dans Sage")]))
    st.apply_fast_result(fast(slots=[fill("bakery-orders", "tool", "", "")]))
    assert st.get_use_case("bakery-orders").slots["tool"].value == "ERP Sage" and "tool" not in q.missing_slots()


def test_fast_pass_fills_slots_and_the_view_shows_what_is_still_missing(tmp_path):
    s = Session(client=slots_client(), session_dir=tmp_path / "s", engine=OfflineEngine(),
                source_factory=lambda d: ReplaySource({}),
                settings=Settings(sync_interval=3600, first_sync_delay=3600, poll_interval=3600, fast_enabled=False))
    st = s.state
    st.advance("test")  # capacity-limit -> next upcoming
    while st.focus_id != "orders-walkthrough":
        st.advance("test")
    q = st.current_question
    assert q.rung == "business"
    assert s.view()["current"]["missing"] == [label("baseline"), label("target")]
    assert s.view()["current"]["use_case"] == {"id": "bakery-orders", "missing": list(SLOT_KEYS),
                                               "title": "Saisie des commandes des boulangeries"}
    st.apply_fast_result(fast(upd(q.id, "partial", "Dix minutes par commande.", rung="business"),
                              slots=[fill("bakery-orders", "baseline", ORDER_FACTS["baseline"])]))
    cur = s.view()["current"]
    assert cur["missing"] == [label("target")] and label("baseline") in cur["covered"]
    st.apply_fast_result(fast(upd(q.id, "partial", "", piece="Objectif : une minute.", rung="business"),
                              slots=[fill("bakery-orders", "target", ORDER_FACTS["target"])]))
    cur = s.view()["current"]
    assert cur["rung"] == "workflow"  # business slots filled: the ladder climbs to the workflow slots
    assert cur["missing"] == [label("trigger"), label("actors"), label("processing"), label("distribution")]


def test_slots_are_shared_by_the_questions_of_a_use_case():
    st = slots_state()
    st.apply_sync_result(result(slots=[fill("delivery-tracking", k, ORDER_FACTS[k])
                                       for k in ("trigger", "actors", "tool")]))
    q = st.get("logistics-delivery")
    assert q.missing_slots() == ["volume", "distribution", "baseline"]  # canonical slot order
    st.apply_sync_result(result(queue_edits=[edit("add", "delivery-calls", "import", "Combien d'appels de suivi ?",
                                                  "How many tracking calls?", use_case="delivery-tracking")]))
    added = st.get("delivery-calls")
    assert added.use_case == "delivery-tracking" and len(added.slot_map) == len(SLOT_KEYS)
    assert set(added.missing_slots()) == set(SLOT_KEYS) - {"trigger", "actors", "tool"}  # starts with what is known


def test_a_new_use_case_found_live_gets_its_own_slot_set():
    st = slots_state()
    rep = st.apply_sync_result(result(queue_edits=[edit(
        "add", "freight-quotes", "import", "Racontez-moi la dernière recherche de transporteur.",
        "Walk me through the last freight forwarder search.", use_case="freight-quotes",
        use_case_title="Comparaison des devis de fret")]))
    uc = st.get_use_case("freight-quotes")
    assert uc is not None and uc.origin == "added" and uc.title("fr") == "Comparaison des devis de fret"
    assert uc.missing() == list(SLOT_KEYS)
    q = st.get("freight-quotes")
    assert q.use_case == "freight-quotes" and q.missing_slots() == list(SLOT_KEYS) and not q.sufficient()
    assert any(r.kind == "use_case" for r in rep.applied)
    st.apply_sync_result(result(slots=[fill("freight-quotes", "volume", "Un réassort par semaine")]))
    assert "volume" not in q.missing_slots()


def test_unknown_use_case_in_a_slot_fill_is_rejected():
    st = slots_state()
    rep = st.apply_sync_result(result(slots=[fill("nope", "tool", "Excel")]))
    assert any(r.kind == "slot" and r.target == "nope" for r in rep.rejected)


def test_state_with_slots_survives_a_save_and_load(tmp_path):
    st = slots_state()
    st.apply_sync_result(result(slots=[fill("bakery-orders", "tool", "ERP Sage")]))
    st.save(tmp_path / "state.json")
    back = SessionState.load(tmp_path / "state.json")
    assert back.get_use_case("bakery-orders").slots["tool"].value == "ERP Sage"
    assert back.get("orders-walkthrough").missing_slots() == st.get("orders-walkthrough").missing_slots()


# --- prompts ----------------------------------------------------------------------------------------


def test_prompts_describe_the_slots_and_send_what_is_missing():
    st = slots_state()
    glossary = slots_client().glossary
    for system in (build_sync_system(st, glossary), build_fast_system(st, glossary)):
        assert "integration slots" in system and "missing_slots" in system
        assert all(f"- {k} (" in system for k in SLOT_KEYS)
        assert "never guess" in system
    assert "use_case_title" in build_sync_system(st, glossary)  # new use cases get their own slot set
    while st.focus_id != "orders-walkthrough":
        st.advance("test")
    snap = build_sync_user(st, [], [], 60)
    state = json.loads(snap.split("<state>\n", 1)[1].split("\n</state>", 1)[0])
    assert state["current_question"]["use_case"] == "bakery-orders"
    assert state["current_question"]["missing_slots"] == list(SLOT_KEYS)
    assert {u["id"] for u in state["use_cases"]} >= {"bakery-orders", "warranty-claims"}
    fast_user = build_fast_user(st, [], [])
    assert '"missing_slots"' in fast_user and "<use_cases>" in fast_user
