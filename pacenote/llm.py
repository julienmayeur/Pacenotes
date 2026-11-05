"""Engines that turn (state, transcript delta) into a SyncResult, and write the plan.

ClaudeCliEngine - DEFAULT. Runs the Claude Code CLI (`claude -p`) already logged in on the
                  Mac, so calls are covered by the Claude subscription: no API key, no
                  separate API billing.
ClaudeEngine    - Anthropic API through the Python SDK (needs ANTHROPIC_API_KEY).
OfflineEngine   - keyword heuristics, no network. Demos/tests only.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import unicodedata
import weakref
from dataclasses import dataclass
from typing import Protocol

import anthropic

from .contract import (
    PLAN_SECTIONS,
    SLOT_KEYS,
    ContractError,
    Fact,
    FastResult,
    ModelChoice,
    Passage,
    PlanDelta,
    PlanDoc,
    PlanSlots,
    PlanUseCase,
    QuestionUpdate,
    Recipe,
    Score,
    SyncResult,
    Bottleneck,
    CurrentState,
    FirstWin,
    Measure,
    Metric,
    PlanDecision,
    PlanFlow,
    PlanPerson,
    PlanStore,
    PlanSystem,
    Success,
    empty_maps,
    ThemeCoverage,
    parse_fast_result,
    parse_plan_doc,
    parse_sync_result,
)
from .finalize import render_plan_md
from .srt import format_clock
from .state import NEXT_COUNT, SessionState
from .transcript import Segment

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_FAST_MODEL = "claude-haiku-5-5"  # fast incremental pass
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class SyncFailed(RuntimeError):
    pass


class ClaudeCliNotFound(RuntimeError):
    pass


class Engine(Protocol):
    name: str

    def sync(self, system: str, user: str, *, state: SessionState, delta: list[Segment]) -> SyncResult: ...

    def write_plan_doc(self, system: str, user: str, *, state: SessionState) -> PlanDoc: ...

    # Optional: engines may also implement
    #   fast(system, user, *, state, lines) -> FastResult   (fast incremental pass)
    #   write_plan(system, user, *, state) -> str           (legacy: a Markdown plan, used when
    #                                                        write_plan_doc is missing)


@dataclass
class ClaudeEngine:
    model: str = DEFAULT_MODEL
    sync_effort: str = "medium"
    plan_effort: str = "high"
    use_fallbacks: bool = True
    client: anthropic.Anthropic | None = None
    fast_model: str = DEFAULT_FAST_MODEL
    name: str = "claude"

    def __post_init__(self) -> None:
        if self.client is None:
            # Reads ANTHROPIC_API_KEY (or another configured credential) from the environment.
            self.client = anthropic.Anthropic(max_retries=2, timeout=180.0)

    def _extra(self) -> dict:
        # Server-side refusal fallback: if a safety classifier declines, the API re-runs
        # the request on Anthropic's recommended fallback model in the same call.
        if not self.use_fallbacks:
            return {}
        return {"betas": [FALLBACK_BETA], "fallbacks": "default"}

    def sync(self, system: str, user: str, *, state: SessionState, delta: list[Segment]) -> SyncResult:
        try:
            response = self.client.beta.messages.parse(
                model=self.model,
                max_tokens=16000,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                thinking={"type": "adaptive"},
                output_config={"effort": self.sync_effort},
                output_format=SyncResult,
                **self._extra(),
            )
        except anthropic.APIConnectionError as exc:
            raise SyncFailed(f"network error talking to the Anthropic API: {exc}") from exc
        except anthropic.RateLimitError as exc:
            raise SyncFailed("rate limited by the Anthropic API; will retry next round") from exc
        except anthropic.APIStatusError as exc:
            raise SyncFailed(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
        except ValueError as exc:
            # The SDK validates the JSON against SyncResult while parsing; a truncated or
            # empty body (max_tokens, refusal) fails here. pydantic.ValidationError is a ValueError.
            raise SyncFailed(f"sync response did not match the contract: {exc}") from exc
        if response.stop_reason == "refusal":
            raise SyncFailed("the model declined this slice (refusal); will retry with the next one")
        if response.stop_reason == "max_tokens":
            raise SyncFailed("sync response truncated (max_tokens)")
        parsed = response.parsed_output
        if parsed is None:
            text = "".join(b.text for b in response.content if b.type == "text")
            try:
                return parse_sync_result(text)
            except ContractError as exc:
                raise SyncFailed(str(exc)) from exc
        return parse_sync_result(parsed.model_dump())

    def fast(self, system: str, user: str, *, state: SessionState, lines: list[Segment]) -> FastResult:
        try:
            response = self.client.messages.parse(
                model=self.fast_model,
                max_tokens=4000,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                output_config={"effort": "low"},
                output_format=FastResult,
            )
        except anthropic.APIError as exc:
            raise SyncFailed(f"fast pass API error: {exc}") from exc
        except ValueError as exc:
            raise SyncFailed(f"fast response did not match the contract: {exc}") from exc
        if response.stop_reason in ("refusal", "max_tokens") or response.parsed_output is None:
            raise SyncFailed(f"fast pass stopped: {response.stop_reason}")
        return parse_fast_result(response.parsed_output.model_dump())

    def write_plan_doc(self, system: str, user: str, *, state: SessionState) -> PlanDoc:
        # Long output: stream so the HTTP request never times out, then take the final message.
        try:
            with self.client.beta.messages.stream(
                model=self.model,
                max_tokens=64000,
                system=system,
                messages=[{"role": "user", "content": user}],
                thinking={"type": "adaptive"},
                output_config={"effort": self.plan_effort},
                output_format=PlanDoc,
                **self._extra(),
            ) as stream:
                message = stream.get_final_message()
        except ValueError as exc:
            raise SyncFailed(f"plan response did not match the contract: {exc}") from exc
        if message.stop_reason == "refusal":
            raise SyncFailed("the model declined to write the plan (refusal)")
        if message.stop_reason == "max_tokens":
            raise SyncFailed("plan truncated (max_tokens reached)")
        parsed = getattr(message, "parsed_output", None)
        if parsed is not None:
            return parse_plan_doc(parsed.model_dump())
        text = "".join(b.text for b in message.content if b.type == "text").strip()
        try:
            return parse_plan_doc(text)
        except ContractError as exc:
            raise SyncFailed(str(exc)) from exc


# --- Claude Code CLI engine (subscription) ------------------------------------------

# Where the Claude Code CLI is usually installed when `claude` is not on the PATH
# of the process that launched the server.
CLAUDE_FALLBACK_PATHS = (
    "~/.claude/local/claude",
    "~/.local/bin/claude",
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
)

# Never forwarded to the CLI: with any of these set, `claude` would bill an API key /
# token instead of using the subscription login.
STRIPPED_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def find_claude(explicit: str | None = None) -> str:
    """Resolve the claude binary: --claude-bin / PACENOTE_CLAUDE_BIN > PATH > usual install dirs."""
    candidates: list[str] = []
    explicit = explicit or os.environ.get("PACENOTE_CLAUDE_BIN")
    if explicit:
        candidates.append(os.path.expanduser(explicit))
    else:
        on_path = shutil.which("claude")
        if on_path:
            candidates.append(on_path)
        candidates += [os.path.expanduser(p) for p in CLAUDE_FALLBACK_PATHS]
    for c in candidates:
        resolved = shutil.which(c) if os.sep not in c else c
        if resolved and os.path.isfile(resolved) and os.access(resolved, os.X_OK):
            return resolved
    raise ClaudeCliNotFound(
        "Claude Code CLI not found. Install it (https://code.claude.com), run `claude` once to log in "
        "with your Claude subscription, or pass --claude-bin / set PACENOTE_CLAUDE_BIN."
    )


def cli_env(base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    for key in STRIPPED_ENV:
        env.pop(key, None)
    # No autoupdater / telemetry / error reporting from the child: less latency, less traffic.
    env.setdefault("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
    return env


@dataclass
class CliResult:
    text: str
    structured: dict | None
    seconds: float


def result_from_payload(payload: dict, seconds: float, returncode: int = 0, err: str = "") -> CliResult:
    if payload.get("is_error") or payload.get("subtype", "success") != "success" or returncode != 0:
        msg = str(payload.get("result") or payload.get("subtype") or err or "unknown error").strip()
        raise SyncFailed(f"Claude CLI error: {msg[:400]}")
    structured = payload.get("structured_output")
    return CliResult(
        text=str(payload.get("result") or ""),
        structured=structured if isinstance(structured, dict) else None,
        seconds=seconds,
    )


class ClaudeStreamSession:
    """One long-lived `claude -p --input-format stream-json --output-format stream-json`.

    Each ask() writes one user message on stdin and waits for its `result` event. This skips
    the ~1 s CLI start-up of a fresh `claude -p` per call (measured: ~1.3-1.7 s per warm turn
    vs ~2.3-3.4 s fresh, Haiku 5.5). The conversation grows with each turn, so the process
    is recycled every `max_turns` turns; the replacement is started right away so it is warm
    by the next call.
    """

    # 10, not 30: every turn embeds a full state snapshot, so a long conversation re-reads many
    # stale ones (token growth, and the model can anchor on an old turn's state). Recycling is
    # cheap: the next process is started right away and is warm by the next call.
    def __init__(self, cmd: list[str], cwd: str, max_turns: int = 10):
        self.cmd = cmd
        self.cwd = cwd
        self.max_turns = max_turns
        self.turns = 0
        self.proc: subprocess.Popen | None = None
        self._lines: queue.Queue = queue.Queue()
        self._stderr = None
        self._lock = threading.Lock()

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        self.close()
        self._lines = queue.Queue()
        self._stderr = tempfile.TemporaryFile(mode="w+")
        env = cli_env()
        try:
            self.proc = subprocess.Popen(
                self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=self._stderr, cwd=self.cwd, env=env, text=True, encoding="utf-8", bufsize=1,
                start_new_session=True,
            )
        except OSError as exc:
            self.proc = None
            raise SyncFailed(f"could not start the Claude CLI: {exc}") from exc
        self.turns = 0
        threading.Thread(target=self._read, args=(self.proc, self._lines), daemon=True).start()

    @staticmethod
    def _read(proc: subprocess.Popen, lines: queue.Queue) -> None:
        for line in proc.stdout:  # type: ignore[union-attr]
            line = line.strip()
            if line:
                lines.put(line)
        lines.put(None)  # EOF

    def _stderr_tail(self) -> str:
        try:
            self._stderr.seek(0)
            return self._stderr.read()[-400:].strip()
        except Exception:
            return ""

    def ask(self, prompt: str, timeout: float) -> CliResult:
        with self._lock:
            if not self.alive:
                self.start()
            started = time.monotonic()
            msg = {"type": "user", "message": {"role": "user", "content": prompt}}
            try:
                self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")  # type: ignore[union-attr]
                self.proc.stdin.flush()  # type: ignore[union-attr]
            except (BrokenPipeError, OSError) as exc:
                self.close()
                raise SyncFailed(f"Claude CLI session closed: {self._stderr_tail() or exc}") from exc
            deadline = started + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.close()
                    raise SyncFailed(f"Claude CLI timed out after {timeout:.0f}s")
                try:
                    line = self._lines.get(timeout=remaining)
                except queue.Empty:
                    continue
                if line is None:
                    detail = self._stderr_tail()
                    self.close()
                    raise SyncFailed(f"Claude CLI session ended: {detail or 'no output'}")
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict) and event.get("type") == "result":
                    break
            self.turns += 1
            if self.turns >= self.max_turns:
                self.start()  # recycle now so the next call finds a warm process
            return result_from_payload(event, time.monotonic() - started)

    def close(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            proc.stdin.close()  # type: ignore[union-attr]
        except Exception:
            pass
        _kill_group(proc)
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


@dataclass
class ClaudeCliEngine:
    """`claude -p` subprocess per call: prompt on stdin, system prompt via --system-prompt,
    JSON output, all tools disabled, no MCP servers, no session saved to disk."""

    model: str = DEFAULT_MODEL
    sync_effort: str = "low"
    plan_effort: str = "high"
    claude_bin: str | None = None
    sync_timeout: float = 120.0
    plan_timeout: float = 900.0
    fast_model: str = DEFAULT_FAST_MODEL
    fast_effort: str = "low"
    fast_timeout: float = 30.0
    persistent_fast: bool = True  # keep one stream-json `claude` process warm for the fast pass
    translate_timeout: float = 180.0
    name: str = "claude-cli"

    def __post_init__(self) -> None:
        self._workdir = tempfile.mkdtemp(prefix="pacenote-claude-")  # empty cwd: no project CLAUDE.md
        weakref.finalize(self, shutil.rmtree, self._workdir, ignore_errors=True)  # an engine never cancelled
        self._active: set[subprocess.Popen] = set()
        self._fast_session: ClaudeStreamSession | None = None
        self._fast_key: str | None = None
        self._fast_failures = 0

    def cancel(self) -> None:
        """Kill any running `claude -p` (server shutdown)."""
        for proc in list(self._active):
            _kill_group(proc)
        self.close()
        shutil.rmtree(self._workdir, ignore_errors=True)  # a later call makes it again (_cwd)

    def _cwd(self) -> str:
        os.makedirs(self._workdir, exist_ok=True)
        return self._workdir

    def close(self) -> None:
        if self._fast_session is not None:
            self._fast_session.close()
            self._fast_session = None

    def command(
        self,
        system: str,
        effort: str,
        json_schema: dict | None = None,
        model: str | None = None,
        stream: bool = False,
    ) -> list[str]:
        io = (["--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]
              if stream else ["--output-format", "json"])
        cmd = [
            find_claude(self.claude_bin),
            "-p",
            *io,
            "--model", model or self.model,
            "--effort", effort,
            "--system-prompt", system,
            "--tools", "",  # text only: no Bash/Read/Edit/Web tools
            "--strict-mcp-config",  # and no MCP servers from the user's config
            "--no-session-persistence",  # don't keep the transcript in ~/.claude
        ]
        if json_schema is not None:
            cmd += ["--json-schema", json.dumps(json_schema, separators=(",", ":"))]
        return cmd

    def run(self, cmd: list[str], prompt: str, timeout: float) -> CliResult:
        started = time.monotonic()
        env = cli_env()
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self._cwd(),
                env=env,
                text=True,
                encoding="utf-8",
                start_new_session=True,
            )
        except OSError as exc:
            raise SyncFailed(f"could not start the Claude CLI: {exc}") from exc
        self._active.add(proc)
        try:
            out, err = proc.communicate(prompt, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            _kill_group(proc)
            proc.communicate()  # reap
            raise SyncFailed(f"Claude CLI timed out after {timeout:.0f}s") from exc
        finally:
            self._active.discard(proc)
        seconds = time.monotonic() - started
        try:
            payload = json.loads(out)
        except json.JSONDecodeError as exc:
            detail = (err or out or "").strip()[-400:]
            raise SyncFailed(f"Claude CLI exited {proc.returncode} without JSON output: {detail}") from exc
        if not isinstance(payload, dict):
            raise SyncFailed("unexpected Claude CLI output")
        return result_from_payload(payload, seconds, proc.returncode, err)

    def sync(self, system: str, user: str, *, state: SessionState, delta: list[Segment]) -> SyncResult:
        cmd = self.command(system, self.sync_effort, SyncResult.model_json_schema())
        res = self.run(cmd, user + "\n\nReply with the JSON object only.", self.sync_timeout)
        try:
            # Prefer the CLI's schema-validated structured output, else repair-parse the text.
            return parse_sync_result(res.structured if res.structured is not None else res.text)
        except ContractError as exc:
            raise SyncFailed(str(exc)) from exc

    def write_plan_doc(self, system: str, user: str, *, state: SessionState) -> PlanDoc:
        cmd = self.command(system, self.plan_effort, PlanDoc.model_json_schema())
        res = self.run(cmd, user + "\n\nReply with the JSON object only.", self.plan_timeout)
        try:
            return parse_plan_doc(res.structured if res.structured is not None else res.text)
        except ContractError as exc:
            raise SyncFailed(str(exc)) from exc

    # --- fast incremental pass -------------------------------------------------------
    def _fast_stream(self, system: str) -> ClaudeStreamSession:
        key = f"{self.fast_model}\0{system}"
        if self._fast_session is None or self._fast_key != key:
            self.close()
            cmd = self.command(system, self.fast_effort, FastResult.model_json_schema(), self.fast_model, stream=True)
            self._fast_session = ClaudeStreamSession(cmd, self._cwd())
            self._fast_key = key
        return self._fast_session

    def warm_fast(self, system: str) -> None:
        """Start the persistent fast-pass process ahead of the first call (CLI start-up off the clock)."""
        if self.persistent_fast:
            session = self._fast_stream(system)
            if not session.alive:
                session.start()

    def fast(self, system: str, user: str, *, state: SessionState, lines: list[Segment]) -> FastResult:
        if self.persistent_fast:
            try:
                res = self._fast_stream(system).ask(user, self.fast_timeout)
                self._fast_failures = 0
            except SyncFailed:
                self._fast_failures += 1
                if self._fast_failures >= 2:  # streaming doesn't work here: fresh process per call
                    log.warning("persistent claude session failed twice; using one-shot calls for the fast pass")
                    self.persistent_fast = False
                    self.close()
                raise
        else:
            cmd = self.command(system, self.fast_effort, FastResult.model_json_schema(), self.fast_model)
            res = self.run(cmd, user, self.fast_timeout)
        try:
            return parse_fast_result(res.structured if res.structured is not None else res.text)
        except ContractError as exc:
            raise SyncFailed(str(exc)) from exc

    def translate(self, system: str, user: str, *, schema: dict) -> dict:
        """One-shot translation (translate.py) on the fast model, through `claude -p`."""
        cmd = self.command(system, self.fast_effort, schema, self.fast_model)
        res = self.run(cmd, user + "\n\nReply with the JSON object only.", self.translate_timeout)
        if res.structured is not None:
            return res.structured
        try:
            return _loads_json(res.text)
        except ValueError as exc:
            raise SyncFailed(f"translation reply is not JSON: {exc}") from exc

    def ping(self, timeout: float = 90.0) -> CliResult:
        """Tiny round trip used by `pacenote check`: proves the CLI is installed and logged in."""
        cmd = self.command("You are a health check. Reply with exactly: ok", "low")
        return self.run(cmd, "reply ok", timeout)


def _loads_json(text: str) -> dict:
    from .contract import _loads_lenient

    data = _loads_lenient(text)
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    return data


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


# --- offline heuristic engine -----------------------------------------------------


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in s if not unicodedata.combining(c))


_SENT = re.compile(r"(?<=[.!?])\s+")


def _expect_heard(item: str, folded_text: str) -> bool:
    """Offline heuristic: an expected detail counts as heard when one of its significant words
    (5+ letters, a few generic ones excepted) appears in the text."""
    generic = {"combien", "quelle", "quels", "comment", "concretement", "precis", "precise"}
    words = [w for w in re.findall(r"[a-z]{5,}", _fold(item)) if w not in generic]
    return any(w in folded_text for w in words)


@dataclass
class OfflineEngine:
    """Keyword matching on the session-language stream. Crude, deterministic, no network."""

    answered_hits: int = 2
    name: str = "offline"

    def sync(self, system: str, user: str, *, state: SessionState, delta: list[Segment]) -> SyncResult:
        primary = [s for s in delta if s.stream.split("-")[0] == state.language]
        text = " ".join(s.text for s in primary)
        folded = _fold(text)
        sentences = _SENT.split(text)
        updates: list[QuestionUpdate] = []
        for q in state.queue():
            kws = [k for k in q.keywords if k.strip()]
            hits = [k for k in kws if _fold(k) in folded]
            if not hits:
                continue
            already = 1 if q.status == "partial" else 0
            status = "answered" if len(hits) + already >= self.answered_hits else "partial"
            evidence = next((s for s in sentences if any(_fold(h) in _fold(s) for h in hits)), "")
            answer = " ".join(x for x in (q.answer, evidence.strip()) if x)[:400]
            covered = [e for e in q.all_expect() if _expect_heard(e, folded)]
            have = set(covered) | set(q.covered)
            rung = next((r for r in ("business", "workflow", "implementation")
                         if any(e not in have for e in q.rung_items(r))), "done" if q.all_expect() else q.rung)
            missing = [e for e in q.rung_items(rung if rung != "done" else "implementation") if e not in have]
            follow_up = ""
            if missing and status != "answered":
                follow_up = state.tr()("offline.followUp", item=q.expect_text(missing[0], state.language))
            updates.append(QuestionUpdate(question_id=q.id, status=status, answer=answer,
                                          answer_piece=evidence.strip()[:300], evidence=evidence[:200],
                                          covered=covered, rung=rung, follow_up=follow_up, vague=not covered))

        # Simulate the result to compute coverage / enough_data consistently.
        trial = state.model_copy(deep=True)
        trial.apply_sync_result(
            SyncResult(
                question_updates=updates,
                slots=[],
                maps=empty_maps(),
                recipe_matches=[],
                queue_edits=[],
                next_questions=[],
                coverage=[],
                plan_delta=PlanDelta(**{s: [] for s in PLAN_SECTIONS}),
                clean_passages=[],
                enough_data=False,
                enough_data_reason="",
            )
        )
        coverage = trial.computed_coverage()
        must_open = [q for q in trial.queue() if q.priority == 1]
        enough = not must_open and all(v >= 0.6 for v in coverage.values())
        return SyncResult(
            question_updates=updates,
            slots=[],
            maps=empty_maps(),
            recipe_matches=[],
            queue_edits=[],
            next_questions=[q.id for q in trial.queue()[:NEXT_COUNT]],
            coverage=[ThemeCoverage(theme=k, score=v, note="") for k, v in coverage.items()],
            plan_delta=PlanDelta(**{s: [] for s in PLAN_SECTIONS}),
            clean_passages=[
                Passage(start=format_clock(s.start), speaker="unknown", lang=state.language, text=s.text)
                for s in primary
            ],
            enough_data=enough,
            enough_data_reason="offline heuristic: all must-have questions answered" if enough else "",
        )

    def fast(self, system: str, user: str, *, state: SessionState, lines: list[Segment]) -> FastResult:
        r = self.sync(system, user, state=state, delta=lines)
        return FastResult(question_updates=r.question_updates, slots=r.slots, maps=r.maps, coverage=r.coverage, noise=[])

    def write_plan_doc(self, system: str, user: str, *, state: SessionState) -> PlanDoc:
        return offline_plan_doc(state)


def offline_plan_doc(state: SessionState) -> PlanDoc:
    """The integration proposal from what the interview captured live (no API): one use case per
    use case that got a slot or an answer, its filled slots as facts (with their live quote),
    everything else "to confirm". Proposal fields (architecture, model, scores) stay open."""
    lang = state.language
    tr = state.tr()
    todo = tr("plan.toComplete")
    cases = []
    metrics = []
    best = {}  # the strongest live recipe match per use case (state.active_recipe_matches is sorted)
    for m in state.active_recipe_matches():
        if m.use_case and m.use_case not in best:
            best[m.use_case] = Recipe(id=m.recipe_id, fit=m.fit, gaps=list(m.missing))
    for u in state.use_cases:
        answered = any(q.use_case == u.id and q.answer for q in state.questions)
        if not u.slots and not answered:
            continue
        facts = {}
        for k in SLOT_KEYS:
            v = u.slots.get(k)
            facts[k] = (Fact(status="stated", value=v.value, evidence=v.evidence, question="", ask="")
                        if u.filled(k) else Fact(status="to_confirm", value="", evidence="", question="", ask=""))
        metric = ""
        if u.filled("baseline") or u.filled("target"):  # the numbers said live become its metric
            metric = f"{u.id}-metric"
            metrics.append(Metric(id=metric, use_case=u.id, name=u.title(lang), unit="", how_measured="", cadence="",
                                  owner="", baseline=_measure(u.slots.get("baseline")),
                                  target=_measure(u.slots.get("target"))))
        cases.append(PlanUseCase(id=u.id, title=u.title(lang), pattern="other", human_decision="",
                                 success=Success(statement="", check=""), metric=metric, not_claimed="",
                                 first_win=FirstWin(description="", cases=0, week=0), kill_criterion="",
                                 sourcing="to_confirm", sourcing_reason="", adoption="", back_office=False,
                                 recipe=best.get(u.id),
                                 architecture=todo, steps=[], trigger_kind="to_confirm",
                                 actions=[], slots=PlanSlots(**facts), model=ModelChoice(provider="to_confirm", reason=""),
                                 impact=Score(score=0, reason=""), feasibility=Score(score=0, reason="")))
    context = " ".join(state.plan_notes.get("context", [])[:4])
    current, bottlenecks = offline_current_state(state)
    return PlanDoc(context=context, current_state=current, bottlenecks=bottlenecks, metrics=metrics, use_cases=cases,
                   timeline=[], open_items=[], recipe_candidates=[])


def _measure(v) -> Measure:
    if v is None or not v.value.strip():
        return Measure(value="", date="", status="to_confirm", evidence="", question="", ask="")
    return Measure(value=v.value, date="", status="stated" if v.evidence else "partial", evidence=v.evidence,
                   question="", ask="")


def offline_current_state(state: SessionState) -> tuple[CurrentState, list[Bottleneck]]:
    """The maps captured live, as the plan's current state: a record with its quote is stated,
    without one partial (to confirm)."""
    def st(r) -> str:
        return "stated" if r.evidence else "partial"

    def g(r, k: str, default: str = "") -> str:
        return r.fields.get(k, default)

    yn = ("yes", "no")
    people = [PlanPerson(name=g(r, "person"), role=g(r, "role"), department=g(r, "department"),
                         reports_to=g(r, "reports_to"), time_split=g(r, "time_split"), status=st(r), evidence=r.evidence)
              for r in state.map_records("org")]
    flows = [PlanFlow(sender=g(r, "sender"), receiver=g(r, "receiver"), what=g(r, "what"), channel=g(r, "channel"),
                      cadence=g(r, "cadence"), wait=g(r, "wait"), who_waits=g(r, "who_waits"),
                      confidential=g(r, "confidential") if g(r, "confidential") in yn else "unknown",
                      status=st(r), evidence=r.evidence) for r in state.map_records("flows")]
    systems = [PlanSystem(category=g(r, "category", "core_systems"), tool=g(r, "tool"), used_by=g(r, "used_by"),
                          used_for=g(r, "used_for"),
                          source_of_truth=g(r, "source_of_truth") if g(r, "source_of_truth") in yn else "unknown",
                          access_owner=g(r, "access_owner"), status=st(r), evidence=r.evidence)
               for r in state.map_records("catalog")]
    stores = [PlanStore(what=g(r, "what"), where=g(r, "where"), owner=g(r, "owner"), how_found=g(r, "how_found"),
                        reuse_pain=g(r, "reuse_pain"), status=st(r), evidence=r.evidence)
              for r in state.map_records("stores")]
    decisions = [PlanDecision(decision=g(r, "decision"), owner=g(r, "owner"), frequency=g(r, "frequency"),
                              inputs=g(r, "inputs"), prep_time=g(r, "prep_time"), decide_time=g(r, "decide_time"),
                              status=st(r), evidence=r.evidence) for r in state.map_records("decisions")]
    bottlenecks = []
    for r in state.map_records("flows"):
        pattern = g(r, "ceo_pattern", "none")
        if pattern == "none" and not g(r, "wait"):
            continue
        bottlenecks.append(Bottleneck(kind=pattern if pattern != "none" else "handoff",
                                      who_waits=g(r, "who_waits") or g(r, "receiver"), waits_on=g(r, "sender"),
                                      what=g(r, "what"), how_long=g(r, "wait"), status=st(r), evidence=r.evidence,
                                      question="", ask=""))
    return CurrentState(people=people, flows=flows, systems=systems, stores=stores, decisions=decisions), bottlenecks


def offline_plan(state: SessionState) -> str:
    """The offline integration proposal as Markdown (fallback when the plan call fails)."""
    return render_plan_md(offline_plan_doc(state), state)


ENGINES = ("cli", "api", "offline")


def make_engine(
    engine: str = "cli",
    model: str | None = None,
    sync_effort: str | None = None,
    plan_effort: str | None = None,
    use_fallbacks: bool = True,
    claude_bin: str | None = None,
    fast_model: str | None = None,
) -> Engine:
    model = model or os.environ.get("PACENOTE_MODEL", DEFAULT_MODEL)
    sync_effort = sync_effort or os.environ.get("PACENOTE_SYNC_EFFORT", "low")
    plan_effort = plan_effort or os.environ.get("PACENOTE_PLAN_EFFORT", "high")
    fast_model = fast_model or os.environ.get("PACENOTE_FAST_MODEL", DEFAULT_FAST_MODEL)
    if engine == "offline":
        return OfflineEngine()
    if engine == "api":
        return ClaudeEngine(model=model, sync_effort=sync_effort, plan_effort=plan_effort,
                            use_fallbacks=use_fallbacks, fast_model=fast_model)
    if engine == "cli":
        return ClaudeCliEngine(model=model, sync_effort=sync_effort, plan_effort=plan_effort,
                               claude_bin=claude_bin, fast_model=fast_model)
    raise ValueError(f"unknown engine {engine!r} (choose from {', '.join(ENGINES)})")
