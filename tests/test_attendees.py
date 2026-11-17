"""Who is in the meeting (pacenote/attendees.py): every person on the client sheet attends, each with a
persona; the live sync keeps each attendee's stance toward AI (driver, open, hesitant, opposed, unknown) with
its evidence and confidence, only from lines it can attribute (the transcript has no speakers); the card can
suggest addressing a question to a specific attendee. Stances stay in the session data and the console, never
in the plan. Fictional company and people only; no model call."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from pacenote import attendees
from pacenote.capture import ReplaySource
from pacenote.contract import AskTo, AttendeeStance
from pacenote.finalize import render_plan_md
from pacenote.llm import OfflineEngine, offline_plan_doc
from pacenote.prompts import build_fast_system, build_plan_system, build_sync_system, state_snapshot
from pacenote.questionnaire import load_client
from pacenote.server import create_app
from pacenote.session import Session
from pacenote.state import SessionState

from .conftest import CLIENTS, result, upd
from .test_personas import QUIET, _client

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


def stance(who, label, quote, conf=0.7, how="self") -> dict:
    return {"attendee": who, "stance": label, "evidence": quote, "confidence": conf, "attributed": how}


# --- the meeting: everyone on the sheet attends ----------------------------------------------------------------


def test_everyone_on_the_sheet_attends_with_a_persona_and_an_unknown_stance(tmp_path):
    s = _session(tmp_path)
    people = s.state.attendees
    assert [(a["key"], a["role"], a["persona"]) for a in people] == [
        ("a1", "PDG", "executive"), ("a2", "Directeur commercial", "function_leader"),
        ("a3", "Assistante ADV", "operator"), ("a4", "Magasinier", "operator")]
    assert all(a["stance"]["label"] == "unknown" for a in people)
    assert s.state.persona == "executive" and s.state.interviewee["role"] == "PDG"  # the lead sets the questions
    view = s.view()
    assert [a["lead"] for a in view["attendees"]] == [True, False, False, False]
    assert view["attendees"][2]["prep"].startswith("Questions étape par étape")  # prep note from the persona
    assert all("inconnue avant la réunion" in a["prep"] for a in view["attendees"])


def test_the_single_interviewee_picker_is_gone(tmp_path):
    http = TestClient(create_app(_session(tmp_path)))
    assert http.post("/api/interviewee", json={"index": 1}).status_code in (404, 405)


def test_an_operator_alone_sets_operator_questions_and_a_lead_brings_the_executive_ones_back():
    st = _state([CLERK])
    assert st.persona == "operator" and attendees.lead(st.attendees) is None
    st.set_attendees([CLERK, EXEC])
    assert st.persona == "executive" and attendees.lead(st.attendees)["role"] == "PDG"


# --- the stance: discovered, attributed, never invented ---------------------------------------------------------


def test_stances_are_recorded_per_attendee_with_their_evidence():
    st = _state([EXEC, HEAD, CLERK])
    st.apply_sync_result(result(attendee_stances=[
        stance("a1", "driver", "J'utilise déjà un assistant pour mes comptes rendus", 0.8, "self"),
        stance("Paul Ardent", "opposed", "Mes commerciaux n'ont pas besoin de ça", 0.6, "named"),
        stance("a3", "hesitant", "Pour la saisie, je ne vois pas trop", 0.5, "addressed")]), read=10)
    got = {a["key"]: a["stance"] for a in st.attendees}
    assert got["a1"]["label"] == "driver" and got["a1"]["evidence"] == ["J'utilise déjà un assistant pour mes comptes rendus"]
    assert got["a2"]["label"] == "opposed" and got["a2"]["attributed"] == "named"  # found by name
    assert got["a3"]["label"] == "hesitant" and got["a3"]["confidence"] == 0.5
    # a later read refines it and keeps the evidence (newest first, at most three)
    st.apply_sync_result(result(attendee_stances=[stance("a3", "open", "Si ça m'évite de ressaisir, pourquoi pas")]),
                         read=20)
    a3 = st.attendees[2]["stance"]
    assert a3["label"] == "open" and a3["evidence"] == ["Si ça m'évite de ressaisir, pourquoi pas",
                                                        "Pour la saisie, je ne vois pas trop"]


def test_unattributed_unknown_or_stale_reads_are_rejected():
    st = _state([EXEC, CLERK])
    st.apply_sync_result(result(attendee_stances=[stance("a2", "open", "pourquoi pas")]), read=30)
    report = st.apply_sync_result(result(attendee_stances=[
        stance("a9", "driver", "on y va"),  # nobody by that id
        stance("Jean Dupont", "opposed", "non"),  # not in the meeting
        stance("a1", "opposed", ""),  # a stance needs the words it comes from
        stance("a2", "opposed", "jamais de la vie")]), read=12)  # older than the read kept
    assert [a["stance"]["label"] for a in st.attendees] == ["unknown", "open"]
    assert len([r for r in report.rejected if r.kind in ("stance", "stale")]) == 4
    with pytest.raises(ValueError):  # the contract only knows self, named, addressed
        AttendeeStance.model_validate(stance("a1", "driver", "x", how="guessed"))


# --- who to ask: the model's pick, else the rung ----------------------------------------------------------------


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


def test_the_interview_view_carries_who_to_ask_and_the_console_the_stances(tmp_path):
    s = _session(tmp_path)
    st = s.state
    st.mode, st.focus_id = "interview", next(q.id for q in st.questions if q.status != "removed")
    st.apply_sync_result(result(question_updates=[upd(st.focus_id, "partial", "Les commandes arrivent par mail.",
                                                      rung="implementation", follow_up="Combien par jour ?")],
                                attendee_stances=[stance("a2", "opposed", "On a toujours fait comme ça")]), read=8)
    view = s.view()
    assert view["current"]["ask_to"]["persona"] == "operator"
    paul = view["attendees"][1]
    assert paul["stance"] == "opposed" and paul["evidence"] == ["On a toujours fait comme ça"]
    s.persist()  # the stance lives in the session data (sessions/, git-ignored), never next to the client
    saved = json.loads((s.session_dir / "state.json").read_text(encoding="utf-8"))
    assert saved["attendees"][1]["stance"]["label"] == "opposed"
    assert "opposed" not in json.dumps([p.read_text(encoding="utf-8") for p in s.client.dir.rglob("*") if p.is_file()
                                        and p.suffix in (".yaml", ".json", ".md")])


# --- the prompts --------------------------------------------------------------------------------------------------


def test_the_live_prompts_know_every_attendee_and_never_invent_speakers():
    st = _state([EXEC, HEAD, CLERK])
    st.apply_sync_result(result(attendee_stances=[stance("a2", "opposed", "pas pour nous")]), read=3)
    sync = " ".join(build_sync_system(st, "", "").split())
    for phrase in ("# Who is in the meeting", "- a1: Hélène Varenne (PDG) — executive (the lead: sponsor of the first wave)",
                   "- a2: Paul Ardent (Directeur commercial) — function leader; stance so far: opposed (0.7)",
                   "never push, never argue", "never invent who said what", "otherwise the speaker is unknown",
                   "# Each attendee's stance toward AI", "a line you cannot attribute that way moves no stance",
                   "- ask_to: with two attendees or more", "interviewing the PDG (an executive) and 2 other attendees"):
        assert phrase in sync, phrase
    fast = " ".join(build_fast_system(st, "", "").split())
    assert "- a3: Léa Morin (Assistante ADV) — hands-on operator" in fast and "never invent who said what" in fast
    snap = state_snapshot(st)
    assert snap["attendees"][1] == {"id": "a2", "who": "Paul Ardent (Directeur commercial)", "persona": "function_leader",
                                    "stance": "opposed", "evidence": ["pas pour nous"]}


def test_the_plan_never_sees_or_says_a_stance_or_a_name():
    st = _state([EXEC, HEAD, CLERK])
    st.apply_sync_result(result(attendee_stances=[stance("a2", "opposed", "pas pour nous"),
                                                  stance("a3", "hesitant", "je ne sais pas")]), read=3)
    snap = state_snapshot(st, for_plan=True)
    assert snap["attendees"] == [{"role": "PDG", "persona": "executive"},
                                 {"role": "Directeur commercial", "persona": "function_leader"},
                                 {"role": "Assistante ADV", "persona": "operator"}]
    assert "opposed" not in json.dumps(snap) and "pas pour nous" not in json.dumps(snap)
    plan = " ".join(build_plan_system(st, "").split())
    assert "# Who was in the meeting" in plan and "Never write anything about how anyone feels about AI" in plan
    md = render_plan_md(offline_plan_doc(st), st)
    assert "**Dans la réunion** : PDG — Dirigeant; Directeur commercial — Responsable de fonction; " \
           "Assistante ADV — Opérationnel" in md
    for leak in ("Varenne", "Ardent", "Morin", "Opposé", "opposé", "Hésitant", "hésitant", "réticen", "pas pour nous"):
        assert leak not in md, leak


@pytest.mark.parametrize("lang", ["en", "fr"])
def test_every_attendee_string_exists_in_both_languages(lang):
    from pacenote.i18n import catalog

    loc, other = catalog(lang), catalog("fr" if lang == "en" else "en")
    for group in ("prep", "stance"):
        assert set(loc["attendees"][group]) == set(other["attendees"][group])
    assert set(loc["attendees"]["stance"]) == set(attendees.STANCES)
    for key in ("attendees", "attendeesNote", "lead", "stanceSure", "others"):
        assert loc["console"][key] and other["console"][key]
    assert loc["focus"]["askTo"] and loc["admin"]["prepLabel"]
