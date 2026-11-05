"""Live input: ONE `pacenote-listen` process for every spoken language, or replay saved SRTs.

`pacenote-listen` (pacenote/listen/pacenote_listen.swift, compiled on demand with swiftc) captures the
microphone once and runs one on-device SpeechTranscriber per expected locale on the same audio.
It writes JSON lines on stdout; each `segment` is tagged with its locale, which maps to a stream
id ("fr", "en"). Audio never touches this program.

Why one process instead of one `yap dictate` per language: every yap run releases ALL reserved
speech locales and reserves its own again; macOS then starts removing the model that run is
about to load, so `yap dictate --locale fr-FR` fails with "Failed to fetch the GeneralASR asset
for fr" or CancellationError at random, whatever the spacing between runs (reproduced on
2026-11-03 with 5 s between runs: 2 of 4 failed). pacenote-listen reserves the expected locales once,
never releases them, installs a missing model before loading it, and only says "ready" when
every model is loaded and the microphone is open. Nothing at start-up is retried: an error says
its real cause and its remedy.

A language that cannot be made ready (model download failed, unsupported…) never blocks the
interview: the helper skips it (`locale_failed`) and listens to the others; the page shows a banner.
Only when NO language is ready does start-up fail.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Protocol

from . import i18n
from .srt import Cue, format_timestamp, load_srt

log = logging.getLogger(__name__)

# stream id -> locale. The default (the example client): French + English. Any other set comes from
# `--locales de-DE,en-US`, PACENOTE_LOCALES, the questionnaire's `speech_locales`, or the consent screen.
DEFAULT_LOCALES: dict[str, str] = i18n.stream_ids(i18n.DEFAULT_SPEECH)


def locales_map(locales: list[str] | dict[str, str] | str | None) -> dict[str, str]:
    """stream id -> locale for ['de-DE', 'en-US'] / 'de,en' / an existing map / None (default)."""
    if not locales:
        return dict(DEFAULT_LOCALES)
    if isinstance(locales, dict):
        return dict(locales)
    return i18n.stream_ids(i18n.normalize_speech(locales))


@dataclass
class TimedCue:
    stream: str
    start: float  # seconds since session start
    end: float
    text: str


@dataclass
class Health:
    """A backend's health right now. `audio_age`: seconds since audio last reached it (None when
    the backend has no heartbeat); `problem`: why it is not healthy (None when it is)."""

    alive: bool
    audio_age: float | None = None
    problem: str | None = None

    @property
    def ok(self) -> bool:
        return self.alive and self.problem is None


# No audio buffer for this long while the helper is alive = the capture is stuck (a quiet room
# still delivers buffers: silence is audio).
HEARTBEAT_STALE = float(os.environ.get("PACENOTE_HEARTBEAT_STALE", "10"))


class Source(Protocol):
    """What the session reads from: live speech or saved SRTs."""

    def start(self) -> None: ...
    def poll(self, final: bool = False) -> list[TimedCue]: ...
    def stop(self) -> None: ...
    def problems(self) -> list[str]: ...
    def stream_status(self) -> dict[str, dict]: ...


class TranscriptionBackend(Source, Protocol):
    """A speech-to-text engine (pacenote/speech.py picks one per platform and config).

    start()          open the microphone and load every language; returns once at least one is
                     listening, raises CaptureError (cause + fix) when NONE is ready. Never
                     called before the Start button.
    ready_locales()  the locales listening now (a language that is not ready is simply absent)
    poll()           new final segments, each tagged with its stream id (= language)
    health()         alive + seconds since audio last flowed (heartbeat) + problem
    stop()           stop listening (flushes the last segments to poll(final=True))
    Optional: maintain() (watchdog, called every poll), configure_locales(), on_progress.
    """

    name: str

    def ready_locales(self) -> list[str]: ...
    def health(self) -> Health: ...


class CaptureError(RuntimeError):
    """Listening could not start (or stopped for a reason no restart fixes). The message says why
    and what to do."""


class HelperUnavailable(CaptureError):
    """pacenote-listen could not be built (no Xcode Command Line Tools, compile error)."""


# --- the helper binary: built on demand from the Swift source, cached by source hash ----------

SWIFT_SOURCE = Path(__file__).parent / "listen" / "pacenote_listen.swift"
HELPER_NAME = "pacenote-listen"


def helper_cache_dir() -> Path:
    return Path(os.environ.get("PACENOTE_LISTEN_CACHE") or Path.home() / "Library" / "Caches" / "pacenote")


def helper_path(source: Path = SWIFT_SOURCE) -> Path:
    digest = hashlib.sha256(source.read_bytes()).hexdigest()[:12]
    return helper_cache_dir() / f"{HELPER_NAME}-{digest}"


def _clt_missing() -> bool:
    try:
        return subprocess.run(["xcode-select", "-p"], capture_output=True, timeout=10).returncode != 0
    except (OSError, subprocess.SubprocessError):
        return True


CLT_FIX = ("pacenote needs Apple's Xcode Command Line Tools to build its speech helper (once).\n\n"
           "Fix:\n  - Install them:  xcode-select --install\n  - Then:  pacenote check --client {slug}")


def ensure_helper(explicit: str | None = None, *, on_progress=None, slug_hint: str = "<client>",
                  source: Path = SWIFT_SOURCE, swiftc: str | None = None) -> str:
    """The pacenote-listen binary: --listen-bin / PACENOTE_LISTEN_BIN as given, else the cached build of
    `source` (compiled with swiftc the first time, or after the source changed)."""
    explicit = explicit or os.environ.get("PACENOTE_LISTEN_BIN")
    if explicit:
        if os.path.isfile(explicit) and os.access(explicit, os.X_OK):
            return explicit
        raise HelperUnavailable(f"{explicit} (--listen-bin / PACENOTE_LISTEN_BIN) is not an executable file.")
    target = helper_path(source)
    if target.is_file() and os.access(target, os.X_OK):
        return str(target)
    compiler = swiftc or shutil.which("swiftc")
    if not compiler or (swiftc is None and _clt_missing()):
        raise HelperUnavailable(CLT_FIX.format(slug=slug_hint))
    target.parent.mkdir(parents=True, exist_ok=True)
    if on_progress:
        on_progress("  building the speech helper (once, a few seconds)…")
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    try:
        res = subprocess.run([compiler, "-parse-as-library", "-O", "-swift-version", "5", str(source), "-o", str(tmp)],
                             capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as exc:
        raise HelperUnavailable(f"Building the speech helper failed: {exc}\n\n" + CLT_FIX.format(slug=slug_hint)) from exc
    if res.returncode != 0 or not tmp.exists():
        errors = [ln for ln in (res.stderr or "").splitlines() if "error" in ln.lower()][:8]
        tmp.unlink(missing_ok=True)
        raise HelperUnavailable("Building the speech helper failed (swiftc):\n  " + "\n  ".join(errors or [res.stderr[-400:]])
                                + "\n\nFix:\n  - Update the Command Line Tools (macOS 26+ SDK needed): "
                                  "softwareupdate --list, or xcode-select --install")
    os.replace(tmp, target)
    for old in target.parent.glob(f"{HELPER_NAME}-*"):  # earlier builds of an older source
        if old != target and not old.name.endswith(".tmp"):
            old.unlink(missing_ok=True)
    return str(target)


# --- errors reported by the helper ----------------------------------------------------------

# Error codes no restart can fix: the watchdog stops and says what to do.
FATAL_CODES = {"microphone", "unsupported_locale", "speech_unavailable", "too_many_locales", "usage", "audio_format"}

PRIVACY_FIX = ("  - System Settings > Privacy & Security > Microphone: enable the app you run pacenote from\n"
               "    (Terminal, iTerm2, Ghostty, VS Code…), then quit and reopen it.")


def explain(error: dict, slug_hint: str = "<client>") -> str:
    """One readable message for a helper `error` event: what happened, why, what to do."""
    code = error.get("code", "")
    loc = error.get("locale") or ""
    msg = str(error.get("message") or "pacenote-listen stopped").strip()
    lines = [f"Listening could not start{f' for {loc}' if loc else ''}: {msg}", ""]
    if code == "microphone":
        lines += ["Fix:", PRIVACY_FIX]
    elif code in ("unsupported_locale", "too_many_locales"):
        lines += ["Fix:", "  - Pick spoken languages this Mac supports: the client's speech_locales,",
                  f"    or  pacenote run --client {slug_hint} --locales fr-FR,en-US"]
    elif code == "speech_unavailable":
        lines += ["Fix:", "  - On-device transcription needs macOS 26 or later on an Apple silicon Mac."]
    elif code == "install_failed":
        lines += ["Why: Apple's speech model is downloaded once per language and this download failed.",
                  "", "Fix:", "  - Connect the Mac to the internet; the model is downloaded on the next start:",
                  f"        pacenote check --client {slug_hint}"]
    elif code in ("reserve_failed", "not_installed", "model_load_failed"):
        lines += ["Why: macOS did not make this language's speech model available to pacenote.", "",
                  "Fix:", "  - System Settings > General > Language & Region, or > Keyboard > Dictation: make sure",
                  "    this language can be downloaded, then:", f"        pacenote check --client {slug_hint}"]
    else:
        lines += ["The helper's log is in the session folder (listen.log)."]
    return "\n".join(lines).rstrip()


PROGRESS = {
    "locale": lambda e: (f"  {e['locale']}: downloading the speech model (first time only, can take a few minutes)…"
                         if e.get("status") != "installed" else None),
    "install": lambda e: f"  {e['locale']}: speech model installed ({e.get('seconds', 0):.0f}s)",
    "released": lambda e: f"  {e['locale']}: released (not an expected language) to make room",
    "loaded": lambda e: f"  {e['locale']}: model loaded",
    "locale_failed": lambda e: f"  {e['locale']}: NOT ready, listening goes on without it: {e.get('message', '')}",
}


# --- one helper run --------------------------------------------------------------------------

class _Run:
    """One pacenote-listen process: reads its JSON lines on a thread, turns segments into TimedCues
    (session clock = helper clock + offset at `ready`), and mirrors them to <stream>.srt."""

    def __init__(self, cmd: list[str], streams: dict[str, str], t0: float, session_dir: Path,
                 on_event, srt_counters: dict[str, int]):
        self.streams = {loc: sid for sid, loc in streams.items()}  # locale -> stream id
        self.t0 = t0
        self.session_dir = session_dir
        self.on_event = on_event
        self.srt_counters = srt_counters
        self.cues: queue.Queue[TimedCue] = queue.Queue()
        self.ready = threading.Event()
        self.ended = threading.Event()
        self.ready_locales: list[str] = []
        self.prepared_locales: list[str] = []
        self.failed: dict[str, dict] = {}  # locale -> its `locale_failed` event (skipped, the others listen)
        self.heartbeats = 0
        self.buffers = 0
        self.rms = 0.0
        self.audio_at = 0.0  # monotonic time the buffer count last grew (or `ready`)
        self.error: dict | None = None
        self.offset = 0.0
        self.started_at = time.monotonic()
        self.ready_at = 0.0
        self._err = (session_dir / "listen.log").open("ab")
        self.popen = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=self._err,
                                      start_new_session=True)  # own process group: clean, complete shutdown
        self._thread = threading.Thread(target=self._read, daemon=True, name="pacenote-listen-reader")
        self._thread.start()

    @property
    def alive(self) -> bool:
        return self.popen.poll() is None

    def _read(self) -> None:
        try:
            for raw in self.popen.stdout:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    log.debug("pacenote-listen: %s", line)
                    continue
                if not isinstance(ev, dict):
                    continue
                self._handle(ev)
        finally:
            self.popen.wait()
            self.ended.set()
            self.ready.set()  # wake a start() waiting for ready

    def _handle(self, ev: dict) -> None:
        kind = ev.get("event")
        if kind == "segment":
            sid = self.streams.get(ev.get("locale", ""))
            text = str(ev.get("text", "")).strip()
            if sid is None or not text:
                return
            cue = TimedCue(sid, float(ev.get("start", 0)) + self.offset, float(ev.get("end", 0)) + self.offset, text)
            self._write_srt(cue)
            self.cues.put(cue)
            return
        if kind == "heartbeat":  # about every second: audio still flowing? (not logged)
            self.heartbeats += 1
            buffers = int(ev.get("buffers") or 0)
            if buffers > self.buffers or not self.audio_at:
                self.audio_at = time.monotonic()
            self.buffers, self.rms = max(self.buffers, buffers), float(ev.get("rms") or 0.0)
            return
        if kind == "ready":
            self.ready_at = time.monotonic()
            self.audio_at = self.ready_at
            self.offset = self.ready_at - self.t0
            self.ready_locales = list(ev.get("locales") or [])
            self.ready.set()
        elif kind == "locale_failed":
            self.failed[str(ev.get("locale", ""))] = ev
        elif kind == "prepared":
            self.prepared_locales = list(ev.get("locales") or [])
        elif kind == "error":
            self.error = ev
        self.on_event(ev)

    def _write_srt(self, cue: TimedCue) -> None:
        n = self.srt_counters[cue.stream] = self.srt_counters.get(cue.stream, 0) + 1
        try:
            with (self.session_dir / f"{cue.stream}.srt").open("a", encoding="utf-8") as fh:
                fh.write(f"{n}\n{format_timestamp(cue.start)} --> {format_timestamp(cue.end)}\n{cue.text}\n\n")
        except OSError:
            pass

    def drain(self) -> list[TimedCue]:
        out = []
        while True:
            try:
                out.append(self.cues.get_nowait())
            except queue.Empty:
                return out

    def stop(self, timeout: float) -> None:
        if self.alive:
            _signal_group(self.popen, signal.SIGINT)  # the helper flushes its last segments, then exits
        try:
            self.popen.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _signal_group(self.popen, signal.SIGTERM)
            try:
                self.popen.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                _signal_group(self.popen, signal.SIGKILL)
                self.popen.wait()
        self._thread.join(timeout=2.0)
        self.close()

    def close(self) -> None:
        try:
            self._err.close()
        except OSError:
            pass


START_TIMEOUT = float(os.environ.get("PACENOTE_LISTEN_START_TIMEOUT", "900"))  # a first model download included
MAX_ATTEMPTS = int(os.environ.get("PACENOTE_LISTEN_RESTARTS", "5"))  # watchdog restarts per outage
RETRY_GAP = float(os.environ.get("PACENOTE_LISTEN_RESTART_GAP", "2.0"))  # 2, 4, 6, 6… s between restarts
RECOVERED_AFTER = 10.0  # a restarted helper alive this long resets the watchdog's budget


def backoff(attempt: int, base: float | None = None) -> float:
    """Wait after watchdog restart n: base, 2*base, 3*base, then 3*base (2, 4, 6, 6 s by default)."""
    base = RETRY_GAP if base is None else base
    return base * min(max(attempt, 1), 3)


@dataclass
class ListenCapture:
    """Every spoken language from ONE pacenote-listen process: stream id -> locale in `locales`
    (default fr + en); configure_locales() changes it before start.

    start(): builds the helper if needed, starts `pacenote-listen listen --locale … --locale …` and
    waits for `ready` (all models reserved, installed and loaded; microphone open). An `error`
    or an early exit raises CaptureError with the cause and the fix. No retry.

    maintain(): the watchdog, called every poll by the session. If the helper dies it is started
    again (models are still reserved and installed, nothing is released), up to `max_attempts`
    per outage with a growing pause; an error no restart can fix (microphone permission,
    unsupported language) stops it with the fix.
    """

    name: ClassVar[str] = "apple"  # TranscriptionBackend: on-device Apple SpeechAnalyzer (macOS 26+)
    session_dir: Path
    locales: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_LOCALES))
    helper_bin: str | None = None
    start_timeout: float = START_TIMEOUT
    max_attempts: int | None = None
    retry_gap: float | None = None
    recovered_after: float = RECOVERED_AFTER
    stop_timeout: float = 5.0
    on_progress: object = None  # callable(str) for start-up messages (the consent screen mirrors them)
    slug_hint: str = "<client>"  # client slug, for the commands suggested in error messages
    _run: _Run | None = None
    _binary: str = ""
    _t0: float = 0.0
    _stopping: bool = False
    _started: bool = False
    _srt: dict[str, int] = field(default_factory=dict)
    _pending: list[TimedCue] = field(default_factory=list)
    attempts: int = 0  # watchdog restarts in the current outage
    restarts: int = 0  # watchdog restarts over the session
    gave_up: bool = False
    last_error: str = ""
    _next_retry_at: float = 0.0
    prepare_failed: dict[str, str] = field(default_factory=dict)  # prepare(): locale -> why it is not ready

    def __post_init__(self) -> None:
        self.locales = locales_map(self.locales)
        self.max_attempts = MAX_ATTEMPTS if self.max_attempts is None else self.max_attempts
        self.retry_gap = RETRY_GAP if self.retry_gap is None else self.retry_gap

    def configure_locales(self, locales: list[str] | dict[str, str]) -> None:
        """Select the spoken languages (before start), e.g. ["de-DE", "en-US"]."""
        if self._started:
            raise CaptureError("the spoken languages cannot change while listening")
        self.locales = locales_map(locales)

    # --- start --------------------------------------------------------------------------------
    def _cmd(self, command: str = "listen") -> list[str]:
        cmd = [self._binary, command]
        for loc in self.locales.values():
            cmd += ["--locale", loc]
        return cmd

    def _progress(self, msg: str) -> None:
        if callable(self.on_progress):
            self.on_progress(msg)

    def _on_event(self, ev: dict) -> None:
        kind = ev.get("event")
        fmt = PROGRESS.get(kind)
        msg = fmt(ev) if fmt else None
        if kind == "ready":
            msg = f"listening: {', '.join(ev.get('locales') or [])} (one process, pid {self._run.popen.pid if self._run else '?'})"
        elif kind == "error":
            msg = f"error {ev.get('code')}: {ev.get('message')}"
        if msg:
            self._log(msg.strip())
            if kind != "error" and not self._started:
                self._progress(msg)

    def _spawn(self, command: str = "listen") -> _Run:
        log.info("starting %s", " ".join(self._cmd(command)))
        return _Run(self._cmd(command), self.locales, self._t0, self.session_dir, self._on_event, self._srt)

    def prepare(self) -> list[str]:
        """Warm-up WITHOUT the microphone (`pacenote-listen prepare`): every model reserved, installed and
        loaded, then the helper exits. Start is then fast, and the microphone only opens on Start.
        Returns the prepared locales; raises CaptureError with the cause and the fix."""
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self._binary = ensure_helper(self.helper_bin, on_progress=self._progress, slug_hint=self.slug_hint)
        self._t0 = time.monotonic()
        run = self._spawn("prepare")
        if not run.ended.wait(self.start_timeout):
            run.stop(self.stop_timeout)
            raise CaptureError(explain({"code": "exited", "message": f"not ready after {self.start_timeout:.0f}s"},
                                       self.slug_hint))
        run.stop(self.stop_timeout)  # already exited: joins the reader, closes the log
        if run.error or run.popen.returncode != 0:
            raise CaptureError(explain(run.error or {"code": "exited", "message": _tail(self.session_dir / "listen.log")
                                                     or f"exit code {run.popen.returncode}"}, self.slug_hint))
        self.prepare_failed = {loc: explain(ev, self.slug_hint).splitlines()[0] for loc, ev in run.failed.items()}
        return run.prepared_locales

    def start(self) -> None:
        if self._started:
            return
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self._binary = ensure_helper(self.helper_bin, on_progress=self._progress, slug_hint=self.slug_hint)
        self._t0 = time.monotonic()
        run = self._run = self._spawn()
        if not run.ready.wait(self.start_timeout) or not run.alive or run.error:
            detail = run.error or {"code": "exited",
                                   "message": _tail(self.session_dir / "listen.log")
                                   or (f"exit code {run.popen.returncode}" if not run.alive
                                       else f"not ready after {self.start_timeout:.0f}s")}
            run.stop(self.stop_timeout)
            self._pending += run.drain()
            raise CaptureError(explain(detail, self.slug_hint))
        self._started = True

    # --- watchdog -----------------------------------------------------------------------------
    def maintain(self) -> list[str]:
        """Restart a dead helper (non-blocking). Returns log messages for this call."""
        notes: list[str] = []
        run = self._run
        if self._stopping or run is None or self.gave_up:
            return notes
        now = time.monotonic()
        if run.alive:
            if self.attempts and run.ready.is_set() and now - run.ready_at >= self.recovered_after:
                notes.append(f"listening recovered after {self.attempts} restart(s)")
                self.attempts, self.last_error = 0, ""
        elif now >= self._next_retry_at:
            run.ended.wait(1.0)
            self._pending += run.drain()  # what it said before it died, on its own clock
            run.close()
            err = run.error or {}
            self.last_error = str(err.get("message") or _tail(self.session_dir / "listen.log", 300)
                                  or f"exit code {run.popen.returncode}")
            if err.get("code") in FATAL_CODES:
                self.gave_up = True
                notes.append(f"listening stopped, not restarting: {self.last_error}")
            elif self.attempts >= self.max_attempts:
                self.gave_up = True
                notes.append(f"listening stopped: gave up after {self.attempts} restarts: {self.last_error}")
            else:
                self.attempts += 1
                self.restarts += 1
                notes.append(f"listening stopped ({self.last_error.splitlines()[-1][:160]}); "
                             f"restarting {self.attempts}/{self.max_attempts}")
                self._run = self._spawn()
                self._next_retry_at = now + backoff(self.attempts, self.retry_gap)
        for n in notes:
            log.warning(n)
            self._log(n)
        return notes

    # --- reading --------------------------------------------------------------------------------
    def poll(self, final: bool = False) -> list[TimedCue]:
        out, self._pending = self._pending, []
        if self._run is not None:
            out += self._run.drain()
        return sorted(out, key=lambda c: (c.end, c.stream))

    def stream_status(self) -> dict[str, dict]:
        """Per stream: alive, problem (None when fine), restarts, gave_up, and stopped (stop() was
        called on purpose: nothing is reported and nothing restarts). One process serves every
        stream, so they share their state."""
        run = self._run
        status = {}
        for sid, loc in self.locales.items():
            alive = bool(run and run.alive and run.ready.is_set() and loc in run.ready_locales)
            failed = (run.failed.get(loc) if run is not None and run.alive and run.ready_locales
                      and loc not in run.ready_locales else None)
            if failed is not None:  # skipped at start: the others listen (banner, not an outage)
                status[sid] = {"locale": loc, "alive": False, "unavailable": True, "restarts": self.restarts,
                               "problem": explain(failed, self.slug_hint).splitlines()[0], "gave_up": False,
                               "stopped": self._stopping}
                continue
            problem = None
            if not self._stopping and run is not None and not alive and run.ready.is_set():
                err = self.last_error or str((run.error or {}).get("message") or "") or "no error output"
                if self.gave_up:
                    problem = explain(run.error or {"message": err}, self.slug_hint).splitlines()[0]
                    if (run.error or {}).get("code") == "microphone":
                        problem += (" — check System Settings > Privacy & Security > Microphone for the app "
                                    "you run pacenote from")
                else:
                    problem = (f"listening ({loc}) stopped; restarting (attempt {max(1, self.attempts)}/"
                               f"{self.max_attempts}): {err.splitlines()[-1][:200]}")
            status[sid] = {"locale": loc, "alive": alive, "problem": problem, "restarts": self.restarts,
                           "gave_up": self.gave_up, "stopped": self._stopping}
        return status

    def ready_locales(self) -> list[str]:
        run = self._run
        if run is None or not run.alive or not run.ready.is_set():
            return []
        return [loc for loc in self.locales.values() if loc in run.ready_locales]

    def health(self, stale_after: float | None = None) -> Health:
        """Alive and listening; `audio_age` from the helper's heartbeat (None before the first one,
        e.g. an older helper build without heartbeats)."""
        stale_after = HEARTBEAT_STALE if stale_after is None else stale_after
        run = self._run
        alive = bool(self.ready_locales())
        age = (time.monotonic() - run.audio_at) if run is not None and run.heartbeats and alive else None
        problem = None
        if self.gave_up:
            problem = f"listening stopped: {self.last_error.splitlines()[-1][:200] if self.last_error else 'gave up'}"
        elif age is not None and age > stale_after:
            problem = f"no audio from the microphone for {age:.0f} s"
        elif not alive and self._started and not self._stopping:
            problem = "listening stopped; restarting"
        return Health(alive=alive, audio_age=age, problem=problem)

    def unavailable(self) -> dict[str, str]:
        """Languages skipped at start (locale -> why); the others are listening."""
        return {v["locale"]: v["problem"] for v in self.stream_status().values() if v.get("unavailable")}

    def problems(self) -> list[str]:
        seen: list[str] = []
        for v in self.stream_status().values():
            if v.get("unavailable"):
                continue  # shown as the "language not available" banner, not as an outage
            if v["problem"] and v["problem"] not in seen:
                seen.append(v["problem"])
        return seen

    # --- stop ---------------------------------------------------------------------------------
    def stop(self, timeout: float | None = None) -> None:
        self._stopping = True
        if self._run is not None:
            self._run.stop(self.stop_timeout if timeout is None else timeout)

    @property
    def running(self) -> bool:
        return self._run is not None and self._run.alive

    def _log(self, msg: str) -> None:
        try:
            with (self.session_dir / "capture.log").open("a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
        except OSError:
            pass


def preflight(locales: list[str] | dict[str, str] | None = None, *, helper_bin: str | None = None,
              on_progress=print, slug_hint: str = "<client>", log_dir: Path | None = None,
              microphone: bool = True, report: dict | None = None) -> float:
    """Before the UI. microphone=True (`pacenote check`): the real start once (models reserved,
    installed, loaded; microphone open), then a clean stop. microphone=False (`pacenote run`): the
    models only (`pacenote-listen prepare`), the microphone stays closed until Start is pressed.
    Returns the seconds it took; raises CaptureError with the cause and the fix when NO language is
    ready. `report` (optional dict) receives {"ready": [locales], "failed": {locale: why}}."""
    import tempfile

    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="pacenote-listen-") as tmp:
        cap = ListenCapture(Path(log_dir or tmp), locales_map(locales), helper_bin, on_progress=on_progress,
                            slug_hint=slug_hint)
        if not microphone:
            ready = cap.prepare()
            if report is not None:
                report.update(ready=ready, failed=dict(cap.prepare_failed))
            return time.monotonic() - started
        try:
            cap.start()
            if report is not None:
                report.update(ready=list(cap._run.ready_locales), failed=cap.unavailable())
        finally:
            cap.stop()
    return time.monotonic() - started


def _tail(path: Path, n: int = 400) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-n:].strip()
    except OSError:
        return ""


def _signal_group(popen: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(os.getpgid(popen.pid), sig)
    except (ProcessLookupError, PermissionError):
        pass


class ReplaySource:
    """Feeds saved SRT files as if they were live.

    speed=1.0 is real time, 10.0 is ten times faster, 0 (or less) emits everything at once.
    """

    def __init__(self, files: dict[str, Path], speed: float = 1.0, clock=time.monotonic):
        self.files = {k: Path(v) for k, v in files.items()}
        self.speed = speed
        self._clock = clock
        self._queue: list[TimedCue] = []
        self._t0: float | None = None
        self._stopped = False

    def start(self) -> None:
        cues: list[TimedCue] = []
        for stream, path in self.files.items():
            for c in load_srt(path):
                cues.append(TimedCue(stream, c.start, c.end, c.text))
        self._queue = sorted(cues, key=lambda c: (c.end, c.stream))
        self._t0 = self._clock()

    def elapsed(self) -> float:
        if self._t0 is None:
            return 0.0
        if self.speed <= 0:
            return float("inf")
        return (self._clock() - self._t0) * self.speed

    def poll(self, final: bool = False) -> list[TimedCue]:
        if self._t0 is None:
            return []
        now = float("inf") if final else self.elapsed()
        ready = [c for c in self._queue if c.end <= now]
        self._queue = self._queue[len(ready) :]
        return ready

    @property
    def exhausted(self) -> bool:
        return self._t0 is not None and not self._queue

    def problems(self) -> list[str]:
        return []

    def stream_status(self) -> dict[str, dict]:
        return {k: {"locale": k, "alive": not self._stopped, "problem": None, "stopped": self._stopped}
                for k in self.files}

    name = "replay"

    def ready_locales(self) -> list[str]:
        return [] if self._t0 is None or self._stopped else list(self.files)

    def health(self) -> Health:
        return Health(alive=bool(self.ready_locales()))

    def stop(self) -> None:
        self._stopped = True


def cues_to_timed(stream: str, cues: list[Cue]) -> list[TimedCue]:
    return [TimedCue(stream, c.start, c.end, c.text) for c in cues]
