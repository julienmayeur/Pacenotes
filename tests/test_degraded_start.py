"""A language that is not ready never blocks the interview: it starts with the ready ones and shows
a banner; only when NO language is ready does Start (or `pacenote run`) refuse.

The stand-in helper (test_listen_capture.py) reproduces what the real pacenote-listen does in that case,
verified in file mode on the Mac: `locale_failed` for the language, `ready` with the others.
"""

import json
import socket
import subprocess
import threading
import time

import pytest
from fastapi.testclient import TestClient

from pacenote import i18n
from pacenote.capture import CaptureError, ListenCapture
from pacenote.cli import main
from pacenote.llm import OfflineEngine
from pacenote.server import create_app
from pacenote.session import Session, Settings

from .conftest import REAL_CLIENTS
from .test_i18n import _node_playwright
from .test_listen_capture import _claude, _wait, fake_listen, runs

QUIET = Settings(sync_interval=3600, first_sync_delay=3600, poll_interval=0.05, fast_enabled=False)
EN_FAILS = {"fail_locales": {"en-US": {"code": "install_failed",
                                       "message": "The en-US speech model could not be downloaded or installed: offline."}}}
BOTH_FAIL = {"fail_locales": {"fr-FR": {"code": "install_failed", "message": "fr-FR: offline."},
                              "en-US": {"code": "install_failed", "message": "en-US: offline."}}}


def test_start_goes_on_with_the_ready_language(tmp_path):
    script, _ = fake_listen(tmp_path, EN_FAILS)
    cap = ListenCapture(tmp_path / "s", helper_bin=str(script), stop_timeout=3.0)
    cap.start()
    try:
        st = cap.stream_status()
        assert st["fr"]["alive"] and not st["fr"].get("unavailable")
        assert not st["en"]["alive"] and st["en"]["unavailable"] and "could not be downloaded" in st["en"]["problem"]
        assert cap.problems() == []  # not an outage: a banner
        assert list(cap.unavailable()) == ["en-US"]
        assert _wait(lambda: any(c.stream == "fr" for c in cap.poll()))
        assert cap.maintain() == []  # the watchdog leaves a skipped language alone
        assert {c.stream for c in cap.poll()} <= {"fr"}
    finally:
        cap.stop()


def test_start_refuses_only_when_no_language_is_ready(tmp_path):
    script, _ = fake_listen(tmp_path, BOTH_FAIL)
    cap = ListenCapture(tmp_path / "s", helper_bin=str(script), stop_timeout=3.0)
    with pytest.raises(CaptureError, match="No spoken language is ready"):
        cap.start()
    assert not cap.running


def test_interview_starts_degraded_with_a_banner(tmp_path, client_cfg):
    script, _ = fake_listen(tmp_path, EN_FAILS)
    session = Session(client=client_cfg, session_dir=tmp_path / "s", engine=OfflineEngine(), settings=QUIET,
                      source_factory=lambda d: ListenCapture(d, helper_bin=str(script), stop_timeout=3.0))
    with TestClient(create_app(session)) as http:
        st = http.post("/api/start").json()
        assert st["mode"] == "interview" and st["capture_problems"] == []
        streams = {x["stream"]: x for x in st["listening"]["streams"]}
        assert streams["fr"]["alive"] and streams["en"]["unavailable"] and not streams["en"]["alive"]
        time.sleep(0.3)
        st = http.get("/api/state").json()  # a page refresh keeps the banner
        assert st["capture_problems"] == [] and {x["stream"]: x for x in st["listening"]["streams"]}["en"]["unavailable"]


