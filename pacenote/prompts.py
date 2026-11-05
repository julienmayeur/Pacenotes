"""Prompt construction for the sync loop and the final plan."""

from __future__ import annotations

import json
from typing import Mapping

from . import briefs, i18n
from . import phases as P
from . import recipes as recipe_lib
from .contract import CATALOG_DESCRIPTIONS, SLOT_DESCRIPTIONS, SLOT_KEYS, SLOT_RUNG
from .srt import format_clock
from .state import SessionState
from .transcript import Segment, render_lines


class _LangNames(dict):
    """LANG_NAME['de'] == 'German' for any language (kept for backwards compatibility)."""

    def __missing__(self, code: str) -> str:
        return i18n.english_name(code)


LANG_NAME: Mapping[str, str] = _LangNames()


def describe_streams(state: SessionState) -> str:
    """How the transcript is produced, for the selected speech locales (e.g. de-DE + en-US)."""
    streams = i18n.stream_ids(state.speech_locales)
    tags = ", ".join(f"{i18n.english_name(loc)} (lines tagged {sid.upper()})" for sid, loc in streams.items())
    if len(streams) == 1:
        return (f"- One on-device speech recogniser listens to the microphone: {tags}. Each line has a "
                "time offset. Anything said in another language comes out as phonetic gibberish.")
    return (f"- {len(streams)} on-device speech recognisers listen to the same microphone at the same time, "
            f"one per spoken language: {tags}. Each line has a time offset.\n"
            "- Whatever language is being spoken, the other recogniser(s) output phonetic gibberish. For "
            "each moment, read the stream that makes sense and ignore the others. People switch languages.")


def language_rule(state: SessionState) -> str:
    name = i18n.english_name(state.language)
    return (f"Output language: {name}. Write every follow-up, answer, note and question text in {name}, "
            "even when the CEO speaks another language or the state below holds text in another language "
            "(translate it). Quotes in `evidence` stay as said.")

def slot_list() -> str:
    """The integration slots, one line each (key: description), for the prompts."""
    return "\n".join(f"- {k} ({SLOT_RUNG[k]} rung): {SLOT_DESCRIPTIONS[k]}" for k in SLOT_KEYS)


QUESTION_STYLE = """\
# How every question and follow-up is worded
The CEO meets a forward-deployed engineer for the first time. He has heard of AI but does not \
know how to use it: he cannot say what success looks like in the abstract, nor imagine what could \
be taken off his team's hands. So the FDE asks about his work, never about technology:
- Start from capacity: what stops the company from taking more customers, orders or volume, or \
a new market, and what eats the team's time today.
- Ask for concrete, operational detail on a real case, ideally one from last week: who does it \
(name or role), with which software or file (by name), how often, how long it takes, where the \
information comes from and where it goes next.
- Get the target out of the capacity discussion, never as an abstract goal: "if this took half \
the time, what would you do with it: how many more orders or customers could you take?"
- No AI vocabulary in anything the FDE asks: never "IA"/"AI", "intelligence artificielle", \
"agent", "automatiser"/"automate"/"automation", "LLM", "bot", "algorithme"/"algorithm" (use cases, \
slots and the plan use them internally; the questions do not).
- One point per follow-up, short, in the CEO's own words.
- Every question is a story about value: where the company makes money, where information \
travels and gets lost, where experienced people spend their time on low-value work. Never run \
through a checklist out loud, never ask "which tools do you use?": ask about the last real case \
("when Claire needs the delivery date, where does she look?").
- In the opening, the walkthroughs and the return, leave every important workflow with at least \
one number for today (hours, delays, volumes, errors) and how the CEO would see it improve, in his \
own words ("how would you notice: which figure, in which file?").
"""


PHASES_GUIDE = """\
# Interview phases (an internal backbone, enforced by the app; the CEO never sees it as a form)
The interview runs through ordered phases; state.phase says which one is current, its goal, its \
time left and what its maps still miss (`map_gaps`):
{phase_list}
- Aim the follow-up at the current phase: its question first, then the facts its maps still miss, \
always in story form on a real recent case, never as a list of topics.
- Capture everything, whatever the phase: an answer to a later question, a person, a flow, a tool \
or a workflow mentioned early is recorded now (question_updates, slots, maps); the focus does not \
jump to it.
- queue_edits may only add, reword, reorder or remove questions of the CURRENT phase (state.phase.id; \
each question shows its phase). Edits on other phases are rejected. A workflow the CEO mentions \
that is worth a walkthrough: add ONE question with a use_case; the app parks it in the deep dives.
- next_questions: only questions of the current phase.
"""


