"""The strict JSON contract Claude must return on every sync.

The same Pydantic models are used (1) to build the JSON schema sent to the API as a
structured-output format, so the response is schema-valid by construction, and
(2) to validate any response (including fakes in tests) before it touches state.

Every field is required and no extra keys are allowed. Numeric ranges are not
expressed in the schema (structured outputs don't support min/max); they are
clamped when the result is applied.
"""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict

QuestionStatus = Literal["answered", "partial", "pending"]
EditOp = Literal["add", "reword", "reorder", "remove"]
Speaker = Literal["fde", "client", "unknown"]
Rung = Literal["business", "workflow", "implementation", "done"]
RUNGS = ("business", "workflow", "implementation")  # the ladder each question digs down

# --- integration slots -------------------------------------------------------------------
# One schema for every use case, shared by the interview (what is still missing under a question)
# and the plan (one section per use case). Labels shown to people come from the locale files
# (`slots.<key>` in the interview, `plan.fields.<key>` in the plan); the English descriptions below
# are for the model only.
SLOT_KEYS = ("trigger", "access", "tool", "volume", "actors", "processing", "distribution", "baseline", "target")
SlotKey = Literal["trigger", "access", "tool", "volume", "actors", "processing", "distribution", "baseline",
                  "target"]
SLOT_RUNG = {  # where each slot sits on the business > workflow > implementation ladder
    "baseline": "business", "target": "business",
    "trigger": "workflow", "actors": "workflow", "processing": "workflow", "distribution": "workflow",
    "tool": "implementation", "access": "implementation", "volume": "implementation",
}
SLOT_DESCRIPTIONS = {
    "trigger": "trigger and source: what starts the work (an email, a file, a call, a date) and where it lives "
               "(which mailbox, folder, portal, phone line)",
    "access": "access: how an agent could reach that source (mailbox account, shared drive, export, API), the "
              "credentials or permissions needed, and who owns or grants them",
    "tool": "tool: the software involved, by name (ERP, WMS, Excel...), its version if known, and whether it can "
            "export data or has an API",
    "volume": "volume: how many per day, week or month, and the peaks",
    "actors": "actors: who does it today (name or role) and who acts on the result",
    "processing": "processing: what is done with the data (read, extract, check, compare, enter, decide)",
    "distribution": "distribution: who receives the result, through which channel (email, ERP, Teams, portal) and "
                    "in which format",
    "baseline": "current cost / baseline: time spent, delays, errors or missed sales today, with numbers",
    "target": "success metric: the measurable target (time, delay, volume, error rate) and by when",
}

