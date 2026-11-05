# Product approach

What Pacenote is for and why it works the way it does. How it is built is in
[architecture.md](architecture.md). Examples use one fictional company throughout: **Brioche &
Four**, a French distributor of professional bakery equipment (Fornaxa deck ovens, dough mixers)
to independent bakeries, with order intake, stock, delivery and installation, and after-sales
service.

## The problem

Companies struggle with three things: communicating, storing information, and reusing it. The
order desk retypes what a bakery wrote in an email; the after-sales lead keeps the warranty rules
in her head; the CEO learns where a delivery stands by asking someone who opens a spreadsheet.

The span of control (a manager handles 4 to 10 people) exists because communication does not
scale: each extra report adds messages to read, context to carry and questions to answer. AI
removes that limit when it routes to each person only the context they need, distils long threads
into the line that matters to them, and acts on the repetitive steps in between. Humans stay in
the loop only on the high-value decisions, where their experience counts, instead of hunting
through email and documents.

To get there, a forward-deployed engineer (FDE) first has to see where information travels and
gets lost in this company, with names, tools and numbers. The kickoff interview with the CEO is
where that starts.

## Users

- **The FDE** (operator). Runs a 10–15 minute kickoff with the CEO, with the app on a second
  screen. Needs to ask the right next question without reading a form, capture facts with their
  quote, and leave with a plan the team can act on the same week.
- **The CEO** (interviewee). Has heard of AI but does not know how to use it; cannot say what
  success looks like in the abstract or imagine what could be taken off his team's hands. Talks
  readily about last week, his people and what slows the company down. Gives consent before
  anything is transcribed.

## The interview

