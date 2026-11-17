"""Real-browser checks (Node + Playwright, skipped without them) of the horizon and ambassador screens,
with fake data and no model call: the admin pre-study (pace, criteria, competitors, override) and
the ambassadors list (edit, remove), the interview's ambassador question, and the Plan view grouped
by horizon in plain words with its adoption plan. Fixed spare ports 8796 (admin) and 8797 (interview
and Plan view)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import subprocess
import threading
import time

import pytest
import uvicorn

from pacenote.admin import create_admin_app
from pacenote.capture import ReplaySource
from pacenote.contract import parse_plan_doc
from pacenote.llm import OfflineEngine
from pacenote.preparation import load_client
from pacenote.server import create_app
from pacenote.session import Session, Settings

from .conftest import CLIENTS, REAL_CLIENTS, fill, plan_doc, plan_use_case, result
from .test_admin import RESULT
from .test_i18n import _node_playwright
from .test_phases import maps, org

ADMIN_PORT, APP_PORT = 8796, 8797
QUIET = Settings(sync_interval=3600, first_sync_delay=3600, poll_interval=3600, fast_enabled=False)
pytestmark = pytest.mark.skipif(not _node_playwright(), reason="needs node + playwright")


@contextlib.contextmanager
def serve(app, port: int):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, f"port {port} busy?"
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        th.join(timeout=10)


def run_node(tmp_path, script: str, *args: str) -> dict:
    path = tmp_path / f"check-{abs(hash(script)) % 10**6}.js"
    path.write_text(script, encoding="utf-8")
    out = subprocess.run(["node", str(path), *args], capture_output=True, text=True, timeout=120, cwd=tmp_path)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


# --- admin: pre-study and ambassadors -----------------------------------------------------------------

ADMIN_SCRIPT = r"""
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  p.on('pageerror', (e) => errors.push(String(e)));
  await p.addInitScript(() => localStorage.setItem('pacenote.ui-language', 'en'));
  await p.goto(process.argv[2] + '/admin?client=brioche-four');
  await p.waitForSelector('#client:not(.hidden)');
  await p.click('#tabs button[data-tab="prestudy"]');
  await p.waitForSelector('#prestudy .posture .pill');
  const read = () => p.evaluate(() => ({
    pill: document.querySelector('#prestudy .pill').textContent,
    score: (document.querySelector('#prestudy .score') || {}).textContent || '',
    origin: document.querySelector('#prestudy .posture .muted').textContent,
    criteria: [...document.querySelectorAll('#prestudy .criteria b')].map((x) => x.textContent),
    competitors: [...document.querySelectorAll('#prestudy .competitors li')].map((li) => ({ text: li.textContent, counted: !li.classList.contains('not-counted') })),
    operatorOnly: document.querySelector('[data-tab="prestudy"] p.muted').textContent,
    horizon: [...document.querySelectorAll('#cases .hz')].map((x) => x.textContent) }));
  const before = await read();
  await p.selectOption('#posture-override', 'urgent');
  await p.waitForFunction(() => document.querySelector('#prestudy .pill').textContent.startsWith('Urgent'));
  const after = await read();
  await p.click('#tabs button[data-tab="ambassadors"]');
  await p.waitForSelector('#ambassadors tr');
  await p.fill('#ambassadors tr:nth-child(1) .a-quote', 'volunteered to test it');
  await p.click('#amb-add');
  await p.fill('#ambassadors tr:nth-child(3) .a-person', 'Sophie');
  await p.click('#ambassadors tr:nth-child(2) [data-arm]');
  await p.click('#amb-save');
  await p.waitForFunction(() => document.querySelector('#amb-state').textContent === 'Saved.');
  const amb = await p.evaluate(() => [...document.querySelectorAll('#ambassadors tr')].map((tr) => ({
    person: tr.querySelector('.a-person').value, by: tr.querySelector('.by').textContent })));
  console.log(JSON.stringify({ before, after, amb, errors }));
  await b.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""


