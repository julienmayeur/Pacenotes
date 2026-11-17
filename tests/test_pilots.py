"""Pilots and the sponsor: pilots come only from what the interview says (none, a few people, or a group
described as keen, never invented names); nobody guesses a number in advance. The leader in the meeting is
always the sponsor of the first wave, leading by example, alongside any pilots; an operator alone in the
meeting is never the sponsor (to be confirmed with the leadership). The copilot finds out, indirectly, what
the leader would do themself. Fictional people and companies only."""

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

from .conftest import CLIENTS, REAL_CLIENTS, plan_doc, plan_use_case, result, fill
from .test_admin import RESULT
from .test_phases import maps, org

BOSS = {"name": "Hélène Varenne", "role": "PDG", "persona": "executive", "origin": "rule"}
CFO = {"name": "Paul Ardent", "role": "DAF", "persona": "function_leader", "origin": "rule"}
CLERK = {"name": "Léa Morin", "role": "assistante ADV", "persona": "operator", "origin": "rule"}


def _state(people=(BOSS,), lang: str = "fr") -> SessionState:
    st = SessionState.from_questionnaire(load_client(CLIENTS, "brioche-four-slots").questionnaire)
    st.language = lang
    st.posture = "pragmatic"
    st.set_attendees([dict(p) for p in people])
    return st


def _keen(st: SessionState, *people: tuple[str, str, str]) -> None:
    """The interviewee described these people or groups (name, role, quote) as keen to try new tools first."""
    st.apply_sync_result(result(maps=maps(org=[org(rid=re.sub(r"\W+", "-", n.lower()), person=n, role=r,
                                                   adoption_signal=q, evidence=q) for n, r, q in people])))


def _plan(st: SessionState, **adoption) -> str:
    doc = parse_plan_doc(plan_doc(plan_use_case("orders", "Saisie des commandes", horizon="h1"),
                                  adoption={"lighthouse": "orders", **adoption}))
    return render_plan_md(apply_horizons(doc, st), st)


def _adoption(md: str) -> str:
    """The adoption section alone (up to the next heading)."""
    part = md[md.index("## Plan d'adoption" if "## Plan d'adoption" in md else "## Adoption plan"):]
    end = part.find("\n## ", 3)
    return part if end < 0 else part[:end]


# --- the plan: the leader always sponsors, alongside any pilots ----------------------------------------------


def test_several_pilots_are_named_with_their_quotes_and_the_boss_sponsors_alongside():
    st = _state()
    _keen(st, ("Claire", "assistante ADV", "elle essaie toujours en premier"),
          ("Marc", "magasinier", "c'est lui qui a montré le scanner aux autres"))
    st.apply_sync_result(result(slots=[fill("bakery-orders", "actors", "Marc", "Marc saisit aussi")]))
    doc = apply_horizons(parse_plan_doc(plan_doc(plan_use_case("bakery-orders", "Saisie des commandes", horizon="h1"),
                                                 adoption={"lighthouse": "bakery-orders"})), st)
    assert [p.person for p in doc.adoption.pilots] == ["Marc", "Claire"]  # the lighthouse's people first
    assert doc.adoption.sponsor == "PDG"  # the boss is always involved, pilots or not
    md = _adoption(render_plan_md(doc, st))
    assert "**Sponsor** : PDG, qui montre l'exemple : l'utilise d'abord sur son propre travail" in md
    assert "**Pilotes** :\n\n- Marc — « c'est lui qui a montré le scanner aux autres »\n- Claire — « elle essaie" in md
    assert md.index("**Sponsor**") < md.index("**Pilotes**") and "propres chiffres à l'équipe.\n\n**Pilotes**" in md
    assert "1. **Les volontaires (semaines 1 à 4)** — Le sponsor (PDG) l'utilise d'abord sur son propre travail, " \
           "avec les pilotes" in md
    assert "Varenne" not in md and "Hélène" not in md  # the plan names the role, never the person


def test_no_count_is_guessed_in_advance():
    st = _state()
    md = _adoption(_plan(st, pilots=[{"person": "Claire", "evidence": "partante", "group": False}]))
    for leak in ("en général", "compte", "usually", "à attendre", "taille"):
        assert leak not in md, leak
    assert "Expected pilots" not in build_sync_system(st, "", "") and "expected_pilots" not in state_snapshot(st)
    assert "expected number" not in build_plan_system(st, "")
    assert not hasattr(pilots, "expected") and not hasattr(pilots, "hint_line")


