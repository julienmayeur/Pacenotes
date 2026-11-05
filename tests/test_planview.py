"""The plan inside the app: the Plan view (end of the interview and per session from the admin
panel), Export PDF (a real PDF from a headless Chrome-family browser, print dialog otherwise),
plan.md + plan.json as the only files written, every visible string from the locale files."""

import asyncio
import json
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pacenote import i18n, planview
from pacenote.admin import create_admin_app
from pacenote.capture import ReplaySource
from pacenote.llm import OfflineEngine
from pacenote.questionnaire import load_client
from pacenote.server import STATIC_DIR, create_app
from pacenote.session import Session, Settings

from .conftest import CLIENTS, fill, result

QUIET = Settings(sync_interval=3600, first_sync_delay=3600, poll_interval=3600, fast_enabled=False)
SLUG = "brioche-four-slots"
SID = "20261102-164906"
HAS_CHROME = planview.find_chrome() is not None


def _session(sessions: Path, finalize: bool = True) -> Session:
    s = Session(client=load_client(CLIENTS, SLUG), session_dir=sessions / SLUG / SID, engine=OfflineEngine(),
                source_factory=lambda d: ReplaySource({}), settings=QUIET)
    s.state.apply_sync_result(result(slots=[fill("bakery-orders", "tool", "ERP Sage", "on saisit dans Sage"),
                                            fill("bakery-orders", "volume", "40 par semaine", "quarante par semaine")]))
    s.persist()
    if finalize:
        asyncio.run(s.finalize())
    return s


# --- files ----------------------------------------------------------------------------------------


def test_finalize_writes_plan_md_and_json_only(tmp_path):
    s = _session(tmp_path)
    names = sorted(p.name for p in s.session_dir.iterdir() if p.name.startswith("plan"))
    assert names == ["plan.json", "plan.md"]
    assert set(s.state.plan_files) == {"md", "json"}


# --- the interview server: /plan at the end of the interview ----------------------------------------


def test_interview_server_serves_the_plan_view_and_its_data(tmp_path):
    s = _session(tmp_path, finalize=False)
    with TestClient(create_app(s)) as http:
        assert http.get("/api/plan").status_code == 404  # not written yet: the page waits and retries
        page = http.get("/plan")
        assert page.status_code == 200 and '<script src="/static/plan.js"></script>' in page.text
        asyncio.run(s.finalize())
        d = http.get("/api/plan").json()
        assert d["client"] == "Brioche & Four" and d["session"] == SID and d["language"] == "fr"
        assert d["title"] == "Proposition d'intégration — Brioche & Four"
        assert "<h1>Proposition d'intégration — Brioche &amp; Four</h1>" in d["html"] and "<table>" in d["html"]
        assert "ERP Sage" in d["html"]  # a slot captured live, rendered from plan.md
        assert d["files"]["md"].endswith("plan.md") and d["files"]["json"].endswith("plan.json")
        assert http.get("/api/plan/md").text.startswith("# Proposition d'intégration")
        assert http.get("/plan.html").status_code == 404  # gone
        assert http.get("/static/plan.css").status_code == 200


def test_end_of_interview_links_to_the_plan_view():
    app_js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert 'href="/plan"' in app_js and 't("planView.open")' in app_js and "plan.html" not in app_js


# --- the admin panel: every saved session has its Plan view -----------------------------------------


def test_admin_lists_sessions_and_opens_each_plan(tmp_path):
    sessions = tmp_path / "sessions"
    _session(sessions)
    (sessions / SLUG / "20261101-090000").mkdir()  # an empty folder is not a session
    other = sessions / SLUG / "20261101-100000"
    other.mkdir()
    (other / "state.json").write_text(json.dumps({"client": "Brioche & Four", "mode": "interview", "questions": []}))
    with TestClient(create_admin_app(CLIENTS, sessions_dir=sessions)) as http:
        listed = http.get(f"/admin/api/clients/{SLUG}/sessions").json()["sessions"]
        assert [(x["id"], x["has_plan"]) for x in listed] == [(SID, True), ("20261101-100000", False)]
        assert http.get(f"/admin/plan/{SLUG}/{SID}").status_code == 200
        d = http.get(f"/admin/api/plan/{SLUG}/{SID}").json()
        assert d["session"] == SID and "<h1>" in d["html"]
        assert http.get(f"/admin/api/plan/{SLUG}/{SID}/md").status_code == 200
        assert http.get(f"/admin/api/plan/{SLUG}/20261101-100000").status_code == 404  # no plan yet
        for bad in ("..", "%2e%2e", "nope"):
            assert http.get(f"/admin/api/plan/{SLUG}/{bad}").status_code == 404
        assert http.get(f"/admin/api/plan/{SLUG}/..%2F..%2Fetc").status_code == 404
    with TestClient(create_admin_app(CLIENTS)) as http:  # no sessions folder given: nothing listed
        assert http.get(f"/admin/api/clients/{SLUG}/sessions").json() == {"sessions": []}


