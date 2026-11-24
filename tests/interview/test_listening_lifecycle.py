"""Listening belongs to the interview (interview/lifecycle.py): the recogniser starts with the interview
and stops the moment it ends, on every way out; nothing starts it outside the interview.

The speech helper here is the stand-in of test_listen_capture.py: a real process speaking the real
JSON-lines protocol (no microphone). "Stopped" means that process is gone and was not started again.
"""

import ast
import asyncio
import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from pacenote.copilot.engines import OfflineEngine
from pacenote.interview import lifecycle, timebox
from pacenote.interview import session as session_mod
from pacenote.interview.session import Session, Settings
from pacenote.transcription.capture import ListenCapture, ReplaySource
from pacenote.transcription.speech import FallbackSource
from pacenote.web.server import create_app
from tests.conftest import SAMPLES
from tests.shared.test_i18n import _node_playwright
from tests.transcription.test_listen_capture import fake_listen, runs

QUIET = Settings(sync_interval=3600, first_sync_delay=3600, poll_interval=0.05, fast_enabled=False)
PACKAGE = Path(__file__).resolve().parents[2] / "pacenote"


def _live(tmp_path, client_cfg, script, settings=QUIET, engine=None):
    return Session(client=client_cfg, session_dir=tmp_path / "s", engine=engine or OfflineEngine(), settings=settings,
                   source_factory=lambda d: ListenCapture(d, helper_bin=str(script), stop_timeout=3.0))


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def _until(cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _log(session) -> list[dict]:
    path = session.session_dir / "sync_log.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def _assert_stopped(session, log, n_runs=1):
    """The helper process is gone, was started exactly n_runs times, and the screen says not listening."""
    cap = session.source
    assert cap is not None and not cap.running
    assert _gone(cap._run.popen.pid)
    assert len([r for r in runs(log) if r["argv"][0] == "listen"]) == n_runs
    v = session.view()["listening"]
    assert v["on"] is False and v["stopped"] is True


# --- every way out stops the recogniser --------------------------------------------------------------


def test_end_stops_listening_at_once_before_the_plan_is_written(tmp_path, client_cfg):
    """End (e): the helper is gone before the last analysis and the plan run (they can take tens of s)."""
    script, log = fake_listen(tmp_path)
    gate = threading.Event()

    class SlowPlan(OfflineEngine):
        def write_plan_doc(self, system, user, *, state):
            gate.wait(10)  # the plan is still being written...
            return super().write_plan_doc(system, user, state=state)

    session = _live(tmp_path, client_cfg, script, engine=SlowPlan())
    with TestClient(create_app(session)) as http:
        assert http.post("/api/start").json()["mode"] == "interview"
        assert _until(lambda: session.transcript.last_seq > 3)
        assert http.post("/api/finalize").json() == {"ok": True}
        _assert_stopped(session, log)  # ...and listening already stopped
        heard = session.transcript.last_seq
        time.sleep(0.3)
        assert session.transcript.last_seq == heard  # nothing heard after the end
        assert session.state.end_reason == "ended" and session.state.mode in ("finalize", "generating")
        gate.set()
        assert _until(lambda: session.state.mode == "done")
    _assert_stopped(session, log)
    kinds = [e.get("kind") for e in _log(session)]
    assert kinds.index("listening") < kinds.index("end") < kinds.index("plan")


def test_enough_stops_listening(tmp_path, client_cfg):
    script, log = fake_listen(tmp_path)
    session = _live(tmp_path, client_cfg, script)
    with TestClient(create_app(session)) as http:
        http.post("/api/start")
        st = http.post("/api/enough").json()
        assert st["listening"]["on"] is False and st["enough_data"]
        _assert_stopped(session, log)
        assert _until(lambda: session.state.mode == "done")


def test_time_up_ends_the_interview_and_stops_listening(tmp_path, client_cfg, monkeypatch):
    """The planned length is over: the interview ends by itself, listening stops, the plan is written."""
    monkeypatch.setattr(timebox, "total", lambda st: 0.6)  # a 0.6 s interview (the real one: 20 min)
    script, log = fake_listen(tmp_path)
    session = _live(tmp_path, client_cfg, script)
    with TestClient(create_app(session)) as http:
        http.post("/api/start")
        assert _until(lambda: session.state.mode == "done")
        _assert_stopped(session, log)
        assert session.state.end_reason == "time_up"
        assert http.get("/api/state").json()["plan_ready"]  # the plan is presented, not left pending


def test_time_up_on_the_interview_clock_ends_a_replay_with_nothing_heard_after(tmp_path, client_cfg):
    """A replay at its own pace: at the planned length (1 min here) the interview ends, and none of the
    recording said after it is ever ingested (the replay hears no more than a microphone would)."""
    clock = [0.0]
    src = ReplaySource({"fr": SAMPLES / "fr.srt", "en": SAMPLES / "en.srt"}, speed=1.0, clock=lambda: clock[0])
    session = Session(client=client_cfg, session_dir=tmp_path / "r", engine=OfflineEngine(), settings=QUIET,
                      source_factory=lambda d: src)
    session.state.set_target_minutes(1)

    async def run():
        await session.start()
        for t in range(0, 200, 5):
            clock[0] = float(t)
            await asyncio.sleep(0.06)
            if session.state.mode != "interview":
                break
        await asyncio.wait_for(session._plan_task, 10)
        await session.shutdown()

    asyncio.run(run())
    assert session.state.end_reason == "time_up" and session.state.mode == "done"
    assert 60 <= clock[0] <= 70
    assert max(s.end for s in session.transcript.segments) <= 61  # said before the end, nothing after
    assert src.poll() == [] and src.poll(final=True) == []


def test_closing_the_interview_screen_ends_it_after_a_short_grace(tmp_path, client_cfg, monkeypatch):
    """Over real HTTP: the page's live stream closes (tab closed, navigated away). The server notices
    within about a second; after lifecycle.PAGE_GRACE the interview ends and the helper is gone. A reload
    (the stream back within the grace) keeps the interview."""
    import uvicorn

    monkeypatch.setattr(lifecycle, "PAGE_GRACE", 1.0)
    script, log = fake_listen(tmp_path)
    session = _live(tmp_path, client_cfg, script)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(create_app(session), host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    base = f"http://127.0.0.1:{port}"
    try:
        assert _until(lambda: server.started)

        def open_page():
            c = httpx.Client(base_url=base, timeout=5)
            r = c.send(c.build_request("GET", "/api/events"), stream=True)
            lines = r.iter_lines()  # kept: a dropped line iterator closes the stream
            next(lines)
            return c, r, lines

        c, r, _ = open_page()
        assert httpx.post(f"{base}/api/start").json()["mode"] == "interview"
        # a reload: the stream closes and comes back at once
        r.close()
        c.close()
        c, r, _ = open_page()
        time.sleep(1.5)
        assert session.state.mode == "interview" and session.listening, session.state.end_reason
        # the tab is closed
        closed = time.monotonic()
        r.close()
        c.close()
        assert _until(lambda: session.state.mode != "interview", timeout=8)
        took = time.monotonic() - closed
        assert session.state.end_reason == "page_left"
        assert 1.0 <= took < 3.5, took  # the grace + about a second to notice
        _assert_stopped(session, log)
    finally:
        server.should_exit = True
        th.join(10)
    _assert_stopped(session, log)


def test_nobody_heard_for_a_while_ends_the_interview(tmp_path, client_cfg, monkeypatch):
    monkeypatch.setattr(lifecycle, "IDLE_AFTER", 30.0)
    clock = [0.0]
    srt = tmp_path / "fr.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:04,000\nBonjour.\n\n", encoding="utf-8")
    session = Session(client=client_cfg, session_dir=tmp_path / "r", engine=OfflineEngine(), settings=QUIET,
                      source_factory=lambda d: ReplaySource({"fr": srt}, speed=1.0, clock=lambda: clock[0]))

    async def run():
        await session.start()
        for t, still in ((10, True), (33, True), (34.5, False)):
            clock[0] = t
            await asyncio.sleep(0.15)
            assert (session.state.mode == "interview") is still, t
        await session.shutdown()

    asyncio.run(run())
    assert session.state.end_reason == "idle" and not session.listening


def test_idle_counts_from_when_the_recogniser_is_up_not_from_start(tmp_path, client_cfg, monkeypatch):
    """A first Start can download speech models for minutes: that wait is not silence."""
    monkeypatch.setattr(lifecycle, "IDLE_AFTER", 0.5)
    script, log = fake_listen(tmp_path, {"heartbeat": {"freeze_after": 0.0}})  # up, but nothing heard
    slow = tmp_path / "slow-listen"
    slow.write_text(f"#!/bin/sh\nsleep 1.0\nexec {script} \"$@\"\n")  # longer than IDLE_AFTER
    slow.chmod(0o755)
    session = _live(tmp_path, client_cfg, slow)

    async def run():
        await session.start()
        assert session._end_due() is None  # 1 s of start-up, not 1 s of silence
        await asyncio.sleep(0.8)
        assert session.state.end_reason == "idle"  # then 0.5 s with nothing heard ends it
        await session.shutdown()

    asyncio.run(run())
    _assert_stopped(session, log)


def test_server_shutdown_stops_listening_and_keeps_the_session_for_a_plan(tmp_path, client_cfg):
    script, log = fake_listen(tmp_path)
    session = _live(tmp_path, client_cfg, script)
    with TestClient(create_app(session)) as http:
        http.post("/api/start")
        assert _until(lambda: session.transcript.last_seq > 0)
    _assert_stopped(session, log)
    assert session.state.mode == "finalize" and session.state.end_reason == "shutdown"
    saved = json.loads((session.session_dir / "state.json").read_text())
    assert saved["mode"] == "finalize"  # `pacenote finalize` writes its plan later


def test_a_failed_plan_and_its_retry_never_listen_again(tmp_path, client_cfg, monkeypatch):
    script, log = fake_listen(tmp_path)
    session = _live(tmp_path, client_cfg, script)
    monkeypatch.setattr(session_mod, "write_plan_files", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))

    async def run():
        await session.start()
        await asyncio.sleep(0.3)
        assert await session.finalize() == {}  # not even the offline draft: back to "finalize", retry offered
        assert session.state.mode == "finalize"
        _assert_stopped(session, log)
        monkeypatch.undo()
        assert (await session.finalize())["md"]
        await session.shutdown()

    asyncio.run(run())
    _assert_stopped(session, log)
    assert session.state.mode == "done"


