"""One listening process for every spoken language (pacenote-listen): build, start, tagging, watchdog.

The stand-in helper below speaks the real JSON-lines protocol of pacenote/listen/pacenote_listen.swift
and misbehaves on demand, per run number. The real helper is verified on the Mac (README).
"""

import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from pacenote import capture
from pacenote.capture import CaptureError, HelperUnavailable, ListenCapture, ensure_helper, explain, preflight
from pacenote.cli import main
from pacenote.session import Session, Settings

from .conftest import REAL_CLIENTS, result

FAKE_LISTEN = r'''#!{python}
# Stand-in for `pacenote-listen listen|prepare --locale X [--locale Y]`, driven by RULES (keys are optional):
#   "error": {{"code": ..., "message": ...}}, "error_runs": [1]   -> setup error event, exit 3
#   "crash_runs": [1]                                              -> exit 1 at start, stderr only
#   "die_runs": [2], "die_after": 0.3, "die": {{"code", "message"}} -> error event after ready, exit 4
#   "status": "supported"                                          -> model missing: an `install` event
#   "fail_locales": {{"en-US": {{"code", "message"}}}}               -> that language skipped (`locale_failed`),
#                                                                     the others listen; none left -> error, exit 3
#   "heartbeat": {{"freeze_after": 0.3}}                              -> a heartbeat every 0.1 s; the buffer count
#                                                                     stops growing after freeze_after s (no audio)
import json, os, signal, sys, time
LOG, STATE, RULES = {log!r}, {state!r}, {rules!r}
counts = json.load(open(STATE)) if os.path.exists(STATE) else {{}}
n = counts["runs"] = counts.get("runs", 0) + 1
json.dump(counts, open(STATE, "w"))
args = sys.argv[1:]
locales = [args[i + 1] for i, a in enumerate(args) if a == "--locale"]
with open(LOG, "a") as fh:
    fh.write(json.dumps({{"run": n, "argv": args}}) + "\n")

def emit(event, **kw):
    print(json.dumps(dict(kw, event=event)), flush=True)

if n in RULES.get("crash_runs", []):
    sys.stderr.write("dyld: something went wrong\n"); sys.exit(1)
failing = RULES.get("fail_locales", {{}})
for loc in locales:
    if loc in failing:
        emit("locale_failed", locale=loc, **failing[loc]); continue
    status = RULES.get("status", "installed")
    emit("locale", locale=loc, resolved=loc, status=status)
    if status != "installed":
        emit("install", locale=loc, seconds=0.1)
failed, locales = [l for l in locales if l in failing], [l for l in locales if l not in failing]
if failed and not locales:
    first = failing[failed[0]]
    emit("error", locale=failed[0], code=first["code"], message="No spoken language is ready. " + first["message"]); sys.exit(3)
if n in RULES.get("error_runs", [1] if "error" in RULES else []):
    emit("error", **RULES["error"]); sys.exit(3)
for loc in locales:
    emit("loaded", locale=loc, seconds=0.01)
if args[0] == "prepare":
    emit("prepared", locales=locales, failed=failed); sys.exit(0)
emit("ready", locales=locales, failed=failed, source="microphone", sample_rate=16000)

def stop(*_):
    for loc in locales:
        emit("segment", locale=loc, start=9.0, end=9.5, text=f"final {{loc}}")
    emit("stopped"); sys.exit(0)

signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)
started, i = time.monotonic(), 0
while True:  # a segment per language every 0.1 s, on the helper's own clock (starting at 0)
    time.sleep(0.1)
    if n in RULES.get("die_runs", []) and time.monotonic() - started >= RULES.get("die_after", 0.3):
        emit("error", **RULES.get("die", {{"code": "transcription_failed", "message": "analyzer stopped"}}))
        sys.exit(4)
    i += 1
    if "heartbeat" in RULES:
        frozen = time.monotonic() - started >= RULES["heartbeat"].get("freeze_after", 1e9)
        emit("heartbeat", buffers=min(i, int(RULES["heartbeat"].get("freeze_after", 1e9) * 10)) if frozen else i, rms=0.1)
        if frozen:
            continue  # no audio: no segments either
    for loc in locales:
        emit("segment", locale=loc, start=round(i * 0.1, 3), end=round(i * 0.1 + 0.05, 3), text=f"{{loc}} run {{n}} seg {{i}}")
'''