def test_the_model_pilots_win_and_labels_are_dropped_and_the_sponsor_stays():
    st = _state()
    md = _adoption(_plan(st, pilots=[{"person": "Claire", "evidence": "partante", "group": False},
                                     {"person": "Marc", "evidence": "plutôt réticent", "group": False},
                                     {"person": "claire", "evidence": "doublon", "group": False}],
                         sponsor="quelqu'un d'autre", sponsor_plan="prépare son point du lundi avec"))
    assert "**Pilotes** : Claire — « partante »" in md and "Marc" not in md and "doublon" not in md
    assert "**Sponsor** : PDG, qui montre l'exemple : prépare son point du lundi avec" in md  # always the lead


def test_a_group_described_as_keen_is_recorded_as_a_group_without_invented_names():
    st = _state()
    md = _adoption(_plan(st, pilots=[{"person": "toute l'équipe ventes", "evidence": "ils sont tous partants",
                                      "group": True}]))
    assert "**Pilotes** : toute l'équipe ventes (en groupe) — « ils sont tous partants »" in md
    # without the model: the org map's group with its quote, flagged as a group
    st2 = _state(lang="en")
    _keen(st2, ("the whole sales team", "", "they all want to try it"))
    doc = apply_horizons(parse_plan_doc(plan_doc(plan_use_case("orders", "Orders", horizon="h1"),
                                                 adoption={"lighthouse": "orders"})), st2)
    assert [(p.person, p.group) for p in doc.adoption.pilots] == [("the whole sales team", True)]
    assert "**Pilots**: the whole sales team (as a group) — « they all want to try it »" in render_plan_md(doc, st2)
    assert ambassadors.is_group("toute l'équipe ventes") and not ambassadors.is_group("chef d'équipe")


@pytest.mark.parametrize("lang, sponsor, wave", [
    ("fr", "**Sponsor** : PDG, qui montre l'exemple : l'utilise d'abord sur son propre travail",
     "1. **Les volontaires (semaines 1 à 4)** — Le sponsor (PDG) ouvre la voie sur son propre travail"),
    ("en", "**Sponsor**: PDG, leading by example: uses it first on their own work",
     "1. **The volunteers (weeks 1 to 4)** — The sponsor (PDG) opens the way on their own work"),
])
def test_no_pilot_the_boss_sponsors_by_example(lang, sponsor, wave):
    st = _state(lang=lang)
    adoption = _adoption(_plan(st))
    assert sponsor in adoption and wave in adoption


def test_the_model_sponsor_plan_is_kept_and_a_function_leader_can_sponsor_too():
    st = _state((CFO,))
    doc = apply_horizons(parse_plan_doc(plan_doc(plan_use_case("orders", "Commandes", horizon="h1"), adoption={
        "lighthouse": "orders", "sponsor": "quelqu'un d'autre",
        "sponsor_plan": "prépare son point trésorerie du lundi avec, puis le montre au comité"})), st)
    assert doc.adoption.sponsor == "DAF"
    md = _adoption(render_plan_md(doc, st))
    assert "**Sponsor** : DAF, qui montre l'exemple : prépare son point trésorerie du lundi avec" in md


def test_the_most_senior_leader_in_the_meeting_sponsors():
    st = _state((CLERK, CFO, BOSS))
    assert pilots.sponsor(st)["role"] == "PDG" and st.persona == "executive"
    assert "**Sponsor** : PDG, qui montre l'exemple" in _adoption(_plan(st))
    assert pilots.sponsor(_state((CLERK, CFO)))["role"] == "DAF"


def test_an_operator_alone_is_never_the_sponsor_it_is_to_be_confirmed_with_the_leadership():
    st = _state((CLERK,))
    md = _adoption(_plan(st, sponsor="assistante ADV", sponsor_plan="montre l'exemple"))
    assert "**Sponsor** : à confirmer avec la direction." in md and "assistante ADV" not in md.split("**Pilotes**")[0]
    assert "**Pilotes** : **À confirmer** : Qui, dans l'équipe, aurait envie de l'essayer en premier ?" in md
    assert "1. **Les volontaires (semaines 1 à 4)** — Les pilotes et les collègues partants" in md
    plan = " ".join(build_plan_system(st, "").split())
    assert "the sponsor is to be confirmed with the leadership" in plan


