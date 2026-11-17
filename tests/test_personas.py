"""Interviewee personas (pacenote/personas.py): the persona inferred from the title, the company's size
and the notes (the pre-study refines it, a hand pick wins); the questions re-worded and re-weighed per
persona; what the interviewee cannot answer goes to "to check with <role>" instead of being pushed; the
prompts, the lens and the plan follow. Fictional company and people only; no model call."""

from __future__ import annotations

import asyncio
import json
import shutil

import pytest
import yaml
from fastapi.testclient import TestClient

from pacenote import lens, personas
from pacenote.admin import create_admin_app
from pacenote.capture import ReplaySource
from pacenote.clients import ClientInfo, Contact
from pacenote.finalize import render_plan_md
from pacenote.llm import OfflineEngine, offline_plan_doc
from pacenote.prompts import build_fast_system, build_plan_system, build_sync_system, lens_guide, state_snapshot
from pacenote.questionnaire import load_client
from pacenote.research import RESEARCH_SYSTEM, ResearchOutput, build_research_prompt, write_outputs
from pacenote.server import create_app
from pacenote.session import Session, Settings
from pacenote.state import SessionState

from .conftest import CLIENTS, REAL_CLIENTS, fill, result
from .test_admin import RESULT

QUIET = Settings(sync_interval=3600, first_sync_delay=3600, poll_interval=3600, fast_enabled=False)


# --- inference -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("role,headcount,notes,persona", [
    ("CEO", "35", "", "executive"),
    ("PDG", "about 120 employees", "", "executive"),
    ("Directeur général", "10 à 19 salariés", "", "executive"),
    ("Founder & CEO", "6", "", "function_leader"),  # a 6-person company: close to the work
    ("Gérante", "4 salariés", "", "function_leader"),
    ("CFO", "250", "", "function_leader"),
    ("DSI", "", "", "function_leader"),
    ("Head of Operations", "40", "", "function_leader"),
    ("Responsable ADV", "8", "", "operator"),  # a department head in a company under 10 people
    ("Assistante ADV", "45", "", "operator"),
    ("Comptable", "", "", "operator"),
    ("Order desk clerk", "1 200", "", "operator"),
    ("Vice President Sales", "300", "", "function_leader"),  # not the president
    ("PDG", "60", "fait lui-même les devis", "function_leader"),  # the notes say hands-on
    ("Responsable logistique", "80", "hands-on, does it herself", "operator"),
    ("Chef pâtissier", "30", "", "executive"),  # not recognised: the default
])
def test_the_rule_infers_the_persona_from_title_size_and_notes(role, headcount, notes, persona):
    got = personas.infer(role, headcount, notes)
    assert got["persona"] == persona, got
    assert got["origin"] == "rule" and got["reason"] and 0 < got["confidence"] <= 1


def test_the_rule_says_how_sure_it_is():
    assert personas.infer("CEO", "35")["confidence"] > personas.infer("CEO", "")["confidence"]
    assert personas.infer("Chef pâtissier", "30")["confidence"] < 0.5  # unknown title: unsure, shown as such
    assert "35 people" in personas.infer("CEO", "35")["reason"]


@pytest.mark.parametrize("text,n", [("35", 35), ("about 35 employees", 35), ("20-49", 34), ("10 à 19 salariés", 14),
                                    ("50–99", 74), ("1 200", 1200), ("1,200 people", 1200), ("", None), ("n/a", None)])
def test_headcount_reads_numbers_and_ranges(text, n):
    assert personas.headcount_number(text) == n