def test_admin_page_has_the_sessions_box():
    html = (STATIC_DIR / "admin.html").read_text(encoding="utf-8")
    js = (STATIC_DIR / "admin.js").read_text(encoding="utf-8")
    assert 'id="sessions"' in html and 'data-i18n="admin.sessions.title"' in html
    assert "/admin/plan/" in js and "admin.sessions.openPlan" in js


# --- Export PDF ---------------------------------------------------------------------------------------


def _pages(pdf: bytes) -> int:
    return len(re.findall(rb"/Type\s*/Page(?![s\w])", pdf))


@pytest.mark.skipif(not HAS_CHROME, reason="needs Google Chrome, Chromium, Edge, Brave or Playwright's Chromium")
def test_export_pdf_produces_a_real_pdf(tmp_path):
    sessions = tmp_path / "sessions"
    s = _session(sessions)
    with TestClient(create_app(s)) as http:
        r = http.get("/api/plan/pdf")
        assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
        assert f'filename="{SLUG}-{SID}-plan.pdf"' in r.headers["content-disposition"]
        pdf = r.content
        assert pdf.startswith(b"%PDF-") and len(pdf) > 10_000 and _pages(pdf) >= 2
        assert (s.session_dir / "plan.pdf").read_bytes() == pdf  # kept next to plan.md
        assert not (s.session_dir / "plan.html").exists()  # the print page is temporary
        assert http.get("/api/plan").json()["files"]["pdf"].endswith("plan.pdf")
    with TestClient(create_admin_app(CLIENTS, sessions_dir=sessions)) as http:
        r = http.get(f"/admin/api/plan/{SLUG}/{SID}/pdf")
        assert r.status_code == 200 and r.content.startswith(b"%PDF-")


def test_without_a_browser_export_says_so_and_the_page_falls_back_to_print(tmp_path, monkeypatch):
    monkeypatch.setenv("PACENOTE_CHROME", "")  # nothing to print with
    s = _session(tmp_path)
    with TestClient(create_app(s)) as http:
        r = http.get("/api/plan/pdf")
        assert r.status_code == 501 and "PACENOTE_CHROME" in r.json()["detail"]
        assert http.get("/api/plan").json()["pdf_available"] is False
    js = (STATIC_DIR / "plan.js").read_text(encoding="utf-8")
    assert "res.status === 501" in js and "window.print()" in js
    css = (STATIC_DIR / "plan.css").read_text(encoding="utf-8")
    assert "@media print" in css and ".plan-top" in css.split("@media print")[1]  # print = the document only


def test_find_chrome_honours_pacenote_chrome(tmp_path, monkeypatch):
    exe = tmp_path / "chrome"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PACENOTE_CHROME", str(exe))
    assert planview.find_chrome() == str(exe)
    monkeypatch.setenv("PACENOTE_CHROME", str(tmp_path / "missing"))
    assert planview.find_chrome() is None


def test_a_browser_that_writes_nothing_is_a_clear_error(tmp_path, monkeypatch):
    exe = tmp_path / "chrome"
    exe.write_text("#!/bin/sh\necho broken >&2\nexit 3\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PACENOTE_CHROME", str(exe))
    s = _session(tmp_path)
    with TestClient(create_app(s)) as http:
        r = http.get("/api/plan/pdf")
        assert r.status_code == 500 and "did not write a PDF" in r.json()["detail"] and "broken" in r.json()["detail"]


