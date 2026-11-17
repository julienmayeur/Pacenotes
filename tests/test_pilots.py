"""Pilots: the first use case can be carried by several pilots, or by none. How many to expect comes from
the company (size, the sector's pace, public workforce signals), as a light hint, never a label on a
person. When nobody is named and the person interviewed leads the company or a function, the plan names
them as the sponsor of the first wave, leading by example, and the copilot may first check, indirectly,
whether they would carry it. Fictional people and companies only."""

from __future__ import annotations

import json
import re
import shutil

import pytest
import yaml
from fastapi.testclient import TestClient

from pacenote import ambassadors, pilots
from pacenote.admin import create_admin_app
from pacenote.contract import SponsorProbe, parse_plan_doc
from pacenote.finalize import apply_horizons, render_plan_md
from pacenote.i18n import catalog as load_locale
from pacenote.prompts import build_plan_system, build_sync_system, state_snapshot
from pacenote.questionnaire import load_client
from pacenote.state import SessionState

from .conftest import CLIENTS, REAL_CLIENTS, fill, plan_doc, plan_use_case, result
from .test_admin import RESULT
from .test_phases import maps, org

BOSS = {"index": 0, "name": "Hélène Varenne", "role": "PDG", "persona": "executive"}
CFO = {"index": 0, "name": "Paul Ardent", "role": "DAF", "persona": "function_leader"}
CLERK = {"index": 0, "name": "Léa Morin", "role": "assistante ADV", "persona": "operator"}


def _state(interviewee: dict | None = BOSS, hint: dict | None = None, lang: str = "fr") -> SessionState:
    st = SessionState.from_questionnaire(load_client(CLIENTS, "brioche-four-slots").questionnaire)
    st.language = lang
    st.posture = "pragmatic"
    if interviewee is not None:
        st.set_interviewee(interviewee)
    st.pilots_hint = hint or {}
    return st


def _keen(st: SessionState, *people: tuple[str, str, str]) -> None:
    """The interviewee described these people (name, role, quote) as keen to try new tools first."""
    st.apply_sync_result(result(maps=maps(org=[org(rid=n.lower(), person=n, role=r, adoption_signal=q, evidence=q)
                                               for n, r, q in people])))


def _plan(st: SessionState, **adoption) -> str:
    doc = parse_plan_doc(plan_doc(plan_use_case("orders", "Saisie des commandes", horizon="h1"),
                                  adoption={"lighthouse": "orders", **adoption}))
    return render_plan_md(apply_horizons(doc, st), st)


def _adoption(md: str) -> str:
    return md[md.index("## Plan d'adoption" if "## Plan d'adoption" in md else "## Adoption plan"):]


# --- how many pilots to expect ----------------------------------------------------------------------


@pytest.mark.parametrize("headcount, posture, workforce, low, high, leader, factor", [
    ("6", None, None, 0, 1, True, "small_team"),
    ("35", "pragmatic", "unknown", 1, 2, False, None),
    ("35", "urgent", None, 1, 3, False, "pace_urgent"),
    ("35", "explorer", None, 0, 1, True, "pace_explorer"),
    ("20-49", None, None, 1, 2, False, None),
    ("120", None, None, 2, 4, False, None),
    ("120", "urgent", "young", 3, 6, False, "workforce_young"),
    ("120", "explorer", "mature", 1, 3, False, "workforce_mature"),
    ("800", "pragmatic", None, 3, 6, False, None),
    ("800", "explorer", None, 0, 2, True, "large_slow"),
    ("1 200 salariés", "pragmatic", "mature", 0, 2, True, "large_slow"),
    ("800", "urgent", "young", 3, 6, False, "pace_urgent"),
])
def test_expected_pilots_follow_size_and_dynamism(headcount, posture, workforce, low, high, leader, factor):
    h = pilots.expected(headcount, posture, workforce)
    assert (h["low"], h["high"], h["leader_likely"]) == (low, high, leader)
    if factor:
        assert factor in h["factors"]