**Stay on the path of wealth.** Every question is about value: where the company makes and loses
money, what limits its growth, where the signal (information) travels and gets lost, where
expertise is spent on low-value work. The opening asks about capacity ("if tomorrow you signed
thirty more bakeries, who would be swamped first, and on which task?"), and the target comes out of
that discussion ("if this took half the time, how many more orders could you take?"), never from an
abstract "what would success look like in a year?".

**Concrete, on a real case.** Questions and follow-ups ask about the last order, the last
breakdown, last Monday's figures: who did what, in which software or file, how long, how often,
where the information came from and where it went next. No AI vocabulary in anything the CEO
hears.

**One follow-up at a time, down a ladder.** The original question stays on screen; the answer is
captured under it piece by piece; one follow-up digs from the business case (cost, target) to the
workflow (trigger, people, steps, handoffs) to the implementation (tools by name, access, volume).

**Phases are an internal backbone, never a form.** Seven phases order the interview: wealth, who
carries the signal, how it travels, where knowledge lives, where expertise decides, the use-case
deep dives, and the return. Each has a time budget and facts to capture. The FDE sees what is
still missing as a quiet hint to aim the next question; the CEO never hears a checklist. Whatever
the CEO says about a later topic is captured when he says it.

**Enough, then stop.** When the must-have questions are answered and coverage is sufficient, the
screen says so ("C'est parfait, j'ai ce qu'il me faut") and the FDE ends the interview.

## The maps

From the CEO's stories, the app builds a picture of how the company runs today:

- **Who talks to whom**: the people around the CEO, who reports to whom, how much of their time
  goes to judgment versus repetitive glue (chasing status, re-keying, jumping between systems).
- **Tools and channels**: email, phone, fax, supplier and customer portals, shared storage,
  spreadsheets, Slack-like chat, the ERP; who uses each, for what, which one is the source of truth,
  who grants access.
- **Flows**: what passes between people, through which channel, how often, who waits on whom and
  how long, what is confidential.
- **Where information lives**: the inbox, a shared drive, an Excel file, the ERP, or someone's head;
  who finds it and how long it takes.
- **Recurring decisions**: who decides, with what context, time spent catching up versus deciding.

Two CEO patterns get special attention:

- **The CEO as a high-latency blocker.** Work waits on him: a price to approve, a supplier date only
  he received. At Brioche & Four, the purchasing lead waits two days for the CEO to forward the
  Fornaxa pickup date buried in a confidential supplier email.
- **The CEO as a status sink.** To know where things stand he must ask a coworker, who opens a
  spreadsheet. The CEO asks the order desk every morning which installations are late.

## Opportunity shapes

Each opportunity in the plan has a type:

| Shape | What changes | Brioche & Four example |
|---|---|---|
| Route & distil | Each person automatically gets only the part of a message they need | The CEO's confidential supplier email becomes the pickup date and place for Claire in purchasing, without the price negotiation |
| Act on behalf | An agent does the repetitive steps; a person validates | Each bakery order email becomes a draft order in the ERP; the order desk validates it |
| Status without asking | Where things stand reaches the person who needs it | The CEO gets late deliveries and open breakdowns every morning without asking anyone |
| Decision brief | The context for a recurring decision arrives prepared | The weekly replenishment decision arrives with stock, open orders and supplier lead times |
| Capture & find again | Know-how and documents are stored where the next person finds them | The oven warranty rules the after-sales lead knows by heart become a checklist every claim is checked against |

Every opportunity names the decision the human keeps. Humans stay in the loop on high-value
decisions only: unusual orders, prices off the list, a refused warranty claim.

## Metrics first

Value is proven on data, not claimed:

- **Defined**: each opportunity has one metric with a name, unit, how it is measured (data source or
  manual count), cadence and owner.
- **Baselined**: the baseline is a number the CEO said, with its quote ("about forty minutes per
  order"). A number nobody said is never estimated: it becomes an open item with the question to
  ask and whom to ask.
- **Monitored**: after the kickoff, dated measurements go next to the baseline and target, from the
  CLI or the admin page, with a trend.
- **Improved**: a first verified win on about N real cases in a named week, shadow mode before
  anything is sent, kill criteria at weeks 4 and 8, what is explicitly not claimed.

## Recipe library

What worked at one company is reused at the next. A recipe is a tracked, reviewable description
of an opportunity that can recur across companies: the flow (trigger, what the agent does, where
it writes, whom it notifies, what the human decides), the signals that show it fits, the
preconditions and integrations it needs, story-form probes, metric definitions with typical
ranges, the proof plan, and buy / configure / build.

- During the interview, the stories are matched against the library; a follow-up may bring out a
  missing precondition or baseline, always as a story, never naming the recipe.
- The plan says which known recipe fits each opportunity and its gaps, or drafts a new recipe when
  none fits.
- A recipe starts as a **seed** (no evidence). Once an engagement's measured result is recorded as
  evidence (anonymised by default), it becomes **proven**, and the next plan ranks proven recipes
  first.

## Capability briefs

AI capabilities change month to month, so the plan never relies on what a model remembers. It is
written against two dated briefs shipped with the app: what agents can do today (computer use,
connectors, governance, what works in SMEs) and how value is proven in FDE practice. The plan may
not claim a capability absent from the brief, states the brief's date, and the app warns when a
brief is more than 30 days old. Refreshing a brief is one command that researches the web and
rewrites it with sources.

## The plan

The output is a factual integration proposal for the CEO and his team, then for the engineer who
builds it: how the company works today (map, people, flows, tools, stores, decisions), where the
signal is lost, one metric per opportunity, the opportunities ranked recipe-first then
back-office, measurable and high-volume, each wired into the company's real systems through nine
facts (trigger, people, processing, distribution, tool, access, volume, baseline, target), the
engagement timeline (week 1 thin slice, week 4 checkpoint, week 8 go / kill), and every open item
with the question to ask and whom to ask. A fact appears as a fact only when the CEO said it.

## Before the interview

The FDE prepares the client in an admin page: a client sheet filled from public sources with
provenance, a short web research and pre-plan whose use cases become hypotheses to check in the
interview, and profile cards of the people being met (professional, publicly self-published facts
only).

## Privacy

Audio never leaves the FDE's Mac: transcription is on-device and the microphone opens only after
consent. Only text goes to Claude. Client data and session data stay local and out of version
control; sessions can be deleted on request.

## Non-goals

- Not a meeting recorder or note taker: no audio is kept, and the plan is a proposal, not minutes.
- Not a questionnaire the CEO fills in or hears read out.
- Not a delivery tool: it scopes and measures; building the agents happens after.
- No estimated numbers, invented names or capabilities absent from the dated brief.