def test_a_hand_pick_wins_then_the_pre_study_then_the_rule():
    info = ClientInfo(name="Brioche & Four", headcount="35",
                      contacts=[Contact(name="Claire", role="Assistante ADV"),
                                Contact(name="Paul", role="PDG", persona="operator"),
                                Contact(name="Inès", role="Responsable achats")])
    preplan = {"personas": [{"name": "INES", "persona": "operator", "reason": "runs purchasing alone", "confidence": 0.8}]}
    got = personas.interviewees(info, preplan)
    assert [(g["persona"], g["origin"]) for g in got] == [("operator", "rule"), ("operator", "manual"),
                                                         ("operator", "research")]
    assert got[1]["inferred"]["persona"] == "executive"  # what Auto would give back
    assert got[2]["reason"] == "runs purchasing alone" and got[2]["confidence"] == 0.8
    assert [g["index"] for g in got] == [0, 1, 2]


def test_a_contact_persona_must_be_known():
    with pytest.raises(ValueError):
        Contact(name="x", role="y", persona="boss")
    assert Contact(name="x", persona=" Operator ").persona == "operator"


# --- the client and the session ---------------------------------------------------------------------------


def _client(tmp_path, contacts, headcount="35", preplan=None):
    d = tmp_path / "clients" / "brioche-four"
    shutil.copytree(REAL_CLIENTS / "brioche-four", d)
    info = yaml.safe_load((d / "client.yaml").read_text(encoding="utf-8"))
    info |= {"headcount": headcount, "contacts": contacts}
    (d / "client.yaml").write_text(yaml.safe_dump(info, allow_unicode=True), encoding="utf-8")
    if preplan is not None:
        (d / "preplan.json").write_text(json.dumps(preplan, ensure_ascii=False), encoding="utf-8")
    return load_client(tmp_path / "clients", "brioche-four")


def _session(tmp_path, cfg):
    return Session(client=cfg, session_dir=tmp_path / "s" / "20261116-090000", engine=OfflineEngine(),
                   source_factory=lambda d: ReplaySource({}), settings=QUIET)


TEAM = [{"name": "Paul", "role": "PDG"}, {"name": "Claire", "role": "Assistante ADV", "notes": "saisit les commandes"}]


def test_the_session_starts_with_the_first_person_and_their_persona(tmp_path):
    cfg = _client(tmp_path, TEAM)
    assert [(i["role"], i["persona"]) for i in cfg.interviewees] == [("PDG", "executive"), ("Assistante ADV", "operator")]
    s = _session(tmp_path, cfg)
    assert s.state.persona == "executive" and s.state.interviewee["role"] == "PDG"
    view = s.view()
    assert view["interviewee"]["persona"] == "executive" and len(view["interviewees"]) == 2


def test_no_contact_means_the_default_executive(tmp_path):
    s = _session(tmp_path, _client(tmp_path, []))
    assert s.state.persona == "executive" and s.state.interviewee["origin"] == "default"


def test_the_operator_gets_their_own_wording_and_no_strategy_questions(tmp_path):
    cfg = _client(tmp_path, TEAM)
    s = _session(tmp_path, cfg)
    st = s.state
    exec_text = st.get("core-waited-on-you").text("en")
    assert exec_text.startswith("Last week, what sat waiting on your desk")
    asyncio.run(s.set_interviewee(index=1))
    assert st.persona == "operator"
    assert st.get("core-waited-on-you").text("en").startswith("Last week, what did you have to wait for")
    assert st.get("core-waited-on-you").text("fr").startswith("La semaine dernière, qu'avez-vous dû attendre")
    assert st.get("core-expert-time").priority == 1
    asyncio.run(s.set_interviewee(index=0))  # back to the executive: the base wording returns
    assert st.get("core-waited-on-you").text("en") == exec_text and st.get("core-expert-time").priority == 2
    assert st.get("core-last-search").priority == 3  # an executive rarely knows where the old quote was


