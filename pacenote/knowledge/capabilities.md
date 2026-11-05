---
as_of: 2026-11-04
title: What agents can do today
sources:
  - {title: "Google Cloud release notes: Gemini Enterprise computer-use tool GA (2026-10-30)", url: "https://cloud.google.com/release-notes"}
  - {title: "Microsoft Copilot Studio: computer use generally available (May 2026)", url: "https://learn.microsoft.com/microsoft-copilot-studio/"}
  - {title: "GitHub changelog: verified MCP servers (connectors) public preview (2026-10-28)", url: "https://github.blog/changelog/"}
  - {title: "Model Context Protocol", url: "https://modelcontextprotocol.io"}
  - {title: "Microsoft Copilot Studio MCP server", url: "https://learn.microsoft.com/microsoft-copilot-studio/"}
  - {title: "Oracle AI Data Platform Agent Hub (MCP and A2A)", url: "https://www.oracle.com/artificial-intelligence/"}
  - {title: "Nvidia OpenShell sandboxed agent runtime", url: "https://developer.nvidia.com/"}
  - {title: "Gartner: 20% of organizations will flatten their structure with AI through 2026", url: "https://www.gartner.com/"}
  - {title: "McKinsey Global Institute (2012): The social economy", url: "https://www.mckinsey.com/industries/technology-media-and-telecommunications/our-insights/the-social-economy"}
---

# What agents can do today

Dated brief given to the plan writer as "what agents can do today". The plan must not claim a
capability that is not in this brief, and its per-use-case model choice leans on it, not on what a
model remembers. Items marked **(unconfirmed)** come from a single source. Refresh with
`pacenote capabilities refresh`; the app warns when this brief is older than 30 days.

## Operating software without an API (computer use)

- Agents can drive a desktop or browser application through its screen when it has no API:
  Google Gemini Enterprise computer-use tool, generally available since 2026-10-30 (Google Cloud
  release notes); Microsoft Copilot Studio computer use, generally available since May 2026.
- Fit: legacy or portal-only systems (supplier portals, an old ERP screen) at modest volume, with a
  human checking the result. Slower and more fragile than an API or a connector.

## Reaching business systems (connectors)

- MCP (Model Context Protocol) is the standard way agents reach business systems (mail, drives,
  ERP / CRM connectors, databases).
- GitHub verified MCP servers ("connectors"): public preview since 2026-10-28.
- Microsoft Copilot Studio exposes an MCP server usable from Claude Code, Codex and GitHub Copilot.
- Oracle AI Data Platform Agent Hub supports MCP and A2A (agent-to-agent). **(unconfirmed)**
- Fit: prefer an existing connector (mailbox, shared drive, the ERP's own) over custom
  integration; where none exists, an export or computer use.

## Governance and access

- Agents can run with their own approved credentials and scoped permissions (Microsoft Copilot
  Studio, private preview). **(unconfirmed)**
- Sandboxed agent runtimes exist (Nvidia OpenShell). **(unconfirmed)**
- Fit: an agent gets only the access it is granted (a mailbox, a folder, a read-only ERP
  account); who grants it is part of every plan.

## What works in SMEs

- Narrow agents on one workflow, rolled out in phases, work; broad "transformation" claims are
  mostly vendor marketing.
- Buy or configure an existing product before building custom.
- Common first steps: inbox and calendar triage, customer support answers.
- Adoption figures conflict (for example 11 to 17% of enterprises with agents in production):
  directional only, never quoted as a fact to the client.

## Organisation and information flow

- Spans of control are widening with AI (from about 7 to 12-15 reports reported; Gartner forecasts
  that 20% of organisations flatten their structure with AI through 2026), but Meta's 50:1 applied-AI
  organisation is re-hiring managers: flattening only works when context is actually routed to
  each person. **(unconfirmed)**
- Knowledge workers spend about 19% of their time searching for information or colleagues
  (McKinsey, 2012: old, but still the reference figure).

## Model providers

- The plan picks a provider per use case (Anthropic, OpenAI, xAI) on the stated facts: extraction
  quality on emails, PDFs or scans, reasoning on messy requests, latency or cost at the stated
  volume, a data-residency or security constraint. This brief holds no benchmark: when a choice
  depends on one, the plan says the choice is to be confirmed by a test on the client's own cases.
