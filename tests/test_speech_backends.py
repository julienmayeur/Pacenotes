"""Speech backends behind one interface (capture.TranscriptionBackend, speech.py): the contract
every backend meets, the platform-aware selection, the whisper.cpp output parser, and the automatic
fallback (at Start, when the helper gives up, when no audio reaches it).

Fakes only: the Apple helper stand-in of test_listen_capture.py and a whisper-stream stand-in that
prints whisper.cpp's documented VAD-mode output. Nothing here opens a microphone.
"""

import sys
import time

import pytest
from fastapi.testclient import TestClient

from pacenote import i18n, speech
from pacenote.capture import CaptureError, Health, ListenCapture, ReplaySource
from pacenote.cli import main
from pacenote.llm import OfflineEngine
from pacenote.server import create_app
from pacenote.session import Session, Settings
from pacenote.speech import FallbackSource, Plan, WhisperStreamBackend, parse_whisper_stream

from .conftest import REAL_CLIENTS, SAMPLES
from .test_degraded_start import BOTH_FAIL
from .test_listen_capture import _claude, _wait, fake_listen, runs

QUIET = Settings(sync_interval=3600, first_sync_delay=3600, poll_interval=0.05, fast_enabled=False)

WHISPER_OUTPUT = [
    "\x1b[2K\r[Start speaking]",
    "",
    "### Transcription 0 START | t0 = 0 ms | t1 = 4200 ms",
    "",
    "[00:00:00.000 --> 00:00:03.000]   Bonjour, on saisit les commandes dans l'ERP.",
    "[00:00:03.000 --> 00:00:04.200]   [BLANK_AUDIO]",
    "",
    "### Transcription 0 END",
    "### Transcription 1 START | t0 = 4200 ms | t1 = 9000 ms",
    "[00:00:00.500 --> 00:00:04.000]  \x1b[1mThe customer portal\x1b[0m, trois heures par jour.",
    "### Transcription 1 END",
]

FAKE_WHISPER = r'''#!{python}
# Stand-in for whisper.cpp `whisper-stream -m MODEL -l LANG --step 0 …` (VAD mode): init logs on
# stderr, "[Start speaking]", then a transcription block every 0.2 s; Ctrl-C (SIGINT) ends it.
import json, signal, sys, time
with open({log!r}, "a") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\n")
sys.stderr.write("whisper_init_from_file: loading model\n"); sys.stderr.flush()
time.sleep({delay})
print("\x1b[2K\r[Start speaking]", flush=True)
signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
i = 0
while True:
    time.sleep(0.2)
    print(f"\n### Transcription {{i}} START | t0 = {{i * 200}} ms | t1 = {{i * 200 + 180}} ms\n", flush=True)
    print(f"[00:00:00.000 --> 00:00:00.180]   whisper segment {{i}}", flush=True)
    print(f"\n### Transcription {{i}} END", flush=True)
    i += 1
'''


@pytest.fixture
def whisper_env(tmp_path, monkeypatch):
    """A configured whisper.cpp: a stand-in whisper-stream and a (dummy) model file."""
    log = tmp_path / "whisper-calls.log"
    binary = tmp_path / "whisper-stream"
    binary.write_text(FAKE_WHISPER.format(python=sys.executable, log=str(log), delay=0.1))
    binary.chmod(0o755)
    model = tmp_path / "ggml-test.bin"
    model.write_bytes(b"model")
    monkeypatch.setenv("PACENOTE_WHISPER_BIN", str(binary))
    monkeypatch.setenv("PACENOTE_WHISPER_MODEL", str(model))
    return {"bin": str(binary), "model": str(model), "log": log}


# --- the whisper.cpp output ----------------------------------------------------------------------


def test_whisper_stream_output_is_parsed_into_segments():
    segs = parse_whisper_stream(WHISPER_OUTPUT)
    assert segs == [(0.0, 3.0, "Bonjour, on saisit les commandes dans l'ERP."),
                    (4.7, 8.2, "The customer portal, trois heures par jour.")]  # block t0 + segment time


# --- selection --------------------------------------------------------------------------------------


def test_selection_is_platform_aware(whisper_env, monkeypatch):
    assert speech.select("auto", platform="darwin") == Plan("apple", "whisper")
    assert speech.select("auto", platform="win32").primary == "whisper"
    assert speech.select("apple", platform="darwin") == Plan("apple")
    assert speech.select("whisper", platform="darwin") == Plan("whisper")
    with pytest.raises(CaptureError, match="needs macOS"):
        speech.select("apple", platform="win32")
    monkeypatch.setenv("PACENOTE_SPEECH_BACKEND", "whisper")
    assert speech.select(None, platform="darwin").primary == "whisper"  # the env var