def test_ending_while_the_helper_starts_stops_it_as_soon_as_it_is_up(tmp_path, client_cfg):
    script, log = fake_listen(tmp_path)
    slow = tmp_path / "slow-listen"
    slow.write_text(f"#!/bin/sh\nsleep 0.6\nexec {script} \"$@\"\n")
    slow.chmod(0o755)
    session = _live(tmp_path, client_cfg, slow)

    async def run():
        starting = asyncio.create_task(session.start())
        await asyncio.sleep(0.2)  # the helper is still starting
        assert session.end_interview("ended")
        await starting
        await asyncio.sleep(0.3)
        await session.shutdown()

    asyncio.run(run())
    cap = session.source
    assert not cap.running and (cap._run is None or _gone(cap._run.popen.pid))
    assert session.state.mode == "finalize" and session.view()["listening"]["on"] is False


# --- nothing starts it outside the interview ----------------------------------------------------------


def test_nothing_outside_the_interview_starts_the_recogniser(tmp_path, client_cfg):
    script, log = fake_listen(tmp_path)
    session = _live(tmp_path, client_cfg, script)
    with TestClient(create_app(session)) as http:
        with http.stream("GET", "/api/events") as r:  # the page is open
            next(r.iter_lines())
        http.post("/api/language", json={"ui": "en", "interview": "fr", "speech": ["fr-FR"]})
        http.post("/api/wispr", json={"text": "notes"})
        assert http.post("/api/next").status_code == 409
        assert runs(log) == [] and session.source is None and not session.listening
        http.post("/api/start")
        http.post("/api/finalize")
        assert _until(lambda: session.state.mode == "done")
        for path in ("/api/start", "/api/next", "/api/extend", "/api/sync"):  # after the end
            http.post(path)
        session.ingest_once()  # the watchdog's turn: a stopped helper is never restarted
    assert len(runs(log)) == 1
    _assert_stopped(session, log)