def test_admin_prestudy_and_ambassadors_in_a_browser(tmp_path):
    shutil.copytree(REAL_CLIENTS / "brioche-four", tmp_path / "clients" / "brioche-four")
    d = tmp_path / "clients" / "brioche-four"
    meta = {k: RESULT[k] for k in ("summary", "use_cases", "seed_questions", "sources", "prestudy")}
    meta["prestudy"] = {**meta["prestudy"], "competitors": [
        *meta["prestudy"]["competitors"], {"competitor": "Panidis", "signal": "chatbot", "result": "", "url": "", "date": ""}]}
    (d / "preplan.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    (d / "preplan.md").write_text("# Pré-plan\n", encoding="utf-8")
    entry = {"label": "potential", "by": "agent", "session": "s1", "updated_at": "2026-11-15T10:00:00"}
    (d / "ambassadors.json").write_text(json.dumps({"entries": [
        {"id": "claire", "person": "Claire", "role": "ADV", "use_case": "Commandes", "quote": "elle essaie tout", **entry},
        {"id": "marc", "person": "Marc", "role": "Dépôt", "use_case": "Stock", "quote": "toujours partant", **entry}],
        "removed": []}, ensure_ascii=False), encoding="utf-8")
    with serve(create_admin_app(tmp_path / "clients"), ADMIN_PORT) as url:
        r = run_node(tmp_path, ADMIN_SCRIPT, url)
    assert r["errors"] == []
    b, a = r["before"], r["after"]
    assert b["pill"] == "Pragmatic" and b["score"] == "Score 6/10" and "from the pre-study" in b["origin"]
    assert b["criteria"] == ["1/3", "2/3", "1/2", "2/2"]
    assert [c["counted"] for c in b["competitors"]] == [True, False] and "not counted" in b["competitors"][1]["text"]
    assert b["operatorOnly"].startswith("For you only") and b["horizon"] == ["This week"]
    assert a["pill"] == "Urgent · move fast" and "set by you" in a["origin"]
    assert "posture_override: urgent" in (d / "client.yaml").read_text(encoding="utf-8")
    assert r["amb"] == [{"person": "Claire", "by": "edited by you"}, {"person": "Sophie", "by": "edited by you"}]
    saved = json.loads((d / "ambassadors.json").read_text(encoding="utf-8"))
    assert [(e["person"], e["by"], e["quote"]) for e in saved["entries"]] == [
        ("Claire", "manual", "volunteered to test it"), ("Sophie", "manual", "")]
    assert saved["removed"] == ["marc"]


# --- the interview: the ambassador question -----------------------------------------------------------------

INTERVIEW_SCRIPT = r"""
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  p.on('pageerror', (e) => errors.push(String(e)));
  await p.goto(process.argv[2] + '/');
  await p.waitForFunction(() => document.querySelector('#ambassador-text').textContent.length > 5);
  const r = await p.evaluate(() => ({
    label: document.querySelector('#ambassador-ask .amb-label').textContent,
    text: document.querySelector('#ambassador-text').textContent,
    visible: document.querySelector('#ambassador-ask').getClientRects().length > 0,
    card: document.querySelector('#ambassador-ask').textContent,
    afterAsk: document.querySelector('#ask-now').compareDocumentPosition(document.querySelector('#ambassador-ask')) === 4,
    askPx: parseFloat(getComputedStyle(document.querySelector('#ask-text')).fontSize),
    ambPx: parseFloat(getComputedStyle(document.querySelector('#ambassador-text')).fontSize),
    probesVisible: document.querySelector('#console-probe-list').getClientRects().length > 0,
    console: [...document.querySelectorAll('#console-ambassador-list li')].map((li) => li.textContent),
    probes: [...document.querySelectorAll('#console-probe-list li')].map((li) => li.textContent),
    read: document.querySelector('#console-read').textContent }));
  console.log(JSON.stringify({ ...r, errors }));
  await b.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""


def test_interview_shows_the_indirect_people_question_and_keeps_its_reason_in_the_console(tmp_path):
    s = Session(client=load_client(CLIENTS, "brioche-four-slots"), session_dir=tmp_path / "s", engine=OfflineEngine(),
                source_factory=lambda d: ReplaySource({}), settings=QUIET)
    st = s.state
    st.apply_sync_result(result(
        maps=maps(org=[org(rid="claire", person="Claire", role="ADV", adoption_signal="she always tries new things first")]),
        slots=[fill("bakery-orders", "actors", "Claire", "Claire types every order")],
        interviewee_read={"role": "founder", "ai_stance": "wary", "worries": "", "pride": "his order desk", "politics": "",
                          "tone": "warm, careful", "rapport": "established", "approach": "through habits, never change"},
        ambassador_probes=[{"use_case": "bakery-orders", "person": "Claire",
                            "question": "When Sage gets in the way on the order desk, is Claire the one people turn to?",
                            "reason": "whether Claire is the desk's go-to for tools: pilot for orders", "timing": "now"}]))
    st.mode, st.ui_language, st.focus_id = "interview", "en", "orders-walkthrough"
    with serve(create_app(s), APP_PORT) as url:
        r = run_node(tmp_path, INTERVIEW_SCRIPT, url)
    assert r["errors"] == []
    assert r["label"] == "Also worth asking · Bakery order entry"  # tied to the workflow, in the display language
    assert r["text"] == "When Sage gets in the way on the order desk, is Claire the one people turn to?"
    assert r["visible"] and r["afterAsk"] and r["ambPx"] < r["askPx"]  # quiet, under the focal follow-up
    assert "pilot" not in r["card"] and "tools:" not in r["card"]  # the reason never on the interview card
    assert not r["probesVisible"]  # the console is closed: only the interviewer opens it
    assert len(r["probes"]) == 1 and "whether Claire is the desk's go-to for tools" in r["probes"][0]
    assert "to ask now" in r["probes"][0] and r["read"] == "Tone: warm, careful · How to ask: through habits, never change"
    assert len(r["console"]) == 1 and "Claire" in r["console"][0] and "she always tries new things first" in r["console"][0]


# --- the Plan view: opportunities by horizon, the adoption plan ---------------------------------------------

PLAN_SCRIPT = r"""
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  p.on('pageerror', (e) => errors.push(String(e)));
  await p.goto(process.argv[2] + '/plan');
  await p.waitForSelector('#plan-doc h1');
  const r = await p.evaluate(() => ({
    h2: [...document.querySelectorAll('#plan-doc h2')].map((x) => x.textContent),
    h3: [...document.querySelectorAll('#plan-doc h3')].map((x) => x.textContent),
    text: document.querySelector('#plan-doc').textContent }));
  console.log(JSON.stringify({ ...r, errors }));
  await b.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""


class _PlanEngine(OfflineEngine):
    """The offline engine with a written plan: three opportunities on three horizons, Claire named."""

    def write_plan_doc(self, system, user, state=None):
        return parse_plan_doc(plan_doc(
            plan_use_case("orders", "Saisie des commandes", horizon="h1"),
            plan_use_case("sav", "Pré-diagnostic SAV", horizon="h2", feasibility=3),
            plan_use_case("portal", "Portail boulangers", horizon="h3", feasibility=3),
            adoption={"lighthouse": "orders", "pilots": [{"person": "Claire", "evidence": "elle essaie toujours en premier"}],
                      "volunteers": {"plan": "Dix commandes d'hier rejouées devant Claire.", "trigger": ""},
                      "majority": {"plan": "Claire présente le résultat à la réunion du lundi.", "trigger": ""}}))


def test_plan_view_groups_by_horizon_in_plain_words_in_a_browser(tmp_path):
    s = Session(client=load_client(CLIENTS, "brioche-four-slots"), session_dir=tmp_path / "s" / "20261115-100000",
                engine=_PlanEngine(), source_factory=lambda d: ReplaySource({}), settings=QUIET)
    s.state.posture = "urgent"  # operator-only: must never show
    asyncio.run(s.finalize())
    with serve(create_app(s), APP_PORT) as url:
        r = run_node(tmp_path, PLAN_SCRIPT, url)
    assert r["errors"] == []
    groups = [h for h in r["h3"] if h in ("Cette semaine", "Ce trimestre", "D'ici deux ans")]
    assert groups == ["Cette semaine", "Ce trimestre", "D'ici deux ans"]
    h2 = r["h2"]
    assert h2.index("Calendrier d'engagement") < h2.index("Plan d'adoption") < h2.index("Points à confirmer")
    text = r["text"]
    assert "Pilotes : Claire — « elle essaie toujours en premier »" in text
    assert "Claire présente le résultat à la réunion du lundi." in text
    for leak in ("H1", "H2", "H3", "urgent", "Urgent", "posture", "horizon"):
        assert leak not in text, leak