def test_selection_without_whisper(monkeypatch):
    plan = speech.select("auto", platform="darwin")
    assert plan.primary == "apple" and plan.fallback is None and "whisper-stream not found" in plan.note
    with pytest.raises(CaptureError, match="No speech backend is available") as exc:
        speech.select("auto", platform="win32")  # Windows without whisper.cpp: the documented setup
    assert "PACENOTE_WHISPER_MODEL" in str(exc.value) and "never downloads" in str(exc.value)
    with pytest.raises(CaptureError, match="whisper-stream not found"):
        speech.select("whisper", platform="darwin")
    monkeypatch.setenv("PACENOTE_WHISPER_BIN", sys.executable)
    monkeypatch.setenv("PACENOTE_WHISPER_MODEL", "/nonexistent/ggml.bin")
    with pytest.raises(CaptureError, match="is not a file"):
        speech.select("whisper")


def test_make_source_follows_the_plan(tmp_path, whisper_env):
    assert isinstance(speech.make_source(Plan("apple"), tmp_path, ["fr-FR", "en-US"]), ListenCapture)
    assert isinstance(speech.make_source(Plan("whisper"), tmp_path, ["fr-FR", "en-US"]), WhisperStreamBackend)
    src = speech.make_source(Plan("apple", "whisper"), tmp_path, ["fr-FR", "en-US"])
    assert isinstance(src, FallbackSource) and src.name == "apple"


# --- the interface contract ------------------------------------------------------------------------


def _apple(tmp_path, _env):
    script, _ = fake_listen(tmp_path)
    return ListenCapture(tmp_path / "s", {"fr": "fr-FR", "en": "en-US"}, str(script), stop_timeout=3.0)


def _whisper(tmp_path, env):
    return WhisperStreamBackend(tmp_path / "s", {"fr": "fr-FR", "en": "en-US"}, env["bin"], env["model"])


@pytest.mark.parametrize("make", [_apple, _whisper], ids=["apple", "whisper"])
def test_every_backend_meets_the_contract(tmp_path, whisper_env, make):
    b = make(tmp_path, whisper_env)
    assert isinstance(b.name, str) and b.name
    assert b.ready_locales() == [] and not b.health().alive  # nothing before start (no microphone)
    b.start()
    try:
        ready = b.ready_locales()
        assert ready and set(ready) <= {"fr-FR", "en-US"}
        h = b.health()
        assert isinstance(h, Health) and h.alive and h.ok
        cues = []
        assert _wait(lambda: cues.extend(b.poll()) or cues, timeout=5)
        status = b.stream_status()
        assert {c.stream for c in cues} <= set(status) and all(c.text for c in cues)  # tagged with a stream id
        assert all(c.end >= c.start for c in cues)
        assert isinstance(b.problems(), list)
    finally:
        b.stop()
    assert not b.health().alive and b.ready_locales() == []


def test_replay_reports_its_streams_like_a_backend():
    src = ReplaySource({"fr": SAMPLES / "fr.srt", "en": SAMPLES / "en.srt"}, speed=0)
    assert src.name == "replay" and not src.health().alive
    src.start()
    assert src.ready_locales() == ["fr", "en"] and src.health().ok and src.poll()
    src.stop()
    assert not src.health().alive


def test_whisper_backend_runs_one_stream_in_the_primary_language(tmp_path, whisper_env):
    b = _whisper(tmp_path, whisper_env)
    b.start()
    try:
        assert b.ready_locales() == ["fr-FR"]
        st = b.stream_status()
        assert st["fr"]["alive"] and st["en"]["unavailable"] and b.problems() == []
        argv = runs(whisper_env["log"])[0]
        assert argv[argv.index("-l") + 1] == "fr" and argv[argv.index("--step") + 1] == "0"
        assert argv[argv.index("-m") + 1] == whisper_env["model"]
        assert _wait(lambda: (tmp_path / "s" / "fr.srt").exists())
    finally:
        b.stop()


def test_apple_health_reports_the_heartbeat(tmp_path):
    script, _ = fake_listen(tmp_path, {"heartbeat": {"freeze_after": 0.4}})
    cap = ListenCapture(tmp_path / "s", helper_bin=str(script), stop_timeout=3.0)
    cap.start()
    try:
        assert _wait(lambda: cap.health().audio_age is not None)
        assert cap.health(stale_after=5).ok
        assert _wait(lambda: not cap.health(stale_after=0.5).ok, timeout=5)  # buffers stopped growing
        assert "no audio" in cap.health(stale_after=0.5).problem
    finally:
        cap.stop()


# --- automatic fallback ---------------------------------------------------------------------------


def _fallback(tmp_path, env, rules):
    script, _ = fake_listen(tmp_path, rules)
    apple = ListenCapture(tmp_path / "s", {"fr": "fr-FR", "en": "en-US"}, str(script), stop_timeout=3.0)
    return FallbackSource(apple, lambda t0=None: WhisperStreamBackend(tmp_path / "s", {}, env["bin"], env["model"],
                                                                      t0=t0), stale_after=0.5)