def test_strategy_questions_are_not_asked_to_an_operator_and_their_theme_drops_out(tmp_path):
    d = tmp_path / "clients" / "brioche-four"
    cfg = _client(tmp_path, TEAM)
    info = yaml.safe_load((d / "client.yaml").read_text(encoding="utf-8")) | {"posture_override": "urgent"}
    (d / "client.yaml").write_text(yaml.safe_dump(info, allow_unicode=True), encoding="utf-8")
    cfg = load_client(tmp_path / "clients", "brioche-four")
    st = SessionState.from_questionnaire(cfg.questionnaire)
    st.set_interviewee(cfg.interviewees[0])
    assert st.get("hz-competitors").open and "horizons" in st.computed_coverage()
    st.set_interviewee(cfg.interviewees[1])
    assert st.get("hz-competitors").status == "removed" and st.get("hz-two-years").persona_skip
    assert "horizons" not in st.computed_coverage() and "horizons" not in st.coverage
    assert "hz-competitors" not in st.next_questions
    st.set_interviewee(cfg.interviewees[0])
    assert st.get("hz-competitors").open and st.get("hz-competitors").priority == 1


def test_a_skipped_by_persona_question_never_shows_in_the_plan_log(tmp_path):
    from pacenote.finalize import interview_log_md

    cfg = _client(tmp_path, TEAM)
    d = tmp_path / "clients" / "brioche-four"
    info = yaml.safe_load((d / "client.yaml").read_text(encoding="utf-8")) | {"posture_override": "pragmatic"}
    (d / "client.yaml").write_text(yaml.safe_dump(info, allow_unicode=True), encoding="utf-8")
    cfg = load_client(tmp_path / "clients", "brioche-four")
    st = SessionState.from_questionnaire(cfg.questionnaire)
    st.set_interviewee(cfg.interviewees[1])
    text = interview_log_md(st)
    assert "competitors" not in text and "concurrents" not in text


def test_the_interviewee_api_picks_overrides_and_locks_at_start(tmp_path):
    s = _session(tmp_path, _client(tmp_path, TEAM))
    http = TestClient(create_app(s))
    v = http.post("/api/interviewee", json={"index": 1}).json()
    assert v["interviewee"]["role"] == "Assistante ADV" and v["interviewee"]["persona"] == "operator"
    v = http.post("/api/interviewee", json={"persona": "function_leader"}).json()
    assert v["interviewee"]["persona"] == "function_leader" and v["interviewee"]["origin"] == "manual"
    v = http.post("/api/interviewee", json={"persona": ""}).json()  # back to the inferred one
    assert v["interviewee"]["persona"] == "operator" and v["interviewee"]["origin"] == "rule"
    assert http.post("/api/interviewee", json={"index": 7}).status_code == 400
    assert http.post("/api/interviewee", json={"persona": "boss"}).status_code == 400
    s.state.mode = "interview"
    assert http.post("/api/interviewee", json={"index": 0}).status_code == 409


# --- what the interviewee cannot answer: to check with someone else ------------------------------------------


def _slots_state(persona="executive"):
    cfg = load_client(CLIENTS, "brioche-four-slots")
    st = SessionState.from_questionnaire(cfg.questionnaire)
    st.set_interviewee({"role": "PDG", "persona": persona, "origin": "rule"})
    st.mode, st.focus_id = "interview", "orders-walkthrough"
    return st


DEFER = {"use_case": "bakery-orders", "question_id": None, "slots": ["tool", "volume"],
         "question": "Which software are the orders keyed into, and how many a day?",
         "ask": "Claire (order desk)", "evidence": "aucune idée, c'est Claire qui gère ça"}


def test_a_deferred_detail_stops_being_missing_and_is_listed(tmp_path):
    st = _slots_state()
    q = st.get("orders-walkthrough")
    assert {"tool", "volume"} <= set(q.missing_slots())
    st.apply_sync_result(result(to_check=[DEFER]))
    assert not {"tool", "volume"} & set(q.missing_slots())
    assert all(st.get(i).deferred for i in [x.id for x in st.questions if x.use_case == "bakery-orders"])
    assert personas.for_question(st) == [{"ask": "Claire (order desk)", "question": DEFER["question"]}]
    assert state_snapshot(st)["to_check"][0]["ask"] == "Claire (order desk)"  # the model never asks it again
    st.apply_sync_result(result(to_check=[DEFER]))  # the same item again: kept once
    assert len(st.to_check) == 1