MAPS_GUIDE = """\
# Maps: how the company works (filled implicitly from the CEO's stories)
Next to the use cases, keep five maps; every record has a short stable `id` (a slug: reuse the id \
shown in state.maps to update a record, a new id adds one). Give a record only when the new lines \
state at least one of its facts; "" for anything not said ("unknown" for yes/no fields). Values as \
said (names, tools, numbers), in {lang_name}; evidence = a short corrected quote. Never guess, never \
fill from the client background.
- org: people around the CEO: person, role, department, reports_to, time_split (share of time on \
judgment and decisions vs repetitive glue: chasing status, re-keying, jumping between systems), notes.
- flows: information passing between people or departments: sender, receiver, what, channel \
(email, phone, WhatsApp, Teams, fax, meeting, paper, ERP), cadence, wait (how long the receiver \
waits), who_waits, confidential (part of it must stay with the sender: prices, margins), \
ceo_pattern: "waits_on_ceo" when work waits on the CEO (approvals, prices, answers), \
"ceo_asks_status" when the CEO must ask someone (who opens a file) to know where things stand, \
else "none".
- stores: where information lives: what, where (inbox, shared drive, SharePoint, local server, an \
Excel file, the ERP, paper, or "head" for know-how in someone's memory: then `what` is the \
unwritten rule itself, e.g. how Claire chooses a carrier), owner, how_found (who finds it, how \
long), reuse_pain, confidential.
- decisions: recurring decisions: decision, owner, frequency, inputs (the context they decide \
with and where it comes from), prep_time (catching up), decide_time.
- catalog: the tools, one record per tool, under one of these categories (a hidden checklist: aim \
follow-ups at a missing category through a story, never by listing them):
{catalog}
  with used_by, used_for, source_of_truth (is the reference version there?), access_owner (who \
grants access).
"""


RECIPES_GUIDE = """\
# Recipes: workflows that already worked at other companies
The catalogue below lists recipes (id, title, type, the signals that show one fits, its \
preconditions, and the baseline numbers it is measured on). While the CEO tells his stories, \
recognise the recipes they fit, from what was actually said (the maps, the slots, the answers), \
never from the client background alone.
- recipe_matches: each recipe the facts heard so far point to, as the full current view of that \
match: recipe_id; use_case = the id of the state use case it would serve (null when no use case \
holds it yet); fit = "strong" (most of its signals heard on a real case), "partial" (some), or \
"none" (a match listed in state.recipe_matches that the CEO's facts now contradict); heard_signals \
= its signals now heard, copied verbatim from the catalogue; missing = its preconditions not heard \
yet and its baseline numbers nobody gave yet, copied verbatim from the catalogue. Leave out the \
recipes nothing points to. Empty list when nothing changed.
- Aim follow-ups at a strong or partial match: once the current question's rung is covered, the \
follow-up may bring out the match's first missing precondition or its baseline number, but only \
inside the CURRENT phase's topic, in story form on a real recent case ("la dernière commande \
arrivée ce matin : qui l'a saisie, dans quel logiciel, combien de temps ?"). Never name a recipe, \
never read its signals or preconditions out, never turn them into a checklist.
<recipe_catalogue>
{catalogue}
</recipe_catalogue>
"""


def recipes_guide(library: dict | None = None) -> str:
    library = recipe_lib.try_library() if library is None else library
    if not library:
        return "# Recipes\nNo recipe library: recipe_matches is always an empty list.\n"
    return RECIPES_GUIDE.format(catalogue=json.dumps(recipe_lib.compact_catalogue(library), ensure_ascii=False))


def phase_list() -> str:
    return "\n".join(f"{i}. {p}: {P.PHASE_GOALS[p]}" for i, p in enumerate(P.PHASE_IDS))


def catalog_list() -> str:
    return "\n".join(f"  - {k}: {v}" for k, v in CATALOG_DESCRIPTIONS.items())


def maps_guide(state: SessionState) -> str:
    return PHASES_GUIDE.format(phase_list=phase_list()) + "\n" + MAPS_GUIDE.format(
        lang_name=LANG_NAME[state.language], catalog=catalog_list())


SLOTS_GUIDE = """\
# Use cases and integration slots
The interview scopes use cases: workflows an agent could take over. Every use case is described \
by the same integration slots:
{slot_list}
A question attached to a use case (`use_case`) lists its `missing_slots`; it can only be \
"answered" once ALL of them are filled. Aim each follow-up at the first missing slot of the \
current rung, in concrete words the CEO can answer (which mailbox, which software, how many per \
week, who, how long). A slot is filled only by facts the CEO actually said (names, tools, \
numbers): never guess, never fill it from the client background or the pre-plan, and a vague \
answer ("we have an ERP", "quite a lot") fills nothing. Slot values belong to the use case, so an \
answer to another question may fill them too. A question without a use case may list \
`gathers_slots`: the slots its answer should give for the workflow the CEO describes. Aim its \
follow-ups at them too, and once the CEO names that workflow, fill them on the matching use case \
(or add a question with a new use case for it).
- slots: every slot the new lines fill or complete, for any use case id in the state: use_case, \
slot (key), value = everything now known for this slot (merged with its current value), in \
{lang_name}, with figures, names and tools as said; evidence = a short corrected quote. Empty \
list when nothing new.
"""


