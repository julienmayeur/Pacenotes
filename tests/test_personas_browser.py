"""Real-browser checks (Node + Playwright, skipped without them) of the interviewee personas, with fake
data and no model call: the admin's "Who we meet" rows (notes, the inferred persona and why, the small
override), the consent screen's person and persona pick, the interview card for an executive and for an
operator on the same workflow ("To check with" instead of pushing), and the Plan view's "To check with
other people" section. Fixed spare ports 8796 (admin) and 8797 (interview and Plan view)."""

from __future__ import annotations

import asyncio

import pytest
import yaml

from pacenote.admin import create_admin_app
from pacenote.capture import ReplaySource
from pacenote.llm import OfflineEngine
from pacenote.questionnaire import load_client
from pacenote.server import create_app
from pacenote.session import Session

from .conftest import CLIENTS, fill, result, upd
from .test_horizons_browser import ADMIN_PORT, APP_PORT, PLAN_SCRIPT, QUIET, run_node, serve
from .test_i18n import _node_playwright
from .test_personas import DEFER, TEAM, _client

pytestmark = pytest.mark.skipif(not _node_playwright(), reason="needs node + playwright")

ADMIN_SCRIPT = r"""
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  p.on('pageerror', (e) => errors.push(String(e)));
  await p.addInitScript(() => localStorage.setItem('pacenote.ui-language', 'en'));
  await p.goto(process.argv[2] + '/admin?client=brioche-four');
  await p.waitForFunction(() => document.querySelectorAll('#f-contacts li .c-why').length === 3
    && [...document.querySelectorAll('#f-contacts li .c-why')].every((x) => x.textContent.length > 5));
  const read = () => p.evaluate(() => [...document.querySelectorAll('#f-contacts li')].map((li) => ({
    role: li.querySelector('.c-role').value, notes: li.querySelector('.c-notes').value,
    pick: li.querySelector('.c-persona').value,
    auto: li.querySelector('.c-persona option[value=""]').textContent,
    why: li.querySelector('.c-why').textContent })));
  const before = await read();
  await p.selectOption('#f-contacts li:nth-child(2) .c-persona', 'function_leader');
  await p.waitForFunction(() => document.querySelector('#f-contacts li:nth-child(2) .c-why').textContent.includes('set by hand'));
  const after = await read();
  console.log(JSON.stringify({ before, after, errors }));
  await b.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""


def test_admin_rows_show_the_inferred_persona_and_take_an_override(tmp_path):
    _client(tmp_path, [*TEAM, {"name": "Inès", "role": "Responsable achats"}], headcount="35")
    with serve(create_admin_app(tmp_path / "clients"), ADMIN_PORT) as url:
        r = run_node(tmp_path, ADMIN_SCRIPT, url)
    assert r["errors"] == []
    b, a = r["before"], r["after"]
    assert [x["auto"] for x in b] == ["Auto · Executive", "Auto · Hands-on operator", "Auto · Function leader"]
    assert all(x["pick"] == "" for x in b) and b[1]["notes"] == "saisit les commandes"
    assert b[0]["why"].startswith("Executive — Big picture") and "PDG (35 people)" in b[0]["why"]
    assert "from the title and size" in b[0]["why"] and "% sure" in b[0]["why"]
    assert a[1]["pick"] == "function_leader" and a[1]["why"].startswith("Function leader — Process and team")
    saved = yaml.safe_load((tmp_path / "clients" / "brioche-four" / "client.yaml").read_text(encoding="utf-8"))
    assert [c["persona"] for c in saved["contacts"]] == ["", "function_leader", ""]


CONSENT_SCRIPT = r"""
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  p.on('pageerror', (e) => errors.push(String(e)));
  await p.goto(process.argv[2] + '/');
  await p.waitForFunction(() => document.querySelectorAll('#who-person option').length === 2);
  const read = () => p.evaluate(() => ({
    people: [...document.querySelectorAll('#who-person option')].map((o) => o.textContent),
    person: document.querySelector('#who-person').value, persona: document.querySelector('#who-persona').value,
    auto: document.querySelector('#who-persona option[value=""]').textContent,
    why: document.querySelector('#who-why').textContent }));
  const first = await read();
  await p.selectOption('#who-person', '1');
  await p.waitForFunction(() => document.querySelector('#who-persona option[value=""]').textContent.includes('operator'));
  const second = await read();
  console.log(JSON.stringify({ first, second, errors }));
  await b.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""


def test_consent_screen_picks_the_person_and_shows_their_persona(tmp_path):
    s = Session(client=_client(tmp_path, TEAM), session_dir=tmp_path / "s", engine=OfflineEngine(),
                source_factory=lambda d: ReplaySource({}), settings=QUIET)
    s.state.ui_language = "en"
    with serve(create_app(s), APP_PORT) as url:
        r = run_node(tmp_path, CONSENT_SCRIPT, url)
    assert r["errors"] == []
    assert r["first"]["people"] == ["PDG · Paul", "Assistante ADV · Claire"]
    assert r["first"]["person"] == "0" and r["first"]["persona"] == "" and r["first"]["auto"] == "Auto · Executive"
    assert r["second"]["auto"] == "Auto · Hands-on operator" and "step by step" in r["second"]["why"]
    assert s.state.persona == "operator"
    assert s.state.get("core-waited-on-you").text("en").startswith("Last week, what did you have to wait for")