def test_plans_written_before_pilots_or_before_groups_still_load():
    raw = plan_doc(plan_use_case("orders", "Commandes", horizon="h1"))
    raw["adoption"] = {k: v for k, v in raw["adoption"].items() if k not in ("pilots", "sponsor", "sponsor_plan")}
    raw["adoption"] |= {"ambassador": "Claire", "evidence": "elle essaie tout"}
    a = parse_plan_doc(json.loads(json.dumps(raw))).adoption
    assert [(p.person, p.evidence, p.group) for p in a.pilots] == [("Claire", "elle essaie tout", False)]
    raw = plan_doc(plan_use_case("orders", "Commandes", horizon="h1"),
                   adoption={"pilots": [{"person": "Marc", "evidence": "partant"}]})  # no `group`
    assert parse_plan_doc(raw).adoption.pilots[0].group is False


# --- the live sponsor question: what the leader would do themself --------------------------------------------


def _probe(question: str, timing: str = "now", reason: str = "ce qu'il ferait lui-même") -> SponsorProbe:
    return SponsorProbe(question=question, reason=reason, timing=timing)


def test_the_sponsor_question_is_indirect_and_only_when_a_leader_attends():
    st = _state()
    for bad in ("Seriez-vous l'ambassadeur du projet ?", "Vous testeriez l'IA vous-même ?",
                "On pourrait évaluer votre implication ?"):
        st.apply_sync_result(result(sponsor_probe=_probe(bad)))
        assert st.sponsor_probe == {}
    ok = "Quand vous avez changé les tournées l'an dernier, vous l'aviez d'abord fait vous-même ?"
    st.apply_sync_result(result(sponsor_probe=_probe(ok)))
    assert st.sponsor_probe["question"] == ok
    clerk = _state((CLERK,))
    clerk.apply_sync_result(result(sponsor_probe=_probe(ok)))
    assert clerk.sponsor_probe == {}
    assert _state((CLERK, BOSS)).apply_sync_result(result(sponsor_probe=_probe(ok))) is not None


def test_the_sponsor_question_shows_once_and_stays_when_a_pilot_is_named():
    st = _state((CLERK, BOSS))
    q1, q2 = "orders-walkthrough", "stock-walkthrough"
    st.apply_sync_result(result(sponsor_probe=_probe("Dans votre propre semaine, qu'est-ce qui vous prend du temps ?")))
    s = ambassadors.prompt_for(st, q1)
    assert s["kind"] == "sponsor" and s["person"] == "PDG" and s["use_case"] == "" and s["source"] == "model"
    _keen(st, ("Claire", "assistante ADV", "elle essaie toujours en premier"))
    assert ambassadors.prompt_for(st, q1)["kind"] == "sponsor"  # pilots or not, the boss is involved
    ambassadors.mark_shown(st, q1, st.get(q1).use_case, s)
    assert ambassadors.prompt_for(st, q2) is None  # once
    st.apply_sync_result(result(sponsor_probe=_probe("Une autre formulation ?")))  # never reworded once asked
    assert st.sponsor_probe["question"].startswith("Dans votre propre semaine")


def test_later_and_done_hold_it_back():
    st = _state()
    st.apply_sync_result(result(sponsor_probe=_probe("Ça part souvent de vous ?", timing="later")))
    assert ambassadors.prompt_for(st, "orders-walkthrough") is None
    st.apply_sync_result(result(sponsor_probe=_probe("", timing="done")))
    assert st.sponsor_probe["timing"] == "done" and ambassadors.prompt_for(st, "orders-walkthrough") is None


@pytest.mark.parametrize("posture", ["explorer", "pragmatic", "urgent"])
def test_without_a_model_the_template_waits_for_the_deep_dives(posture):
    st = _state()
    st.posture = posture
    st.interviewee_read = {"rapport": "established"}
    early = st.get("capacity-limit")
    early.phase = "signal-flow"  # this fixture only has deep dives: one question moved earlier for the check
    assert ambassadors.prompt_for(st, early.id) is None  # before the deep dives
    s = ambassadors.prompt_for(st, "orders-walkthrough")
    assert s["kind"] == "sponsor" and s["source"] == "template"
    assert s["question"] == load_locale("fr")["focus"]["sponsorProbe"][posture]


@pytest.mark.parametrize("lang", ["en", "fr"])
def test_the_template_sponsor_questions_follow_the_wording_rules(lang):
    for text in load_locale(lang)["focus"]["sponsorProbe"].values():
        assert ambassadors.probe_problem(text) == "", text


def test_the_console_lists_the_sponsor_question_with_its_reason():
    st = _state()
    st.apply_sync_result(result(sponsor_probe=_probe("Ça part souvent de vous ?", reason="ce qu'il ferait")))
    view = ambassadors.probes_view(st)
    assert view[-1] == {"use_case": "", "kind": "sponsor", "person": "PDG", "question": "Ça part souvent de vous ?",
                        "reason": "ce qu'il ferait", "timing": "now", "asked": False}


