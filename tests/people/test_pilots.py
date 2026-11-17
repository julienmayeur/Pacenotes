"""Potential pilots: the engine notices when a use case maps to a named person or role, the
screen suggests an indirect people question once (test_people_probes.py), and a light label with the
CEO's words is kept locally (clients/<slug>/ambassadors.json, never in git), editable in the admin:
the engine fills, a hand edit wins, a removed person never comes back."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from pacenote.people import pilots
from pacenote.web.admin import create_admin_app
from pacenote.transcription.capture import ReplaySource
from pacenote.interview.preparation import load_core_themes
from pacenote.copilot.contract import parse_plan_doc
from pacenote.copilot.engines import OfflineEngine
from pacenote.copilot.prompts import build_fast_system, build_sync_system, state_snapshot
from pacenote.interview.preparation import load_client
from pacenote.interview.session import Session, Settings
from pacenote.interview.state import SessionState

from tests.conftest import CLIENTS, REAL_CLIENTS, ROOT, fill, result
from tests.web.test_admin import RESULT
from tests.interview.test_phases import maps, org

QUIET = Settings(sync_interval=3600, first_sync_delay=3600, poll_interval=3600, fast_enabled=False)


def _state() -> SessionState:
    return SessionState.from_questionnaire(load_client(CLIENTS, "brioche-four-slots").questionnaire)


def _heard(st: SessionState, *, signal: str = "", actors: str = "Claire, à l'ADV", evidence="c'est Claire qui saisit") -> None:
    """The CEO named Claire (org map) and put her on the bakery orders (actors slot)."""
    st.apply_sync_result(result(
        maps=maps(org=[org(rid="claire", person="Claire", role="assistante ADV", adoption_signal=signal,
                           evidence="Claire gère l'ADV")]),
        slots=[fill("bakery-orders", "actors", actors, evidence)]))


def _focus(st: SessionState, qid: str) -> None:
    st.focus_id = qid


# --- detection ----------------------------------------------------------------------------------------


def test_a_use_case_maps_to_the_person_its_actors_slot_names():
    st = _state()
    _heard(st)
    links = pilots.links(st)
    assert links == [{"person": "Claire", "role": "assistante ADV", "use_case": "bakery-orders",
                      "title": st.get_use_case("bakery-orders").title("fr"), "quote": "c'est Claire qui saisit"}]


def test_a_role_or_an_answer_also_maps_and_a_short_actors_value_is_a_person_itself():
    st = _state()
    st.apply_sync_result(result(maps=maps(org=[org(rid="adv", person="", role="assistante ADV", evidence="l'ADV")]),
                                slots=[fill("bakery-orders", "actors", "l'assistante ADV saisit", "l'assistante ADV")]))
    assert [x["person"] for x in pilots.links(st)] == ["assistante ADV"]
    st2 = _state()
    st2.apply_sync_result(result(maps=maps(org=[org(rid="marc", person="Marc", evidence="Marc au dépôt")])))
    st2.get("stock-walkthrough").answer = "Marc regarde le stock dans Excel."
    assert [(x["person"], x["use_case"], x["quote"]) for x in pilots.links(st2)] == [
        ("Marc", "stock-requests", "Marc au dépôt")]
    st3 = _state()
    st3.apply_sync_result(result(slots=[fill("bakery-orders", "actors", "le magasinier", "le magasinier")]))
    assert [x["person"] for x in pilots.links(st3)] == ["le magasinier"]
    st4 = _state()  # a sentence is not a person
    st4.apply_sync_result(result(slots=[fill("bakery-orders", "actors", "Claire saisit, Marc valide ensuite")]))
    assert pilots.links(st4) == []


def test_names_match_whole_words_without_accents():
    assert pilots._mentions("Hélène", "c'est helene qui fait")
    assert not pilots._mentions("Ann", "Annie fait les commandes")
    assert not pilots._mentions("Al", "Al fait")  # too short to be trusted


# --- the question on screen ----------------------------------------------------------------------------


def test_the_screen_suggests_asking_about_the_person_on_the_use_case_question():
    st = _state()
    _heard(st)
    _focus(st, "stock-walkthrough")
    assert pilots.prompt_for(st) is None  # another use case
    _focus(st, "capacity-limit")
    assert pilots.prompt_for(st) is None  # no use case
    _focus(st, "orders-walkthrough")
    assert pilots.prompt_for(st) == {  # no model probe yet: the indirect template, pragmatic tone
        "person": "Claire", "use_case": st.get_use_case("bakery-orders").title("fr"),
        "question": "Quand l'équipe adopte une astuce Excel ou un nouvel outil, ça vient plutôt de Claire, "
                    "ou de quelqu'un d'autre ?", "reason": "", "source": "template"}


def test_the_suggestion_is_made_once_per_person():
    st = _state()
    _heard(st)
    st.apply_sync_result(result(slots=[fill("weekly-reporting", "actors", "Claire", "Claire fait le reporting")]))
    _focus(st, "orders-walkthrough")
    assert pilots.prompt_for(st)["person"] == "Claire"
    st.advance()  # the focus leaves the question: recorded
    assert st.ambassador_prompted == {"orders-walkthrough": "Claire"}
    _focus(st, "data-reporting")
    assert pilots.prompt_for(st) is None  # Claire was already suggested
    assert pilots.prompt_for(st, "orders-walkthrough")["person"] == "Claire"  # still the same there


def test_the_interview_view_carries_the_suggestion_and_the_console_list(tmp_path):
    s = Session(client=load_client(CLIENTS, "brioche-four-slots"), session_dir=tmp_path / "s", engine=OfflineEngine(),
                source_factory=lambda d: ReplaySource({}), settings=QUIET)
    _heard(s.state, signal="elle essaie toujours les nouveautés en premier")
    s.state.mode = "interview"
    _focus(s.state, "orders-walkthrough")
    view = s.view()
    assert view["current"]["ambassador"]["person"] == "Claire"
    assert view["ambassadors"] == [{"id": "claire", "person": "Claire", "role": "assistante ADV",
                                    "use_case": s.state.get_use_case("bakery-orders").title("fr"),
                                    "quote": "elle essaie toujours les nouveautés en premier", "keen": True}]


def test_the_ceo_words_about_a_person_win_over_the_use_case_link():
    st = _state()
    _heard(st)
    assert pilots.candidates(st)[0]["quote"] == "c'est Claire qui saisit"
    assert pilots.for_use_case(st, "bakery-orders")["quote"] == "c'est Claire qui saisit"
    _heard(st, signal="les autres l'écoutent quand elle dit que ça marche")
    cands = pilots.candidates(st)
    assert len(cands) == 1 and cands[0]["quote"] == "les autres l'écoutent quand elle dit que ça marche"
    assert pilots.for_use_case(st, "bakery-orders")["quote"].startswith("les autres l'écoutent")
    assert pilots.for_use_case(st, "stock-requests") is None


def test_prompts_ask_for_the_adoption_signal_and_show_the_people_heard():
    st = _state()
    for build in (build_sync_system, build_fast_system):
        text = build(st, "", "")
        assert "# Potential pilots" in text and "`adoption_signal`" in text
        assert "never record reluctance, criticism or personal judgments" in text
    _heard(st)
    assert state_snapshot(st)["potential_ambassadors"] == [
        {"person": "Claire", "role": "assistante ADV", "use_case": st.get_use_case("bakery-orders").title("fr"),
         "quote": "c'est Claire qui saisit", "keen": False}]


def test_the_core_question_asks_who_tries_new_tools_first_and_who_others_listen_to():
    q = next(q for t in load_core_themes() for q in t.questions if q.id == "core-first-to-try")
    st = SessionState.from_questionnaire(load_client(REAL_CLIENTS, "brioche-four", use_preplan=False).questionnaire)
    assert st.get("core-first-to-try").phase == "signal-carriers"
    assert "en premier" in q.fr and "écoutent" in q.fr and "listen" in q.en


# --- the local file: the engine fills, a hand edit wins ----------------------------------------------------


def test_record_merges_and_never_overwrites_a_hand_edit_or_brings_back_a_removed_person(tmp_path):
    st = _state()
    assert pilots.record(tmp_path, st)["entries"] == [] and not (tmp_path / "ambassadors.json").exists()
    _heard(st)
    data = pilots.record(tmp_path, st, "s1")
    assert [(e["id"], e["by"], e["label"], e["session"]) for e in data["entries"]] == [("claire", "agent", "potential", "s1")]
    edited = pilots.save_edit(tmp_path, [{**data["entries"][0], "quote": "volontaire pour tester"}], [])
    assert edited["entries"][0]["by"] == "manual"
    _heard(st, signal="elle essaie tout")
    again = pilots.record(tmp_path, st, "s2")
    assert again["entries"][0]["quote"] == "volontaire pour tester"  # the hand edit wins
    pilots.save_edit(tmp_path, [], ["claire"])
    assert pilots.record(tmp_path, st, "s3") == {"entries": [], "removed": ["claire"]}


def test_save_edit_rules(tmp_path):
    st = _state()
    st.apply_sync_result(result(maps=maps(org=[org(rid="claire", person="Claire", adoption_signal="essaie tout"),
                                               org(rid="marc", person="Marc", adoption_signal="toujours partant")])))
    data = pilots.record(tmp_path, st)
    assert {e["id"] for e in data["entries"]} == {"claire", "marc"}
    claire = next(e for e in data["entries"] if e["id"] == "claire")
    out = pilots.save_edit(tmp_path, [claire, {"id": "", "person": "Sophie", "role": "compta", "quote": "motivée"}], [])
    assert [(e["id"], e["by"]) for e in out["entries"]] == [("claire", "agent"), ("sophie", "manual")]
    assert out["removed"] == ["marc"]  # an agent entry dropped from the list is remembered
    cleared = pilots.save_edit(tmp_path, [{**out["entries"][0], "person": "  "}, out["entries"][1]], [])
    assert [e["id"] for e in cleared["entries"]] == ["sophie"] and "claire" not in cleared["removed"]
    with pytest.raises(ValueError):
        pilots.save_edit(tmp_path, [{"person": "Léa"}, {"person": "Lea"}], [])
    assert json.loads((tmp_path / "ambassadors.json").read_text())["entries"][0]["person"] == "Sophie"


def test_the_file_never_goes_to_git():
    for path in ("clients/acme/ambassadors.json", "clients.example/brioche-four/ambassadors.json",
                 "tests/fixtures/clients/brioche-four/ambassadors.json"):
        out = subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT)
        assert out.returncode == 0, path


def test_finalize_records_the_ambassadors_and_the_plan_names_one(tmp_path):
    shutil.copytree(CLIENTS / "brioche-four-slots", tmp_path / "clients" / "brioche-four-slots")
    s = Session(client=load_client(tmp_path / "clients", "brioche-four-slots"), session_dir=tmp_path / "s" / "x",
                engine=OfflineEngine(), source_factory=lambda d: ReplaySource({}), settings=QUIET)
    _heard(s.state, signal="elle essaie toujours en premier")
    s.state.apply_sync_result(result(slots=[fill("bakery-orders", k, v) for k, v in (
        ("tool", "Sage"), ("volume", "40 par semaine"), ("trigger", "boîte commandes"), ("baseline", "3 h par jour"))]))
    asyncio.run(s.finalize())
    saved = json.loads((tmp_path / "clients" / "brioche-four-slots" / "ambassadors.json").read_text(encoding="utf-8"))
    assert [(e["person"], e["quote"], e["session"]) for e in saved["entries"]] == [
        ("Claire", "elle essaie toujours en premier", "x")]
    doc = parse_plan_doc(json.loads((tmp_path / "s" / "x" / "plan.json").read_text(encoding="utf-8")))
    assert doc.adoption.lighthouse == "bakery-orders" and [p.person for p in doc.adoption.pilots] == ["Claire"]
    assert doc.adoption.sponsor == ""
    md = (tmp_path / "s" / "x" / "plan.md").read_text(encoding="utf-8")
    assert "**Pilotes** : Claire — « elle essaie toujours en premier »" in md


# --- the admin: list, edit, remove; the pre-study and the pace ------------------------------------------------


@pytest.fixture
def admin(tmp_path):
    shutil.copytree(REAL_CLIENTS / "brioche-four", tmp_path / "brioche-four")
    d = tmp_path / "brioche-four"
    meta = {k: RESULT[k] for k in ("summary", "use_cases", "seed_questions", "sources", "prestudy")}
    meta["prestudy"] = {**RESULT["prestudy"], "competitors": [  # a copy: RESULT is shared by other tests
        *RESULT["prestudy"]["competitors"], {"competitor": "Panidis", "signal": "chatbot", "result": "", "url": "", "date": ""}]}
    (d / "preplan.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    (d / "ambassadors.json").write_text(json.dumps({"entries": [
        {"id": "claire", "person": "Claire", "role": "ADV", "use_case": "Commandes", "quote": "elle essaie tout",
         "label": "potential", "by": "agent", "session": "s1", "updated_at": "2026-11-15T10:00:00"}],
        "removed": []}), encoding="utf-8")
    with TestClient(create_admin_app(tmp_path)) as http:
        yield http, d


def test_admin_shows_the_prestudy_and_the_ambassadors(admin):
    http, _ = admin
    d = http.get("/admin/api/clients/brioche-four").json()
    pre = d["prestudy"]
    assert pre["posture"] == "pragmatic" and pre["origin"] == "prestudy" and pre["score"]["total"] == 6
    assert [c["counted"] for c in pre["prestudy"]["competitors"]] == [True, False]
    assert d["use_cases"][0]["horizon"] == "h1"
    assert d["ambassadors"]["entries"][0]["person"] == "Claire"


def test_admin_posture_override_wins_and_can_be_given_back(admin):
    http, d = admin
    out = http.put("/admin/api/clients/brioche-four/posture", json={"posture": "urgent"}).json()
    assert out["prestudy"]["posture"] == "urgent" and out["prestudy"]["origin"] == "manual"
    assert "posture_override: urgent" in (d / "client.yaml").read_text(encoding="utf-8")
    assert load_client(d.parent, "brioche-four").questionnaire.posture == "urgent"
    assert http.put("/admin/api/clients/brioche-four/posture", json={"posture": "panic"}).status_code == 400
    back = http.put("/admin/api/clients/brioche-four/posture", json={"posture": ""}).json()
    assert back["prestudy"]["posture"] == "pragmatic" and back["prestudy"]["origin"] == "prestudy"


def test_admin_edits_the_ambassadors_with_the_hand_edit_rules(admin):
    http, d = admin
    entry = http.get("/admin/api/clients/brioche-four").json()["ambassadors"]["entries"][0]
    out = http.put("/admin/api/clients/brioche-four/ambassadors",
                   json={"entries": [{**entry, "quote": "volontaire"}], "removed": []}).json()
    assert out["entries"][0]["by"] == "manual" and out["entries"][0]["quote"] == "volontaire"
    gone = http.put("/admin/api/clients/brioche-four/ambassadors", json={"entries": [], "removed": ["claire"]}).json()
    assert gone == {"entries": [], "removed": ["claire"]}
    bad = http.put("/admin/api/clients/brioche-four/ambassadors",
                   json={"entries": [{"person": "Léa"}, {"person": "Lea"}], "removed": []})
    assert bad.status_code == 400 and "duplicate" in bad.json()["detail"]
    assert http.put("/admin/api/clients/nope/ambassadors", json={"entries": [], "removed": []}).status_code == 404