def test_a_saved_session_with_lines_not_analysed_gets_its_last_analysis(tmp_path, client_cfg):
    """`pacenote finalize` on a finished session: the lines never analysed are, before the plan."""
    clock = [0.0]
    session = Session(client=client_cfg, session_dir=tmp_path / "r", engine=OfflineEngine(), settings=QUIET,
                      source_factory=lambda d: ReplaySource({"fr": SAMPLES / "fr.srt"}, speed=1.0, clock=lambda: clock[0]))

    async def run():
        await session.start()
        clock[0] = 60.0
        await asyncio.sleep(0.2)
        await session.finalize()

    asyncio.run(run())
    session.transcript.add("fr", 61.0, 63.0, "une ligne ajoutée après coup", received_at=time.time())
    session.transcript.save_jsonl(session.session_dir / "transcript.jsonl")
    resumed = Session.resume(client_cfg, session.session_dir, OfflineEngine())
    assert resumed.state.mode == "done" and resumed.transcript.since(resumed.last_synced_seq)
    assert asyncio.run(resumed.finalize())["md"]
    assert not resumed.transcript.since(resumed.last_synced_seq) and resumed.state.mode == "done"


def test_resuming_a_saved_session_never_listens(tmp_path, client_cfg):
    script, log = fake_listen(tmp_path)
    session = _live(tmp_path, client_cfg, script)
    asyncio.run(session.start())
    asyncio.run(session.shutdown())
    resumed = Session.resume(client_cfg, session.session_dir, OfflineEngine())
    assert asyncio.run(resumed.finalize())["md"]
    assert len(runs(log)) == 1 and resumed.source is None


