"""Horizons and the pre-study posture: the score from the public signals, the posture's questions and
priorities, the horizon tag of every opportunity, the prompts, and the plan grouped in plain words.
The posture is operator-only: nothing the client reads mentions it."""

from __future__ import annotations

import json
import shutil

import pytest
import yaml

from pacenote import horizons
from pacenote.clients import load_core_themes
from pacenote.contract import parse_plan_doc
from pacenote.finalize import apply_horizons, by_horizon, ranked, render_plan_md
from pacenote.horizons import (checked_horizon, counted_signals, effective_posture, offline_horizon, posture_for,
                               posture_theme, preplan_horizon, score_prestudy)
from pacenote.llm import offline_plan_doc
from pacenote.prompts import build_fast_system, build_plan_system, build_sync_system
from pacenote.questionnaire import load_client
from pacenote.research import ResearchOutput, write_outputs
from pacenote.state import SessionState

from .conftest import CLIENTS, REAL_CLIENTS, fill, plan_doc, plan_use_case, result
from .test_admin import RESULT
from .test_questions import jargon

PRESTUDY = RESULT["prestudy"]


def signal(name="Fournil Pro", result="délai divisé par 3", url="https://fournil.example/a", date="2026-03"):
    return {"competitor": name, "signal": "devis en ligne", "result": result, "url": url, "date": date}


def prestudy(**kw):
    return {**PRESTUDY, **kw}


# --- the posture score ------------------------------------------------------------------------------


@pytest.mark.parametrize("pct, points", [(None, 0), (9.9, 0), (10, 1), (19, 1), (20, 2), (35, 2), (36, 3), (59, 3),
                                         (0.18, 1), ("n/a", 0), (-4, 0)])
def test_sector_adoption_points(pct, points):
    assert score_prestudy(prestudy(sector_adoption_pct=pct))["criteria"]["a"] == points


def test_competitor_signals_count_only_with_url_and_date_and_results_make_them_proven():
    def b(*comps):
        return score_prestudy(prestudy(competitors=list(comps)))["criteria"]["b"]

    assert b() == 0
    assert b(signal(url="")) == 0 and b(signal(date="")) == 0 and b(signal(url="ftp://x")) == 0  # not counted
    assert b(signal(result="")) == 1  # talks about AI, no published result
    assert b(signal(), signal(result="")) == 2  # one competitor with a result
    assert b(signal(), signal(name="FOURNIL PRO ")) == 2  # the same competitor twice is still one
    assert b(signal(), signal(name="Panidis")) == 3
    assert len(counted_signals([signal(), signal(url="")])) == 1


def test_pressure_and_exposure_points_and_the_total():
    sc = score_prestudy(PRESTUDY)  # 13 % (1) + one proven competitor (2) + moderate (1) + high exposure (2)
    assert sc["criteria"] == {"a": 1, "b": 2, "c": 1, "d": 2} and sc["total"] == 6
    assert sc["posture"] == sc["base"] == "pragmatic" and sc["modulators"] == []
    low = score_prestudy(prestudy(sector_adoption_pct=5, competitors=[], market_pressure="none", task_exposure="low"))
    assert low["total"] == 0 and low["posture"] == "explorer"
    high = score_prestudy(prestudy(sector_adoption_pct=59, competitors=[signal(), signal(name="Panidis")],
                                   market_pressure="strong", task_exposure="high"))
    assert high["total"] == 10 and high["posture"] == "urgent"


@pytest.mark.parametrize("total, posture", [(0, "explorer"), (3, "explorer"), (4, "pragmatic"), (6, "pragmatic"),
                                            (7, "urgent"), (10, "urgent")])
def test_posture_bands(total, posture):
    assert posture_for(total) == posture


def test_a_ceo_who_uses_ai_moves_one_step_up_and_regulation_is_only_a_note():
    up = score_prestudy(prestudy(ceo_uses_ai="yes"))
    assert up["base"] == "pragmatic" and up["posture"] == "urgent" and up["modulators"] == ["ceo_uses_ai"]
    top = score_prestudy(prestudy(sector_adoption_pct=59, competitors=[signal(), signal(name="Panidis")],
                                  market_pressure="strong", ceo_uses_ai="yes"))
    assert top["posture"] == "urgent" and top["modulators"] == []  # nothing above urgent
    reg = score_prestudy(prestudy(regulated=True))
    assert reg["posture"] == "pragmatic" and reg["modulators"] == ["regulated"]
    assert score_prestudy(None) is None


