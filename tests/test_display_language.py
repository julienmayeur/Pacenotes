"""One display language at a time, chosen top right of the interview header.

The header toggle (<language-select id="ui-switch" bind="ui" segmented>) is THE display language:
labels AND the question texts, follow-ups, ladder and missing items. No second-language gloss is
ever rendered. It switches live (no reload), persists in the session (state.json, so it survives a
reload), defaults to the client's interview language, and never touches the spoken languages (speech
locales) nor the interview language (prompts, generated follow-ups, plan)."""

import json
import re
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from pacenote import i18n
from pacenote.server import create_app
from pacenote.session import Session
from pacenote.state import SessionState

from .conftest import real_client
from .test_i18n import TranslatingEngine, _node_playwright
from .test_interview_layout import _session as layout_session

STATIC = Path(i18n.__file__).parent / "static"


def test_no_second_language_elements_in_the_markup():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8") + (STATIC / "i18n.js").read_text(encoding="utf-8")
    for gone in ("current-alt", "ask-alt", "consent-secondary", "banner-secondary", "console-ui"):
        assert gone not in html and gone not in js, gone
    assert "secondary" not in re.sub(r"//.*|/\*.*?\*/", "", js, flags=re.S)
    # exactly one display-language toggle on the interview screen (the consent screen keeps its own,
    # the header is hidden under it), and it sits in the header's right-hand block
    assert html.count('bind="ui"') == 2
    header = html.split('<header class="top">', 1)[1].split("</header>", 1)[0]
    meta = header.split('<div class="meta">', 1)[1]
    assert 'id="ui-switch"' in meta and "segmented" in meta.split("</language-select>", 1)[0]


def test_display_language_keys_exist_in_every_ui_language():
    for lang in i18n.available():
        cat = i18n.flatten(i18n.catalog(lang))
        for key in ("lang.display", "consent.ui", "interview.noQuestion", "interview.bannerDone"):
            assert cat.get(key), f"{lang}: {key}"


def _session(tmp_path) -> Session:
    s = layout_session(tmp_path, real_client(), with_follow_up=True)  # the real client: `expect` items
    s.engine = TranslatingEngine()  # stands in for `claude -p`: "[en] <text>"
    return s