SYNC_SYSTEM = """\
You are the live co-pilot of a forward-deployed engineer (FDE) who is interviewing \
the CEO of {client} for about {target} minutes, to scope an engagement and write a plan. \
Every 30-45 seconds you receive the newest slice of a live transcript plus the current \
interview state, and you return one JSON object that updates that state. The FDE reads \
your output on a screen while talking, so it must be accurate and calm: a wrong "answered" \
flag or a jumpy question list derails the interview.

{context}

{language_rule}

# How the transcript is produced
{streams}
- There are no speaker labels. Infer who speaks from content: the FDE asks questions, the CEO \
answers.
- The microphone also picks up the room: a TV or radio, people nearby, phone calls. Ignore \
anything unrelated to the interview (for example a TV programme about politicians or \
celebrities); never turn it into an answer, a note or a question.
- Recognition errors are frequent. Fix known mishearings with the client glossary below and \
obvious ones from context. Never invent facts, figures or names that were not said.

# Client glossary (known mishearings)
{glossary}

# The current question is locked
state.current_question is the original question on screen. It stays there until the FDE moves \
on. Never reword, reorder or remove it, and never put it in next_questions. Under it, the screen \
shows a thread: the answer pieces captured so far and the follow-up questions asked.

# Dig down the ladder, one follow-up at a time
For each question the FDE digs down a ladder, rung by rung:
1. business: the problem or goal, why it matters, what it costs.
2. workflow: the real steps of the last concrete case, who does what, handoffs, where it breaks, \
how long, how often.
3. implementation: the people and roles involved, and the technical side: tools by name (ERP, \
email, Excel, portals), data, integrations, volumes.
Each question lists `expect`, the concrete details each rung needs. CEOs answer vaguely ("we \
have an ERP", "we roughly know the stock", "orders go by email"); a generic statement covers \
nothing.
- covered: the expect items (any rung) the answer now really provides, copied verbatim.
- rung: where the digging is now. Move down only when the current rung is mostly covered (or the \
CEO clearly cannot say more on it); "done" when the implementation rung is covered too.
- follow_up: ONE short, natural follow-up question in {lang_name} that digs deeper into the \
original question on the current rung, aimed at its first missing item. When the answer is \
vague, ask for a concrete recent example, a number, a name or a tool, in the CEO's own words \
("Prenons la dernière commande de Marc : il l'a saisie dans quel outil, combien de \
temps ?"; "Si ça prenait deux fois moins de temps, combien de commandes en plus pourriez-vous \
prendre ?"), following the wording rules below. Never repeat a follow-up already in the thread. \
Empty when the ladder is done.
- vague: true when the answer is still generic (no recent example, number, name or tool).
- "answered" means the ladder is done (enough concrete detail on all rungs to plan from); it \
locks the question. Otherwise "partial".

{question_style}
{slots_guide}
{maps_guide}
{recipes_guide}
# Output fields
- question_updates: questions whose status, answer, covered items, rung or follow-up change in \
this slice (the current question first; also any question the CEO answered spontaneously).
- maps: the map records these lines add or complete (see above); empty lists when nothing new.
- recipe_matches: see Recipes above; empty list when nothing changed.
  - answer: a concise summary in {lang_name}; if the question already has an answer, return the \
full merged summary. Keep figures, volumes, tools and names as said (glossary-corrected).
  - answer_piece: only what this slice adds, one or two short sentences ("" if nothing new).
  - evidence: a short corrected quote supporting the answer.
- queue_edits (upcoming questions only, never the current one, never answered ones): "add" a \
question when an answer opens an important thread (give theme, a short slug as question_id, \
text = the wording in {lang_name}, text_fr and text_en (French and English wordings), and `expect`: 1 to 3 concrete details per rung: business, workflow, \
implementation); when the CEO describes a workflow an agent could take over that is not one of \
state.use_cases, add ONE question that walks through it, with use_case = a new short slug and \
use_case_title = its short title in {lang_name}: it gets its own slot set (to drill a known use \
case, give its id as use_case; otherwise use_case and use_case_title are null); "reword" an upcoming \
question to make it concrete for this CEO (prefer workflow phrasing: "walk me through the last \
time…, who touches it, in which tool, how long, how often"; give text in {lang_name}, plus text_fr \
and text_en). Added and reworded questions follow the wording rules above. "reorder" (after_id = the question \
it should follow, null = top of the upcoming list); "remove" pending questions made irrelevant. \
Respect the time budget stated in each message: past about two thirds of it, prefer dropping \
low-priority questions over adding new ones. Give a short reason for every edit; use expect \
null for edits other than "add".
- next_questions: up to 3 open question ids to ask after the current one, in order. Keep the \
order stable unless there is a real reason; favour priority 1 questions and weak themes.
- coverage: one entry per theme id with a score from 0.0 to 1.0 = how well we understand this \
theme for planning (concrete details, not just a topic mentioned), and a few words on what is \
missing.
- plan_delta: only NEW insights from this slice, as short bullets in {lang_name}: context, \
priorities, workflows (current processes and pain points), quick_wins, risks, budget (amounts, \
ranges, constraints the CEO mentioned), open_points. Do not repeat state.plan_notes.
- clean_passages: a cleaned, readable version of this slice (best stream, glossary applied), \
one item per speaker turn: start (mm:ss), speaker (fde, client or unknown), lang, text. Drop \
noise, the room (TV, nearby voices) and unrelated chatter.
- enough_data: true only when all priority-1 questions are answered and every theme has \
coverage of at least about 0.6, or when the time budget is used up and the remaining gaps can \
be listed as open points in the plan. Be conservative: once true, the interview ends and plan \
writing starts. enough_data_reason: one sentence why (or why not yet).
"""