def test_the_interviewer_override_wins_over_the_score():
    preplan = {"prestudy": PRESTUDY}
    assert effective_posture("", preplan) == ("pragmatic", "prestudy")
    assert effective_posture("explorer", preplan) == ("explorer", "manual")
    assert effective_posture("bogus", preplan) == ("pragmatic", "prestudy")
    assert effective_posture("urgent", None) == ("urgent", "manual")
    assert effective_posture("", {"summary": "no pre-study"}) == (None, "")


# --- the research agent writes the pre-study ------------------------------------------------------------


def test_research_schema_asks_for_the_prestudy_and_a_horizon_per_use_case():
    schema = ResearchOutput.model_json_schema()
    assert "prestudy" in schema["required"]
    assert "horizon" in schema["$defs"]["UseCase"]["required"]
    pre = schema["$defs"]["PreStudy"]
    assert set(pre["required"]) == set(horizons.PreStudy.model_fields)


def test_research_prompt_studies_the_sector_and_its_adoption_of_ai():
    from pacenote.research import RESEARCH_SYSTEM

    system = " ".join(RESEARCH_SYSTEM.format(lang_name="French", horizons=horizons.HORIZON_GUIDE).split())
    for phrase in ("official statistics", "Keep a signal only with its URL and its publication date",
                   "never shown to the client", "each with its horizon: h1 = execution now",
                   "The sector and AI", "headings in French"):
        assert phrase in system, phrase


def test_write_outputs_keeps_the_prestudy_its_score_and_the_horizons(tmp_path):
    data = {**RESULT, "use_cases": [{**RESULT["use_cases"][0], "horizon": "h3", "kind": "big_bet"}]}
    write_outputs(tmp_path, ResearchOutput.model_validate(data), "m")
    meta = json.loads((tmp_path / "preplan.json").read_text(encoding="utf-8"))
    assert meta["prestudy"]["competitors"][0]["competitor"] == "Fournil Pro"
    assert meta["posture"]["total"] == 6 and meta["posture"]["posture"] == "pragmatic"
    assert meta["use_cases"][0]["horizon"] == "h3"


# --- horizon tags ------------------------------------------------------------------------------------------


def test_preplan_horizon_from_the_research_else_from_the_kind():
    assert preplan_horizon({"horizon": "h3", "kind": "quick_win"}) == "h3"
    assert preplan_horizon({"kind": "quick_win", "feasibility": 1}) == "h1"
    assert preplan_horizon({"kind": "big_bet", "feasibility": 4}) == "h2"
    assert preplan_horizon({"kind": "big_bet", "feasibility": 2}) == "h3"
    assert preplan_horizon({"kind": "big_bet", "feasibility": "?"}) == "h3"


def test_h1_must_be_doable_now():
    assert checked_horizon("h1", 5) == "h1" and checked_horizon("h1", 0) == "h1"  # 0 = not assessed
    assert checked_horizon("h1", 2) == "h2" and checked_horizon("h1", 1) == "h2"
    assert checked_horizon("h3", 1) == "h3" and checked_horizon("hx", 4) == "h2"
    assert offline_horizon(5, False) == "h1" and offline_horizon(0, True) == "h1" and offline_horizon(4, False) == "h2"


def _state():
    return SessionState.from_questionnaire(load_client(CLIENTS, "brioche-four-slots").questionnaire)


def test_apply_horizons_demotes_hard_h1_and_picks_the_best_ranked_h1_as_lighthouse():
    doc = parse_plan_doc(plan_doc(
        plan_use_case("hard", "Difficile", impact=5, feasibility=2, horizon="h1"),
        plan_use_case("later", "Plus tard", impact=5, feasibility=5, horizon="h2"),
        plan_use_case("small", "Petit", impact=2, feasibility=3, horizon="h1"),
        plan_use_case("big", "Gros", impact=4, feasibility=4, horizon="h1"),
        adoption={"lighthouse": "later"}))
    out = apply_horizons(doc)
    tags = {u.id: u.horizon for u in out.use_cases}
    assert tags == {"hard": "h2", "later": "h2", "small": "h1", "big": "h1"}
    assert out.adoption.lighthouse == "big"  # "later" is not h1: the best ranked h1 instead
    kept = apply_horizons(doc.model_copy(update={"adoption": doc.adoption.model_copy(update={"lighthouse": "small"})}))
    assert kept.adoption.lighthouse == "small"  # an h1 named by the model stays
    groups = by_horizon(ranked(out))
    assert [h for h, _ in groups] == ["h1", "h2"] and [u.id for u in groups[0][1]] == ["big", "small"]


