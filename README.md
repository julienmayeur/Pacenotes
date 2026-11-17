# Pacenote

Pace notes for your interviews.

In a rally, the co-driver reads the pace notes so the driver keeps their eyes on the road. Pacenote
is a live interview copilot for consulting and discovery kickoffs: during a 10–15 minute
conversation with someone at a client (its CEO, a function leader or a person who does the work), it listens, keeps the question on screen, captures the answer,
proposes the next follow-up, and writes the integration plan when you stop. You keep your mind on
the conversation.

## How it works

```
prepare (admin) ─► consent ─► interview: capture + follow-ups ─► stop ─► plan.md + plan.json ─► Plan view / PDF
```

- **Capture.** A small Swift helper transcribes the microphone on the Mac with Apple's on-device
  speech recognition, in every spoken language at once (e.g. French and English). The microphone
  opens only when you click Start on the consent screen. Audio is never stored or sent anywhere.
- **Question ladder.** The original question stays on screen; the answer is captured under it,
  piece by piece. Follow-ups dig down a ladder: from the business case (cost, target) to the
  workflow (trigger, people, steps, handoffs) to the implementation (tools by name, access,
  volumes).
- **Follow-ups.** A fast pass a few seconds after each answer proposes one follow-up at a time; a
  full sync every ~40 s updates the answers, the facts still missing and the maps of who carries
  which information. Phases order the interview behind the scenes; the interviewee never hears a form.
- **Admin auto-fill.** Before the interview, the admin panel fills the client sheet from public web
  sources (with provenance per field; your own edits are never overwritten) and prepares profile
  cards of the people you will meet (professional, publicly self-published facts only).
- **Research and pre-study.** A research run writes a short brief and a pre-plan whose use cases
  become hypotheses to check during the interview, each tagged with a horizon (this week, this
  quarter, within two years). It also studies the client's industry: how widely its sector uses
  AI, which competitors adopted it and whether it worked (each signal with its source and date),
  market pressure and how much of the work is documents and re-keying. That gives the interview a
  pace, scored out of 10: explorer (gentle, one visible low-risk step), pragmatic, or urgent
  (competitors already report results). The pace is for you only: you can override it in the admin,
  and nothing the client sees mentions it.
- **Pace and horizons in the interview.** The pace sets the tone of the follow-ups and decides which
  closing questions are asked and how much they matter (what competitors changed, what the company
  could do in two years).
- **Potential ambassadors, asked with tact.** Claude keeps a light read of the person you interview
  (role, how they talk about new tools, what worries them, what they are proud of in their team,
  tone, rapport). For each workflow worth piloting, it writes an indirect question about the people
  behind it, phrased for that person ("When Sage gets in the way, is Claire the one people turn
  to?"), never head-on and never as an assessment. It is softer when the sector is behind and more
  direct when competitors already have results. The question shows under the follow-up, tied to the
  workflow, once that workflow has been walked through (in a sector that is behind, only once rapport
  is there), once per person. Why it is asked and the read stay in your console (`c`). A core
  question asks who tries new tools first and who the others listen to. Each potential ambassador is kept as a
  light label with the interviewee's words, on your Mac only, and you can edit or remove it in the admin
  (your edits are kept; a removed person never comes back).
- **Who you interview.** In the admin, list the people you will meet (name, title, optional notes). Each
  gets a persona, with a short reason and how sure it is: **executive** (runs the company: outcomes,
  value, where time goes, who does what), **function leader** (owns a process and a team: who owns each
  step, tools, figures) or **hands-on operator** (does the work: step by step, volumes, exceptions). It
  comes from the title, the company's size and your notes (the CEO of a 6-person company is close to
  the work; the CEO of 20+ is not), and the research refines it; a small menu changes it. On the consent
  screen you pick who is in front of you. The persona shapes the whole interview: the wording and
  priority of the questions (an operator is never asked about strategy), how deep the follow-ups go, the
  talking points (never for an operator) and the metric questions. When the person can't answer
  something, the copilot doesn't push: it shows "To check with" and who would know, and the plan lists
  those points per person.