PLAN_SECTIONS = (
    "context",
    "priorities",
    "workflows",
    "quick_wins",
    "risks",
    "budget",
    "open_points",
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class QuestionUpdate(_Strict):
    question_id: str
    status: QuestionStatus
    answer: str  # merged summary so far; the fast pass may leave it "" (the app appends the piece)
    answer_piece: str  # ONLY what the new lines add (appended to the thread); "" if nothing new
    evidence: str
    covered: list[str]  # `expect` items now heard, copied verbatim (any rung)
    rung: Rung  # where the digging is now: business -> workflow -> implementation -> done
    follow_up: str  # the single next follow-up question to ask now; "" if none
    vague: bool  # the answer so far is generic: no recent example, number, name or tool


class Ladder(_Strict):
    """Concrete details a question needs, rung by rung."""

    business: list[str]
    workflow: list[str]
    implementation: list[str]


class QueueEdit(_Strict):
    op: EditOp
    question_id: str | None
    theme: str | None
    text_fr: str | None
    text_en: str | None
    text: str | None  # wording in the interview language (any language); null = use text_fr / text_en
    after_id: str | None
    expect: Ladder | None  # for "add": the concrete details the new question needs, per rung
    use_case: str | None  # for "add": the use case the question drills (a new id creates a new use case)
    use_case_title: str | None  # for "add" with a new use case: its short title in the interview language
    reason: str


class SlotFill(_Strict):
    """An integration slot of a use case, filled with what the CEO actually said."""

    use_case: str
    slot: SlotKey
    value: str  # the facts as said (names, tools, numbers), merged with what the slot already held
    evidence: str  # a short corrected quote


# --- the maps: how the company works, filled implicitly from the CEO's stories ----------------
# Durable records next to the use cases: who carries the signal (org), how it travels (flows),
# where information lives (stores), where expertise decides (decisions), and the tool catalog, a
# hidden checklist of categories the follow-ups aim at. Each record has a stable `id` the model
# reuses to update it; the app merges field by field ("" keeps the value already held), with the
# same optimistic versioning as the slots (state.py).
YesNo = Literal["yes", "no", "unknown"]
CeoPattern = Literal["waits_on_ceo", "ceo_asks_status", "none"]
CatalogCategory = Literal["internal_messaging", "external_channels", "shared_storage", "spreadsheets",
                          "core_systems", "task_tracking", "reporting"]
CATALOG_CATEGORIES = ("internal_messaging", "external_channels", "shared_storage", "spreadsheets", "core_systems",
                      "task_tracking", "reporting")
CATALOG_DESCRIPTIONS = {
    "internal_messaging": "internal messaging: Slack, Teams, WhatsApp groups, SMS, internal email",
    "external_channels": "external channels: email with customers and suppliers, phone, fax, customer or supplier "
                         "portals, EDI",
    "shared_storage": "shared storage: shared drive, SharePoint, Google Drive, Dropbox, a local server, paper files",
    "spreadsheets": "spreadsheets: the Excel or Google Sheets files the business runs on",
    "core_systems": "core systems: ERP, CRM, WMS, accounting, invoicing, by name",
    "task_tracking": "task and status tracking: how work in progress and its status are followed (a board, a "
                     "shared sheet, the ERP, nobody)",
    "reporting": "reporting: how figures reach the CEO and managers (a weekly file, a dashboard, a meeting)",
}
MAP_KINDS = ("org", "flows", "stores", "decisions", "catalog")


class OrgFact(_Strict):
    id: str  # short slug of the person (or of the role when no name was said); reuse it to update
    person: str  # name as said ("" when only the role is known)
    role: str
    department: str
    reports_to: str  # name or role
    time_split: str  # share of time on judgment / decisions vs repetitive glue (chasing, re-keying), as said
    notes: str
    evidence: str


class FlowFact(_Strict):
    id: str
    sender: str  # who (person, role or department) sends the information
    receiver: str
    what: str  # what passes (an order, a price, a status, a document)
    channel: str  # email, phone, WhatsApp, Teams, fax, meeting, paper, ERP...
    cadence: str  # how often
    wait: str  # how long the receiver waits for it, as said
    who_waits: str  # who is blocked while it waits
    confidential: YesNo  # part of it must stay with the sender (prices, margins, negotiation)
    ceo_pattern: CeoPattern  # waits_on_ceo = work waits on the CEO; ceo_asks_status = the CEO must ask to know
    evidence: str


class StoreFact(_Strict):
    id: str
    what: str  # the information or document (an unwritten rule or expertise: the rule itself)
    where: str  # inbox, shared drive, SharePoint, local server, Excel file, ERP, paper, "head" (someone's memory)
    owner: str  # who keeps it / who knows it
    how_found: str  # how it is found again, by whom, how long it takes
    reuse_pain: str  # what goes wrong when it is needed (lost time, wrong version, person away)
    confidential: YesNo
    evidence: str


class DecisionFact(_Strict):
    id: str
    decision: str  # the recurring decision (an order, a supplier, a price, the Monday meeting)
    owner: str  # who decides
    frequency: str
    inputs: str  # the context they decide with, and where it comes from
    prep_time: str  # time spent catching up / preparing, as said
    decide_time: str  # time spent actually deciding, as said
    evidence: str


class CatalogFact(_Strict):
    id: str
    category: CatalogCategory
    tool: str  # the tool by name (Outlook, WhatsApp, Sage, an Excel file "suivi commandes"...)
    used_by: str
    used_for: str
    source_of_truth: YesNo  # is it where the reference version of this information lives?
    access_owner: str  # who grants access to it
    evidence: str


class MapsUpdate(_Strict):
    org: list[OrgFact]
    flows: list[FlowFact]
    stores: list[StoreFact]
    decisions: list[DecisionFact]
    catalog: list[CatalogFact]


def empty_maps() -> MapsUpdate:
    return MapsUpdate(org=[], flows=[], stores=[], decisions=[], catalog=[])


class ThemeCoverage(_Strict):
    theme: str
    score: float
    note: str


class PlanDelta(_Strict):
    context: list[str]
    priorities: list[str]
    workflows: list[str]
    quick_wins: list[str]
    risks: list[str]
    budget: list[str]
    open_points: list[str]


class Passage(_Strict):
    start: str
    speaker: Speaker
    lang: str  # language code of the passage (fr, en, de…)
    text: str


RecipeFit = Literal["strong", "partial", "none"]


class RecipeMatch(_Strict):
    """A library recipe (recipes.py) the CEO's stories fit, as heard so far. Merged per
    (recipe_id, use_case) with the same optimistic versioning as the slots and maps (state.py)."""

    recipe_id: str
    use_case: str | None  # the state use case it would serve; null when no use case holds it yet
    fit: RecipeFit  # strong = most signals heard on a real case; partial = some; none = now contradicted
    heard_signals: list[str]  # the recipe's signals heard so far, copied verbatim
    missing: list[str]  # its preconditions and baseline numbers not heard yet, copied verbatim


class SyncResult(_Strict):
    question_updates: list[QuestionUpdate]
    slots: list[SlotFill]
    maps: MapsUpdate
    recipe_matches: list[RecipeMatch]
    queue_edits: list[QueueEdit]
    next_questions: list[str]
    coverage: list[ThemeCoverage]
    plan_delta: PlanDelta
    clean_passages: list[Passage]
    enough_data: bool
    enough_data_reason: str


class FastResult(_Strict):
    """Output of the fast incremental pass (a few seconds after new speech), kept small so it
    comes back fast: thread updates for the questions the new lines touch, coverage, and which
    new lines are noise. Queue edits, plan and enough_data stay with the full sync."""

    question_updates: list[QuestionUpdate]
    slots: list[SlotFill]
    maps: MapsUpdate  # only facts the new lines state plainly; usually empty
    coverage: list[ThemeCoverage]
    noise: list[str]  # ids of new lines that are a TV, nearby voices or off-topic (not the gibberish stream)


# --- the plan: a factual integration proposal ----------------------------------------------

FactStatus = Literal["stated", "partial", "to_confirm"]
Provider = Literal["anthropic", "openai", "xai", "to_confirm"]
TriggerKind = Literal["event", "schedule", "manual", "to_confirm"]


class Fact(_Strict):
    """One slot of a use case in the plan. Only `stated` facts come from the interview; anything
    else is an open item with the question to ask and who to ask."""

    status: FactStatus  # stated = said in the interview; partial = some of it said; to_confirm = not said
    value: str  # what was said, as said ("" when to_confirm)
    evidence: str  # short quote from the interview supporting `value` ("" when to_confirm)
    question: str  # what is still to confirm, as a question to ask ("" when stated)
    ask: str  # who to ask: a name or role heard in the interview ("" when unknown)


class PlanSlots(_Strict):
    trigger: Fact
    access: Fact
    tool: Fact
    volume: Fact
    actors: Fact
    processing: Fact
    distribution: Fact
    baseline: Fact
    target: Fact


class ModelChoice(_Strict):
    provider: Provider
    reason: str  # tied to this use case: extraction quality, latency/cost at the stated volume, residency...


class Score(_Strict):
    score: int  # 1 (low) to 5 (high); 0 = cannot be assessed from the interview
    reason: str  # justified from what was said


Pattern = Literal["route_distil", "act_on_behalf", "status_without_asking", "decision_brief", "capture_find_again",
                  "other"]
PATTERNS = ("route_distil", "act_on_behalf", "status_without_asking", "decision_brief", "capture_find_again", "other")
Sourcing = Literal["buy", "configure", "build", "to_confirm"]
BottleneckKind = Literal["waits_on_ceo", "ceo_asks_status", "handoff", "search", "expertise", "other"]


class Success(_Strict):
    statement: str  # what success looks like for the people doing the work, in one sentence
    check: str  # how it is checked: baseline vs target on which data, who verifies


class FirstWin(_Strict):
    """The first verified win: the opportunity run on real cases from the client's own data."""

    description: str  # what is run, on which cases
    cases: int  # about how many real cases (0 = to confirm)
    week: int  # in which week of the engagement (0 = to confirm)


class Recipe(_Strict):
    """The library recipe an opportunity reuses (recipes.py): its id, how well it fits, and what
    this client still lacks for it. null = no recipe fits: a new recipe candidate."""

    id: str
    fit: RecipeFit
    gaps: list[str]


# --- recipes: the shape shared by the library (recipes.py) and the plan's draft candidates ----------


class RecipeFlow(_Strict):
    """What the recipe does, end to end: trigger -> what the agent does -> where it writes / whom
    it notifies -> the decision the human keeps."""

    trigger: str
    agent_does: str
    writes_to: str
    notifies: str
    human_decision: str


class RecipeMetricDef(_Strict):
    """A metric definition in the plan's shape. The typical ranges were seen elsewhere: they are
    never a fact about a client (the plan keeps the client's own numbers, or to_confirm)."""

    id: str
    name: str
    unit: str
    how_measured: str
    cadence: str
    owner: str
    typical_baseline: str
    typical_target: str


class RecipeCandidate(_Strict):
    """A draft recipe the plan writes for an opportunity no library recipe fits. It stays in
    plan.json and plan.md; it never enters the library by itself (a person reviews it first)."""

    id: str
    use_case: str  # the opportunity it was drafted from
    title: str
    type: Pattern
    flow: RecipeFlow
    signals: list[str]  # facts that would show it fits another company (no client names)
    preconditions: list[str]  # access and integrations it needs
    metrics: list[RecipeMetricDef]  # typical ranges "" : one engagement is not a range
    proof: str  # first verified win, shadow mode, kill criteria
    sourcing: Sourcing


class PlanUseCase(_Strict):
    id: str
    title: str
    pattern: Pattern  # route & distil, act on behalf, status without asking, decision brief, capture & find again
    human_decision: str  # the decision the human keeps (the human stays in the loop on it)
    success: Success
    metric: str  # id of its metric in PlanDoc.metrics ("" when none could be defined)
    not_claimed: str  # what the pilot will NOT claim
    first_win: FirstWin
    kill_criterion: str  # written stop condition, checked at the week 4 / week 8 reviews
    sourcing: Sourcing  # buy or configure an existing product first (SMB), build only when nothing fits
    sourcing_reason: str
    adoption: str  # the adoption measure: who uses it every week
    back_office: bool  # back-office work (orders, invoices, claims, reporting), ranked first
    recipe: Recipe | None  # the library recipe it reuses; null = a new recipe candidate
    architecture: str  # one concrete sentence: what the agent watches, does, writes, notifies
    steps: list[str]
    trigger_kind: TriggerKind
    actions: list[str]
    slots: PlanSlots
    model: ModelChoice
    impact: Score
    feasibility: Score


class OpenItem(_Strict):
    topic: str
    question: str
    ask: str


# --- the current state: who carries the signal, how it travels, where it is lost -----------------
# Each record keeps the facts-vs-to_confirm discipline of the slots: `stated` needs a quote.


class PlanPerson(_Strict):
    name: str
    role: str
    department: str
    reports_to: str
    time_split: str  # judgment vs repetitive glue, as said
    status: FactStatus
    evidence: str


class PlanFlow(_Strict):
    sender: str
    receiver: str
    what: str
    channel: str
    cadence: str
    wait: str
    who_waits: str
    confidential: YesNo
    status: FactStatus
    evidence: str


class PlanSystem(_Strict):
    category: CatalogCategory
    tool: str
    used_by: str
    used_for: str
    source_of_truth: YesNo
    access_owner: str
    status: FactStatus
    evidence: str


class PlanStore(_Strict):
    what: str
    where: str
    owner: str
    how_found: str
    reuse_pain: str
    status: FactStatus
    evidence: str


class PlanDecision(_Strict):
    decision: str
    owner: str
    frequency: str
    inputs: str
    prep_time: str
    decide_time: str
    status: FactStatus
    evidence: str


class CurrentState(_Strict):
    people: list[PlanPerson]
    flows: list[PlanFlow]
    systems: list[PlanSystem]
    stores: list[PlanStore]
    decisions: list[PlanDecision]


class Bottleneck(_Strict):
    """Where the signal is lost: who waits on whom, how long (the two CEO patterns included)."""

    kind: BottleneckKind
    who_waits: str
    waits_on: str
    what: str
    how_long: str
    status: FactStatus
    evidence: str
    question: str  # what to confirm ("" when stated)
    ask: str


class Measure(_Strict):
    value: str  # with its number, as said ("" when to_confirm)
    date: str  # when it was measured / by when the target is due ("" when unknown)
    status: FactStatus
    evidence: str
    question: str  # what to ask to get the number ("" when stated)
    ask: str


class Metric(_Strict):
    """A metric definition the pilot is judged on (monitored per client in metrics.yaml, metrics.py)."""

    id: str
    use_case: str  # the opportunity it measures
    name: str
    unit: str
    how_measured: str  # data source in the client's systems, or a manual count
    cadence: str
    owner: str  # who verifies it
    baseline: Measure
    target: Measure  # the pilot target


class TimelineStep(_Strict):
    week: int
    milestone: str
    proof: str  # what is shown or decided then


class PlanDoc(_Strict):
    """The structured plan Claude returns; finalize.render_plan_md turns it into Markdown/HTML."""

    context: str  # short header: who the client is and what the interview covered (facts only)
    current_state: CurrentState
    bottlenecks: list[Bottleneck]
    metrics: list[Metric]
    use_cases: list[PlanUseCase]
    timeline: list[TimelineStep]  # week 1 thin slice, week 4 checkpoint, week 8 go / kill
    open_items: list[OpenItem]  # open items not tied to one slot (budget, sponsor, security rules...)
    recipe_candidates: list[RecipeCandidate]  # drafts for the opportunities no library recipe fits


def empty_current_state() -> CurrentState:
    return CurrentState(people=[], flows=[], systems=[], stores=[], decisions=[])


assert tuple(PlanSlots.model_fields) == SLOT_KEYS  # one schema for the interview and the plan


class ContractError(ValueError):
    pass


_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S)