def test_a_deferred_item_counts_towards_sufficiency_but_a_slot_still_fills(tmp_path):
    st = _slots_state()
    q = st.get("orders-walkthrough")
    st.apply_sync_result(result(to_check=[{**DEFER, "slots": list(q.missing_slots())}]))
    assert q.sufficient()
    st.apply_sync_result(result(slots=[fill("bakery-orders", "tool", "Sage 100", "on tape tout dans Sage")]))
    assert st.get_use_case("bakery-orders").filled("tool")  # a later answer is still captured


def test_bad_to_check_items_are_rejected():
    st = _slots_state()
    rep = st.apply_sync_result(result(to_check=[{**DEFER, "use_case": "nope"}, {**DEFER, "ask": ""}]))
    assert st.to_check == [] and len([r for r in rep.rejected if r.kind == "to_check"]) == 2


def test_no_talking_point_for_an_operator():
    st = _slots_state("executive")
    st.apply_sync_result(result(
        slots=[fill("bakery-orders", "actors", "Claire", "Claire tape les commandes"),
               fill("bakery-orders", "processing", "ressaisie dans Excel", "elle retape tout")],
        value_framings=[{"use_case": "bakery-orders", "person": "Claire", "task": "ressaisir les commandes",
                         "point": "Ressaisir chaque commande, ça peut se faire tout seul.", "moves_up_to": "les grands comptes",
                         "reason": "chiffre donné", "timing": "now"}]))
    assert lens.framing_for(st) is not None
    st.interviewee["persona"] = "operator"
    assert lens.framing_for(st) is None


# --- prompts ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("persona,phrase", [
    ("executive", "Do not dig into execution details they cannot know"),
    ("function_leader", "Mix the outcomes and how the process runs"),
    ("operator", "Do not ask about strategy, company goals, budgets or competitors"),
])
def test_the_prompts_say_who_is_interviewed_and_how_deep_to_go(persona, phrase):
    st = _slots_state(persona)
    sync, fast, plan = build_sync_system(st, "", ""), build_fast_system(st, "", ""), build_plan_system(st, "")
    assert phrase in sync and phrase in fast
    assert "# Who you interview" in sync and "record it in to_check" in sync and "- to_check:" in sync
    assert "the full analysis records who would know" in fast and "to_check (" not in fast
    assert "# Who was interviewed" in plan and "state.to_check" in plan
    article = "an" if persona == "executive" else "a"
    assert f"interviewing the PDG ({article} " in sync
    for text in (sync, fast, plan):
        assert "the CEO of " not in text and "CEOs answer" not in text


def test_the_lens_is_off_for_an_operator_and_metrics_follow_the_persona():
    assert "value_framings: always an empty list" in lens_guide(_slots_state("operator"))
    assert "how many per day, how long per item" in lens_guide(_slots_state("operator"))
    ex = lens_guide(_slots_state("executive"))
    assert "write ONE short talking point" in ex and "ask for the figure they follow" in ex


def test_the_prompts_carry_the_reason_but_never_need_the_name():
    st = _slots_state()
    st.interviewee |= {"reason": "PDG of a 35-person company"}
    assert "(Why this persona: PDG of a 35-person company.)" in build_sync_system(st, "", "")


# --- the plan ---------------------------------------------------------------------------------------------------