def test_no_headcount_no_hint_and_the_prestudy_size_band_or_workforce_fill_in():
    assert pilots.expected("", "urgent", "young") is None
    info = type("Info", (), {"headcount": ""})()
    pre = {"prestudy": {"size_band": "250-499 salariés", "workforce": "mature"}}
    h = pilots.for_client(info, pre, "explorer")
    assert h["size"] == 374 and h["leader_likely"] and "workforce_mature" in h["factors"]
    assert pilots.for_client(info, {"prestudy": {"workforce": "teenagers"}}, None) is None  # unknown size


@pytest.mark.parametrize("lang", ["en", "fr"])
def test_the_plan_hint_is_neutral_about_the_company_and_never_about_people(lang):
    tr = _state(lang=lang).tr()
    lines = [pilots.hint_line(pilots.expected(n, p, w), named, tr) for n, p, w, named in (
        ("120", "urgent", "young", 2), ("120", None, None, 1), ("35", None, None, 0), ("800", "explorer", "mature", 0),
        ("6", None, None, 0))]
    assert all(lines) and len(set(lines)) == 5
    text = " ".join(lines).lower()
    for leak in ("sector", "secteur", "young", "jeune", "âge", "age ", "older", "mature", "slow", "lent", "retard",
                 "behind", "pace", "rythme", "reluctan", "réticen"):
        assert leak not in text, leak
    assert pilots.hint_line(None, 0, tr) == ""


def test_the_hint_reaches_the_session_from_the_client_sheet(tmp_path):
    shutil.copytree(CLIENTS / "brioche-four-slots", tmp_path / "acme")
    (tmp_path / "acme" / "client.yaml").write_text(yaml.safe_dump(
        {"name": "Fournil Atlas", "headcount": "800", "posture_override": "explorer",
         "contacts": [{"name": "Hélène Varenne", "role": "PDG"}]}, allow_unicode=True), encoding="utf-8")
    cfg = load_client(tmp_path, "acme")
    st = SessionState.from_questionnaire(cfg.questionnaire)
    assert st.pilots_hint["low"] == 0 and st.pilots_hint["leader_likely"] and st.pilots_hint["size"] == 800


# --- the plan: several pilots, or the leader as sponsor --------------------------------------------------


def test_several_pilots_are_all_named_with_their_quotes():
    st = _state(hint=pilots.expected("120", "urgent", "young"))
    _keen(st, ("Claire", "assistante ADV", "elle essaie toujours en premier"),
          ("Marc", "magasinier", "c'est lui qui a montré le scanner aux autres"))
    st.apply_sync_result(result(slots=[fill("bakery-orders", "actors", "Marc", "Marc saisit aussi")]))
    doc = parse_plan_doc(plan_doc(plan_use_case("bakery-orders", "Saisie des commandes", horizon="h1"),
                                  adoption={"lighthouse": "bakery-orders"}))
    doc = apply_horizons(doc, st)
    assert [p.person for p in doc.adoption.pilots] == ["Marc", "Claire"]  # the lighthouse's people first
    assert doc.adoption.sponsor == ""
    md = _adoption(render_plan_md(doc, st))
    assert "**Pilotes** :\n\n- Marc — « c'est lui qui a montré le scanner aux autres »\n- Claire — « elle essaie" in md
    assert "l'entretien en a cité 2" in md and "3 à 6" in md
    assert "**Sponsor**" not in md
    assert "1. **Les volontaires (semaines 1 à 4)** — Les pilotes et les collègues partants" in md


def test_the_model_pilots_win_labels_are_dropped_and_a_sponsor_never_sits_next_to_pilots():
    st = _state()
    md = _adoption(_plan(st, pilots=[{"person": "Claire", "evidence": "partante"},
                                     {"person": "Marc", "evidence": "plutôt réticent"},
                                     {"person": "claire", "evidence": "doublon"}],
                         sponsor="PDG", sponsor_plan="montre l'exemple"))
    assert "**Pilotes** : Claire — « partante »" in md and "Marc" not in md and "doublon" not in md
    assert "**Sponsor**" not in md