def parse_sync_result(raw: str | dict | SyncResult) -> SyncResult:
    """Validate a raw response against the contract. Raises ContractError."""
    if isinstance(raw, SyncResult):
        return raw
    try:
        if isinstance(raw, str):
            raw = _loads_lenient(raw)
        return SyncResult.model_validate(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ContractError(f"sync response violates the contract: {exc}") from exc


def parse_fast_result(raw: str | dict | FastResult) -> FastResult:
    if isinstance(raw, FastResult):
        return raw
    try:
        if isinstance(raw, str):
            raw = _loads_lenient(raw)
        return FastResult.model_validate(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ContractError(f"fast response violates the contract: {exc}") from exc


def _loads_lenient(text: str):
    """Repair path for text replies: strip a ```json fence, or take the outermost {...}
    when the model wrapped the object in a sentence."""
    m = _FENCE.match(text)
    candidate = m.group(1) if m else text.strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(candidate[start : end + 1])


def parse_plan_doc(raw: str | dict | PlanDoc) -> PlanDoc:
    if isinstance(raw, PlanDoc):
        return raw
    try:
        if isinstance(raw, str):
            raw = _loads_lenient(raw)
        return PlanDoc.model_validate(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ContractError(f"plan response violates the contract: {exc}") from exc


def empty_result() -> SyncResult:
    return SyncResult(
        question_updates=[],
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