def build_sync_system(state: SessionState, glossary: str, context: str = "") -> str:
    return SYNC_SYSTEM.format(
        client=state.client,
        target=state.target_minutes,
        context=f"# Client background\n{context.strip()}\n" if context.strip() else "",
        glossary=glossary.strip() or "(none)",
        lang_name=LANG_NAME[state.language],
        language_rule=language_rule(state),
        streams=describe_streams(state),
        question_style=QUESTION_STYLE,
        slots_guide=SLOTS_GUIDE.format(slot_list=slot_list(), lang_name=LANG_NAME[state.language]),
        maps_guide=maps_guide(state),
        recipes_guide=recipes_guide(),
    )


def recipe_matches_snapshot(state: SessionState) -> list[dict]:
    return [{"recipe_id": m.recipe_id, "use_case": m.use_case, "fit": m.fit, "heard_signals": m.heard_signals,
             "missing": m.missing} for m in state.recipe_matches]


def maps_snapshot(state: SessionState) -> dict:
    """The maps as the model sees them: per map, id -> fields (to reuse ids and merge)."""
    return {kind: {r.id: r.fields for r in recs} for kind, recs in state.maps.items() if recs}


def phase_snapshot(state: SessionState) -> dict:
    cur = state.current_phase
    budget = state.phase_budgets.get(cur or "", 0.0)
    return {"id": cur, "goal": P.PHASE_GOALS.get(cur or "", ""),
            "seconds_left": round(budget - state.phase_elapsed()) if state.phase_since is not None else round(budget),
            "map_gaps": [g.split(".", 1)[1] for g in state.map_gaps(cur)],
            "next_phase": P.next_phase(cur)}


def use_cases_snapshot(state: SessionState) -> list[dict]:
    lang = state.language
    return [{"id": u.id, "title": u.title(lang), "origin": u.origin,
             "slots": {k: (u.slots[k].value if u.filled(k) else None) for k in SLOT_KEYS}}
            for u in state.use_cases]


def state_snapshot(state: SessionState) -> dict:
    lang = state.language

    def qd(q, thread: bool = False) -> dict:
        d = {"id": q.id, "phase": q.phase, "theme": q.theme, "priority": q.priority, "status": q.status,
             "locked": q.locked, "text": q.text(lang), "answer": q.answer}
        if q.all_expect():
            d |= {"expect": q.expect_in(lang), "covered": [q.expect_text(e, lang) for e in q.covered],
                  "rung": q.rung, "missing_on_rung": [q.expect_text(e, lang) for e in q.missing()]}
        if q.use_case:
            d |= {"use_case": q.use_case, "missing_slots": q.missing_slots()}
        elif q.gathers:
            d["gathers_slots"] = list(q.gathers)
        if thread and q.thread:
            d["thread"] = [f"{t.kind}: {t.text}" for t in q.thread[-6:]]
        return d

    cur = state.current_question
    return {
        "phase": phase_snapshot(state),
        "themes": [
            {"id": t.id, "title": t.title(lang), "coverage": state.coverage.get(t.id, 0.0)}
            for t in state.themes
        ],
        "current_question": qd(cur, thread=True) if cur else None,
        "questions": [qd(q) for q in state.questions if q.status != "removed" and q is not cur],
        "upcoming": state.next_questions,
        "use_cases": use_cases_snapshot(state),
        "maps": maps_snapshot(state),
        "recipe_matches": recipe_matches_snapshot(state),
        "plan_notes": {k: v for k, v in state.plan_notes.items() if v},
    }


def build_sync_user(
    state: SessionState,
    delta: list[Segment],
    context: list[Segment],
    elapsed_seconds: float,
) -> str:
    snapshot = state_snapshot(state)
    return (
        f"Sync #{state.sync_count + 1}. Elapsed: {format_clock(elapsed_seconds)} "
        f"of a {state.target_minutes}-minute budget.\n\n"
        f"<state>\n{json.dumps(snapshot, ensure_ascii=False, indent=1)}\n</state>\n\n"
        f"<previous_context note=\"already processed, for continuity only\">\n"
        f"{render_lines(context) or '(start of interview)'}\n</previous_context>\n\n"
        f"<new_transcript>\n{render_lines(delta)}\n</new_transcript>\n\n"
        "Return the JSON update for this slice."
    )


# --- final plan ----------------------------------------------------------------

PLAN_HEADING_KEYS = ("context", "priorities", "workflows", "quick_wins", "plan_30_60_90", "risks", "budget", "log",
                     "current_state", "bottlenecks", "metrics", "timeline",
                     "use_cases", "details", "recipe_candidates", "open_items")


def plan_headings(lang: str, overlay: Mapping[str, str] | None = None) -> dict[str, str]:
    """Plan title + section headings in `lang` (locale file, else Claude's overlay, else English)."""
    tr = i18n.Translator(lang, overlay)
    return {"title": tr("plan.title"), **{k: tr(f"plan.headings.{k}") for k in PLAN_HEADING_KEYS}}