def test_the_plan_says_who_was_interviewed_by_role_and_what_to_check_with_whom():
    st = _slots_state("executive")
    st.interviewee |= {"name": "Paul", "role": "PDG"}
    st.apply_sync_result(result(slots=[fill("bakery-orders", "actors", "Claire", "Claire tape les commandes")], to_check=[
        DEFER, {**DEFER, "slots": [], "question": "How many orders come in by phone?"},
        {**DEFER, "use_case": None, "slots": [], "question": "Who grants access to Sage?", "ask": "IT provider"}]))
    md = render_plan_md(offline_plan_doc(st), st)
    assert "**Personne interrogée** : PDG — Dirigeant" in md
    assert "Paul" not in md  # the plan is shared: role and persona, never the name
    section = md[md.index("## À vérifier avec d'autres personnes"):md.index("## Points à confirmer")]
    assert section.index("Avec Claire (order desk)") < section.index("Avec IT provider")
    assert "    - How many orders come in by phone? _(Saisie des commandes des boulangeries)_" in section
    assert "    - Who grants access to Sage?\n" in section
    rows = md[md.index("## Points à confirmer"):]  # the deferred slots are asked of the person named
    assert "Outil | Quel logiciel" in rows and "| Claire (order desk) |" in rows


def test_no_to_check_section_when_everything_was_answered():
    st = _slots_state()
    md = render_plan_md(offline_plan_doc(st), st)
    assert "À vérifier avec d'autres personnes" not in md


def test_the_english_plan_and_the_speaker_labels():
    from pacenote.finalize import interview_log_md

    st = _slots_state("operator")
    st.language = "en"
    st.interviewee |= {"role": "Order desk assistant"}
    st.apply_sync_result(result(clean_passages=[{"start": "00:10", "speaker": "client", "lang": "en", "text": "Hello."}],
                                to_check=[DEFER]))
    md = render_plan_md(offline_plan_doc(st), st)
    assert "**Interviewed**: Order desk assistant — Hands-on operator" in md
    assert "## To check with other people" in md and "With Claire (order desk)" in md
    assert "**Interviewee**" in interview_log_md(st) and "CEO" not in interview_log_md(st)


# --- admin and research -------------------------------------------------------------------------------------------


def test_the_admin_saves_notes_and_a_persona_and_shows_why(tmp_path):
    _client(tmp_path, [])
    http = TestClient(create_admin_app(tmp_path / "clients"))
    d = http.get("/admin/api/clients/brioche-four").json()
    info = {**d["info"], "headcount": "35", "contacts": [
        {"name": "Paul", "role": "PDG"}, {"name": "Claire", "role": "Assistante ADV", "notes": "saisit les commandes"},
        {"name": "Inès", "role": "Responsable achats", "persona": "operator"}]}
    d = http.put("/admin/api/clients/brioche-four", json=info).json()
    assert [(p["persona"], p["origin"]) for p in d["personas"]] == [
        ("executive", "rule"), ("operator", "rule"), ("operator", "manual")]
    assert d["personas"][2]["inferred"]["persona"] == "function_leader"
    assert d["info"]["contacts"][1]["notes"] == "saisit les commandes" and d["persona_options"] == list(personas.PERSONAS)
    bad = {**info, "contacts": [{"name": "Paul", "role": "PDG", "persona": "boss"}]}
    assert http.put("/admin/api/clients/brioche-four", json=bad).status_code == 422


def test_the_research_guesses_each_persona_and_keeps_it(tmp_path):
    info = ClientInfo(name="Brioche & Four", contacts=[Contact(name="Paul", role="PDG"),
                                                       Contact(name="Claire", role="ADV", notes="saisit tout")])
    prompt = build_research_prompt(info, "", [])
    assert "Paul (PDG); Claire (ADV) — notes: saisit tout" in prompt
    assert "- personas: one per person listed" in RESEARCH_SYSTEM and "the CEO of a" not in RESEARCH_SYSTEM
    out = ResearchOutput.model_validate({**RESULT, "personas": [
        {"name": "Paul", "persona": "executive", "reason": "PDG, 35 salariés", "confidence": 0.9}]})
    write_outputs(tmp_path, out, "m")
    meta = json.loads((tmp_path / "preplan.json").read_text(encoding="utf-8"))
    assert meta["personas"] == [{"name": "Paul", "persona": "executive", "reason": "PDG, 35 salariés", "confidence": 0.9}]
    assert personas.research_guess(meta, "paul")["origin"] == "research"