- **Guiding the executive on value.** Fifteen minutes with an executive is not weeks with the staff, so
  the copilot helps you steer toward value, workflow by workflow. When the interviewee describes manual work
  (someone opens emails by hand and re-keys them into Excel), a short, tactful talking point shows
  under the follow-up ("Worth saying"): the task brings that person little, it can run on its own, and
  their time goes to the decisions only they can make. It is about the task, never the person; why it
  is suggested now stays in your console. Every workflow also needs a baseline (today, with a number)
  and a success metric (time spent, volume, error rate or delay): when one is missing once the
  workflow was walked through, the question to get it shows under "Also worth asking".
- **Plan and PDF export.** When you stop, Claude writes a factual integration proposal
  (`plan.md` + `plan.json`): current state, bottlenecks, opportunities with one metric each and the
  time each gives back (the task that goes away and the decision the person moves up to), grouped
  under "This week", "This quarter" and "Within two years", an adoption plan in three waves (the
  volunteers keen to go further prove the first use case; their measured results convince most of
  the team; then everyone follows), open items and who to ask. Nothing
  that was not said is presented as a fact. Read it in the Plan view and export it to PDF. Metrics can then be tracked per client, and measured results feed a
  library of reusable recipes.

Model calls go through the Claude Code CLI logged in with a Claude subscription (no API key
needed); only text is sent.

## Requirements

- macOS 26 (Tahoe) or later for live listening. Replay, the tests and the web UI run anywhere
  Python 3.12+ runs.
- Python 3.12+.
- Xcode Command Line Tools (`xcode-select --install`): the speech helper is built once with
  `swiftc` and cached in `~/Library/Caches/pacenote/`.
- The [Claude Code CLI](https://docs.claude.com/en/docs/claude-code), logged in (`claude`, then
  `/login`).
- Optional: a Chrome-family browser for PDF export; Node with the `playwright` package for the
  real-browser tests.

## Install