def test_pacenote_run_starts_without_a_language_that_is_not_ready(tmp_path, monkeypatch, capsys):
    from pacenote import cli

    script, log = fake_listen(tmp_path, EN_FAILS)
    served = {}
    monkeypatch.setattr(cli, "_serve", lambda session, args, auto_start: served.update(s=session) or 0)
    code = main(["run", "--client", "brioche-four", "--clients-dir", str(REAL_CLIENTS), "--listen-bin", str(script),
                 "--claude-bin", _claude(tmp_path), "--sessions-dir", str(tmp_path / "sessions"), "--no-browser"])
    out = capsys.readouterr()
    assert code == 0 and "s" in served
    assert "ready: fr-FR" in out.out and "warning: en-US is not ready" in out.err
    assert [r["argv"][0] for r in runs(log)] == ["prepare"]


def test_pacenote_run_refuses_when_no_language_is_ready(tmp_path, capsys):
    script, _ = fake_listen(tmp_path, BOTH_FAIL)
    code = main(["run", "--client", "brioche-four", "--clients-dir", str(REAL_CLIENTS), "--listen-bin", str(script),
                 "--claude-bin", _claude(tmp_path), "--sessions-dir", str(tmp_path / "sessions"), "--no-browser"])
    err = capsys.readouterr().err
    assert code == 3 and "Not starting: no spoken language" in err and not (tmp_path / "sessions").exists()


def test_pacenote_check_flags_a_language_that_is_not_ready(tmp_path, capsys):
    script, _ = fake_listen(tmp_path, EN_FAILS)
    code = main(["check", "--client", "brioche-four", "--clients-dir", str(REAL_CLIENTS), "--listen-bin", str(script),
                 "--engine", "offline"])
    out = capsys.readouterr().out
    assert code == 1  # the day before: fix it
    assert "[ok] fr-FR: speech models installed and loaded" in out and "[!!] en-US: not ready" in out


def test_banner_text_exists_in_every_ui_language():
    for lang in i18n.available():
        text = i18n.t("live.unavailable", lang, langs="English", ready="French")
        assert "English" in text and "French" in text and text != "live.unavailable", lang


class _DegradedSource:
    """A source whose second language was not ready at Start (what ListenCapture reports)."""

    def start(self):
        pass

    def poll(self, final=False):
        return []

    def stop(self):
        pass

    def problems(self):
        return []

    def stream_status(self):
        return {"fr": {"locale": "fr-FR", "alive": True, "problem": None},
                "en": {"locale": "en-US", "alive": False, "unavailable": True,
                       "problem": "Listening could not start for en-US: offline"}}


@pytest.mark.skipif(not _node_playwright(), reason="needs node + playwright + chromium")
def test_browser_shows_the_banner_in_the_display_language(tmp_path, client_cfg):
    import uvicorn

    session = Session(client=client_cfg, session_dir=tmp_path / "s", engine=OfflineEngine(), settings=QUIET,
                      source_factory=lambda d: _DegradedSource())
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(create_app(session), host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    script = tmp_path / "banner.js"
    script.write_text("""
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage();
  await p.goto(process.argv[2]);
  await p.click('#start');
  await p.waitForSelector('#alerts .alert.degraded', { timeout: 8000 });
  const out = { banner: await p.textContent('#alerts'), chip: await p.evaluate(() =>
    [...document.querySelectorAll('#langs .chip')].map((c) => c.className + ':' + c.textContent)) };
  await b.close();
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e); process.exit(1); });
""", encoding="utf-8")
    try:
        deadline = time.time() + 10
        while not server.started and time.time() < deadline:
            time.sleep(0.05)
        out = subprocess.run(["node", str(script), f"http://127.0.0.1:{port}/"], capture_output=True, text=True,
                             timeout=120, cwd=tmp_path)
        assert out.returncode == 0, out.stderr
        res = json.loads(out.stdout.strip().splitlines()[-1])
        lang = session.state.ui_language
        prefix = i18n.t("live.unavailable", lang, langs="@", ready="@").split("@")[0].strip()  # "Indisponible :"
        assert prefix and prefix in res["banner"] and "offline" in res["banner"], res
        assert any(c.startswith("chip off") for c in res["chip"]) and any(c.startswith("chip ok") for c in res["chip"])
    finally:
        server.should_exit = True
        th.join(timeout=10)