def test_a_live_recogniser_refuses_to_start_automatically(tmp_path, client_cfg):
    script, log = fake_listen(tmp_path)
    session = _live(tmp_path, client_cfg, script)
    with pytest.raises(RuntimeError, match="Start button"), TestClient(create_app(session, auto_start=True)):
        pass
    assert runs(log) == [] and session.state.mode == "consent"


def test_a_fallback_switch_after_the_end_opens_nothing(tmp_path):
    started = []

    class Backend:
        locales = {"fr": "fr-FR"}
        running = False

        def start(self, wait=True):
            started.append(self)
            self.running = True

        def stop(self):
            self.running = False

    primary = Backend()
    src = FallbackSource(primary, lambda t0: Backend())
    src.start()
    src.stop()
    assert src._switch("listening stopped", wait=True) == [] and src.maintain() == []
    assert started == [primary] and not primary.running


def test_listening_outside_the_interview_is_caught_on_save(tmp_path, client_cfg):
    """Belt and braces: if any code ever left the interview without the lifecycle door, the next save
    stops the recogniser and logs the violation."""
    script, log = fake_listen(tmp_path)
    session = _live(tmp_path, client_cfg, script)

    async def run():
        await session.start()
        session.state.mode = "finalize"  # a write that bypasses Session._set_mode
        session.persist()
        _assert_stopped(session, log)
        await session.shutdown()

    asyncio.run(run())
    assert any(e.get("kind") == "listening-invariant" for e in _log(session))


def test_only_the_lifecycle_door_writes_the_mode():
    """No code in the package assigns `.mode` but Session._set_mode (a structural check)."""
    offenders = []
    for path in PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        for node in ast.walk(tree):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(
                node, (ast.AugAssign, ast.AnnAssign)) else []
            for t in targets:
                if isinstance(t, ast.Attribute) and t.attr == "mode":
                    fn = node
                    while fn in parents and not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        fn = parents[fn]
                    if getattr(fn, "name", None) != "_set_mode":
                        offenders.append(f"{path.relative_to(PACKAGE)}:{node.lineno}")
    assert offenders == []