# --- the prompts ------------------------------------------------------------------------------------------


def test_the_live_prompt_learns_pilots_from_the_answers_and_always_involves_the_leader():
    st = _state()
    text = " ".join(build_sync_system(st, "", "").split())
    for phrase in ("# Potential pilots", "Nobody knows in advance how many there are",
                   "Infer it only from what is said", "recorded as that group, never with invented names",
                   "do not stop at the first one", "- sponsor_probe:", "they sponsor the first wave whatever the pilots",
                   'Never "would you be the ambassador / the sponsor"'):
        assert phrase in text, phrase
    assert state_snapshot(st)["sponsor_probe"] is None


def test_the_plan_prompt_names_every_pilot_or_group_and_the_leader_as_sponsor():
    st = _state((CFO, CLERK))
    text = " ".join(build_plan_system(st, "", "").split())
    for phrase in ("- adoption.pilots: everyone the interview named as keen to go first, only as heard",
                   "group = true; never invent names inside it", "never invent pilots",
                   "the app sets it to DAF (the lead in the meeting), alongside any pilots",
                   "what they will do themself in wave 1, as found out in the interview", "leading by example"):
        assert phrase in text, phrase
    assert "ambassador:" not in text and "Expected pilots" not in text


def test_the_research_no_longer_asks_for_a_workforce_signal():
    from pacenote import horizons
    from pacenote.research import RESEARCH_SYSTEM, ResearchOutput

    system = " ".join(RESEARCH_SYSTEM.format(lang_name="French", horizons=horizons.HORIZON_GUIDE).split())
    assert "workforce" not in system.lower() and "âge" not in system
    assert "workforce" not in json.dumps(ResearchOutput.model_json_schema())


# --- the admin: no expected count; an old pre-plan with a workforce signal still loads -----------------------


def test_an_old_preplan_with_a_workforce_signal_still_loads_and_no_hint_is_shown(tmp_path):
    shutil.copytree(REAL_CLIENTS / "brioche-four", tmp_path / "brioche-four")
    d = tmp_path / "brioche-four"
    meta = {k: RESULT[k] for k in ("summary", "use_cases", "seed_questions", "sources", "prestudy")}
    meta["prestudy"] = {**RESULT["prestudy"], "workforce": "young", "workforce_note": "Programme d'alternance [3]"}
    (d / "preplan.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    info = yaml.safe_load((d / "client.yaml").read_text(encoding="utf-8")) or {}
    (d / "client.yaml").write_text(yaml.safe_dump({**info, "headcount": "35"}, allow_unicode=True), encoding="utf-8")
    with TestClient(create_admin_app(tmp_path)) as http:
        got = http.get("/admin/api/clients/brioche-four").json()
    assert "pilots_hint" not in got and got["prestudy"]["posture"]
    cfg = load_client(tmp_path, "brioche-four")  # the tailored questionnaire loads too
    assert cfg.questionnaire.posture and "pilots_hint" not in cfg.questionnaire.model_dump()


def test_an_old_session_state_with_a_pilots_hint_still_loads(tmp_path):
    st = _state()
    raw = json.loads(st.model_dump_json()) | {"pilots_hint": {"low": 0, "high": 2, "leader_likely": True}}
    raw.pop("attendees")
    (tmp_path / "state.json").write_text(json.dumps(raw), encoding="utf-8")
    old = SessionState.load(tmp_path / "state.json")
    assert old.persona == "executive" and pilots.sponsor(old)["role"] == "PDG"  # its one interviewee attends


@pytest.mark.parametrize("lang", ["en", "fr"])
def test_every_new_string_exists_in_both_languages_and_the_old_ones_are_gone(lang):
    loc = load_locale(lang)
    other = load_locale("fr" if lang == "en" else "en")
    for path in ("plan.fields.pilots", "plan.fields.sponsor", "plan.adoption.sponsorLine", "plan.adoption.sponsorTbc",
                 "plan.adoption.group", "plan.adoption.default.volunteersSponsor",
                 "plan.adoption.default.volunteersSponsorAlone", "console.sponsorProbe", "console.sponsorTemplateWhy"):
        a, b = loc, other
        for k in path.split("."):
            a, b = a[k], b[k]
        assert a and b and (a != b or re.fullmatch(r"[A-Za-z ]+", a)), path
    assert "hint" not in loc["plan"]["adoption"]
    assert not any(k.startswith("pilots") or k.startswith("workforce") for k in loc["admin"]["prestudy"])