def test_fallback_at_start_when_no_language_is_ready(tmp_path, whisper_env):
    src = _fallback(tmp_path, whisper_env, BOTH_FAIL)
    src.start()
    try:
        assert src.name == "whisper" and "No spoken language is ready" in src.switched_reason
        assert src.ready_locales() == ["fr-FR"]
        assert _wait(lambda: any(c.stream == "fr" and "whisper" in c.text for c in src.poll()))
    finally:
        src.stop()
    assert not src.primary.running and not src.running


def test_fallback_when_the_helper_gives_up(tmp_path, whisper_env):
    src = _fallback(tmp_path, whisper_env, {"die_runs": [1], "die_after": 0.2,
                                            "die": {"code": "microphone", "message": "the microphone went away"}})
    src.start()
    try:
        assert src.name == "apple"
        assert _wait(lambda: src.name == "whisper", timeout=8, step=src.maintain)
        assert "microphone went away" in src.switched_reason
        assert _wait(lambda: any("whisper" in c.text for c in src.poll()), timeout=5)
    finally:
        src.stop()


def test_fallback_when_no_audio_reaches_the_helper(tmp_path, whisper_env):
    src = _fallback(tmp_path, whisper_env, {"heartbeat": {"freeze_after": 0.3}})
    src.start()
    try:
        assert _wait(lambda: src.name == "whisper", timeout=8, step=src.maintain)
        assert "no audio" in src.switched_reason and not src.primary.running  # the helper was stopped first
    finally:
        src.stop()


def test_no_switch_while_the_primary_is_healthy(tmp_path, whisper_env):
    src = _fallback(tmp_path, whisper_env, {"heartbeat": {}})
    src.start()
    try:
        deadline = time.monotonic() + 1.2
        while time.monotonic() < deadline:
            src.maintain()
            time.sleep(0.05)
        assert src.name == "apple" and src.fallback is None and runs(whisper_env["log"]) == []
    finally:
        src.stop()


def test_the_page_says_the_backup_transcription_is_in_use(tmp_path, client_cfg, whisper_env):
    session = Session(client=client_cfg, session_dir=tmp_path / "s", engine=OfflineEngine(), settings=QUIET,
                      source_factory=lambda d: _fallback(tmp_path, whisper_env, BOTH_FAIL))
    with TestClient(create_app(session)) as http:
        st = http.post("/api/start").json()
    assert st["listening"]["backend"] == "whisper" and "No spoken language is ready" in st["listening"]["fallback"]
    for lang in i18n.available():
        assert "whisper" in i18n.t("live.fallback", lang, engine="whisper")


# --- the CLI ----------------------------------------------------------------------------------------


def test_pacenote_run_with_the_whisper_backend_skips_the_apple_helper(tmp_path, whisper_env, monkeypatch, capsys):
    from pacenote import cli

    served = {}
    monkeypatch.setattr(cli, "_serve", lambda session, args, auto_start: served.update(s=session) or 0)
    code = main(["run", "--client", "brioche-four", "--clients-dir", str(REAL_CLIENTS), "--speech-backend", "whisper",
                 "--listen-bin", "/nonexistent/pacenote-listen", "--claude-bin", _claude(tmp_path),
                 "--sessions-dir", str(tmp_path / "sessions"), "--no-browser"])
    assert code == 0 and "Speech: whisper" in capsys.readouterr().out
    src = served["s"].source_factory(tmp_path / "x")
    assert isinstance(src, WhisperStreamBackend) and runs(whisper_env["log"]) == []  # nothing listens yet


def test_pacenote_run_falls_back_when_apple_speech_is_not_ready(tmp_path, whisper_env, monkeypatch, capsys):
    from pacenote import cli

    script, _ = fake_listen(tmp_path, BOTH_FAIL)
    served = {}
    monkeypatch.setattr(cli, "_serve", lambda session, args, auto_start: served.update(s=session) or 0)
    monkeypatch.setattr(speech, "apple_supported", lambda platform=None: True)
    code = main(["run", "--client", "brioche-four", "--clients-dir", str(REAL_CLIENTS), "--listen-bin", str(script),
                 "--claude-bin", _claude(tmp_path), "--sessions-dir", str(tmp_path / "sessions"), "--no-browser"])
    out = capsys.readouterr()
    assert code == 0 and "whisper.cpp fallback" in out.err and "Speech: whisper" in out.out


def test_pacenote_run_on_windows_without_whisper_says_how_to_set_it_up(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(speech, "apple_supported", lambda platform=None: False)
    code = main(["run", "--client", "brioche-four", "--clients-dir", str(REAL_CLIENTS), "--claude-bin", _claude(tmp_path),
                 "--sessions-dir", str(tmp_path / "sessions"), "--no-browser"])
    err = capsys.readouterr().err
    assert code == 2 and "No speech backend is available" in err and "PACENOTE_WHISPER_MODEL" in err