CARD_SCRIPT = r"""
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  p.on('pageerror', (e) => errors.push(String(e)));
  await p.goto(process.argv[2] + '/');
  await p.waitForFunction(() => document.querySelector('#ask-text').textContent.length > 5);
  const r = await p.evaluate(() => ({
    chip: document.querySelector('#who-chip').textContent,
    ask: document.querySelector('#ask-text').textContent,
    toCheckLabel: document.querySelector('#to-check .amb-label').textContent,
    toCheck: document.querySelector('#to-check').getClientRects().length ? document.querySelector('#to-check-text').textContent : '',
    say: document.querySelector('#framing-say').getClientRects().length ? document.querySelector('#framing-text').textContent : '',
    missing: [...document.querySelectorAll('#missing li')].map((li) => li.textContent),
    console: [...document.querySelectorAll('#console-to-check-list li')].map((li) => li.textContent) }));
  console.log(JSON.stringify({ ...r, errors }));
  await b.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""

EXEC_FOLLOW = "Roughly, how much of the order desk's week goes into keying in the bakeries' orders?"
OPER_FOLLOW = "Take this morning's first order: which screen do you open first, and how long does that one order take?"
POINT = "Re-keying every order can run on its own: Claire's time goes to the big bakery accounts."


def card_session(tmp_path, persona: str) -> Session:
    s = Session(client=load_client(CLIENTS, "brioche-four-slots"), session_dir=tmp_path / persona, engine=OfflineEngine(),
                source_factory=lambda d: ReplaySource({}), settings=QUIET)
    st = s.state
    st.language = st.ui_language = "en"
    role = "CEO" if persona == "executive" else "Order desk assistant"
    asyncio.run(s.set_interviewee(persona=persona))
    st.interviewee |= {"role": role}
    st.mode, st.focus_id = "interview", "orders-walkthrough"
    common = [fill("bakery-orders", "actors", "Claire", "Claire types every order"),
              fill("bakery-orders", "trigger", "the orders mailbox", "they come in by email")]
    if persona == "executive":
        st.apply_sync_result(result(
            question_updates=[upd("orders-walkthrough", "partial", "Claire keys the bakeries' email orders in.",
                                  follow_up=EXEC_FOLLOW, rung="workflow")],
            slots=common + [fill("bakery-orders", "processing", "re-keyed into Excel then the ERP", "she types it all again")],
            to_check=[{**DEFER, "evidence": "no idea, Claire handles that"}],
            value_framings=[{"use_case": "bakery-orders", "person": "Claire", "task": "re-keying order emails",
                             "point": POINT, "moves_up_to": "the big bakery accounts", "reason": "described on a real case",
                             "timing": "now"}]))
    else:
        st.apply_sync_result(result(
            question_updates=[upd("orders-walkthrough", "partial", "Orders arrive by email; each is typed in by hand.",
                                  follow_up=OPER_FOLLOW, rung="workflow")],
            slots=common + [fill("bakery-orders", "processing", "typed line by line into Excel, then Sage", "I type each line")],
            to_check=[{"use_case": "bakery-orders", "question_id": None, "slots": ["target"],
                       "question": "Which figure would show the order desk is doing better, and by when?",
                       "ask": "the CEO", "evidence": "that's for the boss to say"}]))
    return s


@pytest.mark.parametrize("persona", ["executive", "operator"])
def test_the_card_for_an_executive_and_an_operator_on_the_same_workflow(tmp_path, persona):
    s = card_session(tmp_path, persona)
    with serve(create_app(s), APP_PORT) as url:
        r = run_node(tmp_path, CARD_SCRIPT, url)
    assert r["errors"] == []
    if persona == "executive":
        assert r["chip"] == "Interviewing: CEO · Executive" and r["ask"] == EXEC_FOLLOW
        assert r["toCheckLabel"] == "To check with"
        assert r["toCheck"] == "Claire (order desk): Which software are the orders keyed into, and how many a day?"
        assert r["say"] == POINT
        assert not any("software" in m.lower() or "how many" in m.lower() for m in r["missing"])  # not pushed
    else:
        assert r["chip"] == "Interviewing: Order desk assistant · Hands-on operator" and r["ask"] == OPER_FOLLOW
        assert r["toCheck"] == "the CEO: Which figure would show the order desk is doing better, and by when?"
        assert r["say"] == ""  # no talking point about their own work
    assert len(r["console"]) == 1


def test_plan_view_lists_what_to_check_with_whom(tmp_path):
    s = card_session(tmp_path, "executive")
    s.state.language = "fr"
    s.session_dir = tmp_path / "s" / "20261116-100000"
    s.session_dir.mkdir(parents=True)
    asyncio.run(s.finalize())
    with serve(create_app(s), APP_PORT) as url:
        r = run_node(tmp_path, PLAN_SCRIPT, url)
    assert r["errors"] == []
    text = r["text"]
    assert "Personne interrogée : CEO — Dirigeant" in text
    assert "À vérifier avec d'autres personnes" in text and "Avec Claire (order desk)" in text
    assert "Which software are the orders keyed into, and how many a day?" in text
