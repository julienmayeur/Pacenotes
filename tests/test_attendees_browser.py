"""Real-browser checks (Node + Playwright, skipped without them) of who is in the meeting, with fake data and
no model call: the admin's attendee rows with their persona and prep note, the interviewer console's stance
view (three attendees: one driving it, two hesitant or against), and the interview card suggesting who to
address the question to. Fixed spare ports 8796 (admin) and 8797 (interview). Each script takes an optional
screenshot path. Fictional people and companies only."""

from __future__ import annotations

import pytest

from pacenote.admin import create_admin_app
from pacenote.capture import ReplaySource
from pacenote.contract import AskTo
from pacenote.llm import OfflineEngine
from pacenote.questionnaire import load_client
from pacenote.server import create_app
from pacenote.session import Session

from .conftest import CLIENTS, fill, result, upd
from .test_horizons_browser import ADMIN_PORT, APP_PORT, QUIET, run_node, serve
from .test_i18n import _node_playwright
from .test_personas import _client

pytestmark = pytest.mark.skipif(not _node_playwright(), reason="needs node + playwright")

ROOM = [{"name": "Hélène Varenne", "role": "PDG"},
        {"name": "Paul Ardent", "role": "Directeur commercial"},
        {"name": "Léa Morin", "role": "Assistante ADV", "notes": "saisit les commandes"}]

ADMIN_SCRIPT = r"""
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  p.on('pageerror', (e) => errors.push(String(e)));
  await p.addInitScript(() => localStorage.setItem('pacenote.ui-language', 'fr'));
  await p.goto(process.argv[2] + '/admin?client=brioche-four');
  await p.waitForFunction(() => document.querySelectorAll('#f-contacts li .c-prep').length === 3
    && [...document.querySelectorAll('#f-contacts li .c-why')].every((x) => x.textContent.length > 5));
  const r = await p.evaluate(() => ({
    title: document.querySelector('[data-i18n="admin.who"]').textContent,
    rows: [...document.querySelectorAll('#f-contacts li')].map((li) => ({
      role: li.querySelector('.c-role').value, why: li.querySelector('.c-why').textContent,
      prep: li.querySelector('.c-prep').textContent })) }));
  if (process.argv[3]) {
    const box = await p.evaluate(() => {
      const r = document.querySelector('#f-contacts').closest('.wide').getBoundingClientRect();
      return { x: r.left - 8, y: r.top + window.scrollY - 8, width: r.width + 16, height: r.height + 16 };
    });
    await p.screenshot({ path: process.argv[3], clip: box, fullPage: true });
  }
  console.log(JSON.stringify({ ...r, errors }));
  await b.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""


def test_admin_lists_who_will_be_in_the_meeting_with_a_prep_note_each(tmp_path):
    _client(tmp_path, ROOM, headcount="120")
    with serve(create_admin_app(tmp_path / "clients"), ADMIN_PORT) as url:
        r = run_node(tmp_path, ADMIN_SCRIPT, url)
    assert r["errors"] == [] and r["title"] == "Qui sera dans la réunion"
    rows = r["rows"]
    assert [x["role"] for x in rows] == ["PDG", "Directeur commercial", "Assistante ADV"]
    assert rows[0]["prep"].startswith("Préparation : Questions au niveau des processus")
    assert "il montre l'exemple" in rows[0]["prep"]
    assert rows[2]["prep"].startswith("Préparation : Questions étape par étape")
    assert all("Position sur l'IA : inconnue avant la réunion" in x["prep"] for x in rows)


def meeting_session(tmp_path) -> Session:
    """Three people in the meeting: the PDG drives it, the sales director is against it, the order desk
    assistant hesitates; the card is on the order entry, at the per-item detail."""
    s = Session(client=_client(tmp_path, ROOM, headcount="120"), session_dir=tmp_path / "s" / "20261117-140000",
                engine=OfflineEngine(), source_factory=lambda d: ReplaySource({}), settings=QUIET)
    st = s.state
    st.language = st.ui_language = "fr"
    st.mode = "interview"
    st.focus_id = next(q.id for q in st.questions if q.status != "removed" and q.phase == "deep-dives") \
        if any(q.phase == "deep-dives" for q in st.questions) else st.next_questions[0]
    st.apply_sync_result(result(
        question_updates=[upd(st.focus_id, "partial", "Les commandes des boulangeries arrivent par mail.",
                              rung="implementation", follow_up="Sur la commande de ce matin : combien de lignes, et "
                                                              "combien de temps pour la saisir ?")],
        attendee_stances=[
            {"attendee": "a1", "stance": "driver", "evidence": "J'utilise déjà un assistant pour préparer mes comités",
             "confidence": 0.85, "attributed": "self"},
            {"attendee": "a2", "stance": "opposed", "evidence": "Mes commerciaux ont besoin du contact, pas d'un robot",
             "confidence": 0.7, "attributed": "self"},
            {"attendee": "a3", "stance": "hesitant", "evidence": "Pour la saisie, je ne vois pas trop ce que ça changerait",
             "confidence": 0.55, "attributed": "addressed"}]), read=40)
    return s


INTERVIEW_SCRIPT = r"""
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  p.on('pageerror', (e) => errors.push(String(e)));
  await p.goto(process.argv[2] + '/');
  await p.waitForFunction(() => document.querySelector('#ask-text').textContent.length > 5);
  const card = await p.evaluate(() => ({
    chip: document.querySelector('#who-chip').textContent,
    askTo: document.querySelector('#ask-to').getClientRects().length ? document.querySelector('#ask-to').textContent : '',
    alsoLabel: document.querySelector('#ambassador-ask').getClientRects().length
      ? document.querySelector('#ambassador-ask .amb-label').textContent : '',
    page: document.querySelector('main') ? document.querySelector('main').textContent : document.body.textContent }));
  if (process.argv[3]) await p.screenshot({ path: process.argv[3] });
  await p.keyboard.press('c');
  await p.waitForSelector('#console:not(.hidden) #console-attendees:not(.hidden)');
  const people = await p.evaluate(() => [...document.querySelectorAll('#console-attendee-list li')].map((li) => li.textContent));
  if (process.argv[4]) {
    const el = await p.$('#console-attendees');
    await el.screenshot({ path: process.argv[4] });
  }
  console.log(JSON.stringify({ ...card, people, errors }));
  await b.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""