FAST = dict(retry_gap=0.05, recovered_after=0.5, stop_timeout=3.0)


def fake_listen(tmp_path, rules=None):
    log = tmp_path / "listen-calls.log"
    state = tmp_path / "listen-state.json"
    script = tmp_path / "pacenote-listen"
    script.write_text(FAKE_LISTEN.format(python=sys.executable, log=str(log), state=str(state), rules=rules or {}))
    script.chmod(0o755)
    return script, log


def runs(log: Path) -> list[dict]:
    return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []


def _wait(cond, timeout=5.0, step=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if step:
            step()
        if cond():
            return True
        time.sleep(0.05)
    return False


def _kill(cap: ListenCapture) -> None:
    os.killpg(os.getpgid(cap._run.popen.pid), signal.SIGKILL)
    cap._run.popen.wait()
    cap._run.ended.wait(2)


# --- building the helper ------------------------------------------------------------------------


def _fake_swiftc(tmp_path, ok=True):
    calls = tmp_path / "swiftc-calls"
    script = tmp_path / "swiftc"
    body = ('out=""; while [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done\n'
            f'echo x >> {calls}\nprintf "#!/bin/sh\\necho built\\n" > "$out"; chmod +x "$out"\n') if ok else \
        f'echo x >> {calls}\necho "pacenote_listen.swift:12:3: error: cannot find SpeechAnalyzer in scope" >&2\nexit 1\n'
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(0o755)
    return str(script), calls


def test_helper_is_built_once_and_cached_by_source_hash(tmp_path):
    source = tmp_path / "pacenote_listen.swift"
    source.write_text("// v1\n")
    swiftc, calls = _fake_swiftc(tmp_path)
    notes = []
    first = ensure_helper(source=source, swiftc=swiftc, on_progress=notes.append)
    assert Path(first).parent == tmp_path / "cache" and os.access(first, os.X_OK)
    assert ensure_helper(source=source, swiftc=swiftc) == first
    assert calls.read_text().count("x") == 1 and "building the speech helper" in notes[0]
    source.write_text("// v2\n")  # a new source: rebuilt, the old build removed
    second = ensure_helper(source=source, swiftc=swiftc)
    assert second != first and not Path(first).exists() and calls.read_text().count("x") == 2


def test_helper_build_errors_say_what_to_do(tmp_path, monkeypatch):
    source = tmp_path / "pacenote_listen.swift"
    source.write_text("// v1\n")
    swiftc, _ = _fake_swiftc(tmp_path, ok=False)
    with pytest.raises(HelperUnavailable) as err:
        ensure_helper(source=source, swiftc=swiftc)
    assert "cannot find SpeechAnalyzer" in str(err.value) and "Command Line Tools" in str(err.value)
    assert not list((tmp_path / "cache").glob("*.tmp"))
    monkeypatch.setattr(capture.shutil, "which", lambda name: None)  # no swiftc at all
    with pytest.raises(HelperUnavailable) as err:
        ensure_helper(source=source, slug_hint="brioche-four")
    assert "xcode-select --install" in str(err.value) and "pacenote check --client brioche-four" in str(err.value)
    with pytest.raises(HelperUnavailable):
        ensure_helper(str(tmp_path / "missing"))
    script, _ = fake_listen(tmp_path)
    monkeypatch.setenv("PACENOTE_LISTEN_BIN", str(script))
    assert ensure_helper() == str(script)


def test_bundled_swift_source_is_packaged():
    assert capture.SWIFT_SOURCE.exists()
    text = capture.SWIFT_SOURCE.read_text()
    # the property the whole design relies on: reserve once, never release an expected locale
    assert "AssetInventory.reserve(locale:" in text
    assert "release(reservedLocale: extra)" in text and text.count("AssetInventory.release(") == 1


# --- start ----------------------------------------------------------------------------------------


def test_one_process_listens_to_every_language_and_tags_segments(tmp_path):
    script, log = fake_listen(tmp_path)
    sess = tmp_path / "sess"
    cap = ListenCapture(sess, helper_bin=str(script), **FAST)
    cap.start()
    try:
        assert [r["argv"] for r in runs(log)] == [["listen", "--locale", "fr-FR", "--locale", "en-US"]]
        status = cap.stream_status()
        assert set(status) == {"fr", "en"} and all(v["alive"] and v["problem"] is None for v in status.values())
        cues = []
        assert _wait(lambda: len({c.stream for c in cues}) == 2 and len(cues) >= 6, step=lambda: cues.extend(cap.poll()))
        assert all(c.text.startswith("fr-FR") for c in cues if c.stream == "fr")
        assert all(c.text.startswith("en-US") for c in cues if c.stream == "en")
        assert all(c.start >= 0 and c.end > c.start for c in cues)
    finally:
        cap.stop()
    final = cap.poll(final=True)
    assert {c.text for c in final} >= {"final fr-FR", "final en-US"}  # SIGINT flushes the last segments
    assert "fr-FR run 1 seg 1" in (sess / "fr.srt").read_text() and "final en-US" in (sess / "en.srt").read_text()
    assert "listening: fr-FR, en-US (one process" in (sess / "capture.log").read_text()
    assert len(runs(log)) == 1


def test_configured_locales_follow_the_client(tmp_path):
    script, log = fake_listen(tmp_path)
    cap = ListenCapture(tmp_path / "sess", helper_bin=str(script), **FAST)
    cap.configure_locales(["de-DE", "en-US"])
    cap.start()
    try:
        assert runs(log)[0]["argv"] == ["listen", "--locale", "de-DE", "--locale", "en-US"]
        assert set(cap.stream_status()) == {"de", "en"}
        with pytest.raises(CaptureError):
            cap.configure_locales(["fr-FR"])
    finally:
        cap.stop()


def test_a_missing_model_is_installed_before_ready_and_reported(tmp_path):
    script, _ = fake_listen(tmp_path, {"status": "supported"})
    notes = []
    cap = ListenCapture(tmp_path / "sess", helper_bin=str(script), on_progress=notes.append, **FAST)
    cap.start()
    cap.stop()
    text = "\n".join(notes)
    assert "fr-FR: downloading the speech model" in text and "fr-FR: speech model installed" in text
    assert "en-US: model loaded" in text


@pytest.mark.parametrize("code,message,fix", [
    ("install_failed", "The fr-FR speech model could not be downloaded or installed: offline.",
     "Connect the Mac to the internet"),
    ("microphone", "The microphone could not be opened: denied.", "Privacy & Security > Microphone"),
    ("unsupported_locale", "xx-XX is not a speech language this Mac can transcribe on device.", "--locales"),
    ("model_load_failed", "The fr-FR speech model is installed but could not be loaded: boom.", "Language & Region"),
])
def test_a_start_error_is_reported_once_with_its_cause_and_fix_never_retried(tmp_path, code, message, fix):
    script, log = fake_listen(tmp_path, {"error": {"code": code, "message": message, "locale": "fr-FR"}})
    cap = ListenCapture(tmp_path / "sess", helper_bin=str(script), slug_hint="brioche-four", **FAST)
    with pytest.raises(CaptureError) as err:
        cap.start()
    text = str(err.value)
    assert message in text and fix in text
    assert "retry" not in text.lower() and "attempt" not in text.lower() and "again" not in text.lower()
    assert len(runs(log)) == 1 and not cap.running


def test_a_helper_that_exits_without_an_error_event_shows_its_stderr(tmp_path):
    script, _ = fake_listen(tmp_path, {"crash_runs": [1]})
    cap = ListenCapture(tmp_path / "sess", helper_bin=str(script), **FAST)
    with pytest.raises(CaptureError) as err:
        cap.start()
    assert "dyld: something went wrong" in str(err.value)


def test_explain_covers_every_setup_error_code():
    for code in ["microphone", "unsupported_locale", "too_many_locales", "speech_unavailable", "install_failed",
                 "reserve_failed", "not_installed", "model_load_failed", "other"]:
        text = explain({"code": code, "message": "m", "locale": "fr-FR"}, "brioche-four")
        assert text.startswith("Listening could not start for fr-FR: m")
        assert "Fix:" in text or code == "other"


# --- watchdog ---------------------------------------------------------------------------------------


def test_watchdog_restarts_the_single_process_and_keeps_the_clock(tmp_path):
    script, log = fake_listen(tmp_path)
    sess = tmp_path / "sess"
    cap = ListenCapture(sess, helper_bin=str(script), **FAST)
    cap.start()
    try:
        time.sleep(0.4)
        first = cap.poll()
        assert first
        time.sleep(0.5)  # let the session clock move on before the crash
        _kill(cap)
        st = cap.stream_status()
        assert not st["fr"]["alive"] and not st["en"]["alive"]
        assert "restarting" in st["fr"]["problem"]
        assert _wait(lambda: cap.stream_status()["fr"]["alive"], step=cap.maintain)
        assert cap.stream_status()["en"]["restarts"] == 1
        again = []
        assert _wait(lambda: any("run 2" in c.text for c in again), step=lambda: again.extend(cap.poll()))
        again = [c for c in again if "run 2" in c.text]
        # the new process's clock restarts at 0; its segments are placed after the restart
        assert min(c.start for c in again) > max(c.start for c in first)
        # the same command, both languages again: nothing is released or re-chosen
        assert runs(log)[1]["argv"] == runs(log)[0]["argv"]
        assert _wait(lambda: cap.attempts == 0, timeout=3, step=cap.maintain)  # recovered: budget reset
        assert "restarting 1/5" in (sess / "capture.log").read_text()
    finally:
        cap.stop()


def test_watchdog_gives_up_after_its_restarts_with_the_cause(tmp_path):
    script, log = fake_listen(tmp_path, {"die_runs": list(range(2, 20)), "die_after": 0.0,
                                         "die": {"code": "transcription_failed", "message": "analyzer stopped"}})
    cap = ListenCapture(tmp_path / "sess", helper_bin=str(script), max_attempts=3, **FAST)
    cap.start()
    try:
        _kill(cap)
        assert _wait(lambda: cap.gave_up, timeout=8, step=cap.maintain)
        assert len(runs(log)) == 1 + 3
        st = cap.stream_status()["fr"]
        assert not st["alive"] and st["gave_up"] and "analyzer stopped" in st["problem"]
    finally:
        cap.stop()


def test_watchdog_does_not_restart_on_a_microphone_permission_error(tmp_path):
    script, log = fake_listen(tmp_path, {"die_runs": [1], "die_after": 0.2,
                                         "die": {"code": "microphone", "message": "microphone permission denied"}})
    cap = ListenCapture(tmp_path / "sess", helper_bin=str(script), **FAST)
    cap.start()
    try:
        assert _wait(lambda: cap.gave_up, timeout=5, step=cap.maintain)
        assert len(runs(log)) == 1
        assert "Microphone" in cap.stream_status()["fr"]["problem"]
    finally:
        cap.stop()


def test_stopped_capture_reports_no_problem_and_never_restarts(tmp_path):
    script, log = fake_listen(tmp_path)
    cap = ListenCapture(tmp_path / "sess", helper_bin=str(script), **FAST)
    cap.start()
    cap.stop()
    status = cap.stream_status()
    assert all(v["stopped"] and not v["alive"] and v["problem"] is None for v in status.values())
    assert cap.problems() == [] and cap.maintain() == []
    assert len(runs(log)) == 1


# --- session, CLI -------------------------------------------------------------------------------------


class _NoFast:
    name = "nofast"

    def sync(self, system, user, *, state, delta):
        return result()

    def write_plan(self, system, user, *, state):
        return "# Plan\n"


def test_session_shows_both_languages_listening_and_restarts(tmp_path, client_cfg):
    script, log = fake_listen(tmp_path, {"die_runs": [1], "die_after": 0.4})

    async def run():
        session = Session(
            client=client_cfg, session_dir=tmp_path / "s", engine=_NoFast(),
            source_factory=lambda d: ListenCapture(d, helper_bin=str(script), **FAST),
            settings=Settings(sync_interval=3600, first_sync_delay=3600, poll_interval=0.05),
        )
        await session.start()
        await asyncio.sleep(0.2)
        before = session.view()
        for _ in range(100):  # the helper dies at 0.4 s; the watchdog restarts it
            await asyncio.sleep(0.05)
            if session.source.restarts and all(v["alive"] for v in session.source.stream_status().values()):
                break
        await asyncio.sleep(0.3)
        after = session.view()
        segs = list(session.transcript.segments)
        await session.shutdown()
        return before, after, segs

    before, after, segs = asyncio.run(run())
    assert [(s["stream"], s["alive"]) for s in before["listening"]["streams"]] == [("fr", True), ("en", True)]
    assert all(s["alive"] for s in after["listening"]["streams"]) and after["capture_problems"] == []
    assert {s.stream for s in segs} == {"fr", "en"}
    assert len(runs(log)) == 2
    notes = [json.loads(x).get("note", "") for x in (tmp_path / "s" / "sync_log.jsonl").read_text().splitlines()]
    assert any("restarting 1/5" in n for n in notes)


def _claude(tmp_path):
    claude = tmp_path / "claude"
    claude.write_text("#!/bin/sh\necho '{}'\n")
    claude.chmod(0o755)
    return str(claude)


def test_run_refuses_to_start_when_listening_cannot_start(tmp_path, capsys):
    script, log = fake_listen(tmp_path, {"error": {"code": "install_failed", "locale": "fr-FR",
                                                   "message": "The fr-FR speech model could not be downloaded or installed: offline."}})
    sessions = tmp_path / "sessions"
    code = main(["run", "--client", "brioche-four", "--clients-dir", str(REAL_CLIENTS), "--listen-bin", str(script), "--claude-bin", _claude(tmp_path),
                 "--sessions-dir", str(sessions), "--no-browser"])
    out = capsys.readouterr()
    assert code == 3
    assert "Checking speech for fr-FR + en-US (models only, the microphone stays off until Start)" in out.out
    assert [r["argv"][0] for r in runs(log)] == ["prepare"]  # the check never opens the microphone
    assert "could not be downloaded" in out.err and "Connect the Mac to the internet" in out.err
    assert "Not starting" in out.err and not sessions.exists()


def test_check_starts_and_stops_listening_once(tmp_path, capsys):
    script, log = fake_listen(tmp_path)
    code = main(["check", "--client", "brioche-four", "--clients-dir", str(REAL_CLIENTS), "--listen-bin", str(script),
                 "--engine", "offline"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "[ok] fr-FR and en-US: speech models installed and loaded, microphone live" in out
    assert len(runs(log)) == 1


def test_preflight_is_one_clean_start(tmp_path):
    script, log = fake_listen(tmp_path)
    notes = []
    assert preflight(["fr-FR", "en-US"], helper_bin=str(script), on_progress=notes.append) < 5
    assert len(runs(log)) == 1 and any("model loaded" in n for n in notes)


def test_soak_passes_when_listening_stays_up(tmp_path, capsys):
    script, _ = fake_listen(tmp_path)
    code = main(["soak", "--client", "brioche-four", "--sessions-dir", str(tmp_path / "s"), "--listen-bin", str(script),
                 "--minutes", "0.02", "--every", "0.5"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "PASS: both languages alive" in out and "fr-FR alive" in out and "en-US alive" in out
