"""Real-browser checks (Node + Playwright, skipped without them) of who is in the meeting, with fake data and
no model call: the admin's attendee rows with their persona and prep note, the interviewer console's views
heard (three distinct viewpoints, grouped by statement, not by person), the interview card suggesting who to
address the question to, and the plan listing the points to address with no one named. Fixed spare ports
8796 (admin) and 8797 (interview). Each script takes optional screenshot paths. Fictional people and
companies only."""

from __future__ import annotations

import asyncio

import pytest

from pacenote.copilot.contract import AskTo
from pacenote.copilot.engines import OfflineEngine
from pacenote.interview.preparation import load_client
from pacenote.interview.session import Session
from pacenote.transcription.capture import ReplaySource
from pacenote.web.admin import create_admin_app
from pacenote.web.server import create_app
from tests.conftest import CLIENTS, fill, result, upd
from tests.people.test_personas import _client
from tests.people.test_sponsor_browser import PLAN_SCRIPT
from tests.people.test_viewpoints import THREE
from tests.research.test_horizons_browser import ADMIN_PORT, APP_PORT, QUIET, run_node, serve
from tests.shared.test_i18n import _node_playwright

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
    assert all("Position sur l'IA" not in x["prep"] for x in rows)  # no stance per person


def meeting_session(tmp_path) -> Session:
    """Three people in the meeting and three views heard (keen, a practical concern, against it), kept by
    statement; the card is on the order entry, at the per-item detail."""
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
        viewpoints=THREE), read=40)
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
  const views = await p.evaluate(() => [...document.querySelectorAll('#console-viewpoint-list li')].map((li) => li.textContent));
  const speakers = await p.evaluate(() => document.querySelector('#console-speakers').textContent);
  if (process.argv[4]) {
    const box = await p.evaluate(() => {
      const a = document.querySelector('#console-attendees').getBoundingClientRect();
      const v = document.querySelector('#console-viewpoints').getBoundingClientRect();
      return { x: Math.min(a.left, v.left) - 8, y: a.top + window.scrollY - 8,
               width: Math.max(a.width, v.width) + 16, height: v.bottom - a.top + 16 };
    });
    await p.screenshot({ path: process.argv[4], clip: box, fullPage: true });
  }
  console.log(JSON.stringify({ ...card, people, views, speakers, errors }));
  await b.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""


def test_the_console_shows_the_views_heard_by_statement_and_the_card_who_to_ask(tmp_path):
    s = meeting_session(tmp_path)
    with serve(create_app(s), APP_PORT) as url:
        r = run_node(tmp_path, INTERVIEW_SCRIPT, url)
    assert r["errors"] == []
    assert r["chip"] == "Dans la réunion : PDG · Dirigeant +2"
    assert r["askTo"] == "→ Demander à Léa Morin (Assistante ADV)"  # the per-item detail: the one who does it
    p = r["people"]
    assert p[0].startswith("PDG · Hélène Varenne · Dirigeant · responsable · sponsor")
    assert all(x not in " ".join(p) for x in ("Moteur", "Opposé", "Hésitant", "sûr à"))  # no stance per person
    v = r["views"]
    assert len(v) == 3
    assert v[0].startswith("Partant aller plus loin avec un assistant déjà utilisé · comités de direction")
    assert "« En tant que PDG, j'utilise déjà un assistant pour préparer mes comités » — Hélène Varenne (PDG)" in v[0]
    assert v[1].startswith("Opposé garder le contact humain avec les clients · relation client")
    assert v[1].endswith("« Nos clients veulent parler à quelqu'un, pas à un robot »")  # nobody named: no name
    assert v[2].startswith("Question pratique fiabilité de l'extraction")
    assert r["speakers"] == ""  # no final transcript pasted yet
    for leak in ("Partant", "Opposé", "robot", "fiabilité"):  # the views never on the interview card itself
        assert leak not in r["page"], leak


def test_after_the_meeting_a_labelled_transcript_shows_the_voices(tmp_path):
    from tests.people.test_viewpoints import LABELLED

    s = meeting_session(tmp_path)
    asyncio.run(s.set_wispr(LABELLED))
    with serve(create_app(s), APP_PORT) as url:
        r = run_node(tmp_path, INTERVIEW_SCRIPT, url)
    assert r["errors"] == []
    assert r["speakers"] == "Après la réunion : voix tirées de la transcription collée (3 citations retrouvées)."
    assert "voix : Speaker 3" in r["views"][1]
    asyncio.run(s.set_wispr("Une transcription dictée, sans intervenants."))
    with serve(create_app(s), APP_PORT) as url:
        r = run_node(tmp_path, INTERVIEW_SCRIPT, url)
    assert r["speakers"].startswith("Après la réunion : voix indisponibles") and "voix :" not in " ".join(r["views"])


def test_the_plan_lists_the_points_to_address_with_no_one_named(tmp_path):
    s = meeting_session(tmp_path)
    asyncio.run(s.finalize())
    with serve(create_app(s), APP_PORT) as url:
        r = run_node(tmp_path, PLAN_SCRIPT, url)
    assert r["errors"] == []
    a = r["adoption"]
    assert "Points soulevés pendant la réunion, à traiter :" in a
    assert "garder le contact humain avec les clients (relation client)" in a
    assert "fiabilité de l'extraction sur les commandes manuscrites, mise en doute (saisie des commandes)" in a
    for leak in ("Varenne", "Ardent", "Morin", "robot", "Opposé", "Partant", "Prudent", "Speaker"):
        assert leak not in r["text"], leak


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