def test_the_console_shows_each_attendee_stance_and_the_card_who_to_ask(tmp_path):
    s = meeting_session(tmp_path)
    with serve(create_app(s), APP_PORT) as url:
        r = run_node(tmp_path, INTERVIEW_SCRIPT, url)
    assert r["errors"] == []
    assert r["chip"] == "Dans la réunion : PDG · Dirigeant +2"
    assert r["askTo"] == "→ Demander à Léa Morin (Assistante ADV)"  # the per-item detail: the one who does it
    p = r["people"]
    assert p[0].startswith("PDG · Hélène Varenne · Dirigeant · responsable · sponsor Moteur")
    assert "sûr à 85\u00a0%" in p[0] and "« J'utilise déjà un assistant pour préparer mes comités »" in p[0]
    assert "Opposé" in p[1] and "« Mes commerciaux ont besoin du contact, pas d'un robot »" in p[1]
    assert "Hésitant" in p[2] and "sûr à 55\u00a0%" in p[2]
    assert r["alsoLabel"] == "À demander aussi · PDG"  # the sponsor question says it is for the lead
    for leak in ("Moteur", "Opposé", "Hésitant", "robot"):  # stances never on the interview card itself
        assert leak not in r["page"], leak


def test_the_model_can_address_the_question_to_a_specific_attendee(tmp_path):
    s = meeting_session(tmp_path)
    s.state.apply_sync_result(result(ask_to=[AskTo(question_id=s.state.focus_id, attendee="a2",
                                                   reason="suit les grands comptes")]), read=41)
    with serve(create_app(s), APP_PORT) as url:
        r = run_node(tmp_path, INTERVIEW_SCRIPT, url)
    assert r["errors"] == []
    assert r["askTo"] == "→ Demander à Paul Ardent (Directeur commercial) · suit les grands comptes"


def test_one_attendee_no_ask_line(tmp_path):
    s = Session(client=load_client(CLIENTS, "brioche-four-slots"), session_dir=tmp_path / "s" / "x",
                engine=OfflineEngine(), source_factory=lambda d: ReplaySource({}), settings=QUIET)
    s.state.set_attendees([{"role": "PDG", "persona": "executive", "origin": "rule"}])
    s.state.mode, s.state.focus_id = "interview", "orders-walkthrough"
    s.state.apply_sync_result(result(slots=[fill("bakery-orders", "actors", "Claire", "Claire saisit")]))
    with serve(create_app(s), APP_PORT) as url:
        r = run_node(tmp_path, INTERVIEW_SCRIPT, url)
    assert r["errors"] == [] and r["askTo"] == "" and r["chip"] == "Dans la réunion : PDG · Dirigeant"