@pytest.mark.parametrize("lang, sponsor, wave", [
    ("fr", "**Sponsor** : PDG, qui montre l'exemple : l'utilise d'abord sur son propre travail",
     "1. **Les volontaires (semaines 1 à 4)** — Le sponsor (PDG) ouvre la voie sur son propre travail"),
    ("en", "**Sponsor**: PDG, leading by example: uses it first on their own work",
     "1. **The volunteers (weeks 1 to 4)** — The sponsor (PDG) opens the way on their own work"),
])
def test_no_pilot_in_a_large_slow_company_the_boss_sponsors_by_example(lang, sponsor, wave):
    st = _state(hint=pilots.expected("800", "explorer", "mature"), lang=lang)
    md = render_plan_md(apply_horizons(parse_plan_doc(plan_doc(
        plan_use_case("orders", "Orders", horizon="h1"), adoption={"lighthouse": "orders"})), st), st)
    adoption = _adoption(md)
    assert sponsor in adoption and wave in adoption
    assert ("le dirigeant ouvre la voie" if lang == "fr" else "the leader opens the way") in adoption
    assert "Varenne" not in md and "Hélène" not in md  # the plan names the role, never the person


def test_the_model_sponsor_plan_is_kept_and_a_function_leader_can_sponsor_too():
    st = _state(CFO)
    doc = apply_horizons(parse_plan_doc(plan_doc(plan_use_case("orders", "Commandes", horizon="h1"), adoption={
        "lighthouse": "orders", "sponsor": "quelqu'un d'autre",
        "sponsor_plan": "prépare son point trésorerie du lundi avec, puis le montre au comité"})), st)
    assert doc.adoption.sponsor == "DAF"  # always the leader interviewed, by role
    md = _adoption(render_plan_md(doc, st))
    assert "**Sponsor** : DAF, qui montre l'exemple : prépare son point trésorerie du lundi avec" in md


def test_an_operator_interviewed_sponsors_nothing_and_the_plan_asks_who_would_go_first():
    st = _state(CLERK)
    md = _adoption(_plan(st))
    assert "**Sponsor**" not in md
    assert "**Pilotes** : **À confirmer** : Qui, dans l'équipe, aurait envie de l'essayer en premier ?" in md


def test_a_plan_written_before_pilots_still_loads():
    raw = plan_doc(plan_use_case("orders", "Commandes", horizon="h1"))
    raw["adoption"] = {k: v for k, v in raw["adoption"].items() if k not in ("pilots", "sponsor", "sponsor_plan")}
    raw["adoption"] |= {"ambassador": "Claire", "evidence": "elle essaie tout"}
    a = parse_plan_doc(json.loads(json.dumps(raw))).adoption
    assert [(p.person, p.evidence) for p in a.pilots] == [("Claire", "elle essaie tout")] and a.sponsor == ""


# --- the live sponsor question: indirect, once, only while no pilot was named ------------------------------


def _probe(question: str, timing: str = "now", reason: str = "s'il ouvrirait la voie") -> SponsorProbe:
    return SponsorProbe(question=question, reason=reason, timing=timing)


def test_the_sponsor_question_is_indirect_and_only_for_a_leader():
    st = _state()
    for bad in ("Seriez-vous l'ambassadeur du projet ?", "Vous testeriez l'IA vous-même ?",
                "On pourrait évaluer votre implication ?"):
        st.apply_sync_result(result(sponsor_probe=_probe(bad)))
        assert st.sponsor_probe == {}
    ok = "Quand vous avez changé les tournées l'an dernier, vous l'aviez d'abord fait vous-même ?"
    st.apply_sync_result(result(sponsor_probe=_probe(ok)))
    assert st.sponsor_probe["question"] == ok
    clerk = _state(CLERK)
    clerk.apply_sync_result(result(sponsor_probe=_probe(ok)))
    assert clerk.sponsor_probe == {}


