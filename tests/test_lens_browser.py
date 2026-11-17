"""Real-browser checks (Node + Playwright, skipped without them) of the executive lens, with fake data
and no model call: on the interview card, the talking point ("Worth saying") and the questions also
worth asking (the missing metric, the people question), the reasons kept in the console; in the Plan
view, the time given back and the adoption plan in three waves, the second one started by the first
one's measured result. Fixed spare port 8797."""

from __future__ import annotations

import asyncio

import pytest

from pacenote.capture import ReplaySource
from pacenote.contract import parse_plan_doc
from pacenote.llm import OfflineEngine
from pacenote.preparation import load_client
from pacenote.server import create_app
from pacenote.session import Session, Settings

from .conftest import CLIENTS, fill, plan_doc, plan_fact, plan_use_case, result
from .test_horizons_browser import APP_PORT, PLAN_SCRIPT, run_node, serve
from .test_i18n import _node_playwright

QUIET = Settings(sync_interval=3600, first_sync_delay=3600, poll_interval=3600, fast_enabled=False)
pytestmark = pytest.mark.skipif(not _node_playwright(), reason="needs node + playwright")

INTERVIEW_SCRIPT = r"""
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  p.on('pageerror', (e) => errors.push(String(e)));
  await p.goto(process.argv[2] + '/');
  await p.waitForFunction(() => document.querySelector('#framing-text').textContent.length > 5);
  const r = await p.evaluate(() => ({
    sayLabel: document.querySelector('#framing-say .amb-label').textContent,
    say: document.querySelector('#framing-text').textContent,
    askLabel: document.querySelector('#ambassador-ask .amb-label').textContent,
    metric: document.querySelector('#metric-text').textContent,
    people: document.querySelector('#ambassador-text').textContent,
    cards: document.querySelector('#framing-say').textContent + document.querySelector('#ambassador-ask').textContent,
    order: ['#ask-now', '#framing-say', '#ambassador-ask'].map((id) => document.querySelector(id).getBoundingClientRect().top),
    console: [...document.querySelectorAll('#console-framing-list li')].map((li) => li.textContent) }));
  console.log(JSON.stringify({ ...r, errors }));
  await b.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""

POINT = "Re-keying every order is not what Claire is there for: it can run on its own, and her three hours a day go to the big accounts."


def test_interview_card_says_and_asks_and_keeps_why_in_the_console(tmp_path):
    s = Session(client=load_client(CLIENTS, "brioche-four-slots"), session_dir=tmp_path / "s", engine=OfflineEngine(),
                source_factory=lambda d: ReplaySource({}), settings=QUIET)
    st = s.state
    st.language = "en"
    st.apply_sync_result(result(
        slots=[fill("bakery-orders", "actors", "Claire", "Claire types every order"),
               fill("bakery-orders", "baseline", "3 hours a day", "about three hours a day")],
        value_framings=[{"use_case": "bakery-orders", "person": "Claire", "task": "re-keying order emails into Excel",
                         "point": POINT, "moves_up_to": "the big bakery accounts",
                         "reason": "the CEO just put a number on it", "timing": "now"}],
        metric_questions=[{"use_case": "bakery-orders", "kind": "time", "side": "target",
                           "question": "If it worked, how much time a day would Claire still spend on it, and by when?"}]))
    st.mode, st.ui_language, st.focus_id = "interview", "en", "orders-walkthrough"
    with serve(create_app(s), APP_PORT) as url:
        r = run_node(tmp_path, INTERVIEW_SCRIPT, url)
    assert r["errors"] == []
    assert r["sayLabel"] == "Worth saying · Bakery order entry" and r["say"] == POINT
    assert r["askLabel"] == "Also worth asking · Bakery order entry"
    assert r["metric"].startswith("If it worked, how much time a day") and "Claire" in r["people"]
    assert r["order"] == sorted(r["order"])  # the follow-up, then what to say, then what to ask
    for secret in ("the CEO just put a number on it", "moves up to"):  # why stays in the console
        assert secret not in r["cards"]
    assert len(r["console"]) == 1 and "the CEO just put a number on it" in r["console"][0]
    assert "moves up to: the big bakery accounts" in r["console"][0] and "to say now" in r["console"][0]


class _PlanEngine(OfflineEngine):
    """The offline engine with a written plan: one opportunity, its metric and the time it gives back."""

    def write_plan_doc(self, system, user, state=None):
        metric = {"id": "order-minutes", "use_case": "orders", "name": "Temps de ressaisie", "unit": "h/jour",
                  "how_measured": "relevé de Claire", "cadence": "hebdo", "owner": "Claire",
                  "baseline": {"value": "3 h/jour", "date": "", "status": "stated", "evidence": "trois heures par jour",
                               "question": "", "ask": ""},
                  "target": {"value": "30 min/jour", "date": "mars", "status": "stated", "evidence": "une demi-heure",
                             "question": "", "ask": ""}}
        uc = {**plan_use_case("orders", "Saisie des commandes", horizon="h1",
                              baseline=plan_fact("3 h/jour", "trois heures par jour")), "metric": "order-minutes",
              "freed": {"who": "Claire", "task": "ressaisir les mails de commande dans Excel", "time": "3 h/jour",
                        "evidence": "trois heures par jour", "moves_up_to": "le suivi des grands comptes"}}
        return parse_plan_doc(plan_doc(uc, metrics=[metric], adoption={
            "lighthouse": "orders", "pilots": [{"person": "Claire", "evidence": "elle essaie toujours en premier"}],
            "majority": {"plan": "Claire montre ses chiffres à la réunion du lundi.", "trigger": ""}}))


def test_plan_view_shows_time_given_back_and_three_waves_in_a_browser(tmp_path):
    s = Session(client=load_client(CLIENTS, "brioche-four-slots"), session_dir=tmp_path / "s" / "20261116-100000",
                engine=_PlanEngine(), source_factory=lambda d: ReplaySource({}), settings=QUIET)
    asyncio.run(s.finalize())
    with serve(create_app(s), APP_PORT) as url:
        r = run_node(tmp_path, PLAN_SCRIPT, url)
    assert r["errors"] == []
    text = r["text"]
    assert ("Temps rendu : ressaisir les mails de commande dans Excel — Claire · temps libéré : 3 h/jour · "
            "pour se consacrer à : le suivi des grands comptes") in text
    waves = [text.index(w) for w in ("Les volontaires (semaines 1 à 4)", "Le gros de l'équipe", "Tout le monde")]
    assert waves == sorted(waves)
    assert "Résultat à montrer : Temps de ressaisie — aujourd'hui : 3 h/jour · cible : 30 min/jour (mars)" in text
    assert "Claire montre ses chiffres à la réunion du lundi." in text
    for leak in ("réticent", "ventre mou", "IA "):
        assert leak not in text, leak