def test_the_server_killed_does_not_leave_the_helper_listening(tmp_path):
    """kill -9 of the server: the helper (its own process group) loses its reader and exits on its
    next write, within about a second, instead of keeping the microphone open."""
    script, log = fake_listen(tmp_path, {"heartbeat": {}})
    holder = tmp_path / "holder.py"
    holder.write_text(
        "import sys, time\nfrom pathlib import Path\nfrom pacenote.transcription.capture import ListenCapture\n"
        f"cap = ListenCapture(Path({str(tmp_path / 'h')!r}), helper_bin={str(script)!r})\ncap.start()\n"
        "print(cap._run.popen.pid, flush=True)\ntime.sleep(60)\n")
    env = dict(os.environ, PYTHONPATH=str(PACKAGE.parent))
    server = subprocess.Popen([os.sys.executable, str(holder)], stdout=subprocess.PIPE, env=env)
    helper_pid = int(server.stdout.readline())
    assert not _gone(helper_pid)
    server.kill()
    server.wait()
    assert _until(lambda: _gone(helper_pid) or _zombie(helper_pid), timeout=5)


def _zombie(pid: int) -> bool:
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return out.startswith("Z") or out == ""


# --- the screen says it --------------------------------------------------------------------------------


@pytest.mark.skipif(not _node_playwright(), reason="needs node + playwright + chromium")
def test_the_screen_shows_listening_only_while_the_recogniser_is_open(tmp_path, client_cfg, monkeypatch):
    """A real browser: before Start nothing listens; after Start the quiet "listening" mark is on; after
    End it says listening stopped; closing the tab of a running interview stops the helper."""
    import uvicorn

    monkeypatch.setattr(lifecycle, "PAGE_GRACE", 1.0)
    script, log = fake_listen(tmp_path)
    session = _live(tmp_path, client_cfg, script)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(create_app(session), host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    js = tmp_path / "listen.js"
    js.write_text("""
const { chromium } = require('playwright');
(async () => {
  const url = process.argv[2];
  const b = await chromium.launch();
  const p = await b.newPage();
  const mark = async () => p.evaluate(() => {
    const el = document.getElementById('listen');
    return { cls: el.className, label: document.getElementById('listen-label').textContent };
  });
  await p.goto(url);
  await p.waitForSelector('#start');
  const before = await mark();
  await p.click('#start');
  await p.waitForFunction(() => document.getElementById('listen').classList.contains('on'));
  const during = await mark();
  await p.close();  // the tab is closed mid-interview
  await new Promise((r) => setTimeout(r, 3500));
  const p2 = await b.newPage();
  await p2.goto(url);
  await p2.waitForFunction(() => document.getElementById('listen').classList.contains('ended'));
  const after = await p2.evaluate(() => ({ cls: document.getElementById('listen').className,
                                           label: document.getElementById('listen-label').textContent }));
  await b.close();
  console.log(JSON.stringify({ before, during, after }));
})().catch((e) => { console.error(e); process.exit(1); });
""", encoding="utf-8")
    try:
        assert _until(lambda: server.started)
        out = subprocess.run(["node", str(js), f"http://127.0.0.1:{port}/"], capture_output=True, text=True,
                             timeout=60, env=dict(os.environ))
        assert out.returncode == 0, out.stderr
        seen = json.loads(out.stdout.strip().splitlines()[-1])
    finally:
        server.should_exit = True
        th.join(10)
    assert "on" not in seen["before"]["cls"].split() and seen["before"]["label"] == ""
    assert "on" in seen["during"]["cls"].split() and seen["during"]["label"]
    assert "ended" in seen["after"]["cls"].split() and seen["after"]["label"] != seen["during"]["label"]
    assert session.state.end_reason == "page_left"
    _assert_stopped(session, log)