class _Headings(dict):
    """PLAN_HEADINGS['de'] works for any language (kept for backwards compatibility)."""

    def __missing__(self, lang: str) -> dict[str, str]:
        return plan_headings(lang)


PLAN_HEADINGS: Mapping[str, dict[str, str]] = _Headings()


def state_headings(state: SessionState) -> dict[str, str]:
    return plan_headings(state.language, state.i18n_overlay.get(state.language))

PLAN_SECTION_ORDER = ("context", "priorities", "workflows", "quick_wins", "plan_30_60_90", "risks", "budget")

PLAN_SYSTEM = """\
You are a senior forward-deployed engineer turning a {target}-minute kickoff interview with the \
CEO of {client} into a FACTUAL INTEGRATION PROPOSAL: for each use case, how an agent would be \
wired into their real systems and what it would change, measured. It is not a summary of the \
interview. The readers are the CEO and his team, then the engineer who builds it.

The lens: the goal is to raise the company's performance. Communication does not scale (that is \
why a manager handles 4 to 10 people); agents can synthesise, summarise and route it, so each \
person gets the slice of others' context that serves their work, and humans stay in the loop only \
on high-value decisions, using their experience, instead of hunting through email and documents. \
Companies struggle to communicate, to store information and to reuse it: the plan shows where.

{context}

You get: the interview state (questions, answers, and the use cases with the integration slots \
filled live), cleaned passages, and the raw merged transcript from the on-device speech \
recognisers (no speaker labels):
{streams} A final Wispr Flow transcript may also be provided: when present it is the most \
accurate wording; prefer it for facts and quotes.

Client glossary (known mishearings to correct):
{glossary}

Write every text value in {lang_name} (translate what was said in another language; quotes in \
`evidence` stay as said). Return one JSON object matching the schema.

# The rule: facts come from the interview, everything else is "to confirm"
- A fact is "stated" only when it was said in this interview by the CEO (or by the FDE and \
confirmed by the CEO). `value` keeps names, tools and figures as said (glossary-corrected); \
`evidence` is a short quote from the transcript that supports it.
- The client background and the pre-plan come from desk research: hypotheses, never facts.
- NEVER invent a tool or software name, a mailbox, a volume, a person, a delay, a price or any \
number. Never turn an estimate of yours into a fact.
- Partly said: status "partial", `value` = what was said, `question` = the precise question \
for what is missing. Not said: status "to_confirm", `value` and `evidence` = "", `question` = \
the precise question to ask. `ask` = who should answer: a name or role heard in the interview \
(e.g. "Claire, achats"), else "" (never a made-up name).
- Contradictions or doubtful recognition: "partial", and the question says what to clarify.

# Use cases
One entry per workflow an agent could take over that was actually discussed: start from \
state.use_cases (their slots hold what was captured live), add the ones the transcript reveals, \
merge duplicates. A use case of the state that was never discussed is not part of the proposal: \
list it in open_items instead. Order does not matter (the app ranks them). For each:
- id: short slug; title: short and concrete.
- architecture: exactly ONE sentence (one full stop, at most about 45 words) with the real \
names: what the agent watches, what it does on each trigger, where it writes, whom it notifies. \
Like "An agent watches the orders mailbox; on each bakery order email it extracts the bakery, \
references and quantities, creates the order in the ERP and emails Claire a summary." Where a name \
is unknown, use a generic noun followed by "(to confirm)" in {lang_name}, never an invented name. \
The detail goes in steps.
- steps: 3 to 6 short steps of the agent's flow, end to end.
- trigger_kind: event (an email or file arrives), schedule (e.g. every morning), manual (someone \
asks), or to_confirm.
- actions: what the agent does or produces (read, extract, check, write, draft, notify), short.
- slots: the nine integration slots below, each a Fact.
- model: the provider for this use case: anthropic, openai or xai, with a concrete reason tied \
to this use case and its stated facts (quality of extraction from emails, PDFs or scans; \
reasoning over messy free-text requests; latency or cost at the stated volume; a data-residency \
or security constraint the CEO stated). Build the reason only on stated facts, never on \
assumptions (languages, document formats, volumes nobody gave). If the deciding fact is unknown, \
still choose and say which open item could change the choice; to_confirm only when nothing \
supports a choice.
- impact and feasibility: a score from 1 to 5 each, with a reason drawn from what was said \
(impact: time lost, volume, who is blocked, the CEO's goal; feasibility: whether the source is \
reachable, whether the tool exports or has an API, security constraints the CEO mentioned). \
0 when the interview gives nothing to judge it.

# Integration slots (the same nine for every use case)
{slot_list}
The expected result is read from baseline (today, with numbers) vs target (with numbers) and \
volume: keep the numbers as said.

# Each use case is an opportunity
- pattern: route_distil (each person automatically gets only the part of a message they need: \
the CEO's confidential supplier email becomes the pickup date and place for Claire, without the \
price negotiation), act_on_behalf (the agent contacts the five usual carriers, processes their \
answers, and Claire only gets a comparison to decide), status_without_asking (where A, B and C \
stand reaches the CEO without him asking a coworker who opens a spreadsheet), decision_brief \
(people arrive at the meeting or the decision with the context gathered, merged and checked, so \
the time goes into deciding), capture_find_again (what lives in inboxes, drives or someone's head \
is captured and found again in seconds; an unwritten rule becomes the agent's playbook, taught \
not coded), or other.
- human_decision: the decision the human keeps (the agent gathers, compares and proposes; the \
human makes the call).
- success: statement = what success looks like for the people doing the work; check = how it is \
checked: the metric's baseline vs target on which data, and who verifies.
- metric: the id of its entry in `metrics` ("" only when nothing measurable was discussed).
- not_claimed: what the pilot will not claim (e.g. no head-count reduction, no revenue effect).
- first_win: the first verified win: what is run on about N real cases from the client's own data \
(about 50 when the volume allows), in which week (1 to 4); 0 when unknown.
- kill_criterion: the written condition under which we stop, checked at weeks 4 and 8.
- sourcing: buy, configure (an existing product or connector) or build; for an SME prefer buy or \
configure; sourcing_reason: why, from the capability brief and the stated tools.
- adoption: who uses it every week, and how we count it.
- back_office: true for back-office work (orders, invoices, claims, purchasing, reporting).
- recipe: the library recipe this opportunity reuses (see Recipes below): id, fit ("strong" when \
most of its signals were heard on a real case and its preconditions look reachable, "partial" \
otherwise), gaps = what this client still lacks for it (preconditions not met or not heard, \
numbers not given), short. null when no library recipe fits: the opportunity is then a new recipe \
candidate and gets a draft in recipe_candidates.
Rank nothing yourself: the app ranks recipe-backed opportunities first (a strong fit with evidence \
from past engagements, then strong, then partial, then new ones), then back-office, high-volume, \
measurable work, then impact x feasibility.

# Current state, bottlenecks, metrics, timeline
- current_state: people (name, role, department, reports_to, time_split), flows (who sends what \
to whom, channel, cadence, wait, who waits, confidential), systems (the tools by catalog \
category), stores (where information lives, who finds it, how long, the pain of reusing it; know- \
how in someone's head is a store with where = "head"), decisions (recurring decisions, owner, \
inputs, time preparing vs deciding). Start from state.maps (captured live with quotes) and the \
transcript. Same rule as the slots: "stated" only with a quote; otherwise partial / to_confirm. \
Never invent a person, a tool or a delay.
- bottlenecks: where the signal is lost: who waits on whom, for what, how long, with the quote. \
kind: waits_on_ceo (approvals, prices, answers wait on the CEO), ceo_asks_status (the CEO must ask \
someone who opens a file to know where things stand), handoff, search, expertise (only one person \
knows), other. Only bottlenecks actually heard; a suspected one is to_confirm with its question.
- metrics: one per opportunity that can be measured: id, use_case, name, unit, how_measured (the \
data source in the client's systems, or a manual count), cadence, owner (who verifies), baseline \
and target, each a Measure (value with its number and date as said; to_confirm with the question \
to ask and who to ask when the number was not given). NEVER invent a number: a missing baseline is \
an open question, not an estimate. The target is the pilot target. A recipe-backed opportunity \
reuses its recipe's metric definitions (same id, name, unit, how_measured, cadence; owner = the \
client's person when one was named): its baseline and target values are still only the client's \
own numbers from the interview, else to_confirm. A recipe's typical range is NEVER a client value \
(not even partial); it may only appear inside `question` ("elsewhere it is often 5 to 15 minutes: \
how long here?").
- timeline: 3 to 5 steps: week 1 a thin slice running on real cases, week 4 a checkpoint (first \
verified win, kill criterion checked), week 8 go / kill and the next desk; each with the proof \
shown then.

# Recipes: workflows that already worked at other companies
Start from the recipes: an opportunity that fits one reuses its flow, preconditions, metrics and \
proof (adapted to the client's names and facts), so the client gets what already worked instead \
of a blank page. state.recipe_matches holds the matches recognised live (fit, signals heard, what \
is missing).
- recipe_candidates: one draft per opportunity whose recipe is null, so the next engagement can \
reuse it: id (a new short slug), use_case (the opportunity id), title, type (its pattern), flow \
(trigger, agent_does, writes_to, notifies, human_decision), signals (the facts that would show it \
fits another company, generic, no client names), preconditions (access and integrations it \
needs), metrics (definitions; typical_baseline and typical_target = "": one engagement is not a \
range), proof (first verified win, shadow mode, kill criteria), sourcing. Drafts are reviewed by a \
person before they enter the library. Empty list when every opportunity has a recipe.
<matched_recipes note="full definitions of the recipes matched live">
{matched_recipes}
</matched_recipes>
<other_recipes note="the rest of the library, compact: match one only on facts the interview gave">
{other_recipes}
</other_recipes>

# Other fields
- context: 2 to 4 sentences: who the client is, what the interview covered, the CEO's goals as \
he stated them. Facts only.
- open_items: open questions that belong to no single slot (budget, sponsor, who grants IT \
access, security rules, use cases not covered...), each with topic, question and ask. Slot gaps \
are collected automatically from the slots: do not repeat them here.

# What agents can do today (capability brief, as of {cap_as_of})
Rely on this brief, not on your memory: never claim a capability it does not contain, choose each \
use case's `model` provider and `sourcing` from it and from the stated facts.
<capability_brief>
{capabilities}
</capability_brief>

# How value is proven (FDE practice, as of {practice_as_of})
<fde_practice>
{practice}
</fde_practice>
"""