# --- i18n -----------------------------------------------------------------------------------------------


def test_every_plan_view_string_is_in_every_locale():
    used = set()
    for name in ("plan.html", "plan.js", "admin.html", "admin.js", "app.js"):
        text = (STATIC_DIR / name).read_text(encoding="utf-8")
        used |= set(re.findall(r"""["'](planView\.[\w.]+|admin\.sessions\.[\w.]+)["']""", text))
        used |= set(re.findall(r'data-i18n="((?:planView|admin\.sessions)\.[\w.]+)"', text))
        used |= set(re.findall(r'data-i18n-attr="\w+:((?:planView|admin\.sessions)\.[\w.]+)"', text))
    assert len(used) >= 15, sorted(used)
    for lang in i18n.available():
        cat = i18n.flatten(i18n.catalog(lang))
        missing = sorted(k for k in used if not cat.get(k))
        assert not missing, (lang, missing)
    assert i18n.t("planView.exportPdf", "fr") == "Exporter en PDF" and i18n.t("planView.exportPdf", "en") == "Export PDF"


# --- the real page in a browser (Node + Playwright, when available) ----------------------------------

BROWSER_SCRIPT = r"""
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch(process.env.PACENOTE_CHROME ? { executablePath: process.env.PACENOTE_CHROME } : {});
  const p = await b.newPage({ acceptDownloads: true });
  const errors = [];
  p.on('pageerror', e => errors.push(e.message));
  await p.addInitScript(() => localStorage.setItem('pacenote.ui-language', 'fr'));
  await p.goto(process.argv[2]);
  await p.waitForSelector('#plan-doc h1');
  const snap = () => p.evaluate(() => ({
    exportLabel: document.querySelector('#export').textContent,
    back: document.querySelector('#back').textContent,
    h1: document.querySelector('#plan-doc h1').textContent,
    docLang: document.querySelector('#plan-doc').lang, lang: document.documentElement.lang }));
  const fr = await snap();
  await p.selectOption('language-select select', 'en');
  await p.waitForFunction(() => document.querySelector('#export').textContent === 'Export PDF');
  const en = await snap();
  const [download] = await Promise.all([p.waitForEvent('download'), p.click('#export')]);
  await p.waitForFunction(() => document.querySelector('#status').classList.contains('ok'));
  const status = await p.textContent('#status');
  console.log(JSON.stringify({ fr, en, errors, file: download.suggestedFilename(), status }));
  await b.close();
})().catch(e => { console.error(e); process.exit(1); });
"""


def _node_playwright() -> bool:
    if not shutil.which("node"):
        return False
    return subprocess.run(["node", "-e", "require('playwright')"], capture_output=True).returncode == 0


@pytest.mark.skipif(not (_node_playwright() and HAS_CHROME), reason="needs node + playwright + a Chrome-family browser")
def test_browser_plan_view_is_translated_and_exports(tmp_path):
    import uvicorn

    s = _session(tmp_path)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(create_app(s), host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    try:
        deadline = time.time() + 10
        while not server.started and time.time() < deadline:
            time.sleep(0.05)
        script = tmp_path / "plan.js"
        script.write_text(BROWSER_SCRIPT, encoding="utf-8")
        out = subprocess.run(["node", str(script), f"http://127.0.0.1:{port}/plan"], capture_output=True, text=True,
                             timeout=120, cwd=tmp_path)
        assert out.returncode == 0, out.stderr
        r = json.loads(out.stdout.strip().splitlines()[-1])
        assert r["errors"] == []
        assert r["fr"]["exportLabel"] == "Exporter en PDF" and r["fr"]["back"] == "← Écran de l'entretien"
        assert r["en"]["exportLabel"] == "Export PDF" and r["en"]["back"] == "← Interview screen"
        assert r["fr"]["h1"] == r["en"]["h1"] == "Proposition d'intégration — Brioche & Four"  # the plan keeps its language
        assert r["en"]["docLang"] == "fr" and r["en"]["lang"] == "en"
        assert r["file"] == f"{SLUG}-{SID}-plan.pdf" and r["status"].startswith("PDF exported")
    finally:
        server.should_exit = True
        th.join(timeout=10)
