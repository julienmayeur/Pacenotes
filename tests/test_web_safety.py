"""Listening only starts on the Start button; no other web page can drive the local server; web and
model text is shown as text, never as markup.

The speech helper here is the stand-in of test_listen_capture.py (real JSON-lines protocol, no
microphone): its call log proves when, and in which mode, the helper ran.
"""

import json
import shutil
import socket
import subprocess
import threading
import time

import pytest
from fastapi.testclient import TestClient

from pacenote import web_guard
from pacenote.admin import create_admin_app
from pacenote.capture import ListenCapture, ReplaySource
from pacenote.finalize import md_to_html, safe_url
from pacenote.llm import OfflineEngine
from pacenote.server import create_app
from pacenote.session import Session, Settings

from .conftest import REAL_CLIENTS, SAMPLES
from .test_i18n import _node_playwright
from .test_listen_capture import _claude, fake_listen, runs

QUIET = Settings(sync_interval=3600, first_sync_delay=3600, poll_interval=0.05, fast_enabled=False)
MALICIOUS = ('Résumé <script>window.__xss = 1</script> et <img src=x onerror="window.__xss = 2"> '
             '[lien](javascript:alert(1)) [codé](&#106;avascript:alert(1)) [ok](https://brioche-four.example/)')


# --- 1. listening starts on the Start button only ------------------------------------------------


def _listen_session(tmp_path, client_cfg, script):
    return Session(client=client_cfg, session_dir=tmp_path / "s", engine=OfflineEngine(), settings=QUIET,
                   source_factory=lambda d: ListenCapture(d, helper_bin=str(script), stop_timeout=3.0))


def test_nothing_listens_before_start(tmp_path, client_cfg):
    script, log = fake_listen(tmp_path)
    session = _listen_session(tmp_path, client_cfg, script)
    with TestClient(create_app(session)) as http:  # server start
        assert http.get("/").status_code == 200  # page load
        assert http.get("/api/state").json()["mode"] == "consent"
        st = http.post("/api/language", json={"speech": ["fr-FR", "en-US"], "ui": "en"}).json()  # consent screen
        assert st["mode"] == "consent" and session.source is None
        assert runs(log) == []  # the helper never ran: no microphone, no transcript
        assert http.post("/api/start").json()["mode"] == "interview"  # the Start button
        assert [r["argv"][0] for r in runs(log)] == ["listen"]
    assert not session.source.running  # server shutdown stops it


def test_pacenote_run_warms_up_models_without_the_microphone(tmp_path, monkeypatch):
    from pacenote import cli

    script, log = fake_listen(tmp_path)
    served = {}
    monkeypatch.setattr(cli, "_serve", lambda session, args, auto_start: served.update(s=session, auto=auto_start) or 0)
    code = cli.main(["run", "--client", "brioche-four", "--clients-dir", str(REAL_CLIENTS), "--listen-bin", str(script),
                     "--claude-bin", _claude(tmp_path), "--sessions-dir", str(tmp_path / "sessions"), "--no-browser"])
    assert code == 0 and served["auto"] is False
    assert [r["argv"][0] for r in runs(log)] == ["prepare"]  # models reserved/loaded; the mic stays closed
    assert served["s"].source is None and served["s"].state.mode == "consent"


# --- 2. same-origin guard ------------------------------------------------------------------------


def _replay_session(tmp_path, client_cfg):
    return Session(client=client_cfg, session_dir=tmp_path / "web", engine=OfflineEngine(), settings=QUIET,
                   source_factory=lambda d: ReplaySource({"fr": SAMPLES / "fr.srt"}, speed=0))


