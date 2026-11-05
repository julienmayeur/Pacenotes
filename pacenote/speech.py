"""Which speech-to-text backend listens, per platform and config (capture.TranscriptionBackend).

  apple    pacenote-listen (capture.ListenCapture): on-device Apple SpeechAnalyzer, one process for every
           spoken language. macOS 26+ only. The default on a Mac.
  whisper  whisper.cpp's `whisper-stream` (WhisperStreamBackend), OPTIONAL and light: pacenote vendors
           nothing and downloads nothing. It is used only when you installed the binary yourself
           (on PATH, or PACENOTE_WHISPER_BIN) AND point PACENOTE_WHISPER_MODEL at a model file you
           downloaded (e.g. ggml-large-v3-turbo.bin). One stream in the primary language
           (whisper keeps the English words of a French sentence in that one stream).

Selection (`pacenote run --speech-backend`, PACENOTE_SPEECH_BACKEND; default `auto`):
  auto     macOS: apple, and whisper as an automatic fallback when it is configured (Start fails
           with no language ready, the helper gives up mid-interview, or no audio reaches it for
           PACENOTE_HEARTBEAT_STALE s). Elsewhere (Windows, Linux): whisper when configured, else an
           error that says how to set it up.
  apple / whisper  that one only, no fallback.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, ClassVar

from . import i18n
from .capture import (
    HEARTBEAT_STALE,
    CaptureError,
    Health,
    ListenCapture,
    TimedCue,
    locales_map,
)
from .srt import format_timestamp

BACKENDS = ("auto", "apple", "whisper")
WHISPER_NAMES = ("whisper-stream", "whisper-cpp-stream")

SETUP_WHISPER = (
    "whisper.cpp (optional, local): install its streaming binary `whisper-stream` (macOS: `brew install "
    "whisper-cpp`, then check `which whisper-stream`; Windows/Linux: a whisper.cpp release or a build with "
    "SDL2), put it on PATH or set "
    "PACENOTE_WHISPER_BIN, download a model yourself (e.g. ggml-large-v3-turbo.bin from the whisper.cpp "
    "models page) and set PACENOTE_WHISPER_MODEL to that file. pacenote never downloads a model.")
NO_BACKEND = ("No speech backend is available on this computer: on-device Apple speech needs macOS 26+, "
              "and the whisper.cpp backend is not configured.\n\nFix:\n  - " + SETUP_WHISPER)


def apple_supported(platform: str | None = None) -> bool:
    return (platform or sys.platform) == "darwin"


def whisper_config(env: dict | None = None) -> tuple[str | None, str | None, str]:
    """(binary, model, why-not) for the whisper backend; binary and model are None when missing."""
    env = os.environ if env is None else env
    binary = env.get("PACENOTE_WHISPER_BIN") or next((b for n in WHISPER_NAMES if (b := shutil.which(n))), None)
    model = env.get("PACENOTE_WHISPER_MODEL") or None
    if binary and not (os.path.isfile(binary) and os.access(binary, os.X_OK)):
        return None, model, f"PACENOTE_WHISPER_BIN={binary} is not an executable file"
    if not binary:
        return None, model, "whisper-stream not found (PATH or PACENOTE_WHISPER_BIN)"
    if not model:
        return binary, None, "PACENOTE_WHISPER_MODEL is not set (the path of a whisper.cpp model you downloaded)"
    if not os.path.isfile(model):
        return binary, None, f"PACENOTE_WHISPER_MODEL={model} is not a file"
    return binary, model, ""


@dataclass
class Plan:
    """What `select` decided: the primary backend and the optional fallback."""

    primary: str  # "apple" | "whisper"
    fallback: str | None = None
    note: str = ""  # why there is no fallback, for `pacenote check` / logs


def select(name: str | None = None, platform: str | None = None, env: dict | None = None) -> Plan:
    """Pick the backend for `name` (auto | apple | whisper) on this platform; raises CaptureError
    with the setup steps when nothing can listen."""
    name = (name or (os.environ if env is None else env).get("PACENOTE_SPEECH_BACKEND") or "auto").strip().lower()
    if name not in BACKENDS:
        raise CaptureError(f"unknown speech backend {name!r} (choose one of {', '.join(BACKENDS)})")
    binary, model, why = whisper_config(env)
    whisper_ok = bool(binary and model)
    if name == "apple":
        if not apple_supported(platform):
            raise CaptureError("--speech-backend apple needs macOS (on-device Apple speech).\n\nFix:\n  - "
                               + SETUP_WHISPER)
        return Plan("apple")
    if name == "whisper":
        if not whisper_ok:
            raise CaptureError(f"--speech-backend whisper: {why}.\n\nFix:\n  - " + SETUP_WHISPER)
        return Plan("whisper")
    if apple_supported(platform):
        return Plan("apple", "whisper" if whisper_ok else None, "" if whisper_ok else f"no fallback: {why}")
    if whisper_ok:
        return Plan("whisper")
    raise CaptureError(NO_BACKEND)


def make_source(plan: Plan, session_dir: Path, locales, *, helper_bin: str | None = None, on_progress=None,
                slug_hint: str = "<client>", env: dict | None = None):
    """The session's source for `plan`: ListenCapture, WhisperStreamBackend, or the first with the
    second as its automatic fallback."""
    locales = locales_map(locales)

    def whisper(t0: float | None = None) -> WhisperStreamBackend:
        binary, model, _ = whisper_config(env)
        return WhisperStreamBackend(Path(session_dir), dict(locales), binary or "", model or "", t0=t0,
                                    on_progress=on_progress)

    if plan.primary == "whisper":
        return whisper()
    apple = ListenCapture(Path(session_dir), dict(locales), helper_bin, on_progress=on_progress, slug_hint=slug_hint)
    if plan.fallback == "whisper":
        return FallbackSource(apple, whisper)
    return apple


# --- whisper.cpp ------------------------------------------------------------------------------------

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_BLOCK_START = re.compile(r"^### Transcription \d+ START(?:.*?t0 = (\d+) ms.*?t1 = (\d+) ms)?")
_BLOCK_END = re.compile(r"^### Transcription \d+ END")
_TIMED = re.compile(r"^\[(\d+):(\d+):(\d+)[.,](\d+) --> (\d+):(\d+):(\d+)[.,](\d+)\]\s*(.*)$")
_NOISE = re.compile(r"^[\[(].*[\])]$")  # [BLANK_AUDIO], (music), [Musique]…


def _secs(h, m, s, ms) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def parse_whisper_stream(lines) -> list[tuple[float, float, str]]:
    """Segments (start, end, text) from `whisper-stream --step 0` (VAD mode) output: blocks
    `### Transcription N START | t0 = … ms | t1 = … ms`, lines `[hh:mm:ss.mmm --> …]  text`,
    `### Transcription N END`. Times are seconds since the stream started."""
    out: list[tuple[float, float, str]] = []
    base = None
    for raw in lines:
        line = _ANSI.sub("", raw).strip()
        if not line:
            continue
        m = _BLOCK_START.match(line)
        if m:
            base = int(m.group(1)) / 1000 if m.group(1) else 0.0
            continue
        if _BLOCK_END.match(line):
            base = None
            continue
        if base is None:
            continue  # "[Start speaking]", init logs: not speech
        t = _TIMED.match(line)
        start, end, text = (base, base, line) if not t else (
            base + _secs(*t.group(1, 2, 3, 4)), base + _secs(*t.group(5, 6, 7, 8)), t.group(9))
        text = text.strip()
        if text and not _NOISE.match(text):
            out.append((start, end, text))
    return out


@dataclass
class WhisperStreamBackend:
    """whisper.cpp `whisper-stream` on the microphone, VAD mode (`--step 0`): one stream, the
    primary locale's language, every segment tagged with the primary stream id. The other
    spoken languages are reported unavailable (whisper keeps code-switched words in the one stream).
    Not verified on this Mac against a real whisper-stream: it is fed by the documented output
    format (tests) only; see README "Speech backends"."""

    name: ClassVar[str] = "whisper"
    session_dir: Path
    locales: dict[str, str]
    binary: str
    model: str
    t0: float | None = None  # session clock origin (monotonic); default: start()
    on_progress: Callable[[str], None] | None = None
    start_timeout: float = 120.0
    stop_timeout: float = 5.0
    threads: int = 4
    _popen: subprocess.Popen | None = None
    _lines: list[str] = field(default_factory=list)
    _pending: list[TimedCue] = field(default_factory=list)
    _ready: threading.Event = field(default_factory=threading.Event)
    _ended: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _offset: float = 0.0
    _stopping: bool = False
    _srt_n: int = 0
    error: str = ""

    @property
    def primary(self) -> tuple[str, str]:
        sid, loc = next(iter(self.locales.items()))
        return sid, loc

    def configure_locales(self, locales) -> None:
        self.locales = locales_map(locales)

    def _cmd(self) -> list[str]:
        lang = i18n.language_of(self.primary[1])
        return [self.binary, "-m", self.model, "-l", lang, "--step", "0", "--length", "10000", "-vth", "0.6",
                "-t", str(self.threads)]

    def start(self, wait: bool = True) -> None:
        """Start whisper-stream. wait=False returns at once (a fallback switch on the event loop);
        the backend reports alive once whisper says it listens."""
        if not self.binary or not self.model:
            raise CaptureError("whisper.cpp is not configured.\n\nFix:\n  - " + SETUP_WHISPER)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.t0 = time.monotonic() if self.t0 is None else self.t0
        log = (self.session_dir / "whisper.log").open("ab")
        kw = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"  # type: ignore[attr-defined]
              else {"start_new_session": True})
        try:
            self._popen = subprocess.Popen(self._cmd(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=log,
                                           **kw)
        except OSError as exc:
            log.close()
            raise CaptureError(f"whisper-stream could not start: {exc}\n\nFix:\n  - {SETUP_WHISPER}") from exc
        threading.Thread(target=self._read, args=(log,), daemon=True, name="whisper-reader").start()
        if self.on_progress:
            self.on_progress(f"  whisper.cpp: loading {Path(self.model).name}…")
        if not wait:
            return
        if not self._ready.wait(self.start_timeout) or not self.running:
            self.stop()
            raise CaptureError(f"whisper-stream did not start listening: {self.error or 'no output'} "
                               f"(log: {self.session_dir / 'whisper.log'})")

    def _read(self, log) -> None:
        block: list[str] = []
        try:
            for raw in self._popen.stdout:
                line = _ANSI.sub("", raw.decode("utf-8", errors="replace")).rstrip("\r\n")
                if "[Start speaking]" in line and not self._ready.is_set():
                    self._offset = time.monotonic() - (self.t0 or time.monotonic())
                    self._ready.set()
                    continue
                block.append(line)
                if _BLOCK_END.match(line.strip()):
                    self._emit(parse_whisper_stream(block))
                    block = []
        finally:
            self._popen.wait()
            if not self._stopping and self._popen.returncode:
                self.error = f"whisper-stream exited ({self._popen.returncode})"
            log.close()
            self._ended.set()
            self._ready.set()

    def _emit(self, segments) -> None:
        sid = self.primary[0]
        for start, end, text in segments:
            cue = TimedCue(sid, start + self._offset, end + self._offset, text)
            self._srt_n += 1
            try:
                with (self.session_dir / f"{sid}.srt").open("a", encoding="utf-8") as fh:
                    fh.write(f"{self._srt_n}\n{format_timestamp(cue.start)} --> {format_timestamp(cue.end)}\n{text}\n\n")
            except OSError:
                pass
            with self._lock:
                self._pending.append(cue)

    @property
    def running(self) -> bool:
        return self._popen is not None and self._popen.poll() is None

    def poll(self, final: bool = False) -> list[TimedCue]:
        with self._lock:
            out, self._pending = self._pending, []
        return out

    def ready_locales(self) -> list[str]:
        return [self.primary[1]] if self.running and self._ready.is_set() else []

    def health(self) -> Health:
        alive = bool(self.ready_locales())
        problem = None if alive or self._stopping or not self._popen else (self.error or "whisper-stream is starting")
        return Health(alive=alive, audio_age=None, problem=problem)

    def stream_status(self) -> dict[str, dict]:
        sid0, _ = self.primary
        alive = bool(self.ready_locales())
        out = {}
        for sid, loc in self.locales.items():
            if sid == sid0:
                problem = None if alive or self._stopping or self._popen is None else (
                    self.error or "whisper.cpp is loading its model")
                out[sid] = {"locale": loc, "alive": alive, "problem": problem, "stopped": self._stopping,
                            "restarts": 0, "gave_up": bool(self.error)}
            else:  # one stream only: whisper keeps the other language's words in the primary stream
                out[sid] = {"locale": loc, "alive": False, "unavailable": True, "stopped": self._stopping,
                            "problem": f"whisper.cpp transcribes one stream ({self.primary[1]})",
                            "restarts": 0, "gave_up": False}
        return out

    def problems(self) -> list[str]:
        return [v["problem"] for v in self.stream_status().values() if v["problem"] and not v.get("unavailable")]

    def stop(self, timeout: float | None = None) -> None:
        self._stopping = True
        p = self._popen
        if p is None or p.poll() is not None:
            return
        try:
            if os.name == "nt":
                p.terminate()
            else:
                os.killpg(os.getpgid(p.pid), signal.SIGINT)
            p.wait(timeout=self.stop_timeout if timeout is None else timeout)
        except (subprocess.TimeoutExpired, ProcessLookupError, PermissionError):
            p.kill()
            p.wait()
        self._ended.wait(2.0)


# --- automatic fallback -------------------------------------------------------------------------------


class FallbackSource:
    """The primary backend, switched for the fallback (once) when its health check fails: Start
    fails with no language ready, it gives up mid-interview, or no audio reached it for
    `stale_after` s. Nothing ever switches back during an interview."""

    def __init__(self, primary, fallback_factory: Callable[..., object], stale_after: float | None = None):
        self.primary = primary
        self.fallback_factory = fallback_factory
        self.fallback = None
        self.active = primary
        self.stale_after = HEARTBEAT_STALE if stale_after is None else stale_after
        self.switched_reason = ""
        self._switching: threading.Thread | None = None
        self._on_progress = getattr(primary, "on_progress", None)

    @property
    def name(self) -> str:
        return self.active.name

    @property
    def on_progress(self):
        return self._on_progress

    @on_progress.setter
    def on_progress(self, fn) -> None:  # the session mirrors start-up messages on the consent screen
        self._on_progress = fn
        for b in (self.primary, self.fallback):
            if b is not None and hasattr(b, "on_progress"):
                b.on_progress = fn

    def configure_locales(self, locales) -> None:
        self.primary.configure_locales(locales)

    def _switch(self, reason: str, wait: bool) -> list[str]:
        note = f"switching to the fallback speech backend: {reason}"
        self._log(note)
        try:
            self.primary.stop()
        except Exception:  # it failed already; the fallback must still start
            pass
        fallback = self.fallback_factory(getattr(self.primary, "_t0", None) or None)
        fallback.configure_locales(dict(self.primary.locales))
        fallback.on_progress = self._on_progress
        self.fallback, self.switched_reason = fallback, reason
        fallback.start(wait=wait)
        self.active = fallback
        return [note]

    def _switch_quietly(self, reason: str) -> None:
        try:
            self._switch(reason, wait=False)
        except CaptureError as exc:  # the fallback could not start either: say it on the page
            self.switched_reason = f"{reason}; the fallback did not start: {str(exc).splitlines()[0]}"

    def start(self) -> None:
        try:
            self.primary.start()
        except CaptureError as exc:
            self._switch(str(exc).splitlines()[0], wait=True)

    def maintain(self) -> list[str]:
        if self._switching is not None and self._switching.is_alive():
            return []  # stopping the primary, starting the fallback (off the event loop)
        if self.active is self.primary and self._switching is None:
            notes = list(self.primary.maintain() or []) if hasattr(self.primary, "maintain") else []
            h = self.primary.health(self.stale_after) if hasattr(self.primary, "health") else None
            if getattr(self.primary, "gave_up", False) or (h is not None and h.audio_age is not None
                                                            and h.audio_age > self.stale_after):
                reason = h.problem if h and h.problem else "listening stopped"
                self._switching = threading.Thread(target=self._switch_quietly, args=(reason,), daemon=True,
                                                   name="speech-fallback")
                self._switching.start()
                notes.append(f"switching to the fallback speech backend: {reason}")
            return notes
        return list(getattr(self.active, "maintain", lambda: [])() or [])

    def poll(self, final: bool = False) -> list[TimedCue]:
        cues = self.primary.poll(final) if self.active is not self.primary else []  # its last words
        return sorted(cues + self.active.poll(final), key=lambda c: (c.end, c.stream))

    def ready_locales(self) -> list[str]:
        return self.active.ready_locales()

    def health(self) -> Health:
        return self.active.health()

    def stream_status(self) -> dict[str, dict]:
        return self.active.stream_status()

    def problems(self) -> list[str]:
        return self.active.problems()

    def stop(self, timeout: float | None = None) -> None:
        for b in {id(self.primary): self.primary, id(self.active): self.active}.values():
            b.stop()

    @property
    def running(self) -> bool:
        return bool(getattr(self.active, "running", False))

    def _log(self, msg: str) -> None:
        log = getattr(self.primary, "_log", None)
        if callable(log):
            log(msg)
