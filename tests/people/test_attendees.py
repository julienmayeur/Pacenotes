"""Who is in the meeting (pacenote/attendees.py): every person on the client sheet attends, each with a
persona and a prep note; the card can suggest addressing a question to a specific attendee. There is no
stance per attendee (the transcript has no speakers): the views heard are kept by statement
(test_viewpoints.py). Fictional company and people only; no model call."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pacenote.copilot.contract import AskTo
from pacenote.copilot.engines import OfflineEngine, offline_plan_doc
from pacenote.copilot.prompts import build_fast_system, build_plan_system, build_sync_system, state_snapshot
from pacenote.interview.preparation import load_client
from pacenote.interview.session import Session
from pacenote.interview.state import SessionState
from pacenote.people import attendees
from pacenote.plan.finalize import render_plan_md
from pacenote.transcription.capture import ReplaySource
from pacenote.web.server import create_app
from tests.conftest import CLIENTS, result, upd
from tests.people.test_personas import QUIET, _client

# four people, only one driving it, two against it: the meeting the copilot must read
ROOM = [{"name": "Hélène Varenne", "role": "PDG"}, {"name": "Paul Ardent", "role": "Directeur commercial"},
        {"name": "Léa Morin", "role": "Assistante ADV", "notes": "saisit les commandes"},
        {"name": "Marc Olivier", "role": "Magasinier"}]


def _session(tmp_path, contacts=ROOM) -> Session:
    return Session(client=_client(tmp_path, contacts), session_dir=tmp_path / "s" / "20261117-090000",
                   engine=OfflineEngine(), source_factory=lambda d: ReplaySource({}), settings=QUIET)


def _state(people) -> SessionState:
    st = SessionState.from_questionnaire(load_client(CLIENTS, "brioche-four-slots").questionnaire)
    st.set_attendees(people)
    st.mode, st.focus_id = "interview", "orders-walkthrough"
    return st


EXEC = {"name": "Hélène Varenne", "role": "PDG", "persona": "executive", "origin": "rule"}
HEAD = {"name": "Paul Ardent", "role": "Directeur commercial", "persona": "function_leader", "origin": "rule"}
CLERK = {"name": "Léa Morin", "role": "Assistante ADV", "persona": "operator", "origin": "rule"}


# --- the meeting: everyone on the sheet attends ----------------------------------------------------------------


def test_everyone_on_the_sheet_attends_with_a_persona_and_a_prep_note(tmp_path):
    s = _session(tmp_path)
    people = s.state.attendees
    assert [(a["key"], a["role"], a["persona"]) for a in people] == [
        ("a1", "PDG", "executive"), ("a2", "Directeur commercial", "function_leader"),
        ("a3", "Assistante ADV", "operator"), ("a4", "Magasinier", "operator")]
    assert all("stance" not in a for a in people)
    assert s.state.persona == "executive" and s.state.interviewee["role"] == "PDG"  # the lead sets the questions
    view = s.view()
    assert [a["lead"] for a in view["attendees"]] == [True, False, False, False]
    assert view["attendees"][2]["prep"].startswith("Questions étape par étape")  # prep note from the persona
    assert all("stance" not in a and "Position sur l'IA" not in a["prep"] for a in view["attendees"])


def test_the_single_interviewee_picker_is_gone(tmp_path):
    http = TestClient(create_app(_session(tmp_path)))
    assert http.post("/api/interviewee", json={"index": 1}).status_code in (404, 405)


def test_an_operator_alone_sets_operator_questions_and_a_lead_brings_the_executive_ones_back():
    st = _state([CLERK])
    assert st.persona == "operator" and attendees.lead(st.attendees) is None
    st.set_attendees([CLERK, EXEC])
    assert st.persona == "executive" and attendees.lead(st.attendees)["role"] == "PDG"


# --- who to ask: the consultant directing a question ----------------------------------------------------------


def test_the_card_suggests_who_to_ask_from_the_rung_or_the_model():
    st = _state([EXEC, HEAD, CLERK])
    q = st.get("orders-walkthrough")
    q.rung = "business"
    assert attendees.ask_to_for(st)["who"] == "Hélène Varenne (PDG)"
    q.rung = "workflow"
    assert attendees.ask_to_for(st)["persona"] == "function_leader"
    q.rung = "implementation"
    got = attendees.ask_to_for(st)
    assert got == {"key": "a3", "who": "Léa Morin (Assistante ADV)", "persona": "operator", "reason": "", "source": "rule"}
    st.apply_sync_result(result(ask_to=[AskTo(question_id="orders-walkthrough", attendee="a2",
                                              reason="suit les commandes des grands comptes")]), read=5)
    got = attendees.ask_to_for(st)
    assert got["key"] == "a2" and got["source"] == "model" and got["reason"] == "suit les commandes des grands comptes"


def test_one_attendee_nobody_else_to_ask():
    st = _state([EXEC])
    assert attendees.ask_to_for(st) is None
    report = st.apply_sync_result(result(ask_to=[AskTo(question_id="orders-walkthrough", attendee="a1", reason="x")]))
    assert st.ask_to == {} and any(r.kind == "ask_to" for r in report.rejected)


def test_the_interview_view_carries_who_to_ask(tmp_path):
    s = _session(tmp_path)
    st = s.state
    st.mode, st.focus_id = "interview", next(q.id for q in st.questions if q.status != "removed")
    st.apply_sync_result(result(question_updates=[upd(st.focus_id, "partial", "Les commandes arrivent par mail.",
                                                      rung="implementation", follow_up="Combien par jour ?")]), read=8)
    view = s.view()
    assert view["current"]["ask_to"]["persona"] == "operator"
    assert all("stance" not in a for a in view["attendees"])


# --- the prompts --------------------------------------------------------------------------------------------------


def test_the_live_prompts_know_every_attendee_and_never_invent_speakers():
    st = _state([EXEC, HEAD, CLERK])
    sync = " ".join(build_sync_system(st, "", "").split())
    for phrase in ("# Who is in the meeting", "- a1: Hélène Varenne (PDG) — executive (the lead: sponsor of the first wave)",
                   "- a2: Paul Ardent (Directeur commercial) — function leader", "never invent who said what",
                   "otherwise the speaker is unknown", "- ask_to: with two attendees or more",
                   "interviewing the PDG (an executive) and 2 other attendees"):
        assert phrase in sync, phrase
    assert "stance" not in sync.replace("ai_stance", "")
    fast = " ".join(build_fast_system(st, "", "").split())
    assert "- a3: Léa Morin (Assistante ADV) — hands-on operator" in fast and "never invent who said what" in fast
    assert state_snapshot(st)["attendees"][1] == {"id": "a2", "who": "Paul Ardent (Directeur commercial)",
                                                  "persona": "function_leader"}


def test_the_plan_lists_attendees_by_role_never_by_name():
    st = _state([EXEC, HEAD, CLERK])
    assert state_snapshot(st, for_plan=True)["attendees"] == [
        {"role": "PDG", "persona": "executive"}, {"role": "Directeur commercial", "persona": "function_leader"},
        {"role": "Assistante ADV", "persona": "operator"}]
    plan = " ".join(build_plan_system(st, "").split())
    assert "# Who was in the meeting" in plan and "Never write anything about how anyone feels about AI" in plan
    md = render_plan_md(offline_plan_doc(st), st)
    assert "**Dans la réunion** : PDG — Dirigeant; Directeur commercial — Responsable de fonction; " \
           "Assistante ADV — Opérationnel" in md
    for leak in ("Varenne", "Ardent", "Morin"):
        assert leak not in md, leak


@pytest.mark.parametrize("lang", ["en", "fr"])
def test_every_attendee_string_exists_in_both_languages(lang):
    from pacenote.shared.i18n import catalog

    loc, other = catalog(lang), catalog("fr" if lang == "en" else "en")
    assert set(loc["attendees"]["prep"]) == set(other["attendees"]["prep"])
    assert "stance" not in loc["attendees"] and "stanceSure" not in loc["console"]
    for key in ("attendees", "attendeesNote", "lead", "others"):
        assert loc["console"][key] and other["console"][key]
    assert loc["focus"]["askTo"] and loc["admin"]["prepLabel"]