def test_the_sponsor_question_shows_once_and_goes_away_when_a_pilot_is_named():
    st = _state()
    q1, q2 = "orders-walkthrough", "stock-walkthrough"
    st.apply_sync_result(result(sponsor_probe=_probe("Quand quelque chose de nouveau arrive, ça part de vous ?")))
    s = ambassadors.prompt_for(st, q1)
    assert s["kind"] == "sponsor" and s["person"] == "PDG" and s["use_case"] == "" and s["source"] == "model"
    ambassadors.mark_shown(st, q1, st.get(q1).use_case, s)
    assert ambassadors.prompt_for(st, q2) is None  # once
    assert ambassadors.prompt_for(st, q1)["kind"] == "sponsor"  # back on the same question, the same one
    st.apply_sync_result(result(sponsor_probe=_probe("Une autre formulation ?")))  # never reworded once asked
    assert st.sponsor_probe["question"].startswith("Quand quelque chose")
    _keen(st, ("Claire", "assistante ADV", "elle essaie toujours en premier"))
    assert ambassadors.prompt_for(st, q1) is None  # a pilot was named: no sponsor question


def test_later_and_done_hold_it_back():
    st = _state()
    st.apply_sync_result(result(sponsor_probe=_probe("Ça part souvent de vous ?", timing="later")))
    assert ambassadors.prompt_for(st, "orders-walkthrough") is None
    st.apply_sync_result(result(sponsor_probe=_probe("", timing="done")))
    assert st.sponsor_probe["timing"] == "done" and ambassadors.prompt_for(st, "orders-walkthrough") is None


@pytest.mark.parametrize("posture", ["explorer", "pragmatic", "urgent"])
def test_without_a_model_the_template_waits_for_the_deep_dives_and_a_reason(posture):
    st = _state(hint=pilots.expected("800", "explorer", None))
    st.posture = posture
    st.interviewee_read = {"rapport": "established"}
    early = st.get("capacity-limit")
    early.phase = "signal-flow"  # this fixture only has deep dives: one question moved earlier for the check
    assert ambassadors.prompt_for(st, early.id) is None  # before the deep dives
    s = ambassadors.prompt_for(st, "orders-walkthrough")
    assert s["kind"] == "sponsor" and s["source"] == "template"
    assert s["question"] == load_locale("fr")["focus"]["sponsorProbe"][posture]
    # several pilots expected: the template waits until "who tried it first" was answered with nobody keen
    plain = SessionState.from_questionnaire(load_client(REAL_CLIENTS, "brioche-four", use_preplan=False).questionnaire)
    plain.set_interviewee(BOSS)
    plain.posture, plain.pilots_hint = "pragmatic", pilots.expected("120", None, None)
    assert ambassadors.prompt_for(plain, ambassadors.FIRST_TO_TRY) is None  # a core question: before the deep dives
    assert ambassadors.prompt_for(plain, "orders-walkthrough") is None
    plain.get(ambassadors.FIRST_TO_TRY).answer = "Personne en particulier, c'est arrivé par le siège."
    assert ambassadors.prompt_for(plain, "orders-walkthrough")["kind"] == "sponsor"


@pytest.mark.parametrize("lang", ["en", "fr"])
def test_the_template_sponsor_questions_follow_the_wording_rules(lang):
    for text in load_locale(lang)["focus"]["sponsorProbe"].values():
        assert ambassadors.probe_problem(text) == "", text


def test_the_console_lists_the_sponsor_question_with_its_reason():
    st = _state()
    st.apply_sync_result(result(sponsor_probe=_probe("Ça part souvent de vous ?", reason="ouvrirait-il la voie")))
    view = ambassadors.probes_view(st)
    assert view[-1] == {"use_case": "", "kind": "sponsor", "person": "PDG", "question": "Ça part souvent de vous ?",
                        "reason": "ouvrirait-il la voie", "timing": "now", "asked": False}