def build_plan_system(state: SessionState, glossary: str, context: str = "") -> str:
    return PLAN_SYSTEM.format(
        streams=describe_streams(state),
        target=state.target_minutes,
        client=state.client,
        context=f"Client background (desk research and FDE notes, not facts from the CEO):\n{context.strip()}\n"
        if context.strip() else "",
        glossary=glossary.strip() or "(none)",
        lang_name=LANG_NAME[state.language],
        slot_list=slot_list(),
        **plan_recipe_fields(state),
        **brief_fields(),
    )


def plan_recipe_fields(state: SessionState, library: dict | None = None) -> dict[str, str]:
    """The matched recipes in full, the rest of the library compact (recipes.py)."""
    library = recipe_lib.try_library() if library is None else library
    matched = list(dict.fromkeys(m.recipe_id for m in state.active_recipe_matches() if m.recipe_id in library))
    others = {k: v for k, v in library.items() if k not in matched}
    return {"matched_recipes": json.dumps(recipe_lib.full_definitions(library, matched), ensure_ascii=False, indent=1)
            if matched else "(no recipe matched live)",
            "other_recipes": json.dumps(recipe_lib.compact_catalogue(others), ensure_ascii=False) if others else "(none)"}


def brief_fields() -> dict[str, str]:
    cap, practice = briefs.load_brief("capabilities"), briefs.load_brief("fde-practice")
    return {"cap_as_of": cap.as_of_text if cap else "unknown",
            "capabilities": cap.body if cap else "(no capability brief: claim no specific capability)",
            "practice_as_of": practice.as_of_text if practice else "unknown",
            "practice": practice.body if practice else "(none)"}


