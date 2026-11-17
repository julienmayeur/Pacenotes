"""The executive lens: in about fifteen minutes with the CEO, the copilot helps the interviewer guide
him on value, workflow by workflow. When he describes manual work, a tactful talking point (the task
brings that person little, it can run on its own, their time goes to higher-level decisions) shows
under the follow-up; why now stays in the console. Every workflow gets a baseline and a success metric:
when one is missing, the screen suggests the question. The plan shows the time given back and the
adoption plan in three waves, the second one started by the first one's measured result.
Model replies come from an offline fixture (tests/fixtures/sync/executive-lens.json)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pacenote import i18n, lens
from pacenote.capture import ReplaySource
from pacenote.contract import FastResult, SyncResult, parse_plan_doc, parse_sync_result
from pacenote.finalize import apply_horizons, render_plan_md, wave_text
from pacenote.llm import OfflineEngine, offline_plan_doc
from pacenote.prompts import build_plan_system, build_plan_user, build_sync_system, state_snapshot
from pacenote.preparation import load_client
from pacenote.session import Session, Settings
from pacenote.state import SessionState

from .conftest import CLIENTS, fill, plan_doc, plan_fact, plan_use_case, result

FIXTURE = Path(__file__).parent / "fixtures" / "sync" / "executive-lens.json"
QUIET = Settings(sync_interval=3600, first_sync_delay=3600, poll_interval=3600, fast_enabled=False)
WHY_NOW = "le PDG vient de chiffrer la ressaisie : recadrer vers la valeur"


def _state(lang: str = "fr") -> SessionState:
    st = SessionState.from_questionnaire(load_client(CLIENTS, "brioche-four-slots").questionnaire)
    st.language = lang
    st.focus_id = "orders-walkthrough"
    return st


def _replies() -> list[SyncResult]:
    return [parse_sync_result(x) for x in json.loads(FIXTURE.read_text(encoding="utf-8"))]


def _framing(point="Ressaisir chaque commande, ça peut se faire tout seul : Claire garde les grands comptes.",
             timing="now", use_case="bakery-orders", moves="les grands comptes") -> dict:
    return {"use_case": use_case, "person": "Claire", "task": "ressaisir les commandes", "point": point,
            "moves_up_to": moves, "reason": "pourquoi maintenant", "timing": timing}


def _walked(st: SessionState) -> None:
    st.apply_sync_result(result(slots=[fill("bakery-orders", "actors", "Claire", "c'est Claire qui saisit")]))


# --- the offline fixture, played sync after sync --------------------------------------------------------------


def test_the_fixture_replies_are_contract_valid():
    assert len(_replies()) == 3


def test_the_talking_point_waits_for_now_shows_once_and_closes():
    st = _state()
    s1, s2, s3 = _replies()
    st.apply_sync_result(s1)
    assert lens.framing_for(st) is None  # "later": the CEO has not given the figure yet
    st.apply_sync_result(s2)
    shown = lens.framing_for(st)
    assert shown["point"].startswith("Ressaisir chaque commande à la main")
    assert shown["moves_up_to"] == "le suivi des grands comptes boulangers" and shown["reason"] == WHY_NOW
    st.advance()  # the focus leaves the question while it was on screen: said
    assert st.value_framings["bakery-orders"]["asked"] is True
    assert lens.framing_for(st, "orders-walkthrough") is not None  # back on the same question: still there
    st.apply_sync_result(s3)
    assert lens.framing_for(st, "orders-walkthrough") is None  # done
    assert lens.framings_view(st) == []


def test_the_metric_question_follows_the_gaps_baseline_then_target_then_nothing():
    st = _state()
    s1, s2, s3 = _replies()
    assert lens.metric_ask_for(st) is None  # the workflow was not walked through yet
    st.apply_sync_result(s1)
    ask = lens.metric_ask_for(st)
    assert (ask["side"], ask["kind"], ask["source"]) == ("baseline", "time", "model")
    assert ask["question"].startswith("La semaine dernière, combien de temps Claire")
    st.apply_sync_result(s2)  # the baseline was given
    ask = lens.metric_ask_for(st)
    assert ask["side"] == "target" and ask["question"].startswith("Si ça marchait")
    st.apply_sync_result(s3)  # and the target
    assert lens.metric_ask_for(st) is None


def test_the_interview_view_shows_what_to_say_and_ask_and_keeps_why_for_the_console(tmp_path):
    s = Session(client=load_client(CLIENTS, "brioche-four-slots"), session_dir=tmp_path / "s", engine=OfflineEngine(),
                source_factory=lambda d: ReplaySource({}), settings=QUIET)
    s1, s2, _ = _replies()
    s.state.apply_sync_result(s1)
    s.state.apply_sync_result(s2)
    s.state.mode, s.state.focus_id = "interview", "orders-walkthrough"
    view = s.view()
    cur = view["current"]
    assert cur["framing"]["point"].startswith("Ressaisir chaque commande à la main")
    assert cur["metric_ask"]["side"] == "target"
    title = s.state.get_use_case("bakery-orders").title("fr")
    assert view["framings"] == [{"use_case": title, "point": cur["framing"]["point"],
                                 "moves_up_to": "le suivi des grands comptes boulangers", "reason": WHY_NOW,
                                 "timing": "now", "asked": False}]


# --- the rules ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("point,bad", [
    ("On pourrait automatiser la saisie de Claire.", "automat"),
    ("Avec l'IA, Claire n'aurait plus à ressaisir.", "IA"),
    ("Ce travail est inutile, autant le remplacer.", "remplac"),
    ("That job is pointless for Claire.", "pointless"),
    ("It would cut the head count of the order desk.", "head count"),
])
def test_a_talking_point_that_belittles_or_says_ai_is_rejected(point, bad):
    st = _state()
    rep = st.apply_sync_result(result(value_framings=[_framing(point=point)]))
    assert "bakery-orders" not in st.value_framings
    assert any(r.kind == "framing" and bad.lower() in r.detail.lower() for r in rep.rejected)


def test_unknown_use_case_empty_point_stale_and_already_said_are_rejected():
    st = _state()
    _walked(st)
    rep = st.apply_sync_result(result(value_framings=[_framing(use_case="nope"), _framing(point="")]))
    assert [r.detail for r in rep.rejected] == ["unknown use case 'nope'", "no talking point"]
    st.apply_sync_result(result(value_framings=[_framing()]), read=10)
    rep = st.apply_sync_result(result(value_framings=[_framing(point="Autre chose, plus tard.")]), read=5)
    assert any(r.kind == "stale" for r in rep.rejected)
    st.advance()  # said
    rep = st.apply_sync_result(result(value_framings=[_framing(point="Une autre formulation.")]), read=20)
    assert any(r.detail == "already said" for r in rep.rejected)


def test_without_a_model_question_a_template_in_the_interview_language():
    st = _state("en")
    _walked(st)
    ask = lens.metric_ask_for(st)
    assert ask == {"use_case": st.get_use_case("bakery-orders").title("en"), "side": "baseline", "kind": "time",
                   "question": "On last week's cases, how much time did this take, per day or per week?",
                   "source": "template"}
    st.apply_sync_result(result(metric_questions=[{"use_case": "bakery-orders", "kind": "errors", "side": "target",
                                                   "question": "Automate it?"}]))
    assert st.metric_questions == {}  # AI vocabulary: rejected
    st.apply_sync_result(result(slots=[fill("bakery-orders", "baseline", "3 h a day")]))
    assert lens.metric_ask_for(st)["question"].startswith("Which figure would tell you it got better")


def test_a_metric_question_is_suggested_once_per_side():
    st = _state()
    _walked(st)
    st.focus_id = "orders-walkthrough"
    assert lens.metric_ask_for(st)["side"] == "baseline"
    st.advance()
    assert "bakery-orders:baseline" in st.metric_asked
    assert lens.metric_ask_for(st, "orders-walkthrough") is not None  # same question: still there
    st.lens_prompted.clear()  # another question on the same workflow would not ask it again
    assert lens.metric_ask_for(st, "orders-walkthrough") is None


def test_the_metric_question_and_the_people_question_show_side_by_side(tmp_path):
    s = Session(client=load_client(CLIENTS, "brioche-four-slots"), session_dir=tmp_path / "s", engine=OfflineEngine(),
                source_factory=lambda d: ReplaySource({}), settings=QUIET)
    _walked(s.state)
    s.state.mode, s.state.focus_id = "interview", "orders-walkthrough"
    cur = s.view()["current"]
    assert cur["metric_ask"]["side"] == "baseline" and cur["ambassador"]["person"] == "Claire"
    s.state.advance()  # both were on screen: both asked
    assert s.state.metric_asked == ["bakery-orders:baseline"]
    assert s.state.ambassador_prompted == {"orders-walkthrough": "Claire"}


# --- prompts --------------------------------------------------------------------------------------------------


def test_the_sync_prompt_carries_the_executive_lens_and_the_fast_pass_does_not():
    st = _state()
    text = build_sync_system(st, glossary="")
    assert "# The executive lens" in text and "value_framings" in text and "metric_questions" in text
    assert "time spent, volume, error rate or delay" in text and "never the person" in text
    required = set(SyncResult.model_json_schema()["required"])
    assert {"value_framings", "metric_questions"} <= required
    assert not {"value_framings", "metric_questions"} & set(FastResult.model_fields)


def test_the_plan_gets_the_task_and_the_decision_but_never_why_or_the_words_to_say():
    st = _state()
    for r in _replies()[:2]:
        st.apply_sync_result(r)
    live = state_snapshot(st)
    assert live["value_framings"][0]["timing"] == "now"
    assert live["metric_questions"]["bakery-orders:target"]["kind"] == "time"
    plan = build_plan_user(st, [], None)
    assert "le suivi des grands comptes boulangers" in plan and "ressaisir les lignes" in plan
    for secret in (WHY_NOW, "ça peut se faire tout seul", "metric_questions"):
        assert secret not in plan, secret
    system = build_plan_system(st, glossary="")
    assert "# Adoption plan: three waves" in system and "never on converting the reluctant" in system
    assert "- freed:" in system and "never an estimate" in system


# --- the plan ----------------------------------------------------------------------------------------------------


def _plan(lang: str, freed: dict, adoption: dict | None = None):
    st = _state(lang)
    metric = {"id": "order-minutes", "use_case": "orders", "name": "Temps de ressaisie", "unit": "h/jour",
              "how_measured": "relevé de Claire", "cadence": "hebdo", "owner": "Claire",
              "baseline": {"value": "3 h/jour", "date": "", "status": "stated", "evidence": "trois heures par jour",
                           "question": "", "ask": ""},
              "target": {"value": "30 min/jour", "date": "mars", "status": "stated", "evidence": "une demi-heure",
                         "question": "", "ask": ""}}
    uc = {**plan_use_case("orders", "Saisie des commandes", horizon="h1",
                          baseline=plan_fact("3 h/jour", "trois heures par jour")), "metric": "order-minutes",
          "freed": freed}
    doc = parse_plan_doc(plan_doc(uc, metrics=[metric], adoption={"lighthouse": "orders", **(adoption or {})}))
    return render_plan_md(apply_horizons(doc, st), st)


FREED = {"who": "Claire", "task": "ressaisir les commandes dans Excel et Sage", "time": "3 h/jour",
         "evidence": "trois heures par jour", "moves_up_to": "le suivi des grands comptes"}


def test_the_plan_shows_the_time_given_back_only_from_a_quoted_figure():
    md = _plan("fr", FREED)
    assert ("**Temps rendu** : ressaisir les commandes dans Excel et Sage — Claire · temps libéré : 3 h/jour · "
            "pour se consacrer à : le suivi des grands comptes") in md
    md = _plan("fr", {**FREED, "evidence": ""})  # no quote: the figure is not a fact
    assert "temps libéré : À confirmer · pour" in md and "3 h/jour · pour" not in md
    md = _plan("fr", {"who": "", "task": "", "time": "", "evidence": "", "moves_up_to": ""})
    assert "Temps rendu" not in md  # the interview gave none of it


def test_the_adoption_plan_has_three_waves_and_wave_two_starts_from_the_measured_result():
    md = _plan("en", FREED, {"volunteers": {"plan": "Claire runs yesterday's orders in shadow mode.", "trigger": ""},
                             "majority": {"plan": "Claire shows her figures at the Monday meeting.",
                                          "trigger": "Order entry down to 30 minutes a day on real cases."}})
    assert "_Start with the people keen to go further; their measured results convince most of the team" in md
    assert "1. **The volunteers (weeks 1 to 4)** — Claire runs yesterday's orders in shadow mode." in md
    assert "    - Starts: week 1, on the first use case." in md
    assert "2. **Most of the team** — Claire shows her figures at the Monday meeting." in md
    assert "    - Starts: Order entry down to 30 minutes a day on real cases." in md
    assert "    - Result to show: Temps de ressaisie — today: 3 h/jour · target: 30 min/jour (mars)" in md
    assert "3. **Everyone** — It becomes the usual way of working; the others follow" in md


def test_a_wave_that_labels_people_falls_back_to_the_default_wording():
    assert wave_text("Convaincre les réticents de l'atelier") == ""
    assert wave_text("Then the undecided middle") == "" and wave_text("Le ventre mou suit") == ""
    md = _plan("fr", FREED, {"everyone": {"plan": "On relance les résistants un par un.", "trigger": ""}})
    assert "résistants" not in md
    assert "3. **Tout le monde** — Ça devient la façon de travailler habituelle" in md


def test_the_offline_plan_keeps_the_task_and_the_decision_from_the_interview():
    st = _state()
    for r in _replies():
        st.apply_sync_result(r)
    u = next(x for x in offline_plan_doc(st).use_cases if x.id == "bakery-orders")
    assert u.freed.who == "Claire" and u.freed.moves_up_to == "le suivi des grands comptes boulangers"
    assert u.freed.time == ""  # never an estimate without a model


# --- wording on the client-facing screen --------------------------------------------------------------------------


def test_the_preplan_theme_and_the_lens_strings_say_no_ai():
    for lang in ("en", "fr"):
        tr = i18n.Translator(lang)
        assert lens.wording_problem(tr("admin.preplanTheme")) == ""
        for side in lens.SIDES:
            for kind in lens.KINDS:
                text = tr(f"focus.metric.{side}.{kind}")
                assert text and lens.wording_problem(text) == "", (lang, side, kind)
    assert i18n.Translator("en")("admin.preplanTheme") == "Opportunities to check"
    assert i18n.Translator("fr")("admin.preplanTheme") == "Pistes à vérifier"
