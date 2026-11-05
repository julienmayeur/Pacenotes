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
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import fsutil, i18n, translate
from . import phases as P
from . import recipes as recipe_lib
from .capture import ReplaySource, Source
from .contract import ContractError
from .finalize import apply_recipes, assemble_plan, render_plan_md, write_plan_files
from .llm import Engine, SyncFailed, offline_plan, offline_plan_doc
from .prompts import (
    build_fast_system,
    build_fast_user,
    build_plan_system,
    build_plan_user,
    build_sync_system,
    build_sync_user,
)
from .questionnaire import ClientConfig
from .srt import format_clock
from .state import SessionState
from .transcript import Transcript

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
    advance_silence: float = 4.0  # answered focus question + this much silence -> next question
    freeze_silence: float = 2.5  # the upcoming list only re-orders after this much silence


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
        self.state.start_phase_clock(self.state.started_at)  # the first phase's time budget starts now
        self._t0_mono = time.monotonic()
        self.source = self.source_factory(self.session_dir)
        configure = getattr(self.source, "configure_locales", None)
        if callable(configure):  # the spoken languages picked for this session
            configure(list(self.state.speech_locales))
        self._hook_progress(asyncio.get_running_loop())
        try:
            # yap instances are started one after the other and confirmed alive (a few s)
            await asyncio.to_thread(self.source.start)
        except Exception as exc:  # yap missing, or an instance died right away
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
        for c in cues:
            self.transcript.add(c.stream, c.start, c.end, c.text, received_at=now)
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
            advanced = self.state.mode == "interview" and self._maybe_auto_advance()
            if n or advanced or self.state.capture_problems != before:
                await self.notify()
            await asyncio.sleep(self.settings.poll_interval)

    # --- focus (the question on screen) ---------------------------------------------
    def silence(self) -> float:
        """Seconds since the last transcript line arrived (large when nothing yet)."""
        newest = max((s.received_at for s in self.transcript.segments), default=0.0)
        return time.time() - newest if newest else 1e9

    def _maybe_auto_advance(self) -> bool:
        """The focus only moves on by itself once it is answered AND the room went quiet."""
        cur = self.state.current_question
        if cur is None or cur.status != "answered" or self.silence() < self.settings.advance_silence:
            return False
        if self.fast_running:  # let the last words land on the current question first
            return False
        self._advance(f"answered, {self.settings.advance_silence:g} s of silence")
        return True

    def _advance(self, reason: str) -> None:
        old = self.state.focus_id
        new = self.state.advance(reason)
        self.focus_changes.append({"t": time.time(), "from": old, "to": new, "reason": reason})
        self._log_sync({"kind": "focus", "from": old, "to": new, "reason": reason})
        self.persist()

    async def next_question(self, seen: str | None = None) -> str | None:
        """FDE pressed n / Enter. `seen` is the question the FDE was looking at: if the focus has
        already moved on since (e.g. an auto-advance a second earlier), the key press is ignored
        rather than skipping the new question too."""
        if seen is not None and seen != self.state.focus_id:
            return self.state.focus_id
        self._advance("FDE pressed next")
        await self.notify()
        return self.state.focus_id

    async def next_phase(self, seen: str | None = None) -> str | None:
        """FDE pressed p: close the current phase and move to the next one's first question (the
        current card joins the thread, as with n). `seen`: as for next_question."""
        if seen is not None and seen != self.state.focus_id:
            return self.state.focus_id
        old = self.state.focus_id
        new = self.state.skip_phase()
        self.focus_changes.append({"t": time.time(), "from": old, "to": new, "reason": "FDE skipped to the next phase"})
        self._log_sync({"kind": "focus", "from": old, "to": new, "reason": "FDE skipped to the next phase",
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
        """FDE console only (never the question card): "Possible recipes", the library recipes the
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
                or self.silence() >= self.settings.freeze_silence):
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
        report = self.state.apply_fast_result(result, read=upto)  # stamped with the transcript version it read
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
                                       "reason": "marked answered by the FDE"})
        self.persist()
        await self.notify()
        return ok

    async def skip(self, qid: str) -> bool:
        was_focus = qid == self.state.focus_id
        old = self.state.focus_id
        ok = self.state.skip(qid)
        if ok and was_focus:
            self.focus_changes.append({"t": time.time(), "from": old, "to": self.state.focus_id,
                                       "reason": "skipped by the FDE"})
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
        self.persist()
        await self.notify()

    # --- finalize ------------------------------------------------------------------
    @property
    def finalizing(self) -> bool:
        return self._finalizing

    async def finalize(self) -> dict[str, str]:
        """Stop capture, run a last sync on the tail, then write plan.md + plan.json. One run at a
        time: a second call while one runs returns {}. If even the offline draft cannot be written,
        the session goes back to "finalize" with the error shown, so the FDE can retry."""
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
            self.state.force_enough("FDE ended the interview")
        if self.transcript.since(self.last_synced_seq):
            prev = self.state.mode
            self.state.mode = "finalize"
            try:
                await self.sync_once(force=True)
            finally:
                self.state.mode = prev
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

    def generate_plan(self) -> dict[str, str]:
        if self.wispr_text is None and (self.session_dir / "wispr.txt").exists():
            self.wispr_text = (self.session_dir / "wispr.txt").read_text(encoding="utf-8")
        system = build_plan_system(self.state, self.client.glossary, self.client.questionnaire.context)
        user = build_plan_user(self.state, self.transcript.merged(), self.wispr_text)
        if hasattr(self.engine, "write_plan_doc"):  # the integration proposal, structured then rendered
            doc = apply_recipes(self.engine.write_plan_doc(system, user, state=self.state))  # recipe-first rules
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
    def resume(cls, client: ClientConfig, session_dir: Path, engine: Engine) -> "Session":
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
                                     for e in cur.rung_items(shown_rung)],
                       "missing": [cur.expect_text(e, shown) for e in cur.missing(shown_rung)],
                       "covered": [cur.expect_text(e, shown) for e in cur.covered],
                       "covered_keys": list(cur.covered),
                       "follow_up": cur.thread[-1].shown(shown, lang) if cur.follow_up else "",
                       "vague": cur.vague,
                       "use_case": ({"id": uc.id, "title": uc.title(shown), "missing": cur.missing_slots()}
                                    if (uc := st.get_use_case(cur.use_case)) else None),
                       "thread": thread(cur),
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
        elapsed = time.time() - st.started_at if st.started_at else 0.0
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
                "seconds_left": max(0, int(st.target_minutes * 60 - elapsed)) if st.started_at else st.target_minutes * 60,
            },
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
