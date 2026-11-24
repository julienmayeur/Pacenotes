"""Session orchestration: ingest loop, fast pass, full sync loop, finalize, persistence.

Two model lanes run side by side, each with at most one call in flight:
  * fast pass  - a few seconds after new speech (debounced), a fast model fills draft
                 answers, statuses and coverage from just the new lines;
  * full sync  - first ~15 s after the first speech, then every `sync_interval` seconds,
                 the main model handles queue edits, rewording, plan notes, enough_data.
Everything runs on one asyncio loop; blocking model calls run in worker threads and
their results are applied back on the loop, so state is never mutated concurrently.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import shutil
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from pacenote.copilot.contract import ContractError
from pacenote.copilot.engines import Engine, SyncFailed, offline_plan, offline_plan_doc
from pacenote.copilot.prompts import (
    build_fast_system,
    build_fast_user,
    build_plan_system,
    build_plan_user,
    build_sync_system,
    build_sync_user,
)
from pacenote.interview import coverage, lens, timebox, translate
from pacenote.interview import phases as P
from pacenote.interview.questionnaire import ClientConfig
from pacenote.interview.state import SessionState
from pacenote.knowledge import recipes as recipe_lib
from pacenote.people import attendees as attendees_lib
from pacenote.people import personas, pilots
from pacenote.people import viewpoints as viewpoints_lib
from pacenote.plan.finalize import apply_horizons, apply_recipes, assemble_plan, render_plan_md, write_plan_files
from pacenote.shared import fsutil, i18n
from pacenote.transcription.capture import ReplaySource, Source
from pacenote.transcription.srt import format_clock
from pacenote.transcription.transcript import Transcript

log = logging.getLogger(__name__)


@dataclass
class Settings:
    sync_interval: float = 40.0  # full sync cadence
    first_sync_delay: float = 15.0  # first full sync this long after the first speech
    context_seconds: float = 60.0
    poll_interval: float = 0.5
    min_new_chars: int = 20  # don't call the main model for a slice with almost nothing new
    fast_enabled: bool = True
    fast_debounce: float = 2.5  # fast pass this long after the last new cue...
    fast_max_wait: float = 8.0  # ...or at the latest this long after the first pending cue
    fast_context_seconds: float = 30.0
    fast_min_chars: int = 8
    fast_retry_after: float = 3.0  # back-off after a failed fast call
    # the question being answered stays on screen until the answer is over (see _answer_over): a pause
    # this long after a line that ends a sentence, or `answer_pause_open` after one that does not, counted
    # on the speech clock beyond what the recogniser still holds back (its delivery lag, measured)
    answer_pause: float = 3.5
    answer_pause_open: float = 7.0
    answer_min_chars: int = 12  # something was said since the pinned question went up
    # once the question's time slot is over (timebox.py) the next question moves up at a shorter pause,
    # or at the next line that ends a sentence once the slot is `timebox.GRACE` seconds over
    slot_pause: float = 1.5
    freeze_silence: float = 2.5  # the upcoming list only re-orders after this much silence


_STOP = frozenset((
    "vous nous elle elles ils sont cette votre notre leur leurs quoi quel quelle quels quelles comment "
    "combien pourquoi est-ce estce dans pour avec sans mais donc alors aussi tres plus moins tout tous toute "
    "toutes chez entre comme fait faire etre avoir peut peux sont etait quand where what which that this "
    "with your have does about there their they them from will would could should when then than into"
).split())


def _content_words(text: str) -> set[str]:
    """The words that carry a question (folded, 4+ letters, not a common word): to recognise it read aloud."""
    folded = unicodedata.normalize("NFKD", text.lower())
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return {w for w in re.findall(r"[a-z0-9]+", folded) if len(w) >= 4 and w not in _STOP}


def _ask_key(focus: str | None, item: dict) -> str:
    """A queued item's stable key on screen (the page sends it back when it is clicked)."""
    return f"f:{focus}:{item['at']!r}" if item["kind"] == "followup" else f"n:{item['qid']}"


def new_session_dir(sessions_dir: Path, slug: str) -> Path:
    d = Path(sessions_dir) / slug / datetime.now().strftime("%Y%m%d-%H%M%S")
    d.mkdir(parents=True, exist_ok=False)
    return d


