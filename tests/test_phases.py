"""Interview phases (phases.py) and maps (contract.MapsUpdate): the focus moves phase by phase, queue
edits stay inside the current phase, what is said early is still captured, the core questions are
merged into every client questionnaire (old files included), and map facts merge with versions."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from pacenote import phases as P
from pacenote.clients import load_core_themes, merge_core
from pacenote.contract import FastResult, MapsUpdate, empty_maps, parse_sync_result
from pacenote.llm import OfflineEngine
from pacenote.capture import ReplaySource
from pacenote.questionnaire import load_client, load_questionnaire
from pacenote.server import create_app
from pacenote.session import Session, Settings
from pacenote.state import SessionState

from .conftest import ROOT, SAMPLES, edit, real_client, result, upd
from .test_questions import _texts, jargon


def phased() -> SessionState:
    """The committed example client, core questions merged: wealth -> ... -> return."""
    return SessionState.from_questionnaire(real_client().questionnaire)


def maps(**kinds) -> MapsUpdate:
    return MapsUpdate.model_validate({**empty_maps().model_dump(), **kinds})


def org(rid="claire", person="Claire", role="", department="", reports_to="", time_split="", notes="",
        evidence="Claire s'occupe de l'import"):
    return {"id": rid, "person": person, "role": role, "department": department, "reports_to": reports_to,
            "time_split": time_split, "notes": notes, "evidence": evidence}


def flow(rid="price-ok", sender="Claire", receiver="PDG", what="validation de prix", wait="", ceo="waits_on_ceo",
         channel="", evidence="elle attend mon accord"):
    return {"id": rid, "sender": sender, "receiver": receiver, "what": what, "channel": channel, "cadence": "",
            "wait": wait, "who_waits": "", "confidential": "unknown", "ceo_pattern": ceo, "evidence": evidence}


def fast(*updates, **kinds) -> FastResult:
    return FastResult.model_validate({"question_updates": list(updates), "slots": [],
                                      "maps": maps(**kinds).model_dump(), "coverage": [], "noise": []})


def phase_of(st: SessionState, qid: str) -> str:
    return st.get(qid).phase


# --- structure -----------------------------------------------------------------------------------


def test_questions_are_ordered_phase_by_phase_with_budgets():
    st = phased()
    order = [P.PHASE_INDEX[q.phase] for q in st.questions]
    assert order == sorted(order)
    assert {q.phase for q in st.questions} == set(P.PHASE_IDS)  # every phase has questions
    assert st.focus_id == "capacity-limit" and st.current_phase == "wealth"
    assert abs(sum(st.phase_budgets.values()) - 15 * 60) < 1  # scaled to target_minutes
    assert st.phase_budgets["signal-flow"] == 180.0
    assert P.budgets(30)["wealth"] == 180.0  # a 30-minute interview doubles every budget
    assert P.budgets(15, {"deep-dives": 8})["deep-dives"] > P.budgets(15)["deep-dives"]


def test_core_questions_merge_into_an_old_format_questionnaire(tmp_path):
    """A questionnaire written before phases: no `phase:` anywhere, a capacity-size question the
    core question stands for. It loads; its questions get the default phases; the client's wording
    of capacity-size is kept in the signal-carriers phase and the core duplicate is dropped."""
    d = tmp_path / "old"
    d.mkdir()
    (d / "questionnaire.yaml").write_text(yaml.safe_dump({
        "client": "Old", "slug": "old", "language": "fr", "consent": {"fr": "ok", "en": "ok"},
        "use_cases": [{"id": "orders", "title": {"fr": "Commandes", "en": "Orders"}}],
        "themes": [
            {"id": "capacity", "title": {"fr": "Capacité", "en": "Capacity"}, "questions": [
                {"id": "capacity-limit", "fr": "Qui serait débordé ?", "en": "Who would be swamped?"},
                {"id": "capacity-size", "fr": "Combien de personnes ?", "en": "How many people?"}]},
            {"id": "orders", "title": {"fr": "Commandes", "en": "Orders"}, "questions": [
                {"id": "orders-walk", "use_case": "orders", "fr": "La dernière commande ?", "en": "The last order?"}]},
            {"id": "it", "title": {"fr": "SI", "en": "IT"}, "questions": [
                {"id": "it-access", "fr": "Qui ouvre les accès ?", "en": "Who grants access?"}]},
            {"id": "roi", "title": {"fr": "ROI", "en": "ROI"}, "questions": [
                {"id": "roi-budget", "fr": "Quel budget ?", "en": "Which budget?"}]},
        ]}, allow_unicode=True), encoding="utf-8")
    cfg = load_client(tmp_path, "old")
    st = SessionState.from_questionnaire(cfg.questionnaire)
    assert phase_of(st, "capacity-limit") == "wealth"
    assert phase_of(st, "capacity-size") == "signal-carriers"  # stands for core-who-comes
    assert st.get("core-who-comes") is None
    assert phase_of(st, "orders-walk") == phase_of(st, "it-access") == "deep-dives"
    assert phase_of(st, "roi-budget") == "return"
    core_ids = {q.id for t in load_core_themes() for q in t.questions} - {"core-who-comes"}
    assert core_ids <= {q.id for q in st.questions}
    # opting out keeps the file as written
    q = load_questionnaire(d / "questionnaire.yaml").model_copy(update={"core_questions": False})
    assert merge_core(q) is q


def test_local_client_folders_still_load():
    """clients/* (git-ignored, written before phases) load with the core questions merged in."""
    folders = sorted(p.name for p in (ROOT / "clients").glob("*") if (p / "questionnaire.yaml").exists())
    if not folders:
        pytest.skip("no local client folder on this machine")
    for slug in folders:
        cfg = load_client(ROOT / "clients", slug, use_preplan=False)
        st = SessionState.from_questionnaire(cfg.questionnaire)
        ids = [q.id for q in st.questions]
        assert len(ids) == len(set(ids)), slug
        assert {q.phase for q in st.questions} >= set(P.CORE_PHASES), slug


def test_core_questions_tell_stories_without_ai_vocabulary():
    for theme in load_core_themes():
        assert theme.phase in P.CORE_PHASES
        hits = []
        for where, text in _texts(type("Q", (), {"use_cases": [], "themes": [theme]})()):
            hits += [(where, text) for _ in jargon(text)]
        assert not hits, hits
        for q in theme.questions:
            assert q.fr and q.en and q.gathered_slots(), q.id
            assert "outils utilisez" not in q.fr.lower() and "tools do you use" not in q.en.lower()  # no checklist


# --- focus: phase by phase -----------------------------------------------------------------------------


def test_focus_advances_phase_by_phase_without_jumping_back():
    st = phased()
    seen = [st.current_phase]
    for _ in range(len(st.questions) - 1):  # every question once (n leaves them open)
        if st.advance("n") is None:
            break
        if st.current_phase != seen[-1]:
            seen.append(st.current_phase)
    assert seen == list(P.PHASE_IDS)
    assert [e.target for e in st.edit_log if e.kind == "phase"] == list(P.PHASE_IDS[1:])


def test_a_spent_budget_hands_over_at_the_next_move_never_mid_answer():
    st = phased()
    st.start_phase_clock(time.time() - 10 * 60)  # the wealth budget is long spent
    assert st.focus_id == "capacity-limit" and st.phase_budget_spent()  # the card on screen stays
    assert st.refresh_upcoming()[0] == "core-who-comes"  # upcoming already shows the next phase
    assert st.advance("n") == "core-who-comes"
    assert st.current_phase == "signal-carriers"
    assert st.phase_elapsed() < 5  # the new phase's clock starts now
    assert st.get("capacity-time").open  # not asked: left for the very end


def test_filled_maps_end_a_phase_once_one_question_was_asked():
    st = phased()
    while st.current_phase != "signal-carriers":
        st.advance("n")
    assert st.focus_id == "core-who-comes"
    st.apply_fast_result(fast(org=[org("claire", "Claire", "import", reports_to="PDG"),
                                   org("ines", "Inès", "commandes", reports_to="Claire"),
                                   org("marc", "Marc", "entrepôt", reports_to="PDG")]))
    assert st.phase_done("signal-carriers")
    assert st.advance("n") == "core-waited-on-you"  # core-key-people not needed any more
    assert st.get("core-key-people").open


def test_skip_phase_key_moves_to_the_next_phase(tmp_path):
    session = Session(client=real_client(), session_dir=tmp_path / "s", engine=OfflineEngine(),
                      source_factory=lambda d: ReplaySource({"fr": SAMPLES / "fr.srt"}, speed=0),
                      settings=Settings(sync_interval=3600, first_sync_delay=3600, poll_interval=3600,
                                        fast_enabled=False))
    session.state.mode = "interview"
    http = TestClient(create_app(session))
    view = http.post("/api/next-phase", json={"from": "capacity-limit"}).json()
    assert view["current"]["id"] == "core-who-comes"
    assert view["phases"]["current"]["id"] == "signal-carriers"
    assert [p["state"] for p in view["phases"]["items"]][:2] == ["done", "current"]
    stale = http.post("/api/next-phase", json={"from": "capacity-limit"}).json()  # pressed twice: ignored
    assert stale["current"]["id"] == "core-who-comes"
    assert session.state.phases_skipped == ["wealth"]
    log = (tmp_path / "s" / "sync_log.jsonl").read_text()
    assert "FDE skipped to the next phase" in log
    js = (ROOT / "pacenote" / "static" / "app.js").read_text()
    assert 'k === "p"' in js and "/api/next-phase" in js


# --- queue edits stay in the current phase ------------------------------------------------------------


def test_queue_edits_on_other_phases_are_rejected():
    st = phased()
    assert st.current_phase == "wealth"
    rep = st.apply_sync_result(result(
        queue_edits=[edit("reword", "capacity-growth", fr="Quel marché laissez-vous de côté ?", en="Which market?"),
                     edit("reword", "roi-budget", fr="Quel budget ?", en="Budget?"),
                     edit("remove", "core-handoff"),
                     edit("reorder", "orders-walkthrough", after=None),
                     edit("reorder", "capacity-growth", after="roi-budget"),
                     edit("add", "budget-now", "roi", "Combien êtes-vous prêt à mettre ?", "How much?")],
        next_questions=["capacity-growth", "roi-budget"]))
    assert st.get("capacity-growth").text_fr == "Quel marché laissez-vous de côté ?"  # same phase: applied
    assert st.get("roi-budget").text_fr.startswith("Pour chiffrer")
    assert st.get("core-handoff").status == "pending"
    rejected = {(r.kind, r.target) for r in rep.rejected}
    assert {("phase", "roi-budget"), ("phase", "core-handoff"), ("phase", "orders-walkthrough"),
            ("phase", "capacity-growth"), ("phase", "budget-now"), ("phase", "roi-budget")} <= rejected
    assert st.get("budget-now") is None
    assert st.next_questions[0] == "capacity-growth"


def test_a_workflow_heard_early_is_parked_in_the_deep_dives():
    st = phased()
    rep = st.apply_sync_result(result(queue_edits=[
        edit("add", "returns-walk", "customers", "Prenez le dernier retour de pièce : qui l'a traité ?", "The last return?",
             use_case="parts-returns", use_case_title="Retours de pièces"),
        edit("add", "capacity-who", "capacity", "Qui embaucheriez-vous en premier ?", "Who would you hire first?")]))
    assert not rep.rejected
    parked, here = st.get("returns-walk"), st.get("capacity-who")
    assert parked.phase == "deep-dives" and parked.use_case == "parts-returns"
    assert here.phase == "wealth"
    ids = [q.id for q in st.questions]
    deep = [q.id for q in st.questions if q.phase == "deep-dives"]
    assert deep[-1] == "returns-walk"  # at the end of the deep dives
    assert ids.index("capacity-who") < ids.index("core-who-comes")
    assert st.focus_id == "capacity-limit"  # the focus never jumps


def test_what_is_said_early_is_captured_without_moving_the_focus():
    st = phased()
    st.apply_fast_result(fast(
        upd("capacity-limit", "partial", "Claire serait débordée.", rung="business"),
        upd("core-waited-on-you", "partial", "Les prix attendent la validation du PDG deux jours."),
        flows=[flow(wait="deux jours")]))
    assert st.focus_id == "capacity-limit"
    later = st.get("core-waited-on-you")
    assert later.status == "partial" and later.answer and later.thread_at is not None
    rec = st.maps["flows"][0]
    assert rec.fields["ceo_pattern"] == "waits_on_ceo" and rec.fields["wait"] == "deux jours"
    assert rec.phase == "wealth"  # heard while the opening was on screen


# --- maps: contract and versioned merge ---------------------------------------------------------------


def test_maps_are_strict_in_the_contract():
    raw = result().model_dump()
    raw["maps"]["flows"] = [flow()]
    assert parse_sync_result(raw).maps.flows[0].ceo_pattern == "waits_on_ceo"
    bad = result().model_dump()
    bad["maps"]["org"] = [{**org(), "salary": "x"}]
    with pytest.raises(ValueError):
        parse_sync_result(bad)
    bad = result().model_dump()
    bad["maps"]["catalog"] = [{"id": "x", "category": "crm", "tool": "", "used_by": "", "used_for": "",
                               "source_of_truth": "yes", "access_owner": "", "evidence": ""}]
    with pytest.raises(ValueError):
        parse_sync_result(bad)  # unknown catalog category


def test_map_records_merge_field_by_field_with_versions():
    st = phased()
    st.apply_fast_result(fast(org=[org(role="import")]), read=10)
    st.apply_fast_result(fast(org=[org(role="", reports_to="PDG", evidence="")]), read=12)
    rec = st.maps["org"][0]
    assert rec.fields == {"person": "Claire", "role": "import", "reports_to": "PDG"}  # "" kept the role
    assert rec.evidence == "Claire s'occupe de l'import" and rec.read == 12
    rep = st.apply_sync_result(result(maps=maps(org=[org(role="achats")]).model_dump()), read=8)  # older read
    assert st.maps["org"][0].fields["role"] == "import"
    assert any(r.kind == "stale" and r.target == "org:claire" for r in rep.rejected)
    # no id: derived from the record's identity fields; a record with nothing said is ignored
    st.apply_fast_result(fast(flows=[flow(rid="", sender="Inès", receiver="Marc", what="bon de préparation",
                                          ceo="none")], org=[org(rid="", person="", role="")]), read=13)
    assert [r.id for r in st.maps["flows"]] == ["ines-marc-bon-de-preparation"]
    assert len(st.maps["org"]) == 1


def test_map_gaps_aim_the_phase_and_follow_the_facts():
    st = phased()
    assert st.map_gaps("signal-flow")[:2] == ["gap.waitsOnCeo", "gap.ceoStatus"]
    st.apply_fast_result(fast(flows=[flow(wait="deux jours", channel="WhatsApp")],
                              catalog=[{"id": "whatsapp", "category": "internal_messaging", "tool": "WhatsApp",
                                        "used_by": "équipe", "used_for": "prix", "source_of_truth": "no",
                                        "access_owner": "", "evidence": "sur le groupe WhatsApp"}]))
    gaps = st.map_gaps("signal-flow")
    assert "gap.waitsOnCeo" not in gaps and "gap.wait" not in gaps and "gap.ceoStatus" in gaps
    assert "catalog.internal_messaging" not in gaps and "catalog.external_channels" in gaps


def test_prompts_carry_the_phase_and_the_maps():
    from pacenote.prompts import build_fast_system, build_fast_user, build_sync_system, build_sync_user

    st = phased()
    st.apply_fast_result(fast(org=[org()]))
    system = build_sync_system(st, "", "")
    assert "# Interview phases" in system and "waits_on_ceo" in system and "internal_messaging" in system
    user = build_sync_user(st, [], [], 30)
    assert '"phase": {' in user and '"claire"' in user and '"id": "wealth"' in user
    fast_user = build_fast_user(st, [], [])
    assert "<phase>" in fast_user and "<maps>" in fast_user
    assert "map_gaps" in build_fast_system(st, "")  # the guide names them; the values ride in the message


def test_old_sessions_without_phases_still_load(tmp_path):
    st = phased()
    raw = st.model_dump()
    for q in raw["questions"]:
        q.pop("phase")
    for k in ("maps", "phase", "phase_since", "phase_spent", "phase_budgets", "phases_skipped"):
        raw.pop(k)
    old = SessionState.model_validate(raw)
    assert {q.phase for q in old.questions} == {"deep-dives"}  # one phase: the old behaviour
    assert old.advance("n") is not None