# --- the prompts ------------------------------------------------------------------------------------------


def test_the_live_prompt_expects_several_pilots_or_none_and_asks_the_sponsor_question():
    st = _state(hint=pilots.expected("800", "explorer", "mature"))
    text = " ".join(build_sync_system(st, "", "").split())
    for phrase in ("# Potential pilots", "There can be several", "Expected pilots for a company of 800 people: 0 to 2",
                   "The leader will likely have to open the way themself", "never a label on anyone",
                   "do not stop at the first one", "- sponsor_probe:", "leading by example",
                   'Never "would you be the ambassador / the sponsor"'):
        assert phrase in text, phrase
    snap = state_snapshot(st)
    assert snap["expected_pilots"].startswith("Expected pilots") and snap["sponsor_probe"] is None


def test_the_plan_prompt_names_every_pilot_or_the_leader_as_sponsor():
    st = _state(hint=pilots.expected("35", None, None))
    text = " ".join(build_plan_system(st, "", "").split())
    for phrase in ("- adoption.pilots: everyone the interview named as keen to go first (0, 1 or several)",
                   "never invent pilots to reach the expected number", "- adoption.sponsor and sponsor_plan:",
                   "sponsor = their role (never their name)", "Worded as leading by example",
                   "Expected pilots for a company of 35 people: 1 to 2"):
        assert phrase in text, phrase
    assert "ambassador:" not in text


def test_the_research_asks_for_public_workforce_signals_only():
    from pacenote import horizons
    from pacenote.research import RESEARCH_SYSTEM

    system = " ".join(RESEARCH_SYSTEM.format(lang_name="French", horizons=horizons.HORIZON_GUIDE).split())
    assert "only from public, company-level signals" in system and "Never about an individual" in system
    assert "workforce (young, mixed, mature, unknown" in system


# --- the admin -------------------------------------------------------------------------------------------


def test_the_admin_shows_the_hint_with_its_reasons(tmp_path):
    shutil.copytree(REAL_CLIENTS / "brioche-four", tmp_path / "brioche-four")
    d = tmp_path / "brioche-four"
    meta = {k: RESULT[k] for k in ("summary", "use_cases", "seed_questions", "sources", "prestudy")}
    meta["prestudy"] = {**RESULT["prestudy"], "workforce": "young", "workforce_note": "Programme d'alternance [3]"}
    (d / "preplan.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    info = yaml.safe_load((d / "client.yaml").read_text(encoding="utf-8")) or {}
    (d / "client.yaml").write_text(yaml.safe_dump({**info, "headcount": "35"}, allow_unicode=True), encoding="utf-8")
    with TestClient(create_admin_app(tmp_path)) as http:
        got = http.get("/admin/api/clients/brioche-four").json()
    h = got["pilots_hint"]
    assert (h["low"], h["high"], h["leader_likely"], h["size"]) == (1, 3, False, 35)
    assert h["factors"] == ["workforce_young"]  # the pre-study's pace here is pragmatic: no move
    assert got["prestudy"]["prestudy"]["workforce"] == "young"


@pytest.mark.parametrize("lang", ["en", "fr"])
def test_every_new_string_exists_in_both_languages(lang):
    loc = load_locale(lang)
    other = load_locale("fr" if lang == "en" else "en")
    for path in ("plan.fields.pilots", "plan.fields.sponsor", "plan.adoption.sponsorLine", "plan.adoption.hint.leader",
                 "plan.adoption.default.volunteersSponsor", "console.sponsorProbe", "console.sponsorTemplateWhy",
                 "admin.prestudy.pilots", "admin.prestudy.pilotsFactors.large_slow", "admin.prestudy.workforces.mature"):
        a, b = loc, other
        for k in path.split("."):
            a, b = a[k], b[k]
        assert a and b and (a != b or re.fullmatch(r"[A-Za-z ]+", a)), path
    assert "ambassador" not in loc["plan"]["fields"]