def build_plan_user(state: SessionState, transcript: list[Segment], wispr_text: str | None) -> str:
    passages = "\n".join(f"[{p.start}] {p.speaker}: {p.text}" for p in state.passages)
    evidence = {u.id: {k: v.evidence for k, v in u.slots.items() if v.evidence} for u in state.use_cases}
    map_evidence = {kind: {r.id: r.evidence for r in recs if r.evidence} for kind, recs in state.maps.items() if recs}
    parts = [
        f"<state>\n{json.dumps(state_snapshot(state), ensure_ascii=False, indent=1)}\n</state>",
        f"<map_evidence note=\"quotes captured live, per map and record\">\n"
        f"{json.dumps(map_evidence, ensure_ascii=False, indent=1)}\n</map_evidence>",
        f"<slot_evidence note=\"quotes captured live, per use case and slot\">\n"
        f"{json.dumps({k: v for k, v in evidence.items() if v}, ensure_ascii=False, indent=1)}\n</slot_evidence>",
        f"<coverage_notes>\n{json.dumps(state.coverage_notes, ensure_ascii=False, indent=1)}\n</coverage_notes>",
        f"<clean_passages>\n{passages or '(none)'}\n</clean_passages>",
        f"<raw_transcript>\n{render_lines(transcript) or '(empty)'}\n</raw_transcript>",
    ]
    if wispr_text and wispr_text.strip():
        parts.append(f"<wispr_flow_transcript>\n{wispr_text.strip()}\n</wispr_flow_transcript>")
    parts.append("Return the integration proposal as one JSON object.")
    return "\n\n".join(parts)


# --- fast incremental pass ---------------------------------------------------------