def test_the_offline_plan_tags_horizons_and_names_a_lighthouse():
    st = _state()
    st.apply_sync_result(result(slots=[fill("bakery-orders", k, v) for k, v in (
        ("tool", "Sage"), ("volume", "40 par semaine"), ("actors", "Claire"), ("trigger", "boîte commandes"),
        ("baseline", "3 h par jour"))] + [fill("stock-requests", "tool", "Excel")]))
    doc = offline_plan_doc(st)
    tags = {u.id: u.horizon for u in doc.use_cases}
    assert tags == {"bakery-orders": "h1", "stock-requests": "h2"}
    assert doc.adoption.lighthouse == "bakery-orders"


# --- the posture shapes the questions --------------------------------------------------------------------


def _client_with(tmp_path, preplan: dict | None, **sheet):
    shutil.copytree(REAL_CLIENTS / "brioche-four", tmp_path / "brioche-four")
    d = tmp_path / "brioche-four"
    if preplan is not None:
        (d / "preplan.json").write_text(json.dumps(preplan, ensure_ascii=False), encoding="utf-8")
    (d / "client.yaml").write_text(yaml.safe_dump({"name": "Brioche & Four", **sheet}, allow_unicode=True),
                                   encoding="utf-8")
    return load_client(tmp_path, "brioche-four")


def _horizon_questions(cfg) -> list[tuple[str, int, str]]:
    st = SessionState.from_questionnaire(cfg.questionnaire)
    return [(q.id, q.priority, q.phase) for q in st.questions if q.theme == "horizons"]


def test_each_posture_asks_its_own_horizon_questions(tmp_path):
    def asked(posture):
        return [(i, p) for i, p, _ in _horizon_questions(_client_with(tmp_path / posture, None, posture_override=posture))]

    assert asked("explorer") == [("hz-two-years", 3)]  # gentle: the future only if time allows, no competitors
    assert asked("pragmatic") == [("hz-two-years", 2), ("hz-competitors", 2)]
    assert asked("urgent") == [("hz-competitors", 1), ("hz-two-years", 1)]  # competitors first, must-have


def test_the_prestudy_sets_the_posture_and_its_rationale_and_the_questions_go_last(tmp_path):
    cfg = _client_with(tmp_path, {"summary": "s", "use_cases": [], "seed_questions": [], "prestudy": PRESTUDY})
    q = cfg.questionnaire
    assert q.posture == "pragmatic" and q.posture_rationale.startswith("Secteur en retard")
    st = SessionState.from_questionnaire(q)
    assert st.posture == "pragmatic"
    assert {p for _, _, p in _horizon_questions(cfg)} == {"return"}
    assert st.questions[-1].theme == "horizons"  # at the end of the return phase


def test_no_posture_no_horizon_questions(tmp_path):
    assert _horizon_questions(_client_with(tmp_path / "a", None)) == []
    assert _client_with(tmp_path / "a2", None).questionnaire.posture is None
    off = _client_with(tmp_path / "b", {"summary": "s", "prestudy": PRESTUDY}, use_preplan=False)
    assert off.questionnaire.posture is None and _horizon_questions(off) == []
    manual = _client_with(tmp_path / "c", {"summary": "s", "prestudy": PRESTUDY}, posture_override="explorer")
    assert manual.questionnaire.posture == "explorer" and manual.questionnaire.posture_rationale == ""


def test_the_frozen_test_clients_are_unchanged_without_a_posture():
    assert posture_theme(None) is None and posture_theme("bogus") is None
    st = SessionState.from_questionnaire(load_client(CLIENTS, "brioche-four").questionnaire)
    assert st.posture is None and not any(q.theme == "horizons" for q in st.questions)


def test_horizon_and_ambassador_questions_follow_the_wording_rules():
    theme = posture_theme("urgent")
    texts = [t for q in theme.questions for t in q.text.values()]
    texts += [v for q in theme.questions for r in ("business", "workflow", "implementation")
              for item in getattr(q.expect, r) for v in (item.values() if isinstance(item, dict) else [item])]
    core = next(q for t in load_core_themes() for q in t.questions if q.id == "core-first-to-try")
    texts += list(core.text.values())
    assert texts and all(not jargon(t) for t in texts), [t for t in texts if jargon(t)]
    assert all(q.slots for q in theme.questions)


