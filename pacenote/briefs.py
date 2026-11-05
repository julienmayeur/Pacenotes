"""Dated knowledge briefs the plan writer receives instead of relying on what a model remembers.

pacenote/knowledge/
  capabilities.md   "what agents can do today": the plan never claims a capability absent from it,
                    and its per-use-case model choice leans on it; the plan footer states its date
  fde-practice.md   "how value is proven": baselines, pilot targets, first verified win, kill
                    criteria, buy before build

Each file starts with YAML front matter (`as_of:`, `sources:`). Both are tracked. A brief older
than STALE_DAYS is flagged by `pacenote check` and in the Plan view. `pacenote capabilities refresh` rewrites
them with one web-research `claude -p` run each (research.run_claude_stream: web search and fetch
only, JSON schema, timeout), keeping the same format.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

from . import fsutil

KNOWLEDGE_DIR = Path(__file__).parent / "knowledge"
BRIEFS = {"capabilities": "capabilities.md", "fde-practice": "fde-practice.md"}
STALE_DAYS = 30
REFRESH_TIMEOUT = 20 * 60.0
_FRONT = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.S)


@dataclass
class Brief:
    name: str
    path: Path
    as_of: date | None
    body: str
    sources: list[dict] = field(default_factory=list)
    title: str = ""

    def age_days(self, today: date | None = None) -> int | None:
        return None if self.as_of is None else ((today or date.today()) - self.as_of).days

    def stale(self, today: date | None = None, days: int = STALE_DAYS) -> bool:
        age = self.age_days(today)
        return age is None or age > days

    @property
    def as_of_text(self) -> str:
        return self.as_of.isoformat() if self.as_of else "unknown"


def knowledge_dir() -> Path:
    return Path(os.environ.get("PACENOTE_KNOWLEDGE_DIR") or KNOWLEDGE_DIR)


def parse_brief(name: str, text: str, path: Path) -> Brief:
    m = _FRONT.match(text)
    meta, body = ({}, text) if not m else (yaml.safe_load(m.group(1)) or {}, m.group(2))
    as_of = meta.get("as_of")
    if isinstance(as_of, str):
        try:
            as_of = date.fromisoformat(as_of.strip())
        except ValueError:
            as_of = None
    return Brief(name=name, path=path, as_of=as_of if isinstance(as_of, date) else None, body=body.strip(),
                 sources=[s for s in meta.get("sources") or [] if isinstance(s, dict)], title=str(meta.get("title") or ""))


def load_brief(name: str, directory: Path | None = None) -> Brief | None:
    path = (directory or knowledge_dir()) / BRIEFS[name]
    try:
        return parse_brief(name, path.read_text(encoding="utf-8"), path)
    except OSError:
        return None


def capabilities_as_of() -> str:
    b = load_brief("capabilities")
    return b.as_of_text if b else "unknown"


def stale_briefs(today: date | None = None) -> list[Brief]:
    out = []
    for name in BRIEFS:
        b = load_brief(name)
        if b is None or b.stale(today):
            out.append(b or Brief(name=name, path=knowledge_dir() / BRIEFS[name], as_of=None, body=""))
    return out


def render_brief(name: str, as_of: str, title: str, body: str, sources: list[dict]) -> str:
    meta = {"as_of": as_of, "title": title, "sources": [{"title": s.get("title", ""), "url": s.get("url", "")}
                                                          for s in sources]}
    return "---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False, width=120) + "---\n\n" + body.strip() + "\n"


# --- refresh (web research through the Claude Code CLI) ---------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BriefSource(_Strict):
    title: str
    url: str


class BriefOutput(_Strict):
    body_md: str  # the brief in Markdown, same sections as before, single-source items marked (unconfirmed)
    sources: list[BriefSource]


REFRESH_SYSTEM = {
    "capabilities": """\
You maintain a short, dated brief "What agents can do today" that a planning model reads instead of \
its memory, to propose AI agents to a non-technical SME (orders, stock, logistics, after-sales, \
reporting). Use web search and web fetch to check every item of the current brief and to add what \
changed since its date: computer use (agents operating software without an API), connectors (MCP and \
others) to business systems, governance (agent credentials, scoped permissions, sandboxes), what \
works in SMEs (narrow workflows, buy before build, adoption figures), organisation and information \
flow (spans of control, time spent searching), and what distinguishes the model providers (Anthropic, \
OpenAI, xAI) for extraction, reasoning, latency, cost, data residency. Prefer primary sources \
(release notes, official docs, vendor changelogs), then serious press. Give a date for every \
release. Mark an item **(unconfirmed)** when only one source supports it; mark vendor figures \
(vendor-reported). Remove what is no longer true. Keep it under about 900 words, English, Markdown, \
starting with "# What agents can do today" and a short paragraph on how the plan uses it.""",
    "fde-practice": """\
You maintain a short, dated brief "How value is proven (FDE practice)" that a planning model reads to \
write a forward-deployed engineering plan for a non-technical SME. Use web search and web fetch to \
check every item of the current brief and add what changed: how FDE teams (OpenAI, Anthropic, \
Mistral, Palantir, independent practitioners) measure value (baselines, pilot targets, first verified \
win on real cases, kill criteria, shadow rollout, embed length), and independent studies of AI pilot \
returns. Mark vendor figures **vendor-reported** and single-source items (unconfirmed). Keep it under \
about 700 words, English, Markdown, starting with "# How value is proven (FDE practice)", and end \
with a short section "What the plan does with it".""",
}


def refresh_brief(name: str, *, claude_bin: str | None = None, model: str | None = None, effort: str = "high",
                  timeout: float = REFRESH_TIMEOUT, on_step=None, today: date | None = None, runner=None) -> Brief:
    """Rewrite knowledge/<brief> from one web-research `claude -p` run. The previous file stays
    untouched if the run fails. `runner(job, cmd, prompt, timeout, prefix)`: tests inject a fake."""
    from .llm import DEFAULT_MODEL, find_claude
    from .research import ResearchJob, run_claude_stream

    current = load_brief(name)
    stamp = (today or date.today()).isoformat()
    cmd = [find_claude(claude_bin), "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
           "--model", model or DEFAULT_MODEL, "--effort", effort,
           "--system-prompt", REFRESH_SYSTEM[name],
           "--tools", "WebSearch,WebFetch", "--allowedTools", "WebSearch,WebFetch",
           "--strict-mcp-config", "--no-session-persistence",
           "--json-schema", json.dumps(BriefOutput.model_json_schema(), separators=(",", ":"))]
    prompt = (f"Today is {stamp}. Current brief (as of {current.as_of_text if current else 'never'}):\n\n"
              f"{current.body if current else '(none)'}\n\nSources it cited:\n"
              + "\n".join(f"- {s.get('title')}: {s.get('url')}" for s in (current.sources if current else []))
              + "\n\nCheck it on the web and return the updated brief with every source you relied on.")
    job = ResearchJob(slug=f"brief-{name}", lang="en")
    if on_step is not None:  # mirror progress steps (searches, pages read) as they come
        steps = job.steps

        class _Tap(list):
            def append(self, item):
                super().append(item)
                on_step(item)

        job.steps = _Tap(steps)
    out = BriefOutput.model_validate((runner or run_claude_stream)(job, cmd, prompt, timeout, "pacenote-brief-"))
    if not out.body_md.strip():
        raise RuntimeError("the refresh returned an empty brief")
    title = current.title if current and current.title else name
    path = knowledge_dir() / BRIEFS[name]
    fsutil.atomic_write_text(path, render_brief(name, stamp, title, out.body_md, [s.model_dump() for s in out.sources]))
    return load_brief(name)
