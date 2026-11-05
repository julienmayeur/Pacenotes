"""Desk research + pre-plan for a client, through the Claude Code CLI (subscription, no API key).

One `claude -p` run (Opus, web search and web fetch allowed, nothing else) researches the
company and returns, as structured output: the research write-up with its sources, a pre-plan
(how they work today, what they already do, use cases ranked by value and feasibility, quick
wins vs bigger bets) and a few seed questions for the interview, each with its Business >
Workflow > Implementation ladder. Progress (searches made, pages read) streams to the admin
panel while it runs. Results are written to the client folder; the previous version is moved to
history/<timestamp>/ first.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from . import fsutil, i18n
from .clients import ClientInfo, client_dir, linked_summary, load_info
from .llm import DEFAULT_MODEL, _kill_group, cli_env, find_claude
from .questionnaire import load_questionnaire

log = logging.getLogger(__name__)

OUTPUT_FILES = ("research.md", "preplan.md", "preplan.json")
RESEARCH_TIMEOUT = 30 * 60.0


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Source(_Strict):
    title: str
    url: str


class UseCase(_Strict):
    title: str
    description: str
    value: int  # 1 (low) .. 5 (high)
    feasibility: int  # 1 (hard) .. 5 (easy)
    kind: Literal["quick_win", "big_bet"]
    already_done: bool  # they already do (part of) it


class SeedLadder(_Strict):
    business: list[str]
    workflow: list[str]
    implementation: list[str]


class SeedQuestion(_Strict):
    id: str
    fr: str
    en: str
    priority: int  # 1 must-have, 2 important
    use_case: str
    expect: SeedLadder


class ResearchOutput(_Strict):
    summary: str  # 3-5 sentences, plain
    research_md: str
    preplan_md: str
    use_cases: list[UseCase]
    seed_questions: list[SeedQuestion]
    sources: list[Source]


RESEARCH_SYSTEM = """\
You are a senior forward-deployed engineer preparing a kickoff interview with the CEO of a \
client company. Before the meeting, research the company on the web and write a pre-plan: how \
this company could become AI-native.

Use web search and web fetch. Look for: what the company does (offer, customers, size, \
locations, brands they sell or make), how it operates today (channels, supply chain, partners, \
after-sales, tools or systems mentioned in job posts, press, the website), what it already does \
with software, data or AI, and its sector's typical pain points. Prefer primary sources (the \
company's own site, official registries, press releases, job offers), then trade press. Never \
invent facts, numbers, names or tools: when something is not found, say so, and mark \
inferences as hypotheses.

Write in {lang_name}.