@dataclass
class Session:
    client: ClientConfig
    session_dir: Path
    engine: Engine
    source_factory: Callable[[Path], Source]
    settings: Settings = field(default_factory=Settings)
    state: SessionState | None = None
    transcript: Transcript = field(default_factory=Transcript)
    last_synced_seq: int = 0
    wispr_text: str | None = None
    on_plan_written: Callable[[dict[str, str]], None] | None = None  # e.g. the CLI prints the paths

    def __post_init__(self) -> None:
        self.session_dir = Path(self.session_dir).resolve()  # absolute: shown in the UI and the CLI
        self.session_dir.mkdir(parents=True, exist_ok=True)
        if self.state is None:
            self.state = SessionState.from_questionnaire(self.client.questionnaire)
            # who is in the meeting: everyone on the client sheet, each with their persona (attendees.py)
            self.state.set_attendees(self.client.interviewees)
        self.source: Source | None = None
        self._version = 0
        self._cond: asyncio.Condition | None = None
        self._force_sync: asyncio.Event | None = None
        self._tasks: list[asyncio.Task] = []
        self._sync_lock: asyncio.Lock | None = None
        self._sync_task: asyncio.Task | None = None
        self._fast_task: asyncio.Task | None = None
        self.last_fast_seq = self.last_synced_seq
        self._last_full_tick: float | None = None
        self._first_line_mono: float | None = None
        self._fast_backoff_until = 0.0
        self._finalizing = False  # one finalize at a time (set before its first await)
        self._stream_status: dict[str, dict] = {}
        self.fast_latencies: list[dict] = []  # per fast pass: speech line ingested -> on screen
        self.noise_seqs: set[int] = set()  # transcript lines the fast pass flagged as noise
        self.noise_moments: set[str] = set()  # their mm:ss (one per moment, both streams)
        self._upcoming_display: list[str] | None = None  # frozen upcoming list while people talk
        self._display_focus: str | None = None
        self.focus_changes: list[dict] = []  # every focus change, with its reason (for checks)
        self.ask_changes: list[dict] = []  # every change of what is asked now (follow-up promoted, focus moved)
        self._lags: list[float] = []  # speech clock at ingest - line start, recent lines: how long speech stays unseen
        self._moved_to: str | None = None  # the fast pass heard the answer to the queued next question
        # completion: the focus question seen open at the last check, the one an analysis then marked
        # answered (retired at once), and whether the speech after such a retirement is still the tail
        # of that answer (no pause yet: it must not count as the answer to the new question)
        self._open_focus: str | None = None
        self._completed: str | None = None
        self._tail = False
        self._tail_seq = 0  # the transcript version when the completed question was retired
        self._grace_from: dict[str, int] = {}  # focus id -> the line its slot's grace started after
        self._timing_at = 0.0
        self._t0_mono: float | None = None
        self._translate_task: asyncio.Task | None = None
        self._display_task: asyncio.Task | None = None  # translations for the display language only
        self._display_again = False  # new texts arrived while it ran
        self._display_failed: set[tuple[str, str]] = set()  # (lang, item id) the engine could not translate
        self.capture_stopped = False  # stop_capture() ran: the recognisers ended on purpose
        self.persist()

    # --- notification for SSE ------------------------------------------------
    def _ensure_async(self) -> None:
        if self._cond is None:
            self._cond = asyncio.Condition()
            self._force_sync = asyncio.Event()
            self._sync_lock = asyncio.Lock()

    async def notify(self) -> None:
        self._ensure_async()
        async with self._cond:
            self._version += 1
            self._cond.notify_all()

    async def wait_for_change(self, last: int, timeout: float = 15.0) -> int:
        self._ensure_async()
        async with self._cond:
            try:
                await asyncio.wait_for(self._cond.wait_for(lambda: self._version != last), timeout)
            except TimeoutError:
                pass
            return self._version

    # --- lifecycle ---------------------------------------------------------------
    @property
    def elapsed(self) -> float:
        return 0.0 if self._t0_mono is None else time.monotonic() - self._t0_mono

    async def start(self) -> None:
        """Called once consent has been given. Starts capture, ingest and sync loops."""
        self._ensure_async()
        if self.state.mode != "consent":
            return
        self.state.mode = "interview"
        self.state.started_at = time.time()
        self._t0_mono = time.monotonic()
        self.source = self.source_factory(self.session_dir)
        self.state.set_clock(self.interview_clock)  # budgets and slots run on the speech clock (any replay speed)
        self.state.start_phase_clock(self.state.started_at)  # the first phase's time budget starts now
        configure = getattr(self.source, "configure_locales", None)
        if callable(configure):  # the spoken languages picked for this session
            configure(list(self.state.speech_locales))
        self._hook_progress(asyncio.get_running_loop())
        try:
            # the speech helper starts and confirms each spoken language (a few s)
            await asyncio.to_thread(self.source.start)
        except Exception as exc:  # no speech helper, or it died right away
            self.state.capture_problems = [str(exc)]
            self.state.mode = "consent"
            self.state.started_at = None
            self.persist()
            await self.notify()
            raise
        self.state.capture_problems = []
        self._stream_status = self._read_stream_status()
        self._tasks = [
            asyncio.create_task(self._ingest_loop(), name="ingest"),
            asyncio.create_task(self._sync_loop(), name="sync"),
        ]
        if self.fast_available:
            self._tasks.append(asyncio.create_task(self._fast_loop(), name="fast"))
            warm = getattr(self.engine, "warm_fast", None)
            if warm is not None:  # start the persistent fast-pass process off the clock
                self._tasks.append(asyncio.create_task(asyncio.to_thread(_safe_call, warm, self._fast_system()),
                                                       name="warm"))
        self.persist()
        await self.notify()

    def _hook_progress(self, loop: asyncio.AbstractEventLoop) -> None:
        """Mirror capture start-up messages (model download, retries) on the consent screen."""
        if not hasattr(self.source, "on_progress"):
            return
        inner = self.source.on_progress

        def progress(msg: str) -> None:
            if callable(inner):
                inner(msg)
            self.state.capture_progress = msg.strip()
            asyncio.run_coroutine_threadsafe(self.notify(), loop)

        self.source.on_progress = progress

    # --- languages (rules in i18n.py) ---------------------------------------------
    async def set_languages(self, ui: str | None = None, interview: str | None = None,
                            speech: list[str] | str | None = None) -> None:
        """Switch the screen language, the interview language (translates what is missing in the
        background) or, before capture starts, the spoken languages. Raises i18n.LanguageError."""
        if speech is not None and self.state.mode != "consent":
            raise i18n.LanguageError("the spoken languages can only change before the interview starts")
        before = self.state.language
        self.state.set_languages(ui=ui, interview=interview, speech=speech)
        self._ensure_display_translation()
        self._log_sync({"kind": "languages", "ui": self.state.ui_language, "interview": self.state.language,
                        "speech": self.state.speech_locales})
        if self.state.language != before:
            self._ensure_translation()
            if self.fast_available and self.state.mode == "interview":  # new language: re-warm the fast pass
                warm = getattr(self.engine, "warm_fast", None)
                if warm is not None:  # the same system prompt as the calls: the warm process is reused
                    self._tasks.append(asyncio.create_task(asyncio.to_thread(_safe_call, warm, self._fast_system())))
        self.persist()
        await self.notify()

    def prepare_languages(self) -> None:
        """Called when the server starts: translate the questionnaire if the interview language
        has no variants in it (e.g. `--interview-language de`), and the display language too."""
        self._ensure_translation()
        self._ensure_display_translation()

    def _ensure_display_translation(self) -> None:
        """The display language (screen-language selector) differs from the interview language:
        translate what it is missing, follow-ups included, for display only. Generation, prompts,
        the plan and the speech locales never change. One task at a time; texts the engine could
        not translate are not retried (they show as generated)."""
        lang = self.state.languages.ui
        if lang == self.state.language:
            return  # the interview-language translation (_ensure_translation) covers it
        todo = [i for i in translate.missing_items(self.state, lang) if (lang, i["id"]) not in self._display_failed]
        if not todo:
            return
        if self._display_task is not None and not self._display_task.done():
            self._display_again = True
            return
        self._display_again = False
        self._display_task = asyncio.create_task(self._translate_display(lang), name=f"display-{lang}")

    async def _translate_display(self, lang: str) -> int:
        snapshot = self.state.model_copy(deep=True)
        wanted = [i["id"] for i in translate.missing_items(snapshot, lang)]
        try:
            found = await asyncio.to_thread(translate.fetch, snapshot, lang, self.engine, self.client.glossary,
                                            self.translation_cache)
            error = ""
        except asyncio.CancelledError:
            raise
        except NotImplementedError as exc:
            found, error = getattr(exc, "partial", {}), str(exc)
        except Exception as exc:  # keep the texts as generated
            found, error = {}, str(exc)
        self._display_failed |= {(lang, i) for i in wanted if i not in found}
        n = translate.apply(self.state, lang, found)
        self._log_sync({"kind": "translate-display", "lang": lang, "applied": n, "error": error})
        if n:
            self.persist()
            await self.notify()
        if self._display_again:
            self._ensure_display_translation()
        return n

    def _ensure_translation(self) -> None:
        lang = self.state.language
        if not translate.missing_items(self.state, lang):
            if self.state.translation.get("lang") != lang:
                self.state.translation = {}
            return
        if self._translate_task is not None and not self._translate_task.done():
            self._translate_task.cancel()  # a newer language wins
        self._translate_task = asyncio.create_task(self.translate_once(lang), name=f"translate-{lang}")

    @property
    def translation_cache(self) -> Path:
        return self.session_dir.parent / ".translations" / "cache.json"

    async def translate_once(self, lang: str) -> int:
        """Translate what `lang` is missing (one `claude -p` call on the fast model), then apply it
        on the event loop. Failures leave the fallback texts on screen."""
        self.state.translation = {"lang": lang, "status": "running", "count": 0, "error": ""}
        await self.notify()
        snapshot = self.state.model_copy(deep=True)
        started = time.monotonic()
        try:
            found = await asyncio.to_thread(translate.fetch, snapshot, lang, self.engine, self.client.glossary,
                                            self.translation_cache)
            error = ""
        except asyncio.CancelledError:
            raise
        except NotImplementedError as exc:
            found, error = getattr(exc, "partial", {}), str(exc)
        except Exception as exc:  # SyncFailed, ContractError, validation: keep the fallback texts
            found, error = {}, str(exc)
        n = translate.apply(self.state, lang, found)
        if self.state.language == lang:
            self.state.translation = {"lang": lang, "status": "error" if error else "done", "count": n,
                                      "error": error, "seconds": round(time.monotonic() - started, 1)}
        self._log_sync({"kind": "translate", "lang": lang, "applied": n, "error": error})
        self.persist()
        await self.notify()
        return n

    def _fast_system(self) -> str:
        """The fast pass's system prompt. ONE builder for the warm-up and every call: the warm
        `claude` process is keyed by model + system prompt, so any difference (e.g. the client
        context missing from the re-warm after a language switch) threw the warm process away."""
        return build_fast_system(self.state, self.client.glossary, self.client.questionnaire.context)

    @property
    def fast_available(self) -> bool:
        return self.settings.fast_enabled and callable(getattr(self.engine, "fast", None))

    async def shutdown(self) -> None:
        cancel = getattr(self.engine, "cancel", None)
        if cancel is not None:
            cancel()  # kill in-flight `claude` calls and the persistent fast session
        tasks = self._tasks + [t for t in (self._sync_task, self._fast_task, self._translate_task, self._display_task) if t]
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks = []
        self.stop_capture()
        self.persist()

    def stop_capture(self) -> None:
        """Intentional stop (interview ended, shutdown): no watchdog restart, no capture problem."""
        if self.source is not None:
            self.capture_stopped = True
            self.source.stop()
            self.ingest_once(final=True)
            self.state.capture_problems = []

    # --- ingest ----------------------------------------------------------------
    def ingest_once(self, final: bool = False) -> int:
        if self.source is None:
            return 0
        maintain = getattr(self.source, "maintain", None)
        if callable(maintain) and not final and not self.capture_stopped:
            for note in maintain() or []:  # watchdog: restart a dead recogniser
                self._log_sync({"kind": "capture", "note": note})
        cues = self.source.poll(final=final)
        now = time.time()
        clock = self.speech_clock()
        for c in cues:
            self.transcript.add(c.stream, c.start, c.end, c.text, received_at=now)
            if not final:
                self._lags = [*self._lags, max(0.0, clock - c.start)][-40:]
        if cues and self.state.first_speech_at is None:
            self.state.first_speech_at = now
            self._first_line_mono = time.monotonic()
        problems = [] if self.capture_stopped else self.source.problems()
        if problems != self.state.capture_problems:
            self.state.capture_problems = problems
        self._stream_status = self._read_stream_status()
        if cues:
            self.transcript.save_jsonl(self.session_dir / "transcript.jsonl")
        return len(cues)

    def _read_stream_status(self) -> dict[str, dict]:
        reader = getattr(self.source, "stream_status", None)
        return reader() if callable(reader) else {}

    async def _ingest_loop(self) -> None:
        while True:
            before = list(self.state.capture_problems)
            n = self.ingest_once()
            moved_on = self.state.mode == "interview" and self._maybe_move_on()
            advanced = self.state.mode == "interview" and self._maybe_retire()
            advanced = advanced or moved_on
            if n or advanced or self.state.capture_problems != before:
                await self.notify()
            self._log_timing()
            await asyncio.sleep(self.settings.poll_interval)

    TIMING_EVERY = 2.0  # s between two lines of timing.jsonl

    def _log_timing(self) -> None:
        """timing.jsonl (session folder, local): every few seconds, the speech clock, the last line's end, the
        microphone level, the pause measured and the time slot. What a pause or a move
        was based on can be read back after a real interview."""
        now = time.monotonic()
        if self.state.mode != "interview" or now - self._timing_at < self.TIMING_EVERY:
            return
        self._timing_at = now
        st = self.state
        level = getattr(self.source, "level", None)
        row = {"clock": round(self.speech_clock(), 2),
               "line_end": round(max((s.end for s in self.transcript.segments), default=0.0), 2),
               "level": level() if callable(level) else None,
               "pause": round(min(self.speech_pause(), 1e6), 2), "lines": self.transcript.last_seq,
               "focus": st.focus_id, "slot_left": timebox.slot_left(st), "left": round(timebox.left(st))}
        try:
            with (self.session_dir / "timing.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
        except OSError:
            pass

    # --- focus (the question on screen) ---------------------------------------------
    def silence(self) -> float:
        """Seconds since the last transcript line arrived (large when nothing yet)."""
        newest = max((s.received_at for s in self.transcript.segments), default=0.0)
        return time.time() - newest if newest else 1e9

    def speech_clock(self) -> float:
        """Seconds on the speech clock (segment start/end): the source's own when it has one (a replay,
        at any speed), else since Start (the live helper's clock starts with it)."""
        own = getattr(self.source, "elapsed", None)
        return float(own()) if callable(own) else self.elapsed

    def interview_clock(self) -> float:
        """Epoch seconds on the interview's clock: Start + the speech clock (wall time live; a replay's own
        pace at any speed)."""
        st = self.state
        clock = self.speech_clock()
        if st.started_at is None or not math.isfinite(clock):
            return time.time()
        return st.started_at + clock

    def speech_pause(self) -> float:
        """Seconds of silence we can be sure of: since the end of the last line on the speech clock, minus
        how long speech can stay unseen. A line only arrives once it ends (a ~60-character line takes a few
        seconds to say), and live, Apple's final results come in batches about every 11.5 s on top of that
        (sometimes a batch is skipped); so a line-less stretch means silence only beyond that delay: the
        longest, over the recent lines, of speech clock at arrival − line start, plus 25 %. Large when
        nothing was said yet."""
        if not self.transcript.segments:
            return 1e9
        last_end = max(s.end for s in self.transcript.segments)
        lag = 1.25 * max(self._lags, default=0.0)  # the longest recent delay, with a margin: batches can skip
        return self.speech_clock() - last_end - lag

    @staticmethod
    def line_open(text: str) -> bool:
        """True when a line does not end a sentence: no . ? ! at its end, or an ellipsis. The recogniser
        punctuates and cuts long sentences into ~60-character lines, so a line without a final mark is
        most likely cut mid-sentence: the speaker is going on."""
        t = text.strip().rstrip("»\"' ")
        return not t or t.endswith(("…", "...")) or t[-1] not in ".?!"

    ASK_TURN_GAP = 0.6  # s of speech gap before a line that may be the interviewer reading a queued question

    def _lines(self) -> list:
        """The readable lines in the interview language, noise left out, in speech order."""
        st = self.state
        primary = next((sid for sid in sorted({s.stream for s in self.transcript.segments})
                        if sid.split("-")[0] == st.language), st.language)
        return [s for s in self.transcript.readable(self.transcript.merged(), primary=primary)
                if s.seq not in self.noise_seqs]

    def _turn_since(self, since: int, upto: int) -> bool:
        """A turn starts among the lines after `since`, up to `upto` (a gap before it, or the first line):
        an answer to what was asked, not the rest of the question being read aloud."""
        lines = self._lines()
        return any(since < s.seq <= upto and (not i or s.start - lines[i - 1].end >= self.ASK_TURN_GAP)
                   for i, s in enumerate(lines))

    def _tail_end(self) -> int | None:
        """The end of the answer still going on after a completion move: the interviewer asking the new
        question aloud (its last line), or the last line before a real gap in the speech timestamps (`answer_pause` after a sentence end, `answer_pause_open` otherwise;
        exact even when lines arrive in batches), else, once a real pause is sure, the last line. None
        while it goes on."""
        cur = self.state.current_question
        if cur is not None and (read := self._read_aloud(
                [{"kind": "question", "source": cur.text(self.state.language)}], self._tail_seq, loose=True)):
            return read[2]  # the interviewer asks the new question (in their words): its answer comes next
        lines = self._lines()
        for i in range(1, len(lines)):
            prev = lines[i - 1]
            if lines[i].seq > self._tail_seq and lines[i].start - prev.end >= (
                    self.settings.answer_pause_open if self.line_open(prev.text) else self.settings.answer_pause):
                return prev.seq
        return self.transcript.last_seq if self.speech_pause() >= self.settings.answer_pause else None

    def _asked(self) -> dict | None:
        """The interviewer reads a queued question aloud: lines since the pinned one went up hold most
        of its words. Returns that queued item."""
        hit = self._read_aloud(self.ask_queue(), self.state.ask_seq or 0)
        return hit[0] if hit else None

    def _read_aloud(self, items: list[dict], since: int, loose: bool = False) -> tuple[dict, int, int] | None:
        """The first of `items` read aloud in a turn after line `since`: (item, seq of the line before that
        turn, seq of its last line read). `loose`: a paraphrase is enough (4 of its words, half of the turn's), for a long question."""
        lines = self._lines()
        # a question read aloud starts a turn (after a gap in the speech; the first lines after the pin are
        # the answer) and is mostly made of its own words: the turn's first lines, up to its next gap (a long
        # question takes several ~60-character lines)
        starts = [i for i, s in enumerate(lines) if s.seq > since and i
                  and s.start - lines[i - 1].end >= self.ASK_TURN_GAP]
        windows = []
        for i in starts:
            j = i + 1
            while j < len(lines) and j - i < 8 and lines[j].start - lines[j - 1].end < self.ASK_TURN_GAP:
                windows.append((i, j, _content_words(" ".join(s.text for s in lines[i:j]))))
                j += 1
            windows.append((i, j, _content_words(" ".join(s.text for s in lines[i:j]))))
        clock, now = self.speech_clock(), time.time()
        for item in items:
            words = _content_words(item["source"])
            if len(words) < 2:
                continue
            need = max(2, math.ceil(0.6 * len(words)))
            need, share = (min(need, 4), 2) if loose else (need, 3)
            # a follow-up can only be read once it is on screen; its own wording often echoes the answer
            # it was drawn from, which must not count
            shown = clock - (now - item["at"]) if item["kind"] == "followup" else -1e9
            for i, j, w in windows:
                if lines[i].start >= shown and len(words & w) >= need and len(words & w) * share >= len(w):
                    return item, lines[i - 1].seq, lines[j - 1].seq
        return None

    def ask_queue(self) -> list[dict]:
        """What is queued under the question being asked: the follow-ups, then the next question once
        the focus is answered. `source` is the text in the interview language (what is said aloud)."""
        st = self.state
        st.ensure_ask()
        out = [{"kind": "followup", "at": e.at, "source": e.text, "entry": e} for e in st.ask_next]
        nid = st.next_qid()
        if (nq := st.get(nid)) is not None:
            out.append({"kind": "question", "qid": nid, "source": nq.text(st.language)})
        return out

    def _answer_over(self) -> tuple[dict | None, str] | None:
        """Has the answer to what is asked now ended? Robust signals only, never a timer while people talk:
          * the interviewer reads a queued question (its words in the new lines): it is asked now;
          * the fast pass heard the answer to the queued next question, and none to the current one;
          * a pause on the speech clock after something was said: `answer_pause` after a line that
            ends a sentence, `answer_pause_open` otherwise, once the fast pass has read every line (the
            queue then reflects the whole answer).
        Returns (the queued item to ask now, or None = the focus is answered and nothing is queued, why)."""
        st = self.state
        cur = st.current_question
        if cur is None:
            return None
        queue = self.ask_queue()
        if st.ask_seq is None:
            st.ask_seq = self.transcript.last_seq
        if (self._open_focus == cur.id and cur.status == "answered" and cur.updated_by in ("fast", "sync")
                and (self._tail or self._turn_since(st.ask_seq, cur.read))):
            # an analysis just found it sufficiently covered, from an answer given after what is asked now
            # went up (a late result on lines said before the interviewer's follow-up, or on the rest of
            # that follow-up read aloud, must not yank it); one completed by a tail moves after its pause
            self._completed = cur.id
        self._open_focus = cur.id if cur.open else None
        if self._tail and (end := self._tail_end()) is not None:
            self._tail, st.ask_seq = False, end  # the old answer is over: what follows answers the new one
        if (hit := self._asked()) is not None:
            return hit, "the interviewer asked it"
        if self._moved_to is not None:
            moved, self._moved_to = self._moved_to, None
            hit = next((i for i in queue if i.get("qid") == moved), None)
            if hit is not None:
                return hit, "the interviewee answers the next question"
        if self._completed == cur.id and not self._tail:
            # complete: retire it now, even while the interviewee goes on (one per stretch of speech:
            # a question completed by the tail of the same answer waits for the pause after it)
            self._completed = None
            nxt = next((i for i in queue if i["kind"] == "question"), None)
            return nxt, f"complete, {len(cur.covered)} of {len(cur.all_expect())} details"
        if self._tail:
            return None  # still the tail of the answer just completed: the new question was not asked yet
        if ((timebox.slot_over(st) or coverage.pending_qid(st) is not None)
                and (nxt := next((i for i in queue if i["kind"] == "question"), None))):
            # the question's time is up (timebox.py), or the conversation has stayed on one point and the next
            # area's question is queued (coverage.py): it moves up at the first boundary, before any queued
            # follow-up (digging deeper is what the time no longer allows)
            why = self._slot_boundary()
            return (nxt, why) if why else None
        if not queue and cur.status != "answered":
            return None  # nothing to move to: the question stays
        said = sum(len(s.text) for s in self.transcript.segments if s.seq > st.ask_seq)
        if said < self.settings.answer_min_chars or self.fast_running:
            return None
        if self.fast_available and self.last_fast_seq < self.transcript.last_seq:
            return None  # the last words are not analysed yet: the queue may still change
        last = max(self.transcript.segments, key=lambda s: (s.end, s.seq))
        wait = self.settings.answer_pause_open if self.line_open(last.text) else self.settings.answer_pause
        if self.speech_pause() < wait:
            return None
        if cur.status == "answered":
            nxt = next((i for i in queue if i["kind"] == "question"), None)
            return nxt, f"answered, {wait:g} s pause"
        return queue[0], f"answer over, {wait:g} s pause"

    def _slot_boundary(self) -> str | None:
        """The slot of the question being asked is over: is now a boundary? Something was said since what is
        asked went up, and a sure pause of `slot_pause` seconds; or, once the slot is `timebox.GRACE` seconds
        over, a new line that ends a sentence (a live conversation can go minutes without a pause the
        batched lines can show)."""
        st = self.state
        if sum(len(s.text) for s in self.transcript.segments if s.seq > (st.ask_seq or 0)) < self.settings.answer_min_chars:
            return None
        # the next area's question (coverage.py) has its own clock: on screen at least SHOW seconds (the
        # interviewer reads the transition line and can keep going with x), its grace counted from then
        moving = (mq := coverage.pending_qid(st)) is not None
        age = st.now() - st.cov.move_on.get("at", st.now()) if moving else None
        what = "next area" if moving else "time slot over"
        if age is not None and age < coverage.SHOW:
            return None
        if self.speech_pause() >= self.settings.slot_pause:
            return f"{what}, {self.settings.slot_pause:g} s pause"
        over = age if age is not None else -(timebox.slot_left(st) or 0.0)
        if over < timebox.GRACE and not timebox.closing_due(st):
            return None  # (the closing's time has come: no grace)
        # live, the lines come in batches every ~11.5 s and a pause is rarely sure: the next line that ends a
        # sentence (in the newest batch) is the boundary
        mark = self._grace_from.setdefault(f"{st.focus_id}|{mq}" if moving else st.focus_id or "", self.transcript.last_seq)
        if any(s.seq > mark and not self.line_open(s.text) for s in self._lines()):
            return f"{what}, sentence end"
        return None

    def _maybe_move_on(self) -> bool:
        """The conversation has stayed on one point for its allowance (coverage.py): queue the next area's
        question with its transition line."""
        if not coverage.tick(self.state):
            return False
        mv = self.state.cov.move_on
        self._log_sync({"kind": "move_on", "clock": round(self.speech_clock(), 2), **mv,
                        "dwell": round(coverage.dwell(self.state)), "point": self.state.cov.topic.get("point", "")})
        self.persist()
        return True

    async def extend_point(self) -> bool:
        """The interviewer keeps going on the current point (x): more time, and a queued move-on is set aside."""
        if self.state.mode != "interview" or not coverage.extend(self.state):
            return False
        self._log_sync({"kind": "extend", "clock": round(self.speech_clock(), 2), "area": self.state.cov.topic.get("area")})
        self.persist()
        await self.notify()
        return True

    def _maybe_retire(self) -> bool:
        """What is asked now changes only once its answer is over (see _answer_over)."""
        over = self._answer_over()
        if over is None:
            return False
        item, why = over
        if item is not None and item["kind"] == "followup":
            self.state.promote(item["at"], reason=why)
            self._ask_changed("followup", why)
            self._tail, self._completed = False, None  # the interviewer digs on: a pending completion is moot
            self.persist()
            return True
        talking = self.speech_pause() < self.settings.answer_pause
        self._advance(why, to=item["qid"] if item else None)
        self._tail = why.startswith("complete") and talking  # moved while the old answer goes on
        self._tail_seq = self.transcript.last_seq
        return True

    def _ask_changed(self, kind: str, reason: str) -> None:
        entry = {"t": time.time(), "clock": round(self.speech_clock(), 2), "kind": kind, "reason": reason,
                 "focus": self.state.focus_id}
        self.ask_changes.append(entry)
        self._log_sync({"kind": "ask", **entry})

    async def ask_now(self, key: str) -> bool:
        """The interviewer clicked a queued question: it is asked now (key from view()['current']['ask_next'])."""
        item = next((i for i in self.ask_queue() if _ask_key(self.state.focus_id, i) == key), None)
        if item is None:
            return False
        if item["kind"] == "followup":
            self.state.promote(item["at"], reason="clicked by the interviewer")
            self._ask_changed("followup", "clicked by the interviewer")
            self._tail, self._completed = False, None
            self.persist()
        else:
            self._advance("next question clicked by the interviewer", to=item["qid"])
        await self.notify()
        return True

    def _advance(self, reason: str, to: str | None = None) -> None:
        old = self.state.focus_id
        new = self.state.advance(reason, to=to)
        self._tail = False  # moved by the interviewer or a signal: nothing left of the old answer to wait for
        self.focus_changes.append({"t": time.time(), "from": old, "to": new, "reason": reason})
        self._log_sync({"kind": "focus", "from": old, "to": new, "reason": reason})
        self._ask_changed("question", reason)
        self.persist()

    async def next_question(self, seen: str | None = None) -> str | None:
        """interviewer pressed n / Enter. `seen` is the question the interviewer was looking at: if the focus has
        already moved on since (e.g. an auto-advance a second earlier), the key press is ignored
        rather than skipping the new question too."""
        if seen is not None and seen != self.state.focus_id:
            return self.state.focus_id
        self._advance("interviewer pressed next", to=self.state.ask_next_qid)  # the « Question suivante » shown
        await self.notify()
        return self.state.focus_id

    async def next_phase(self, seen: str | None = None) -> str | None:
        """interviewer pressed p: close the current phase and move to the next one's first question (the
        current card joins the thread, as with n). `seen`: as for next_question."""
        if seen is not None and seen != self.state.focus_id:
            return self.state.focus_id
        old = self.state.focus_id
        new = self.state.skip_phase()
        self.focus_changes.append({"t": time.time(), "from": old, "to": new, "reason": "interviewer skipped to the next phase"})
        self._log_sync({"kind": "focus", "from": old, "to": new, "reason": "interviewer skipped to the next phase",
                        "phase": self.state.phase})
        self.persist()
        await self.notify()
        return self.state.focus_id

    def phases_view(self, shown: str) -> dict:
        """The phase strip: every phase that has questions, its state, time budget and time spent,
        and for the current one what its maps still miss ("À capter dans cette étape")."""
        st = self.state
        cur = st.current_phase
        cur_idx = P.PHASE_INDEX.get(cur or "", 0)
        tr = i18n.Translator(shown, st.i18n_overlay.get(shown))
        present = [p for p in P.PHASE_IDS if any(q.phase == p and q.status != "removed" for q in st.questions)]
        items = []
        for p in present:
            spent = st.phase_spent.get(p, 0.0) + (st.phase_elapsed() if p == st.phase else 0.0)
            idx = P.PHASE_INDEX[p]
            items.append({"id": p, "title": tr(f"phases.{p}"),
                          "state": "current" if p == cur else "done" if idx < cur_idx else "todo",
                          "budget": st.phase_budgets.get(p, 0.0), "spent": round(spent, 1),
                          "open": sum(1 for q in st.questions if q.phase == p and q.open),
                          "answered": sum(1 for q in st.questions if q.phase == p and q.status == "answered")})
        nxt = next((st.get(i) for i in st.upcoming_order() if (q := st.get(i)) is not None and q.phase != cur), None)
        current = None
        if cur is not None:
            budget = st.phase_budgets.get(cur, 0.0)
            current = {"id": cur, "title": tr(f"phases.{cur}"), "index": present.index(cur) + 1 if cur in present else 1,
                       "count": len(present), "budget": budget,
                       "left": round(budget - st.phase_elapsed(), 1) if st.phase_since is not None else budget,
                       "over": st.phase_budget_spent(cur), "done": st.phase_done(cur),
                       "next": tr(f"phases.{nxt.phase}") if nxt is not None else "",
                       "gaps": [tr(f"maps.{g}") for g in st.map_gaps(cur)]}
        return {"current": current, "items": items,
                "maps": {k: st.map_count(k) for k in st.maps}}

    def recipes_view(self, shown: str) -> list[dict]:
        """interviewer console only (never the question card): "Possible recipes", the library recipes the
        interview fits so far, strong first, with what is still missing for each."""
        lib = getattr(self, "_recipe_library", None)
        if lib is None:
            lib = self._recipe_library = recipe_lib.try_library()
        out = []
        for m in self.state.active_recipe_matches():
            r = lib.get(m.recipe_id)
            if r is None:
                continue
            uc = self.state.get_use_case(m.use_case)
            out.append({"id": r.id, "title": r.title_in(shown), "fit": m.fit, "status": r.status,
                        "evidence": len(r.evidence), "use_case": uc.title(shown) if uc else None,
                        "heard": len(m.heard_signals), "missing": list(m.missing)})
        return out

    def _upcoming_ids(self) -> list[str]:
        """Upcoming list as displayed: it never re-orders or grows while someone is talking;
        it refreshes when the focus changes or after `freeze_silence` seconds of silence."""
        st = self.state
        if (self._upcoming_display is None or self._display_focus != st.focus_id
                or self.speech_pause() >= self.settings.freeze_silence):
            self._upcoming_display = st.refresh_upcoming()
            self._display_focus = st.focus_id
        else:  # only drop entries that are no longer open; no re-ordering, no additions
            self._upcoming_display = [i for i in self._upcoming_display
                                      if i != st.focus_id and (q := st.get(i)) is not None and q.open]
        return list(self._upcoming_display)

    # --- fast incremental pass ---------------------------------------------------
    @property
    def fast_running(self) -> bool:
        return self._fast_task is not None and not self._fast_task.done()

    def _fast_due(self) -> bool:
        pending = [s for s in self.transcript.segments if s.seq > self.last_fast_seq]
        if not pending or sum(len(s.text) for s in pending) < self.settings.fast_min_chars:
            return False
        now = time.time()
        newest = max(s.received_at for s in pending)
        oldest = min(s.received_at for s in pending)
        return now - newest >= self.settings.fast_debounce or now - oldest >= self.settings.fast_max_wait

    async def _fast_loop(self) -> None:
        while True:
            await asyncio.sleep(0.2)
            if self.state.mode != "interview" or self.fast_running:
                continue
            if time.monotonic() < self._fast_backoff_until or not self._fast_due():
                continue
            self._fast_task = asyncio.create_task(self.fast_once(), name="fast-call")

    async def fast_once(self) -> bool:
        """Fast pass on the lines not yet seen by it. Returns True when a result was applied.
        An unexpected error (CLI missing, disk, a bad result) is reported and backed off like a
        failed call, never left to kill the task silently."""
        try:
            return await self._fast_once()
        except Exception as exc:
            log.exception("fast pass failed")
            self.state.last_fast_error = f"{type(exc).__name__}: {exc}"
            self._fast_backoff_until = time.monotonic() + self.settings.fast_retry_after
            self._report_unexpected({"kind": "fast", "error": self.state.last_fast_error})
            await self.notify()
            return False

    def _report_unexpected(self, entry: dict) -> None:
        """Log and save after an unexpected error; the disk may be what failed."""
        try:
            self._log_sync(entry)
            self.persist()
        except Exception:
            log.exception("could not record the error")

    async def _fast_once(self) -> bool:
        lines = self.transcript.since(self.last_fast_seq)
        if not lines or not self.fast_available:
            return False
        upto = self.transcript.last_seq
        context = self.transcript.context_before(self.last_fast_seq, self.settings.fast_context_seconds)
        system = self._fast_system()
        user = build_fast_user(self.state, lines, context)
        started = time.monotonic()
        await self.notify()  # shows "analyse…" while the pass runs
        try:
            result = await asyncio.to_thread(
                self.engine.fast, system, user, state=self.state.model_copy(deep=True), lines=lines
            )
        except (SyncFailed, ContractError) as exc:
            self.state.last_fast_error = str(exc)
            self.state.last_fast_seconds = round(time.monotonic() - started, 2)
            self._fast_backoff_until = time.monotonic() + self.settings.fast_retry_after
            self._log_sync({"kind": "fast", "error": str(exc), "lines": len(lines)})
            await self.notify()
            return False
        if self.state.mode not in ("interview", "finalize"):
            return False
        nxt = self.state.next_qid()  # queued under the answered focus question, if any
        report = self.state.apply_fast_result(result, read=upto)  # stamped with the transcript version it read
        pieces = {u.question_id for u in result.question_updates if u.answer_piece.strip()}
        if nxt is not None and nxt in pieces and self.state.focus_id not in pieces:
            self._moved_to = nxt  # the interviewee is answering the next question: the ask moves there
        applied = time.time()
        self.last_fast_seq = upto
        flagged = {n.strip().lstrip("#") for n in result.noise}
        for seg in lines:  # remember which lines the fast pass called noise (TV, nearby voices)
            if str(seg.seq) in flagged:
                self.noise_seqs.add(seg.seq)
                self.noise_moments.add(format_clock(seg.start))
        call = round(time.monotonic() - started, 2)
        received = [s.received_at for s in lines if s.received_at]
        latency = {
            "call": call,
            "newest": round(applied - max(received), 2) if received else None,
            "oldest": round(applied - min(received), 2) if received else None,
        }
        self.fast_latencies.append(latency)
        self.state.last_fast_seconds = call
        self.state.last_fast_latency = latency["newest"]
        self._log_sync(
            {
                "kind": "fast",
                "fast": self.state.fast_count,
                "applied_at": applied,
                "elapsed": format_clock(self.elapsed),
                "latency": latency,
                "engine": self.engine.name,
                "lines": [s.__dict__ for s in lines],
                "result": result.model_dump(),
                "read": upto,
                "rejected": [r.model_dump() for r in report.rejected],
            }
        )
        self._ensure_display_translation()  # a new follow-up: translated for display if needed
        self.persist()
        await self.notify()
        return True

    # --- sync --------------------------------------------------------------------
    def request_sync(self) -> None:
        self._ensure_async()
        self._force_sync.set()

    @property
    def sync_running(self) -> bool:
        return self._sync_task is not None and not self._sync_task.done()

    def _full_sync_due(self) -> bool:
        now = time.monotonic()
        if self._last_full_tick is None:  # first full sync: shortly after the first speech
            return self._first_line_mono is not None and now - self._first_line_mono >= self.settings.first_sync_delay
        return now - self._last_full_tick >= self.settings.sync_interval

    async def _sync_loop(self) -> None:
        """Full sync: first ~`first_sync_delay` s after the first speech, then a tick every
        `sync_interval` seconds (or on "sync now").

        Only one full-sync call runs at a time. If the previous call is still running when a
        tick fires, the tick is skipped (counted in state.skipped_ticks); the lines it would
        have sent simply go out with the next call.
        """
        while True:
            forced = False
            try:
                await asyncio.wait_for(self._force_sync.wait(), 0.25)
                forced = True
            except TimeoutError:
                pass
            self._force_sync.clear()
            if self.state.mode != "interview":
                continue
            if not forced and not self._full_sync_due():
                continue
            self._last_full_tick = time.monotonic()
            if self.sync_running:
                self.state.skipped_ticks += 1
                await self.notify()
                continue
            self._sync_task = asyncio.create_task(self.sync_once(), name="sync-call")

    async def sync_once(self, force: bool = False) -> bool:
        """One sync round. Returns True when a result was applied. An unexpected error is
        reported in last_sync_error like a failed call (the delta is re-sent next round)."""
        self._ensure_async()
        try:
            return await self._sync_once(force)
        except Exception as exc:
            log.exception("sync failed")
            self.state.sync_in_progress = False
            self.state.last_sync_error = f"{type(exc).__name__}: {exc}"
            self._report_unexpected({"error": self.state.last_sync_error, "engine": self.engine.name})
            await self.notify()
            return False

    async def _sync_once(self, force: bool) -> bool:
        async with self._sync_lock:
            self.ingest_once()
            delta = self.transcript.since(self.last_synced_seq)
            new_chars = sum(len(s.text) for s in delta)
            if not delta or (new_chars < self.settings.min_new_chars and not force):
                return False
            upto = self.transcript.last_seq
            context = self.transcript.context_before(self.last_synced_seq, self.settings.context_seconds)
            system = build_sync_system(self.state, self.client.glossary, self.client.questionnaire.context)
            user = build_sync_user(self.state, delta, context, self.elapsed)
            started = time.monotonic()
            self.state.sync_in_progress = True
            await self.notify()
            try:
                result = await asyncio.to_thread(
                    self.engine.sync, system, user, state=self.state.model_copy(deep=True), delta=delta
                )
            except (SyncFailed, ContractError) as exc:
                # Delta is kept: it will be re-sent (with whatever arrived since) next round.
                self.state.sync_in_progress = False
                self.state.last_sync_seconds = round(time.monotonic() - started, 1)
                self.state.last_sync_error = str(exc)
                self._log_sync({"error": str(exc), "delta_lines": len(delta), "seconds": self.state.last_sync_seconds,
                                "engine": self.engine.name})
                self.persist()
                await self.notify()
                return False
            finally:
                self.state.sync_in_progress = False
            self.state.last_sync_seconds = round(time.monotonic() - started, 1)
            if self.state.mode not in ("interview", "finalize"):
                return False
            # stamped with the transcript version it read: what a fast pass wrote meanwhile from newer
            # lines is kept (state.py, optimistic locking)
            report = self.state.apply_sync_result(result, thread=not self.fast_available, read=upto)
            self.last_synced_seq = upto
            self._log_sync(
                {
                    "sync": self.state.sync_count,
                    "elapsed": format_clock(self.elapsed),
                    "seconds": round(time.monotonic() - started, 1),
                    "engine": self.engine.name,
                    "delta": [s.__dict__ for s in delta],
                    "read": upto,
                    "result": result.model_dump(),
                    "rejected": [r.model_dump() for r in report.rejected],
                }
            )
            self._ensure_display_translation()
            self.persist()
            await self.notify()
            return True

    # --- manual controls -----------------------------------------------------
    async def mark_answered(self, qid: str) -> bool:
        was_focus = qid == self.state.focus_id
        old = self.state.focus_id
        ok = self.state.mark_answered(qid)
        if ok and was_focus:
            self.focus_changes.append({"t": time.time(), "from": old, "to": self.state.focus_id,
                                       "reason": "marked answered by the interviewer"})
        self.persist()
        await self.notify()
        return ok

    async def skip(self, qid: str) -> bool:
        was_focus = qid == self.state.focus_id
        old = self.state.focus_id
        ok = self.state.skip(qid)
        if ok and was_focus:
            self.focus_changes.append({"t": time.time(), "from": old, "to": self.state.focus_id,
                                       "reason": "skipped by the interviewer"})
        self.persist()
        await self.notify()
        return ok

    async def force_enough(self) -> None:
        self.state.force_enough()
        self.persist()
        await self.notify()

    async def set_wispr(self, text: str) -> None:
        self.wispr_text = text.strip() or None
        if self.wispr_text:
            fsutil.atomic_write_text(self.session_dir / "wispr.txt", self.wispr_text)
        self.state.wispr_merged = bool(self.wispr_text)
        self.refine_speakers()
        self.persist()
        await self.notify()

    def refine_speakers(self) -> None:
        """After the meeting: when the final transcript has speaker labels, map the views heard to the voices
        that said them (viewpoints.refine); a plain transcript leaves the speakers unavailable."""
        if self.wispr_text:
            viewpoints_lib.refine(self.state, self.wispr_text, "pasted")
        else:
            self.state.viewpoint_speakers = {}

    # --- finalize ------------------------------------------------------------------
    @property
    def finalizing(self) -> bool:
        return self._finalizing

    async def finalize(self) -> dict[str, str]:
        """Stop capture, run a last sync on the tail, then write plan.md + plan.json. One run at a
        time: a second call while one runs returns {}. If even the offline draft cannot be written,
        the session goes back to "finalize" with the error shown, so the interviewer can retry."""
        self._ensure_async()
        if self._finalizing or self.state.mode == "generating":
            return {}
        self._finalizing = True
        try:
            return await self._finalize()
        finally:
            self._finalizing = False

    async def _finalize(self) -> dict[str, str]:
        for t in self._tasks:
            t.cancel()
        for task in (self._fast_task, self._sync_task):  # let in-flight calls land first
            if task is not None and not task.done():
                try:
                    await task
                except Exception:
                    pass
        self.stop_capture()
        if self.state.mode == "interview":
            self.state.force_enough("interviewer ended the interview")
        if self.transcript.since(self.last_synced_seq):
            prev = self.state.mode
            self.state.mode = "finalize"
            try:
                await self.sync_once(force=True)
            finally:
                self.state.mode = prev
        self.record_ambassadors()
        before = self.state.mode
        self.state.mode = "generating"
        self.persist()
        await self.notify()
        try:
            try:
                files = await asyncio.to_thread(self.generate_plan)
            except Exception as exc:
                log.exception("plan generation failed")
                self.state.last_sync_error = f"plan generation failed: {exc}; wrote an offline draft instead"
                files = write_plan_files(self.session_dir, assemble_plan(offline_plan(self.state), self.state),
                                         self.state, doc=offline_plan_doc(self.state))
        except Exception as exc:  # not even the offline draft: back to "finalize", error shown, retry
            log.exception("offline plan draft failed")
            self.state.mode = before
            self.state.last_sync_error = f"plan not written: {exc}; try again"
            self._report_unexpected({"kind": "plan", "error": self.state.last_sync_error})
            await self.notify()
            return {}
        self.state.plan_files = {k: str(Path(v).resolve()) for k, v in files.items()}
        files = dict(self.state.plan_files)
        self.state.mode = "done"
        self.persist()
        self._log_sync({"kind": "plan", "files": files})
        if self.on_plan_written is not None:
            try:
                self.on_plan_written(files)
            except Exception:  # printing must never break the session
                log.exception("on_plan_written failed")
        await self.notify()
        return files

    def record_ambassadors(self) -> None:
        """The potential pilots this interview surfaced go to clients/<slug>/ambassadors.json
        (local, git-ignored; edited in the admin, where a hand edit wins)."""
        try:
            pilots.record(self.client.dir, self.state, self.session_dir.name)
        except Exception:  # a label file must never block the plan
            log.exception("could not record the potential ambassadors")

    def generate_plan(self) -> dict[str, str]:
        if self.wispr_text is None and (self.session_dir / "wispr.txt").exists():
            self.wispr_text = (self.session_dir / "wispr.txt").read_text(encoding="utf-8")
        system = build_plan_system(self.state, self.client.glossary, self.client.questionnaire.context)
        user = build_plan_user(self.state, self.transcript.merged(), self.wispr_text)
        if hasattr(self.engine, "write_plan_doc"):  # the integration proposal, structured then rendered
            doc = apply_recipes(self.engine.write_plan_doc(system, user, state=self.state))  # recipe-first rules
            doc = apply_horizons(doc, self.state)  # horizon guard, lighthouse, ambassador
            body = render_plan_md(doc, self.state)
            return write_plan_files(self.session_dir, assemble_plan(body, self.state), self.state, doc=doc)
        body = self.engine.write_plan(system, user, state=self.state)  # legacy engines: Markdown as returned
        return write_plan_files(self.session_dir, assemble_plan(body, self.state), self.state)

    # --- persistence -----------------------------------------------------------------
    def persist(self) -> None:
        self.state.save(self.session_dir / "state.json")
        meta = {"last_synced_seq": self.last_synced_seq}
        fsutil.atomic_write_text(self.session_dir / "session.json", json.dumps(meta))

    def _log_sync(self, entry: dict) -> None:
        with (self.session_dir / "sync_log.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")

    @classmethod
    def resume(cls, client: ClientConfig, session_dir: Path, engine: Engine) -> Session:
        """Re-open a finished/interrupted session (for `pacenote finalize`)."""
        session_dir = Path(session_dir)
        state = SessionState.load(session_dir / "state.json")
        transcript = Transcript.load_jsonl(session_dir / "transcript.jsonl")
        meta_path = session_dir / "session.json"
        last = json.loads(meta_path.read_text())["last_synced_seq"] if meta_path.exists() else 0
        s = cls(
            client=client,
            session_dir=session_dir,
            engine=engine,
            source_factory=lambda d: ReplaySource({}),
            state=state,
            transcript=transcript,
            last_synced_seq=last,
        )
        return s

    # --- view for the UI ---------------------------------------------------------------
    def strip_lines(self, n: int = 5) -> list[dict]:
        """Live transcript strip: for each moment only the readable recogniser, minus the lines
        the fast pass flagged as noise (TV, nearby voices). Lines not analysed yet are marked
        pending (shown dimmed)."""
        lang = self.state.language
        streams = {sid for sid in i18n.stream_ids(self.state.speech_locales)} | {s.stream for s in self.transcript.segments}
        lang = next((sid for sid in sorted(streams) if sid.split("-")[0] == lang), lang)
        recent = self.transcript.merged()[-6 * n:]
        out = []
        for s in self.transcript.readable(recent, primary=lang):
            if s.seq in self.noise_seqs:
                continue
            out.append({"seq": s.seq, "t": format_clock(s.start), "text": s.text, "stream": s.stream,
                        "pending": self.fast_available and s.seq > self.last_fast_seq})
        return out[-n:]

    def view(self) -> dict:
        st = self.state
        lang = st.language  # interview language: generation, prompts, plan (never the display)
        langs = st.languages
        # display language = the screen-language selector (top right): labels AND question texts,
        # follow-ups, ladder/missing items. ONE language at a time: no gloss in a second language.
        shown = langs.ui
        # coverage first: one language on screen (the question and the line leading to it, as shown)
        cov_view = coverage.view(st, shown, st.tr(shown), shown)

        def qv(q) -> dict:
            return {"id": q.id, "theme": q.theme, "text": q.text(shown),
                    "status": q.status, "origin": q.origin, "answer": q.answer, "updated_at": q.updated_at,
                    "updated_by": q.updated_by, "priority": q.priority,
                    "translated": shown in q.machine,  # machine-translated by Claude
                    "fallback": not q.has_text(shown)}  # no text in `shown` yet: the closest language is shown

        def summary(q) -> dict:
            """The answer so far, refreshed by every fast pass (pieces appended) and full sync
            (rewritten as a concise summary); `read` = the transcript line the analysis that wrote
            it had read (shown discreetly: "analyse à la lecture N")."""
            return {"summary": q.answer, "summary_by": q.updated_by if q.answer else None,
                    "summary_at": q.updated_at if q.answer else None, "read": q.read}

        def thread(q, n: int = 12) -> list[dict]:
            return [{"i": i, "kind": t.kind, "text": t.shown(shown, lang), "rung": t.rung}
                    for i, t in enumerate(q.thread)][-n:]

        st.ensure_ask()  # the ask belongs to the focus question (reset when the focus moved)
        cur = st.current_question
        current = None
        if cur is not None:
            rank = ("business", "workflow", "implementation", "done").index(cur.rung)
            rungs = [{"name": r, "state": "done" if i < rank else "current" if i == rank else "todo",
                      "total": len(cur.rung_items(r)), "covered": sum(1 for e in cur.rung_items(r) if e in cur.covered)}
                     for i, r in enumerate(("business", "workflow", "implementation"))]
            shown_rung = cur.rung if cur.rung != "done" else "implementation"
            current = {**qv(cur), **summary(cur), "rung": cur.rung, "rungs": rungs,
                       # "key" (the item's identity, source language) only when the shown text differs
                       "checklist": [{"item": cur.expect_text(e, shown), "done": e in cur.covered}
                                     | ({"key": e} if cur.expect_text(e, shown) != e else {})
                                     for e in cur.rung_items(shown_rung) if e not in cur.deferred],
                       "missing": [cur.expect_text(e, shown) for e in cur.missing(shown_rung)],
                       "covered": [cur.expect_text(e, shown) for e in cur.covered],
                       "covered_keys": list(cur.covered),
                       # what is asked now: the follow-up being answered, else the question itself; it
                       # stays until the answer is over. Under it, what is queued, never in its place.
                       "ask": ({"key": f"f:{cur.id}:{pin.at!r}", "kind": "followup", "text": pin.shown(shown, lang),
                                "rung": pin.rung} if (pin := st.ask_pin) is not None
                               else {"key": f"q:{cur.id}", "kind": "question", "text": cur.text(shown)}),
                       # the area's question just went up from a move-on: the line that leads to it, a moment
                       "transition": cov_view.get("said") if cov_view else None,
                       "ask_next": [{"key": _ask_key(cur.id, i), "kind": i["kind"],
                                     "text": (i["entry"].shown(shown, lang) if i["kind"] == "followup"
                                              else st.get(i["qid"]).text(shown)),
                                     # the next area's question (coverage.py): with the line that leads to it
                                     **({"transition": cov_view["move_on"]["transition"]}
                                        if cov_view.get("move_on") and i.get("qid") == cov_view["move_on"]["qid"] else {}),
                                     **({"rung": i["entry"].rung} if i["kind"] == "followup" else {"qid": i["qid"]})}
                                    for i in self.ask_queue()],
                       "follow_up": pin.shown(shown, lang) if (pin := st.ask_pin) is not None else "",
                       "vague": cur.vague,
                       "use_case": ({"id": uc.id, "title": uc.title(shown), "missing": cur.missing_slots()}
                                    if (uc := st.get_use_case(cur.use_case)) else None),
                       "thread": thread(cur),
                       # the indirect people question tied to this workflow (pilots.prompt_for): its
                       # reason (the ambassador angle) is for the interviewer, shown only in the console
                       "ambassador": pilots.prompt_for(st, lang=shown),
                       # the executive lens (lens.py): a talking point to say about this workflow's
                       # manual work, and the question for its missing baseline or success metric
                       "framing": lens.framing_for(st, lang=shown),
                       "metric_ask": lens.metric_ask_for(st, lang=shown),
                       # what the interviewee could not answer on this workflow, and who would know
                       "to_check": personas.for_question(st),
                       # who in the meeting to address it to (attendees.py), with two attendees or more
                       "ask_to": attendees_lib.ask_to_for(st),
                       "deferred": {cur.expect_text(e, shown): who for e, who in cur.deferred.items()},
                       "since": st.focus_since}
        upcoming = [qv(q) for q in (st.get(i) for i in self._upcoming_ids()) if q is not None]
        streams = [
            {"stream": k, "locale": v.get("locale", k), "alive": bool(v.get("alive")),
             "problem": None if self.capture_stopped else v.get("problem"),
             "unavailable": bool(v.get("unavailable")),  # skipped at start: the others listen (banner)
             "stopped": self.capture_stopped or bool(v.get("stopped"))}
            for k, v in self._stream_status.items()
        ]
        newest = max((s.received_at for s in self.transcript.segments), default=None)
        ov = st.overview()
        return {
            "version": self._version,
            "client": st.client,
            "language": lang,
            "ui_language": langs.ui,  # = the display language
            "languages": {
                "ui": langs.ui,
                "interview": lang,
                "speech": list(langs.speech),
                "streams": langs.stream_ids(),
                "ui_options": i18n.available(),
                "interview_options": self._interview_options(),
                "speech_options": sorted(set(i18n.COMMON_SPEECH) | set(langs.speech)),
                "speech_editable": st.mode == "consent",
                "translation": st.translation,
                "overlay": st.i18n_overlay.get(lang, {}),
            },
            "mode": st.mode,
            "consent": {"fr": st.consent_text("fr"), "en": st.consent_text("en"),
                        "primary": st.consent_text(shown)},
            "capture_progress": st.capture_progress,
            "started_at": st.started_at,
            "target_minutes": st.target_minutes,
            "current": current,
            "next": upcoming,
            "phases": self.phases_view(shown),
            "recipes": self.recipes_view(shown),  # console only: "Possible recipes"
            "ambassadors": pilots.candidates(st),  # console only: "Potential ambassadors"
            "probes": pilots.probes_view(st, shown),  # console only: the people questions and their reasons
            "read": pilots.read_view(st),  # console only: the interviewee's tone and how to ask
            "framings": lens.framings_view(st, shown),  # console only: the talking points and why now
            "to_check": personas.view(st, shown),  # console: what to check with whom
            # who is in the meeting (consent screen, header chip, console): role, persona, prep note
            "attendees": attendees_lib.view(st, st.tr(shown)),
            # console only: the views heard, by statement, not by person (viewpoints.py)
            "viewpoints": viewpoints_lib.view(st, shown),
            "persona": st.persona,
            # the interview thread above the current question: what was asked or answered, in order
            "history": [{**qv(q), **summary(q), "thread": thread(q, 20), "thread_at": q.thread_at,
                         "skipped": q.status == "removed"} for q in st.history()],
            "status": {
                "coverage": ov["coverage"],
                "answered": ov["answered"],
                "open": ov["open"],
                "must_open": [st.get(i).text(shown) for i in ov["must_open"]][:4],
                "must_open_count": len(ov["must_open"]),
                "missing_details": ov["missing_details"],
                "seconds_left": max(0, int(timebox.left(st))),
            },
            # the time budget (timebox.py): elapsed / total for the screen; the slot for the details
            "time": timebox.view(st),
            # coverage first (coverage.py): the areas (quiet strip) and the pending move-on with its transition
            "coverage": cov_view,
            "themes": [
                {
                    "id": t.id,
                    "title": t.title(shown),
                    "coverage": st.coverage.get(t.id, 0.0),
                    "note": st.coverage_notes.get(t.id, ""),
                }
                for t in st.themes
            ],
            "answered": ov["answered"],
            "open": ov["open"],
            "enough_data": st.enough_data,
            "enough_data_reason": st.enough_data_reason,
            "sync_count": st.sync_count,
            # versioning: transcript version (last line seq) and the version each analysis read
            "analysis": {"transcript": self.transcript.last_seq, "read": st.analysis_read,
                         "fast": st.fast_read, "sync": st.sync_read},
            "last_sync_at": st.last_sync_at,
            "last_sync_error": st.last_sync_error,
            "capture_problems": st.capture_problems,
            "listening": {"streams": streams, "line_seq": self.transcript.last_seq, "last_line_at": newest,
                          "stopped": self.capture_stopped,
                          # which speech backend listens (speech.py), and why it switched to the fallback
                          "backend": getattr(self.source, "name", None) if self.source is not None else None,
                          "fallback": getattr(self.source, "switched_reason", "") if self.source is not None else ""},
            "engine": self.engine.name,
            "tail": self.strip_lines(),
            "noise_ignored": len(self.noise_moments),
            "plan_ready": bool(st.plan_files),
            "session_dir": str(self.session_dir),
            "plan_files": dict(st.plan_files),
            "wispr_merged": st.wispr_merged,
            "interval": self.settings.sync_interval,
            "sync_running": self.sync_running or st.sync_in_progress,
            "analysing": self.fast_running,
            "last_sync_seconds": st.last_sync_seconds,
            "skipped_ticks": st.skipped_ticks,
            "fast": {
                "enabled": self.fast_available,
                "running": self.fast_running,
                "count": st.fast_count,
                "seconds": st.last_fast_seconds,
                "latency": st.last_fast_latency,
                "error": st.last_fast_error,
                "model": getattr(self.engine, "fast_model", None),
            },
        }


    def _interview_options(self) -> list[str]:
        """Languages offered for the interview: the questionnaire's variants, the locale files,
        the spoken languages and common ones; any other language code is accepted too."""
        st = self.state
        have = {k for q in st.questions for k in q.texts} | set(i18n.available())
        have |= {i18n.language_of(x) for x in st.speech_locales} | {i18n.language_of(x) for x in i18n.COMMON_SPEECH}
        return sorted(have | {st.language})


def _safe_call(fn, *args) -> None:
    try:
        fn(*args)
    except Exception as exc:  # warm-up is best effort
        log.info("warm-up failed: %s", exc)


def purge(sessions_dir: Path, slug: str, session_id: str | None = None) -> list[Path]:
    """Delete session data (transcripts, SRTs, state, plans). Returns deleted paths."""
    base = (Path(sessions_dir) / slug).resolve()
    root = Path(sessions_dir).resolve()
    if root not in base.parents:
        raise ValueError("refusing to delete outside the sessions directory")
    targets = [base / session_id] if session_id else ([base] if base.exists() else [])
    deleted = []
    for t in targets:
        t = t.resolve()
        if base != t and base not in t.parents:
            raise ValueError("refusing to delete outside the client's sessions directory")
        if t.exists():
            shutil.rmtree(t)
            deleted.append(t)
    return deleted
