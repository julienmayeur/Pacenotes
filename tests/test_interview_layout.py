"""The live interview card's hierarchy, in a real browser: a calm theme header (with the question's own
Business > Workflow > Implementation ladder), ONE focal point (the follow-up to ask now, or the theme
question itself when there is none yet), quiet secondary details, and a small type scale."""

import json
import socket
import subprocess
import threading
import time

import pytest
import uvicorn

from pacenote.capture import ReplaySource
from pacenote import i18n
from pacenote.llm import OfflineEngine
from pacenote.server import create_app
from pacenote.session import Session, Settings
from pacenote.state import ThreadEntry

from .conftest import SAMPLES
from .test_i18n import _node_playwright

QUIET = Settings(sync_interval=3600, first_sync_delay=3600, poll_interval=3600, fast_enabled=False)


def test_ask_now_and_theme_keys_exist_in_every_ui_language():
    for lang in ("fr", "en"):
        cat = i18n.flatten(i18n.catalog(lang))
        for key in ("focus.theme", "focus.ask", "focus.askTheme", "focus.missing", "focus.got", "focus.summary",
                    "focus.followups", "focus.ladder"):
            assert cat.get(key), f"{lang}: {key}"


def _session(tmp_path, client_cfg, *, with_follow_up: bool):
    s = Session(client=client_cfg, session_dir=tmp_path / "s", engine=OfflineEngine(),
                source_factory=lambda d: ReplaySource({"fr": SAMPLES / "fr.srt", "en": SAMPLES / "en.srt"}, speed=0),
                settings=QUIET)
    st = s.state
    st.mode = "interview"
    q = st.current_question
    business = q.rung_items("business")
    q.status, q.rung, q.covered = "partial", "business", business[:1]
    q.answer = "Il faudrait du personnel en plus ; le téléphone coincerait en premier."
    now = time.time()
    q.thread = [ThreadEntry(kind="answer", text="Le téléphone.", rung="business", at=now - 60),
                ThreadEntry(kind="followup", text="Combien d'appels par jour ?", rung="business", at=now - 50),
                ThreadEntry(kind="answer", text="Un toutes les 15 minutes.", rung="business", at=now - 30)]
    if with_follow_up:
        q.thread.append(ThreadEntry(kind="followup", text="Qui décroche ces appels aujourd'hui ?", rung="business",
                                    at=now - 10))
    q.updated_at, q.updated_by = now - 5, "fast"
    return s


LAYOUT_SCRIPT = r"""
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const out = {};
  for (const [name, w, h] of [['narrow', 623, 1024], ['desktop', 1280, 900]]) {
    const p = await b.newPage({ viewport: { width: w, height: h } });
    const errors = [];
    p.on('pageerror', (e) => errors.push(String(e)));
    await p.goto(process.argv[2]);
    await p.waitForFunction(() => document.querySelector('#ask-text').textContent.length > 3);
    await p.waitForTimeout(400);
    out[name] = await p.evaluate(() => {
      const visible = (el) => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
      const ownText = (el) => [...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim());
      const texts = (root) => [...root.querySelectorAll('*')].filter((el) => visible(el) && ownText(el));
      const px = (el) => parseFloat(getComputedStyle(el).fontSize);
      const ask = document.querySelector('#ask-text');
      const page = texts(document.body);
      const largest = Math.max(...page.map(px));
      const card = texts(document.querySelector('#current-card'));
      return {
        askPx: px(ask), largest, largestEl: page.filter((e) => px(e) === largest).map((e) => e.id || e.className),
        askInMissing: document.querySelector('#missing-card').contains(ask),
        askLabel: document.querySelector('#ask-label').textContent,
        askText: ask.textContent,
        cardSizes: [...new Set(card.map(px))].sort((a, b) => a - b),
        ladderInTheme: document.querySelector('#theme-head').contains(document.querySelector('#ladder')),
        ladderAboveAsk: document.querySelector('#ladder').getBoundingClientRect().bottom
          <= document.querySelector('#ask-now').getBoundingClientRect().top,
        ladderInTop: document.querySelector('header.top').contains(document.querySelector('#ladder')),
        themeShown: visible(document.querySelector('#current')),
        currentText: document.querySelector('#current').textContent,
      };
    });
    out[name].errors = errors;
    await p.close();
  }
  // keyboard shortcuts still drive the interview: "n" moves to the next question
  const p = await b.newPage({ viewport: { width: 1280, height: 900 } });
  await p.goto(process.argv[2]);
  await p.waitForFunction(() => document.querySelector('#current').textContent.length > 3);
  const before = await p.evaluate(() => document.querySelector('#current').textContent);
  await p.keyboard.press('n');
  await p.waitForFunction((t) => document.querySelector('#current').textContent !== t, before, { timeout: 8000 });
  out.movedOn = true;
  await b.close();
  console.log(JSON.stringify(out));
})().catch(e => { console.error(e); process.exit(1); });
"""


def _run(tmp_path, session):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(create_app(session), host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    try:
        deadline = time.time() + 10
        while not server.started and time.time() < deadline:
            time.sleep(0.05)
        script = tmp_path / "layout.js"
        script.write_text(LAYOUT_SCRIPT, encoding="utf-8")
        out = subprocess.run(["node", str(script), f"http://127.0.0.1:{port}/"], capture_output=True, text=True,
                             timeout=120, cwd=tmp_path)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout.strip().splitlines()[-1])
    finally:
        server.should_exit = True
        th.join(timeout=10)


@pytest.mark.skipif(not _node_playwright(), reason="needs node + playwright + chromium")
def test_ask_now_is_the_focal_point_and_the_card_uses_a_small_type_scale(tmp_path, client_cfg):
    session = _session(tmp_path, client_cfg, with_follow_up=True)
    theme = session.state.current_question.text("fr")
    r = _run(tmp_path, session)
    for name in ("narrow", "desktop"):
        v = r[name]
        assert v["errors"] == [], v["errors"]
        # the follow-up to ask is THE largest text on the page, outside "Ce qui manque"
        assert v["askText"] == "Qui décroche ces appels aujourd'hui ?"
        assert v["askLabel"] == "à demander maintenant"
        assert v["askPx"] == v["largest"], (name, v["largestEl"])
        assert not v["askInMissing"]
        # the theme question stays visible, as a modest header
        assert v["themeShown"] and v["currentText"] == theme
        # 4 type sizes at most inside the interview card
        assert len(v["cardSizes"]) <= 4, (name, v["cardSizes"])
        # the breadcrumb is the question's own ladder: attached to the theme header, above "ask now"
        assert v["ladderInTheme"] and v["ladderAboveAsk"] and not v["ladderInTop"]
    assert r["movedOn"]


@pytest.mark.skipif(not _node_playwright(), reason="needs node + playwright + chromium")
def test_without_a_follow_up_the_theme_question_takes_the_focal_spot(tmp_path, client_cfg):
    session = _session(tmp_path, client_cfg, with_follow_up=False)
    theme = session.state.current_question.text("fr")
    r = _run(tmp_path, session)
    for name in ("narrow", "desktop"):
        v = r[name]
        assert v["errors"] == []
        assert v["askText"] == theme and v["askLabel"] == "Question à poser"
        assert v["askPx"] == v["largest"]
        assert not v["themeShown"]  # shown once, at focal size; the header keeps its label and ladder
        assert len(v["cardSizes"]) <= 4, (name, v["cardSizes"])
