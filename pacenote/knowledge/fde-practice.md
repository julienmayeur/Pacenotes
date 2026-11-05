---
as_of: 2026-11-04
title: How value is proven (FDE practice)
sources:
  - {title: "OpenAI: What FDE means at OpenAI (June 2026)", url: "https://openai.com/"}
  - {title: "CNBC: Anthropic and AWS program to train 10,000 forward-deployed engineers (2026-10-15)", url: "https://www.cnbc.com/"}
  - {title: "Mistral AI: Forge, custom models on the customer's own data (March 2026)", url: "https://mistral.ai/"}
  - {title: "Palantir AIP bootcamps", url: "https://www.palantir.com/"}
  - {title: "MIT NANDA: The GenAI Divide, State of AI in Business 2025 (August 2025)", url: "https://nanda.media.mit.edu/"}
---

# How value is proven (FDE practice)

Dated brief given to the plan writer as "how value is proven". Figures marked **vendor-reported**
come from the vendor itself; never present them to the client as independent results. Refresh with
`pacenote capabilities refresh --brief fde-practice`.

## What the forward-deployed teams measure

- OpenAI: FDEs are judged on production adoption, measurable workflow impact and eval-driven
  feedback. Vendor-reported: about 48% time saved on directed test generation; Thrive Holdings
  tax pilot, 7,000 returns, about +50% throughput; a telecom voice agent's accuracy from 65.6%
  (December 2024) to 92.3% (May 2026) through evals plus model snapshots.
- Anthropic and AWS (CNBC, 2026-10-15): a $100M program to train 10,000 FDEs by the end of 2027.
  Early deployments show productivity gains, not yet clear financial returns: tasks up to 6x
  faster, $5-10M of capacity freed (**vendor-reported**).
- Mistral: FDEs embedded with customers (often 50% of their time), custom models on the client's
  own data (Forge, March 2026). Typical pattern: the system gathers scattered data and runs the
  checks; humans keep the final decision.
- Palantir AIP bootcamps: about 5 days from client data to a working demo on 1-2 high-value
  problems, then a small first contract that expands once value is proven (the 5-day median and
  about 70% conversion come from one weak source).

## Independent FDE practice

- First 30 days deliver: a workflow map, a target KPI with its baseline, a data inventory, an
  eval baseline and a working thin slice.
- Run the first workflow on about 50 real inputs from the client's own data.
- Success metrics state a baseline, a pilot target and what is NOT claimed.
- Roll out in shadow mode, one desk first, with stop conditions; written kill criteria at weeks
  4 and 8.
- Stay embedded about 90-120 days (3-6 months for SMB studios). Key metric: days to the first
  verified win on real cases.

## Where the return is

- MIT NANDA (August 2025): about 95% of GenAI pilots show no measurable P&L impact; the gap is
  integration, not the model. The best ROI is in the back office (cutting outsourcing and agency
  spend), not sales and marketing. Buying from specialised vendors or partnering succeeds about
  67% of the time, roughly twice as often as internal builds.

## What the plan does with it

- Each opportunity: a baseline metric with a number from the interview (else to confirm), a pilot
  target and what will not be claimed, a first verified win on about N real cases in a given week,
  a kill criterion, buy / configure / build (prefer buy or configure for an SME), an adoption
  measure (who uses it every week).
- Rank back-office, high-volume, measurable work first.
- Timeline: week 1 thin slice, week 4 checkpoint, week 8 go / kill.