# --- prompts --------------------------------------------------------------------------------------------


def test_live_prompts_carry_the_horizons_and_the_posture_only_when_set():
    st = _state()
    for build in (build_sync_system, build_fast_system):
        plain = build(st, "", "")
        assert "# Horizons" in plain and "h1 = execution now" in plain and "# Pace" not in plain
    st.posture, st.posture_rationale = "urgent", "Deux concurrents ont publié des résultats."
    for build in (build_sync_system, build_fast_system):
        text = build(st, "", "")
        assert "# Pace (from the desk research; operator-only: never mention it" in text
        assert "urgent (competitors already have results: move fast)" in text
        assert "Deux concurrents ont publié des résultats." in text and "brisk pace" in text
    st.posture = "explorer"
    assert "gentle pace" in build_sync_system(st, "", "") and "brisk pace" not in build_sync_system(st, "", "")


def test_plan_prompt_asks_for_a_horizon_and_the_adoption_plan_and_keeps_the_pace_hidden():
    st = _state()
    plain = build_plan_system(st, "", "")
    assert "- horizon: h1 = execution now" in plain and "never write \"horizon\" or H1/H2/H3" in plain
    assert "# Adoption plan" in plain and "adoption.lighthouse" in plain and "The pace" not in plain
    st.posture = "explorer"
    text = build_plan_system(st, "", "")
    assert "The pace (operator guidance from the desk research; never mention it, never name it in any text)" in text
    assert "propose one h1 opportunity as the lighthouse" in text


# --- what the client reads ------------------------------------------------------------------------------------

LEAKS = ("posture", "explorer", "explorateur", "pragmati", "urgent", "H1", "H2", "H3", "h1", "h2", "h3", "horizon")


def _plan(lang: str, posture: str | None = "urgent"):
    st = _state()
    st.language, st.posture, st.posture_rationale = lang, posture, "Concurrents en avance."
    doc = parse_plan_doc(plan_doc(plan_use_case("orders", "Commandes", horizon="h1"),
                                  plan_use_case("sav", "SAV", horizon="h2", feasibility=3),
                                  plan_use_case("portal", "Portail", horizon="h3", feasibility=3)))
    return render_plan_md(apply_horizons(doc, st), st)


@pytest.mark.parametrize("lang, heads", [("fr", ["### Cette semaine", "### Ce trimestre", "### D'ici deux ans"]),
                                         ("en", ["### This week", "### This quarter", "### Within two years"])])
def test_the_plan_groups_opportunities_by_horizon_in_plain_words(lang, heads):
    md = _plan(lang)
    pos = [md.index(h) for h in heads]
    assert pos == sorted(pos)
    adoption = "## Plan d'adoption" if lang == "fr" else "## Adoption plan"
    assert adoption in md
    lowered = md.lower()
    assert not [w for w in LEAKS if w.lower() in lowered.replace("urgence", "")], [w for w in LEAKS if w.lower() in lowered]


def test_a_named_pilot_is_quoted_in_the_adoption_plan():
    st = _state()
    st.language = "en"
    doc = parse_plan_doc(plan_doc(plan_use_case("orders", "Orders", horizon="h1"), adoption={
        "lighthouse": "orders", "pilots": [{"person": "Claire", "evidence": "she always tries new things first"}],
        "volunteers": {"plan": "Yesterday's 10 orders", "trigger": ""},
        "majority": {"plan": "Claire shows it on Monday", "trigger": ""}}))
    md = render_plan_md(apply_horizons(doc, st), st)
    assert "**Pilots**: Claire — « she always tries new things first »" in md
    # nobody listed in the meeting: no leader to name; the sponsor is to be confirmed, even with a pilot
    assert "**Sponsor**: to be confirmed with the leadership." in md
    assert "**Start with**: Orders" in md and "**When**: This week" in md
    assert "1. **The volunteers (weeks 1 to 4)** — Yesterday's 10 orders" in md
    assert "2. **Most of the team** — Claire shows it on Monday" in md
    assert "3. **Everyone** — It becomes the usual way of working" in md  # the default when empty