```bash
git clone https://github.com/julienmayeur/Pacenotes.git && cd Pacenotes
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Set up a client

Client files live in `clients/<slug>/` (git-ignored). The repo ships one fictional example,
`clients.example/brioche-four/` (a bakery equipment distributor). Copy it to try the app:

```bash
mkdir -p clients && cp -r clients.example/brioche-four clients/
pacenote check --client brioche-four
```

A client folder holds:

| File | Purpose |
|---|---|
| `client.yaml` | The client sheet (company, the people you will interview and their persona, your notes) |
| `questionnaire.yaml` | Seed questions, phases and languages |
| `glossary.md` | Expected mishearings (people, products, jargon) → what was said |
| `research.md`, `preplan.md`, `preplan.json` | Written by the research run: the brief, the pre-plan, the pre-study |
| `ambassadors.json` | Potential ambassadors from the interviews, with the interviewee's words (edit them in the admin) |

Start a new one with `pacenote new-client <slug> --name "Company"`, or in the admin panel.

`pacenote check` verifies the client files, builds the speech helper, opens the microphone once per
spoken language (downloading missing speech models) and makes one tiny `claude -p` call. Run it on
good Wi-Fi the day before an interview.

## Configuration

Everything works with defaults. Optional environment variables (names only; see
[docs/architecture.md](docs/architecture.md#configuration) for defaults and details):

| Variable | What it sets |
|---|---|
| `PACENOTE_ENGINE` | `cli` (default), `api` or `offline` |
| `PACENOTE_CLAUDE_BIN` | Path to the Claude Code CLI |
| `PACENOTE_MODEL`, `PACENOTE_FAST_MODEL` | Models for full syncs and the plan / the fast pass |
| `PACENOTE_RESEARCH_MODEL`, `PACENOTE_AUTOFILL_MODEL`, `PACENOTE_PEOPLE_MODEL` | Models for the admin agents |
| `PACENOTE_LOCALES`, `PACENOTE_INTERVIEW_LANGUAGE`, `PACENOTE_UI_LANGUAGE` | Spoken languages, interview language, screen language |
| `PACENOTE_CLIENTS_DIR`, `PACENOTE_SESSIONS_DIR` | Data folders (default `clients/`, `sessions/`) |
| `PACENOTE_HOST`, `PACENOTE_PORT`, `PACENOTE_ADMIN_PORT` | Bind address and ports (default `127.0.0.1`, 8765, 8766) |
| `PACENOTE_ENGINEER_CONTEXT` | One line about your own company, used by profile cards |
| `PACENOTE_CHROME` | Browser used for PDF export |
| `ANTHROPIC_API_KEY` | Only for `--engine api`; the default CLI engine never passes it on |

## Run

```bash
pacenote run --client brioche-four
```

Open http://127.0.0.1:8765. Other languages:

```bash
pacenote run --client acme --locales de-DE,en-US --interview-language de --ui-language en
```

Prepare clients in the admin panel (http://127.0.0.1:8766/admin, also at `/admin` on a running
interview server):

```bash
pacenote admin
```

Replay a saved session from its transcripts instead of the microphone (`--offline` makes no model
calls):

```bash
pacenote replay --client brioche-four --srt fr-FR=sessions/brioche-four/<id>/fr.srt --speed 8 --offline
```

### Keys during the interview

| Key | Action |
|---|---|
| `n` / Enter | Next question |
| `p` | Close the current phase |
| `a` | Mark the current question answered and move on |
| `k` | Skip the current question |
| `s` | Sync now |
| `c` | Console (languages, transcript paste, possible recipes, potential ambassadors, Stop & generate plan) |
| `t` | Transcript tail |
| `f` | Full screen |
| `l` | Next screen language |

### Commands

| Command | What it does |
|---|---|
| `pacenote run` | Live interview (microphone, every spoken language, the UI) |
| `pacenote replay` | Same app, fed from SRT files (`--srt LOCALE=PATH`, `--speed`) |
| `pacenote finalize --session <id>` | (Re)generate the plan of a saved session |
| `pacenote purge [--session <id>]` | Delete session data (one session or all of a client's) |
| `pacenote check` | Pre-flight: client files, speech models and microphone, Claude CLI login |
| `pacenote soak --minutes 2` | Listen for N minutes with the watchdog, no UI or model calls; PASS/FAIL |
| `pacenote new-client <slug> --name "…"` | Scaffold `clients/<slug>/` |
| `pacenote next [<slug> \| --clear]` | Show or set the default client |
| `pacenote admin` | Admin panel only |
| `pacenote capabilities [refresh]` | Show or refresh the dated knowledge briefs the plan writer reads |
| `pacenote metrics seed\|show\|add\|import <slug>` | Track a client's plan metrics over time |
| `pacenote recipes list\|record` | The recipe library and its measured evidence |

`pacenote <command> --help` lists every option.

### Before an interview

1. The day before, on good Wi-Fi: `pacenote check --client <slug>`, then
   `pacenote soak --client <slug> --minutes 2`. First runs download speech models and trigger the
   microphone permission prompt for your terminal app.
2. Add expected mishearings to `clients/<slug>/glossary.md`.
3. In the room: test the microphone (`t`), then `pacenote purge --client <slug>` to clear the test.
4. Second screen in full screen (`f`), Do Not Disturb on, Mac plugged in (`caffeinate -dims`).
5. Read the consent line; click Start only once the interviewee agrees.

## Tests

```bash
pytest
```

The suite runs offline with stand-in `claude` and speech-helper scripts and synthetic transcripts;
it never opens the microphone. Real-browser tests need Node and the `playwright` package
(`NODE_PATH=/path/to/node_modules pytest`) and are skipped without them; the PDF test needs a
Chrome-family browser.

## Privacy

- Audio stays on the Mac: transcription is on-device and no audio is recorded.
- Client and interview data stay local. `clients/` and `sessions/` are git-ignored; only the
  fictional `clients.example/` is in the repo. `pacenote purge` deletes sessions on request.
- Only text (questions, transcript lines, client context) is sent to Claude. `sync_log.jsonl` in each
  session folder is the exact record of what was sent.
- Profile cards keep professional, publicly self-published facts only.
- Potential ambassadors are a name or role, the use case and the interviewee's own words, nothing more.
  They stay in `clients/<slug>/ambassadors.json`, which git ignores wherever it is.
- The read of the interviewee and the people questions stay in the session (`sessions/` is
  git-ignored) and in your console. They are never sent to the plan or shown in it.

## Layout

```
pacenote/          the app (server, session, state, prompts, engines, plan, admin agents, CLI)
  static/          interview, admin and Plan view pages (HTML/CSS/JS, no framework)
  listen/          the on-device speech helper (Swift)
  core/            core questions merged into every questionnaire
  knowledge/       dated briefs and the recipe library
  locales/         UI strings (fr, en)
clients.example/   the fictional example client
docs/              architecture.md
tests/             pytest suite and frozen fixtures
```

## License

MIT. See [LICENSE](LICENSE).
