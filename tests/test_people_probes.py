"""People questions: finding out, from the CEO, whether the staff behind a workflow could carry a first
pilot, without ever asking it head-on. The live sync keeps a light read of the interviewee and writes,
per workflow, an indirect question phrased for that person, in the tone the sector's pace sets; the app
times it (workflow walked through; rapport established in a laggard sector), shows the question under
the follow-up and keeps the reason (the ambassador angle) in the interviewer console. Nothing of it
reaches the plan. Model replies come from an offline fixture (tests/fixtures/sync/people-probes.json)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from pacenote import ambassadors, i18n
from pacenote.capture import ReplaySource
from pacenote.contract import FastResult, SyncResult, parse_sync_result
from pacenote.llm import OfflineEngine
from pacenote.prompts import (build_fast_system, build_plan_user, build_sync_system, build_sync_user,
                              people_guide, state_snapshot)
from pacenote.questionnaire import load_client
from pacenote.session import Session, Settings
from pacenote.state import SessionState

from .conftest import CLIENTS, fill, result
from .test_phases import maps, org
from .test_questions import jargon

FIXTURE = Path(__file__).parent / "fixtures" / "sync" / "people-probes.json"
QUIET = Settings(sync_interval=3600, first_sync_delay=3600, poll_interval=3600, fast_enabled=False)
REASON = "savoir si Claire est la référence outils de l'ADV : pilote des commandes"


def _state(posture: str | None = None) -> SessionState:
    st = SessionState.from_questionnaire(load_client(CLIENTS, "brioche-four-slots").questionnaire)
    st.posture = posture
    return st


def _replies() -> list[SyncResult]:
    return [parse_sync_result(x) for x in json.loads(FIXTURE.read_text(encoding="utf-8"))]


def _probe(use_case="bakery-orders", person="Claire", question="Quand Sage coince, c'est vers Claire qu'on se tourne ?",
           reason="Claire, référence outils ?", timing="now") -> dict:
    return {"use_case": use_case, "person": person, "question": question, "reason": reason, "timing": timing}


def _read(**kw) -> dict:
    return {"role": "", "ai_stance": "unknown", "worries": "", "pride": "", "politics": "", "tone": "",
            "rapport": "building", "approach": "", **kw}


def _walked(st: SessionState) -> None:
    """The orders workflow was walked through: Claire re-keys every order."""
    st.apply_sync_result(result(maps=maps(org=[org(rid="claire", person="Claire", role="ADV", evidence="Claire à l'ADV")]),
                                slots=[fill("bakery-orders", "actors", "Claire", "c'est Claire qui saisit")]))


# --- the offline fixture, played sync after sync -------------------------------------------------------------


def test_the_fixture_replies_are_contract_valid():
    assert len(_replies()) == 3


def test_a_probe_waits_for_the_walkthrough_and_the_rapport_then_shows_once_and_closes():
    st = _state("explorer")
    s1, s2, s3 = _replies()
    st.focus_id = "orders-walkthrough"

    rep = st.apply_sync_result(s1)
    assert st.interviewee_read["ai_stance"] == "wary" and st.interviewee_read["rapport"] == "building"
    assert st.ambassador_probes["bakery-orders"]["timing"] == "later"
    assert ambassadors.prompt_for(st) is None  # held for later, workflow not walked through, no rapport
    assert any(e.kind == "probe" for e in rep.applied)

    rep = st.apply_sync_result(s2)
    shown = ambassadors.prompt_for(st)
    assert shown == {"person": "Claire", "use_case": st.get_use_case("bakery-orders").title("fr"),
                     "question": "Quand Excel ou Sage coince à l'ADV, c'est vers Claire que les autres se tournent ?",
                     "reason": REASON, "source": "model"}
    assert st.interviewee_read["tone"] == "chaleureux, prudent"  # an empty field keeps the earlier read
    assert st.interviewee_read["rapport"] == "established"
    bad = [e for e in rep.rejected if e.kind == "probe"]
    assert [e.target for e in bad] == ["stock-requests"] and bad[0].detail.startswith("wording:")
    assert "stock-requests" not in st.ambassador_probes

    st.advance()  # the focus leaves the question with the suggestion on screen: asked
    assert st.ambassador_probes["bakery-orders"]["asked"] is True
    st.apply_sync_result(s3)
    assert st.ambassador_probes["bakery-orders"]["timing"] == "done"
    assert st.interviewee_read["ai_stance"] == "curious" and st.interviewee_read["rapport"] == "established"
    st.focus_id = "orders-walkthrough"
    assert ambassadors.prompt_for(st) is None  # closed: no template either
    assert ambassadors.probes_view(st) == []


def test_the_interview_view_shows_the_question_and_keeps_the_reason_for_the_console(tmp_path):
    s = Session(client=load_client(CLIENTS, "brioche-four-slots"), session_dir=tmp_path / "s", engine=OfflineEngine(),
                source_factory=lambda d: ReplaySource({}), settings=QUIET)
    s1, s2, _ = _replies()
    s.state.apply_sync_result(s1)
    s.state.apply_sync_result(s2)
    s.state.mode, s.state.focus_id = "interview", "orders-walkthrough"
    view = s.view()
    assert view["current"]["ambassador"]["question"].startswith("Quand Excel ou Sage coince")
    assert view["probes"] == [{"use_case": s.state.get_use_case("bakery-orders").title("fr"), "person": "Claire",
                               "question": "Quand Excel ou Sage coince à l'ADV, c'est vers Claire que les autres se tournent ?",
                               "reason": REASON, "timing": "now", "asked": False}]
    assert view["read"] == {"tone": "chaleureux, prudent",
                            "approach": "passer par les habitudes et l'entraide, jamais par le changement",
                            "rapport": "established"}


# --- timing and tone -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("posture,expected", [
    (None, "Quand l'équipe adopte une astuce Excel ou un nouvel outil, ça vient plutôt de Claire, ou de quelqu'un d'autre ?"),
    ("pragmatic", "Quand l'équipe adopte une astuce Excel ou un nouvel outil, ça vient plutôt de Claire, ou de quelqu'un d'autre ?"),
    ("urgent", "Si on essayait une façon plus rapide de faire ça, est-ce que ça tenterait Claire de l'essayer en premier ?"),
])
def test_without_a_model_probe_an_indirect_template_in_the_sector_tone(posture, expected):
    st = _state(posture)
    _walked(st)
    st.focus_id = "orders-walkthrough"
    got = ambassadors.prompt_for(st)
    assert got["question"] == expected and got["source"] == "template" and got["reason"] == ""


def test_a_laggard_sector_waits_for_an_established_rapport():
    st = _state("explorer")
    _walked(st)
    st.focus_id = "orders-walkthrough"
    assert ambassadors.prompt_for(st) is None
    st.apply_sync_result(result(interviewee_read=_read(rapport="established")))
    assert ambassadors.prompt_for(st)["question"].startswith("Quand Excel ou le logiciel coince")
    st.apply_sync_result(result(interviewee_read=_read(rapport="building")))
    assert st.interviewee_read["rapport"] == "established"  # rapport only grows


def test_nothing_shows_before_the_workflow_is_walked_through():
    st = _state("urgent")
    st.apply_sync_result(result(ambassador_probes=[_probe()], interviewee_read=_read(rapport="established")))
    st.focus_id = "orders-walkthrough"
    assert ambassadors.prompt_for(st) is None  # no workflow slot filled yet
    st.apply_sync_result(result(slots=[fill("bakery-orders", "trigger", "emails des boulangeries")]))
    assert ambassadors.prompt_for(st)["source"] == "model"


def test_later_holds_back_the_template_too_and_a_probe_is_shown_under_one_question_only():
    st = _state()
    _walked(st)
    st.apply_sync_result(result(ambassador_probes=[_probe(timing="later")]))
    st.focus_id = "orders-walkthrough"
    assert ambassadors.prompt_for(st) is None
    st.apply_sync_result(result(ambassador_probes=[_probe(timing="now")]))
    assert ambassadors.prompt_for(st)["source"] == "model"
    st.advance()
    assert ambassadors.prompt_for(st, "orders-walkthrough")["person"] == "Claire"  # same question: still there
    rep = st.apply_sync_result(result(ambassador_probes=[_probe(question="Et Claire, elle range comment ses fichiers ?")]))
    assert [e.detail for e in rep.rejected if e.kind == "probe"] == ["already asked"]


def test_a_stale_or_unknown_probe_is_rejected():
    st = _state()
    st.apply_sync_result(result(ambassador_probes=[_probe()]), read=10)
    rep = st.apply_sync_result(result(ambassador_probes=[_probe(question="Autre formulation ?"), _probe(use_case="nope")]),
                               read=5)
    assert sorted(e.kind for e in rep.rejected) == ["probe", "stale"]
    assert st.ambassador_probes["bakery-orders"]["question"].startswith("Quand Sage coince")


def test_a_stale_read_is_ignored():
    st = _state()
    st.apply_sync_result(result(interviewee_read=_read(tone="direct")), read=10)
    st.apply_sync_result(result(interviewee_read=_read(tone="guarded")), read=4)
    assert st.interviewee_read["tone"] == "direct"


@pytest.mark.parametrize("text", [
    "Claire, elle serait réticente ?", "Is Marc reluctant to change?", "Who would resist it?",
    "Comment évaluez-vous Claire sur les outils ?", "Could Claire be our ambassador?", "Claire aimerait l'IA ?",
    "Would Claire like an AI assistant?", "Un outil qui automatise ça plairait à Claire ?", "Who is your champion?",
])
def test_people_question_wording_never_judges_or_names_the_purpose(text):
    assert ambassadors.probe_problem(text)


@pytest.mark.parametrize("text", [
    "Quand Excel coince, vers qui les gens se tournent ?", "How did the team handle the last change of software?",
    "Who brings the Excel tricks on the order desk?", "Qu'est-ce que Claire ferait d'une heure de plus par jour ?",
])
def test_indirect_questions_pass(text):
    assert ambassadors.probe_problem(text) == ""


def test_the_templates_exist_in_english_and_french_and_follow_the_rules():
    for lang in ("en", "fr"):
        tr = i18n.Translator(lang)
        for tone in ambassadors.TONES:
            text = tr(f"focus.probe.{tone}", person="Claire")
            assert "Claire" in text and text.endswith("?") and "{{" not in text
            assert ambassadors.probe_problem(text) == "" and jargon(text) == []
        assert "Claire" in tr("console.probeTemplateWhy", person="Claire")
        assert tr("console.probes") and tr("console.readLine", tone="a", approach="b")


# --- prompt construction -------------------------------------------------------------------------------------


def test_the_sync_prompt_reads_the_interviewee_and_never_asks_head_on():
    st = _state()
    text = build_sync_system(st, "", "")
    assert "# Reading the interviewee" in text and "# People questions" in text
    assert "Never head-on, never like an assessment of people" in text
    assert "(no pre-study: the default tone)" in text
    assert "interviewee_read and ambassador_probes" in text
    fast = build_fast_system(st, "", "")
    assert "# People questions" not in fast and "Never ask the interviewee head-on" in fast


@pytest.mark.parametrize("posture,phrase", [
    ("explorer", "softest. The sector has barely taken the turn"),
    ("pragmatic", "indirect, through curiosity about how the team works"),
    ("urgent", "more direct. The sector already has results"),
])
def test_the_sector_pace_sets_the_tone_of_the_people_questions(posture, phrase):
    st = _state(posture)
    assert phrase in people_guide(st) and phrase in build_sync_system(st, "", "")
    assert "default tone" not in people_guide(st)


def test_the_running_read_and_the_probes_go_back_to_the_model_but_never_to_the_plan():
    st = _state()
    s1, s2, _ = _replies()
    st.apply_sync_result(s1)
    st.apply_sync_result(s2)
    snap = state_snapshot(st)
    assert snap["interviewee_read"]["pride"].startswith("son ADV")
    assert snap["ambassador_probes"] == [{"use_case": "bakery-orders", "person": "Claire",
                                          "question": "Quand Excel ou Sage coince à l'ADV, c'est vers Claire que les autres se tournent ?",
                                          "timing": "now", "asked": False}]
    user = build_sync_user(st, [], [], 300)
    assert "jamais par le changement" in user and "Quand Excel ou Sage coince" in user
    plan = build_plan_user(st, [], None)
    for secret in ("interviewee_read", "ambassador_probes", "people_questions_shown_for", "jamais par le changement",
                   "chaleureux, prudent", "Quand Excel ou Sage coince", REASON):
        assert secret not in plan, secret
    assert "interviewee_read" not in state_snapshot(st, for_plan=True)


def test_the_schema_asks_the_full_sync_only():
    required = set(SyncResult.model_json_schema()["required"])
    assert {"interviewee_read", "ambassador_probes"} <= required
    assert not {"interviewee_read", "ambassador_probes"} & set(FastResult.model_fields)
    probe = SyncResult.model_json_schema()["$defs"]["AmbassadorProbe"]
    assert set(probe["required"]) == {"use_case", "person", "question", "reason", "timing"}
    assert re.search(r"now.*later.*done", json.dumps(probe["properties"]["timing"]))
