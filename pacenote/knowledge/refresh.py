"""`pacenote capabilities refresh`: rewrite a knowledge brief from one web-research `claude -p` run (web search
and fetch only, JSON schema, timeout), in the same format, into the local knowledge folder."""

from __future__ import annotations

import json
from datetime import date

from pydantic import BaseModel, ConfigDict

from pacenote.copilot.engines import DEFAULT_MODEL, find_claude
from pacenote.copilot.web_agent import AgentJob, run_claude_stream
from pacenote.knowledge.briefs import BRIEFS, Brief, knowledge_dir, load_brief, render_brief
from pacenote.shared import fsutil

REFRESH_TIMEOUT = 20 * 60.0


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
    "delivery-practice": """\
You maintain a short, dated brief "How value is proven (delivery practice)" that a planning model reads to \
write a consulting engineering plan for a non-technical SME. Use web search and web fetch to \
check every item of the current brief and add what changed: how vendor delivery teams (OpenAI, Anthropic, \
Mistral, Palantir, independent practitioners) measure value (baselines, pilot targets, first verified \
win on real cases, kill criteria, shadow rollout, embed length), and independent studies of AI pilot \
returns. Mark vendor figures **vendor-reported** and single-source items (unconfirmed). Keep it under \
about 700 words, English, Markdown, starting with "# How value is proven (delivery practice)", and end \
with a short section "What the plan does with it".""",
}


def refresh_brief(name: str, *, claude_bin: str | None = None, model: str | None = None, effort: str = "high",
                  timeout: float = REFRESH_TIMEOUT, on_step=None, today: date | None = None, runner=None) -> Brief:
    """Rewrite a brief from one web-research `claude -p` run, into the local knowledge folder. The previous
    file stays untouched if the run fails. `runner(job, cmd, prompt, timeout, prefix)`: tests inject a fake."""
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
    job = AgentJob(slug=f"brief-{name}", lang="en")
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
    path.parent.mkdir(parents=True, exist_ok=True)
    fsutil.atomic_write_text(path, render_brief(name, stamp, title, out.body_md, [s.model_dump() for s in out.sources]))
    return load_brief(name)