def test_cross_origin_requests_are_refused(tmp_path, client_cfg):
    session = _replay_session(tmp_path, client_cfg)
    with TestClient(create_app(session), base_url="http://127.0.0.1:8798") as http:
        # another page in the browser (a site, or another local app on another port) tries to start
        for origin in ("https://evil.example", "http://127.0.0.1:3000", "http://localhost:8798", "null"):
            r = http.post("/api/start", headers={"Origin": origin})
            assert r.status_code == 403 and "cross-origin" in r.json()["detail"], origin
        r = http.post("/api/start", headers={"Sec-Fetch-Site": "cross-site"})  # no Origin, but a browser said so
        assert r.status_code == 403
        assert http.post("/api/enough", headers={"Origin": "https://evil.example"}).status_code == 403
        assert session.state.mode == "consent" and session.source is None  # nothing started
        # DNS rebinding: the page's own domain in Host, even for a read
        assert http.get("/api/state", headers={"Host": "evil.example:8798"}).status_code == 403
        # a cross-origin fetch of an API GET (e.g. the PDF export) is refused; following a link is not
        assert http.get("/api/state", headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "cors"}).status_code == 403
        assert http.get("/api/state", headers={"Sec-Fetch-Site": "cross-site",
                                               "Sec-Fetch-Mode": "navigate"}).status_code == 200
        # the page itself (same origin) and a non-browser client (curl: no Origin) still work
        assert http.post("/api/language", json={"ui": "en"}, headers={"Origin": "http://127.0.0.1:8798",
                                                                      "Sec-Fetch-Site": "same-origin"}).status_code == 200
        assert http.post("/api/start", headers={"Origin": "http://127.0.0.1:8798"}).json()["mode"] == "interview"


def test_admin_app_refuses_cross_origin_writes(tmp_path):
    clients = tmp_path / "clients"
    shutil.copytree(REAL_CLIENTS / "brioche-four", clients / "brioche-four")
    app = create_admin_app(clients)
    with TestClient(app, base_url="http://localhost:8766") as http:
        r = http.put("/admin/api/clients/brioche-four/research-md", json={"markdown": "x"},
                     headers={"Origin": "https://evil.example"})
        assert r.status_code == 403
        assert http.post("/admin/api/clients/brioche-four/research", headers={"Origin": "https://evil.example"}).status_code == 403
        ok = http.put("/admin/api/clients/brioche-four/research-md", json={"markdown": "y"},
                      headers={"Origin": "http://localhost:8766"})
        assert ok.status_code == 200


def test_guard_rules():
    hosts = web_guard.allowed_hosts("127.0.0.1")
    assert web_guard.check("GET", "/", {"host": "127.0.0.1:8765"}, hosts) is None
    assert web_guard.check("GET", "/", {"host": "[::1]:8765"}, hosts) is None
    assert web_guard.check("POST", "/api/start", {"host": "localhost:8765", "origin": "http://localhost:8765"}, hosts) is None
    assert web_guard.check("POST", "/api/start", {"host": "localhost:8765", "origin": "http://localhost:9999"}, hosts)
    assert web_guard.check("GET", "/", {"host": "attacker.test"}, hosts)
    assert "192.168.1.20" in web_guard.allowed_hosts("192.168.1.20")  # --host on a LAN address
    assert web_guard.allowed_hosts("0.0.0.0") == web_guard.allowed_hosts(None)  # wildcard: no name


# --- 3. web research / model text is data ----------------------------------------------------------


def test_markdown_from_the_web_never_becomes_markup():
    out = md_to_html(MALICIOUS + "\n\n<div onclick=x>block</div>\n\n| a |\n|---|\n| <b>x</b> |\n\n> cité")
    assert "<script" not in out and "<img" not in out and "<div" not in out and "<b>" not in out
    assert "&lt;script&gt;" in out and "&lt;img src=x" in out  # shown as text
    assert 'href="javascript' not in out and "&#106;avascript" not in out
    assert out.count('href="#"') == 2 and 'href="https://brioche-four.example/"' in out
    assert "<table>" in out and "<blockquote>" in out  # Markdown itself still renders
    assert not safe_url("data:text/html,x") and not safe_url("//evil.example") and not safe_url(" JaVa\tscript:x")
    assert safe_url("plan.md") and safe_url("#a") and safe_url("/plan") and safe_url("mailto:a@b.c")