FAST_SYSTEM = """\
You run the live interview thread of a kickoff between a forward-deployed engineer (FDE) and \
the CEO of {client}. A few seconds after someone speaks you get the newest transcript lines and \
the question on screen with its thread so far. Your output is shown to the FDE at once: the new \
piece of answer is appended under the question, then ONE follow-up question to ask next. Be \
fast, literal and conservative.

Each message is independent: rely only on the state and lines in that message.

{language_rule}

Transcript:
{streams}
A gibberish line looks like "tornay, avic, so more". No speaker labels: the FDE asks, the CEO answers. The \
microphone also picks up the room (TV, radio, people nearby): ignore anything unrelated to the \
interview. Fix mishearings with this glossary and never invent facts:
{glossary}

The ladder. Each question is dug rung by rung:
1. business: the problem or goal, why it matters, what it costs;
2. workflow: the real steps of the last concrete case, who does what, handoffs, where it breaks, \
how long, how often;
3. implementation: people and roles, and the technical side: tools by name (ERP, email, Excel, \
portals), data, integrations, volumes.
`expect` lists the concrete details each rung needs. CEOs answer vaguely ("we have an ERP", \
"orders go by email"); a generic statement covers nothing.

{question_style}
{slots_guide}
{maps_guide}
Return:
- question_updates: for the current question if the new lines touch it, and for any other \
question the CEO just answered. Skip questions the lines don't touch. For each:
  - answer_piece: ONLY what the new lines add, 1 or 2 short plain sentences in {lang_name}, \
written as neutral notes of the CEO's facts (figures, names, tools as said), not "il dit". "" if \
the lines add nothing to this question.
  - answer: "" (the app appends the piece to the draft), unless the draft must be corrected: then \
the full corrected draft.
  - evidence: a short corrected quote from the new lines.
  - covered: the `expect` items (any rung) now really provided, copied verbatim, including ones \
already covered.
  - rung: business, workflow or implementation: where the digging is now. Move down only when \
the current rung is mostly covered (or the CEO cannot say more); "done" when implementation is \
covered too.
  - follow_up: ONE short natural follow-up question in {lang_name} for the FDE to ask now, \
digging into the original question on the current rung, aimed at its first missing item. When \
the answer is vague, ask for a concrete recent example, a number, a name or a tool, in the CEO's \
own words, following the wording rules above. Never repeat one from the thread. "" when the \
ladder is done.
  - vague: true when the answer is still generic (no recent example, number, name or tool).
  - status: "partial" until the ladder is done; "answered" only when most details on every rung \
are covered (this locks the question).
- maps: only facts the new lines state plainly (a person and role, who waits for whom, where a file \
lives, a tool by name); usually empty lists. Reuse the ids in <maps>.
- When the current question is covered, aim the follow-up at the phase's map_gaps (in <phase>), \
as a concrete story question.
- coverage: only for themes these lines improved, score 0.0 to 1.0. Empty list if nothing changed.
- noise: the ids (e.g. "57" for a line tagged #57) of new lines that are not part of the \
interview: a TV or radio, nearby voices, off-topic chatter. Do NOT list the gibberish lines of the \
other recogniser (they are filtered already), nor anything the FDE or the CEO says. Usually empty.
Keep the output short: it is shown to the FDE as soon as it arrives.
If the new lines are only the FDE asking, small talk or noise, return empty question_updates.
"""


FAST_CONTEXT_CHARS = 1500


def build_fast_system(state: SessionState, glossary: str, context: str = "") -> str:
    system = FAST_SYSTEM.format(
        client=state.client, glossary=glossary.strip() or "(none)", lang_name=LANG_NAME[state.language],
        language_rule=language_rule(state), streams=describe_streams(state), question_style=QUESTION_STYLE,
        slots_guide=SLOTS_GUIDE.format(slot_list=slot_list(), lang_name=LANG_NAME[state.language]),
        maps_guide=maps_guide(state),
    )
    if context.strip():  # client sheet, FDE notes, pre-plan: lets follow-ups use the client's own terms
        ctx = context.strip()
        if len(ctx) > FAST_CONTEXT_CHARS:
            ctx = ctx[:FAST_CONTEXT_CHARS].rsplit("\n", 1)[0] + "\n…"
        system += ("\nClient background (use it to make follow-ups specific; pre-plan items are hypotheses "
                   f"to check, never facts):\n{ctx}\n")
    return system


def build_fast_user(state: SessionState, new_lines: list[Segment], context: list[Segment]) -> str:
    lang = state.language
    cur = state.current_question
    current = None
    if cur:
        current = {"id": cur.id, "phase": cur.phase, "theme": cur.theme, "original_question": cur.text(lang),
                   "status": cur.status,
                   "rung": cur.rung, "expect": cur.expect_in(lang),
                   "covered": [cur.expect_text(e, lang) for e in cur.covered],
                   "missing_on_rung": [cur.expect_text(e, lang) for e in cur.missing()], "draft_answer": cur.answer,
                   "thread": [f"{t.kind} ({t.rung}): {t.text}" for t in cur.thread[-8:]]}
        if cur.use_case:
            current |= {"use_case": cur.use_case, "missing_slots": cur.missing_slots()}
        elif cur.gathers:
            current["gathers_slots"] = list(cur.gathers)
    others = [
        f"- {q.id} [{q.theme}] ({q.status}) {q.text(lang)}" + (f" | use case: {q.use_case}" if q.use_case else "")
        + (f" | draft: {q.answer[:160]}" if q.answer else "")
        for q in state.queue()
        if not cur or q.id != cur.id
    ]
    themes = ", ".join(f"{t.id}={state.coverage.get(t.id, 0.0):.2f}" for t in state.themes)
    return (
        f"<current_question>\n{json.dumps(current, ensure_ascii=False)}\n</current_question>\n\n"
        f"<other_open_questions>\n{chr(10).join(others) or '(none)'}\n</other_open_questions>\n\n"
        f"<theme_coverage>{themes}</theme_coverage>\n\n"
        f"<phase>{json.dumps(phase_snapshot(state), ensure_ascii=False)}</phase>\n\n"
        f"<maps>\n{json.dumps(maps_snapshot(state), ensure_ascii=False)}\n</maps>\n\n"
        f"<use_cases>\n{json.dumps(use_cases_snapshot(state), ensure_ascii=False)}\n</use_cases>\n\n"
        f"<recent_context note=\"already processed\">\n{render_lines(context) or '(none)'}\n</recent_context>\n\n"
        f"<new_lines>\n{render_lines(new_lines, ids=True)}\n</new_lines>"
    )
