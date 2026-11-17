"""The Claude web agent: one `claude -p` run with web search and fetch only, streamed as progress.

Desk research, the client-sheet auto-fill, the interviewee profile cards and the knowledge-brief refresh all
run through `run_claude_stream`; each run is an `AgentJob` whose steps (searches made, pages read, writing)
the admin panel shows while it runs. Infrastructure only: no domain rules here.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Literal

from . import i18n
from .contract import loads_lenient
from .llm import cli_env, kill_group


@dataclass
class AgentJob:
    slug: str
    status: Literal["running", "done", "error", "stopped"] = "running"
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    steps: list[dict] = field(default_factory=list)
    error: str | None = None
    archived: str | None = None  # history/<ts> the previous version went to
    _proc: subprocess.Popen | None = None
    _stop: threading.Event = field(default_factory=threading.Event)

    lang: str = "fr"  # the client's language: language of `text` in the steps (logs)

    def step(self, kind: str, text: str, key: str | None = None, params: dict | None = None) -> None:
        """A progress step. `key`/`params` (catalog `admin.step.<key>`) let each screen show it in
        its own language; `text` is the same message rendered in the client's language."""
        entry = {"t": round(time.time() - self.started_at, 1), "kind": kind, "text": text[:300]}
        if key:
            entry |= {"key": f"admin.step.{key}", "params": params or {}}
        self.steps.append(entry)

    def say(self, kind: str, key: str, **params) -> None:
        self.step(kind, str(i18n.t(f"admin.step.{key}", self.lang, params)), key, params)

    def view(self) -> dict:
        end = self.finished_at or time.time()
        return {"slug": self.slug, "status": self.status, "started_at": self.started_at,
                "finished_at": self.finished_at, "seconds": round(end - self.started_at, 1),
                "steps": self.steps[-200:], "error": self.error, "archived": self.archived,
                "searches": sum(1 for s in self.steps if s["kind"] == "search"),
                "pages": sum(1 for s in self.steps if s["kind"] == "read")}


def run_claude_stream(job: AgentJob, cmd: list[str], prompt: str, timeout: float, prefix: str,
                      writing: str = "writing") -> dict:
    """Run one `claude -p --output-format stream-json` call: the prompt goes on stdin, progress
    events become job steps (`writing`: the catalog key shown when the final answer starts), and the
    structured output comes back as a dict. Stoppable through job._stop / job._proc; an empty
    working dir (no project CLAUDE.md), no API key in the child's environment (cli_env)."""
    workdir = tempfile.mkdtemp(prefix=prefix)
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            cwd=workdir, env=cli_env(), text=True, encoding="utf-8", bufsize=1,
                            start_new_session=True)
    job._proc = proc
    stopped_early = job._stop.is_set()  # stopped while starting: don't feed a dying process
    if stopped_early:
        kill_group(proc)
    timer = threading.Timer(timeout, lambda: kill_group(proc))
    timer.start()
    stderr: list[str] = []  # drained alongside stdout: a chatty CLI must not block on a full pipe
    drain = threading.Thread(target=lambda: stderr.append(proc.stderr.read() or ""), daemon=True)
    drain.start()
    try:
        try:
            if not stopped_early:
                proc.stdin.write(prompt)
            proc.stdin.close()
        except BrokenPipeError:
            pass
        result = None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                if event.get("type") == "result":
                    result = event
                else:
                    progress(job, event, writing)
        proc.wait(timeout=30)
        drain.join(timeout=30)
        err = "".join(stderr).strip()
    finally:
        timer.cancel()
        shutil.rmtree(workdir, ignore_errors=True)
    if job._stop.is_set():
        raise RuntimeError("stopped")
    if result is None:
        raise RuntimeError(f"Claude CLI ended without a result (exit {proc.returncode}): {err[-300:] or 'no output'}")
    if result.get("is_error") or result.get("subtype", "success") != "success":
        raise RuntimeError(f"Claude CLI error: {str(result.get('result') or result.get('subtype'))[:300]}")
    data = result.get("structured_output")
    if not isinstance(data, dict):
        data = loads_lenient(str(result.get("result") or ""))
    if not isinstance(data, dict):
        raise RuntimeError("Claude CLI returned no structured output")
    return data


def write_step(job: AgentJob, writing: str = "writing") -> None:
    if not job.steps or job.steps[-1]["kind"] != "write":
        job.say("write", writing)


def progress(job: AgentJob, event: dict, writing: str = "writing") -> None:
    """Turn stream-json events into readable progress steps (catalog keys admin.step.*); `writing`
    is the key of the step shown when the final (structured) answer starts."""
    if event.get("type") == "stream_event":  # partial messages: only the start of the final answer
        ev = event.get("event") or {}
        block = ev.get("content_block") or {}
        if (ev.get("type") == "content_block_start" and block.get("type") == "tool_use"
                and block.get("name") == "StructuredOutput"):
            write_step(job, writing)
    elif event.get("type") == "assistant":
        for block in (event.get("message") or {}).get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                name, inp = block.get("name"), block.get("input") or {}
                if name == "WebSearch":
                    job.say("search", "search", query=inp.get("query", ""))
                elif name == "WebFetch":
                    job.say("read", "read", url=inp.get("url", ""))
                elif name == "StructuredOutput":
                    write_step(job, writing)
            elif block.get("type") == "text" and block.get("text", "").strip():
                job.step("note", block["text"].strip().splitlines()[0])
    elif event.get("type") == "user":
        content = (event.get("message") or {}).get("content")
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                detail = str(block.get("content"))
                if "required schema" in detail:  # the CLI sends a malformed answer back to be fixed
                    job.say("warn", "reformat")
                else:
                    job.say("warn", "unreachable", detail=detail[:120])