BROWSER = r"""
const { chromium } = require('playwright');
(async () => {
  const url = process.argv[2];
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 623, height: 1024 } });
  const errors = [];
  p.on('pageerror', (e) => errors.push(String(e)));
  await p.goto(url);
  await p.waitForFunction(() => document.querySelector('#ask-text').textContent.length > 3
    && document.querySelector('#ui-switch button.on'));
  await p.waitForTimeout(300);
  const snap = () => p.evaluate(async () => {
    const st = await (await fetch('/api/state')).json();
    const sw = document.querySelector('#ui-switch');
    const r = sw.getBoundingClientRect();
    return {
      lang: document.documentElement.lang,
      on: sw.querySelector('button.on').dataset.lang,
      buttons: [...sw.querySelectorAll('button')].map((x) => x.textContent),
      topRight: r.right > window.innerWidth * 0.7 && r.top < 80,
      label: document.querySelector('[data-i18n="focus.theme"]').textContent,
      missingTitle: document.querySelector('.missing-title').textContent,
      current: document.querySelector('#current').textContent,
      ask: document.querySelector('#ask-text').textContent,
      followups: [...document.querySelectorAll('#thread li > span:first-child')].map((x) => x.textContent),
      missing: [...document.querySelectorAll('#missing li')].map((x) => x.textContent),
      upcoming: [...document.querySelectorAll('#next .q')].map((x) => x.textContent),
      bodyText: document.body.innerText,
      secondaryEls: document.querySelectorAll('[id$="-alt"], [id$="-secondary"], .current-alt, .ask-alt').length,
      speech: st.languages.speech, streams: st.languages.streams, interview: st.language,
      chips: [...document.querySelectorAll('#langs .chip')].map((x) => x.textContent),
    };
  });
  const fr = await snap();
  await p.click('#ui-switch button[data-lang="en"]');            // live, no reload
  await p.waitForFunction((was) => document.documentElement.lang === 'en'
    && document.querySelector('#ask-text').textContent.startsWith('[en] ')
    && document.querySelector('#current').textContent !== was, fr.current, { timeout: 8000 });
  await p.waitForTimeout(300);
  const en = await snap();
  const navs = await p.evaluate(() => performance.getEntriesByType('navigation').length);
  await p.reload();                                                // persists across a reload
  await p.waitForFunction(() => document.querySelector('#ui-switch button.on')
    && document.querySelector('#current').textContent.length > 3);
  await p.waitForTimeout(300);
  const reloaded = await snap();
  console.log(JSON.stringify({ fr, en, navs, reloaded, errors }));
  await b.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""


def _run(tmp_path, session) -> dict:
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
        script = tmp_path / "display.js"
        script.write_text(BROWSER, encoding="utf-8")
        out = subprocess.run(["node", str(script), f"http://127.0.0.1:{port}/"], capture_output=True, text=True,
                             timeout=120, cwd=tmp_path)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout.strip().splitlines()[-1])
    finally:
        server.should_exit = True
        th.join(timeout=10)


@pytest.mark.skipif(not _node_playwright(), reason="needs node + playwright + chromium")
def test_display_language_switches_live_persists_and_leaves_capture_alone(tmp_path):
    session = _session(tmp_path)
    st = session.state
    q = st.current_question
    speech0 = list(st.speech_locales)
    r = _run(tmp_path, session)
    fr, en, again = r["fr"], r["en"], r["reloaded"]
    assert r["errors"] == [], r["errors"]

    # default: the client's interview language; the toggle is FR | EN, top right
    assert fr["interview"] == "fr" and fr["on"] == "fr" and fr["lang"] == "fr"
    assert fr["buttons"] == ["EN", "FR"] or fr["buttons"] == ["FR", "EN"]
    assert fr["topRight"]
    assert fr["label"] == "Thème d'étude" and fr["current"] == q.text("fr")
    assert fr["ask"] == "Qui décroche ces appels aujourd'hui ?"
    # no second language anywhere: no gloss element, the English wording is nowhere on the page
    assert fr["secondaryEls"] == 0 and q.text("en")[:30] not in fr["bodyText"]

    # FR -> EN: labels, question, follow-ups (asked + to ask), missing items and upcoming, live
    assert en["lang"] == "en" and en["on"] == "en" and r["navs"] == 1
    assert en["label"] == "Study theme" and en["missingTitle"] == i18n.t("focus.missing", "en")
    assert en["current"] == q.text("en") and en["ask"] == "[en] Qui décroche ces appels aujourd'hui ?"
    assert en["followups"] == ["[en] Combien d'appels par jour ?"]
    assert fr["missing"] == [q.expect_text(e, "fr") for e in q.missing("business")]
    assert en["missing"] == [q.expect_text(e, "en") for e in q.missing("business")] != fr["missing"]
    assert en["upcoming"] and en["upcoming"] != fr["upcoming"]
    assert en["secondaryEls"] == 0 and q.text("fr")[:30] not in en["bodyText"]

    # persisted (session state, so it survives a reload) and still EN after it
    assert again["on"] == "en" and again["current"] == q.text("en") and again["ask"].startswith("[en] ")
    assert SessionState.load(tmp_path / "s" / "state.json").ui_language == "en"

    # the spoken languages (speech capture) and the interview language never moved
    for v in (fr, en, again):
        assert v["speech"] == speech0 and v["interview"] == "fr"
        assert v["streams"] == fr["streams"] and v["chips"] == fr["chips"]
    assert st.language == "fr" and st.speech_locales == speech0
    # follow-ups are stored (and prompted) as generated, in French; English is display only
    assert [e.text for e in q.thread if e.kind == "followup"] == ["Combien d'appels par jour ?",
                                                                 "Qui décroche ces appels aujourd'hui ?"]