def test_admin_and_plan_views_escape_research_and_plan_text(tmp_path, client_cfg):
    clients = tmp_path / "clients"
    shutil.copytree(REAL_CLIENTS / "brioche-four", clients / "brioche-four")
    (clients / "brioche-four" / "research.md").write_text(f"# Recherche\n\n{MALICIOUS}\n", encoding="utf-8")
    (clients / "brioche-four" / "preplan.md").write_text(f"# Pré-plan\n\n{MALICIOUS}\n", encoding="utf-8")
    with TestClient(create_admin_app(clients)) as http:
        d = http.get("/admin/api/clients/brioche-four").json()
        saved = http.put("/admin/api/clients/brioche-four/preplan", json={"markdown": MALICIOUS}).json()
    for html in (d["research_html"], d["preplan_html"], saved["html"]):
        assert "<script" not in html and "<img" not in html and "javascript:" not in html.replace("&lt;", "")
    session = _replay_session(tmp_path, client_cfg)
    (session.session_dir / "plan.md").write_text(f"# Plan\n\n{MALICIOUS}\n", encoding="utf-8")
    with TestClient(create_app(session)) as http:
        html = http.get("/api/plan").json()["html"]
    assert "<script" not in html and "<img" not in html and "&lt;img" in html


def test_admin_links_from_agents_are_http_only(tmp_path):
    """admin.js puts agent-found URLs (sources, websites, LinkedIn) in href: only http(s) survive."""
    if not shutil.which("node"):
        pytest.skip("needs node")
    from pacenote.server import STATIC_DIR

    src = (STATIC_DIR / "admin.js").read_text(encoding="utf-8")
    helpers = "\n".join(ln for ln in src.splitlines() if ln.startswith(("const esc =", "const safeHref =")))
    script = helpers + "\nconsole.log(JSON.stringify(['javascript:alert(1)', ' JAVASCRIPT:x', 'data:text/html,1'," \
                       " 'https://ex.fr/a?b=1&c=\"2\"', 'http://x.y', 'brioche-four.example'].map(safeHref)));"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == ["#", "#", "#", "https://ex.fr/a?b=1&amp;c=&quot;2&quot;", "http://x.y", "#"]


@pytest.mark.skipif(not _node_playwright(), reason="needs node + playwright + chromium")
def test_browser_page_from_elsewhere_cannot_start_listening(tmp_path, client_cfg):
    """A real browser: a page that is not the app (here a data: page, origin null) fires the request
    a malicious site would (no-cors POST, and a form post). The interview stays on the consent screen."""
    import uvicorn

    session = _replay_session(tmp_path, client_cfg)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(create_app(session), host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    script = tmp_path / "xorigin.js"
    script.write_text("""
const { chromium } = require('playwright');
(async () => {
  const url = process.argv[2];
  const b = await chromium.launch();
  const p = await b.newPage();
  await p.goto('data:text/html,<form id=f method=post action="' + url + 'api/start"></form>');
  await p.evaluate((u) => fetch(u + 'api/start', { method: 'POST', mode: 'no-cors' }).catch(() => {}), url);
  await p.evaluate(() => document.getElementById('f').submit());
  await p.waitForTimeout(800);
  const body = await p.evaluate(() => document.body.innerText);
  await b.close();
  console.log(JSON.stringify({ body }));
})().catch((e) => { console.error(e); process.exit(1); });
""", encoding="utf-8")
    try:
        deadline = time.time() + 10
        while not server.started and time.time() < deadline:
            time.sleep(0.05)
        out = subprocess.run(["node", str(script), f"http://127.0.0.1:{port}/"], capture_output=True, text=True,
                             timeout=120, cwd=tmp_path)
        assert out.returncode == 0, out.stderr
        assert "refused" in json.loads(out.stdout.strip().splitlines()[-1])["body"]
        assert session.state.mode == "consent" and session.source is None
    finally:
        server.should_exit = True
        th.join(timeout=10)