Return:
- summary: 3 to 5 plain sentences: who they are and the most promising directions.
- research_md: Markdown. Sections: L'entreprise; Comment elle fonctionne aujourd'hui; Ce qu'elle \
fait déjà (numérique, données, IA); Le secteur et ses irritants; Ce qu'on n'a pas trouvé. Cite \
sources inline as [n] and end with a "Sources" list ([n] title — URL).
- preplan_md: Markdown pre-plan. Sections: Synthèse; Ce qu'ils font déjà; Cas d'usage (a table: \
cas d'usage | valeur 1-5 | faisabilité 1-5 | type quick win / gros pari | pourquoi); Quick wins \
(30-60 jours); Gros paris (3-12 mois); Questions à creuser en entretien; Hypothèses à vérifier. \
Ranked by value x feasibility. Concrete, specific to this company, no generic AI hype.
- use_cases: the same use cases, structured (value and feasibility from 1 to 5).
- seed_questions: 2 to 4 interview questions for the CEO about the work behind the most \
promising use cases. The CEO meets us for the first time and does not know how AI could help, so \
ask about his work, never about technology: start from what limits capacity (taking more \
customers, orders or volume, or a new market) or eats the team's time, and ask for a real case \
from last week told as a story ("Prenez la dernière commande de la semaine dernière : qui l'a \
reçue, dans quel logiciel, combien de temps…"): who does it, with which software or file, how \
often, how long, where the information comes from and where it goes. No AI vocabulary in the \
questions (never IA/AI, agent, automatiser/automate, LLM, bot, algorithme). Each has a short id \
(slug), fr and en text, priority (1 must-have, 2 important), the use case it explores, and \
`expect`: 1 to 3 concrete details per rung that a usable answer needs — business (what it costs \
today in hours, delays or lost sales, and what the freed time would allow: how many more orders \
or customers), workflow (who does what on the last real case, where the information comes from \
and goes, handoffs), implementation (software or files by name, how many per day or week).
- sources: every URL you relied on, with a title.
"""


def build_research_prompt(info: ClientInfo, questionnaire_context: str, themes: list[str]) -> str:
    people = "; ".join(f"{c.name} ({c.role})" if c.role else c.name for c in info.contacts) or "(not given)"
    return (
        f"Company: {info.name}\n"
        f"Website: {info.website or '(not given: find it)'}\n"
        f"Sector: {info.sector or '(not given)'}\n"
        f"Country: {info.country or '(not given)'}\n"
        + "".join(f"{label}: {value}\n" for label, value in (
            ("Registered company", info.legal_name), ("Registration number", info.registration_number),
            ("Registered address", info.registered_address), ("Linked brands and entities", linked_summary(info)),
            ("Head office", info.hq_city), ("Headcount", info.headcount), ("About", info.description),
            ("Products and brands", info.products), ("Leadership", info.leadership),
            ("LinkedIn", info.linkedin_url), ("Working languages", info.working_languages)) if value)
        + f"People we will meet: {people}\n\n"
        f"Notes from the FDE (what we want to talk about: context, goals, topics, constraints):\n"
        f"{info.notes.strip() or '(none)'}\n\n"
        f"Background already in the interview questionnaire:\n{questionnaire_context.strip() or '(none)'}\n\n"
        f"Themes the interview already covers: {', '.join(themes) or '(none)'}\n\n"
        "Research the company now, then return the pre-plan."
    )


# --- the job ---------------------------------------------------------------------------------------


@dataclass
class ResearchJob:
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


class ResearchManager:
    """At most one research run per client; runs in a background thread."""

    def __init__(self, clients_dir: Path, claude_bin: str | None = None, model: str = DEFAULT_MODEL,
                 effort: str = "high", timeout: float = RESEARCH_TIMEOUT):
        self.clients_dir = Path(clients_dir)
        self.claude_bin = claude_bin
        self.model = model
        self.effort = effort
        self.timeout = timeout
        self.jobs: dict[str, ResearchJob] = {}
        self._lock = threading.Lock()

    def status(self, slug: str) -> dict | None:
        job = self.jobs.get(slug)
        return job.view() if job else None

    def start(self, slug: str) -> ResearchJob:
        client_dir(self.clients_dir, slug)  # validates the slug
        with self._lock:
            job = self.jobs.get(slug)
            if job is not None and job.status == "running":
                return job
            job = ResearchJob(slug=slug)
            self.jobs[slug] = job
        threading.Thread(target=self._run, args=(job,), daemon=True, name=f"research-{slug}").start()
        return job

    def stop(self, slug: str) -> bool:
        job = self.jobs.get(slug)
        if job is None or job.status != "running":
            return False
        job._stop.set()
        if job._proc is not None:
            _kill_group(job._proc)
        return True

    def wait(self, slug: str, timeout: float = 60.0) -> ResearchJob | None:
        """Tests and the CLI: block until the run ends."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self.jobs.get(slug)
            if job is None or job.status != "running":
                return job
            time.sleep(0.05)
        return self.jobs.get(slug)

    # --- the run -------------------------------------------------------------------------------
    def _run(self, job: ResearchJob) -> None:
        try:
            output = self._research(job)
            if job._stop.is_set():
                job.status = "stopped"
                job.say("info", "stopped")
                return
            job.archived = write_outputs(client_dir(self.clients_dir, job.slug), output, self.model)
            job.status = "done"
            job.say("done", "ready", cases=len(output.use_cases), questions=len(output.seed_questions),
                    sources=len(output.sources))
        except Exception as exc:  # the previous files stay untouched
            log.exception("research failed for %s", job.slug)
            job.status = "stopped" if job._stop.is_set() else "error"
            job.error = None if job._stop.is_set() else str(exc)
            if job.error:
                job.step("error", job.error)
            else:
                job.say("info", "stopped")
        finally:
            job.finished_at = time.time()
            job._proc = None

    def _research(self, job: ResearchJob) -> ResearchOutput:
        cdir = client_dir(self.clients_dir, job.slug)
        info = load_info(self.clients_dir, job.slug)
        q = load_questionnaire(cdir / "questionnaire.yaml")
        job.lang = info.language
        lang_name = i18n.english_name(info.language)
        cmd = [
            find_claude(self.claude_bin), "-p",
            "--output-format", "stream-json", "--verbose",
            "--include-partial-messages",  # so "writing the pre-plan" shows as soon as it starts
            "--model", self.model, "--effort", self.effort,
            "--system-prompt", RESEARCH_SYSTEM.format(lang_name=lang_name),
            "--tools", "WebSearch,WebFetch",  # web research only: no shell, no files
            "--allowedTools", "WebSearch,WebFetch",
            "--strict-mcp-config", "--no-session-persistence",
            "--json-schema", json.dumps(ResearchOutput.model_json_schema(), separators=(",", ":")),
        ]
        prompt = build_research_prompt(info, q.context, [t.title.get(info.language) for t in q.themes])
        job.say("info", "start", model=self.model)
        return ResearchOutput.model_validate(run_claude_stream(job, cmd, prompt, self.timeout, "pacenote-research-"))


def run_claude_stream(job: "ResearchJob", cmd: list[str], prompt: str, timeout: float, prefix: str,
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
        _kill_group(proc)
    timer = threading.Timer(timeout, lambda: _kill_group(proc))
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
                    _progress(job, event, writing)
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
        from .contract import _loads_lenient

        data = _loads_lenient(str(result.get("result") or ""))
    if not isinstance(data, dict):
        raise RuntimeError("Claude CLI returned no structured output")
    return data


def _write_step(job: ResearchJob, writing: str = "writing") -> None:
    if not job.steps or job.steps[-1]["kind"] != "write":
        job.say("write", writing)


def _progress(job: ResearchJob, event: dict, writing: str = "writing") -> None:
    """Turn stream-json events into readable progress steps (catalog keys admin.step.*); `writing`
    is the key of the step shown when the final (structured) answer starts."""
    if event.get("type") == "stream_event":  # partial messages: only the start of the final answer
        ev = event.get("event") or {}
        block = ev.get("content_block") or {}
        if (ev.get("type") == "content_block_start" and block.get("type") == "tool_use"
                and block.get("name") == "StructuredOutput"):
            _write_step(job, writing)
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
                    _write_step(job, writing)
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


# --- files -------------------------------------------------------------------------------------------


def write_outputs(cdir: Path, output: ResearchOutput, model: str) -> str | None:
    """Write research.md, preplan.md, preplan.json; move the previous version to history/<ts>/.
    Returns the history folder name used (or None when there was nothing to keep)."""
    cdir = Path(cdir)
    archived = archive_current(cdir)
    stamp = datetime.now().isoformat(timespec="seconds")
    fsutil.atomic_write_text(cdir / "research.md", output.research_md.strip() + "\n")
    fsutil.atomic_write_text(cdir / "preplan.md", output.preplan_md.strip() + "\n")
    meta = {"generated_at": stamp, "model": model, "summary": output.summary,
            "use_cases": [u.model_dump() for u in output.use_cases],
            "seed_questions": [s.model_dump() for s in output.seed_questions],
            "sources": [s.model_dump() for s in output.sources]}
    fsutil.atomic_write_text(cdir / "preplan.json", json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    return archived


def archive_current(cdir: Path) -> str | None:
    present = [f for f in OUTPUT_FILES if (cdir / f).exists()]
    if not present:
        return None
    name = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = cdir / "history" / name
    n = 2
    while dest.exists():
        dest = cdir / "history" / f"{name}-{n}"
        n += 1
    dest.mkdir(parents=True)
    for f in present:
        shutil.move(str(cdir / f), str(dest / f))
    return dest.name


def list_history(cdir: Path) -> list[dict]:
    h = Path(cdir) / "history"
    if not h.is_dir():
        return []
    out = []
    for d in sorted((p for p in h.iterdir() if p.is_dir()), reverse=True):
        out.append({"version": d.name, "files": sorted(f.name for f in d.iterdir() if f.name in OUTPUT_FILES)})
    return out


def restore_version(cdir: Path, version: str) -> str | None:
    """Make a history version current again (the current one is archived first)."""
    src = Path(cdir) / "history" / version
    if not src.is_dir() or "/" in version or ".." in version:
        raise FileNotFoundError(version)
    archived = archive_current(Path(cdir))
    for f in OUTPUT_FILES:
        if (src / f).exists():
            shutil.copy2(src / f, Path(cdir) / f)
    return archived
