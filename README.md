# Pacenote

Pace notes for your interviews.

In a rally, the co-driver reads the pace notes so the driver keeps their mind on the road. Pacenote
does that for a forward-deployed engineer (FDE) running a 10–15 minute kickoff interview with a
client CEO: it drafts the next question and the follow-up, takes the notes and keeps the method,
so the FDE has a free mind to ask something even better.

The app listens on the Mac (Apple's on-device transcription, through a small bundled
helper), keeps the original question on screen, captures the answer under it piece by piece, and
proposes one follow-up at a time, from the business case down to the workflow and the
implementation (people, tools, data, volumes). At the end it writes a factual integration proposal
(`plan.md` + `plan.json`), read in the app's Plan view and exported to PDF.

```
consent ─► interview (fast pass every answer, full sync every ~40 s) ─► « C'est parfait… » ─► plan.md + plan.json ─► Plan view / PDF
```

- Audio never leaves the Mac; only text goes to Claude, through the Claude Code CLI logged in with
  a Claude subscription (no API key).
- An admin page prepares each client (sheet filled from the web, research and pre-plan, profile
  cards of the people met).
- The plan's metrics are monitored after the kickoff, and measured results feed a recipe library
  reused at the next company.

Docs:

- [docs/prd.md](docs/prd.md): the product approach (problem, interview, maps, opportunities,
  metrics, recipes).
- [docs/architecture.md](docs/architecture.md): components, data on disk, the live loop, the sync
  contract and its rules, finalize and the feedback loop, configuration.

## Requirements

- macOS 26 (Tahoe) or later for live listening; replay, the tests and the web UI run anywhere
  Python 3.12+ runs.
- Python 3.12+.
- Xcode Command Line Tools (`xcode-select --install`): `pacenote check` builds the speech helper with
  `swiftc` once, cached in `~/Library/Caches/pacenote/`.
- The Claude Code CLI, logged in with your Claude subscription (`claude`, then `/login`).

## Install

```bash
python3.12 -m venv .venv && source .venv/bin/activate
```

```bash
pip install -e ".[dev]"
```

Client data never goes to git: `clients/` and `sessions/` are git-ignored. The repo ships one
fictional example client, `clients.example/brioche-four/` (a bakery equipment distributor). Copy it
to try the app:

```bash
mkdir -p clients && cp -r clients.example/brioche-four clients/
```

```bash
pacenote check --client brioche-four
```

`pacenote check` verifies the client files, builds the speech helper, starts listening once for every
spoken language (downloading a missing speech model, opening the microphone), and makes one tiny
`claude -p "reply ok"` call. Run it on good Wi-Fi the day before an interview.

## Run

```bash
pacenote run --client brioche-four
```

The server starts on http://127.0.0.1:8765. The microphone opens only when you click Start on the
consent screen. Other languages:

```bash
pacenote run --client acme --locales de-DE,en-US --interview-language de --ui-language en
```

Replay a saved session's transcripts instead of the microphone (`--offline` = no model calls):

```bash
pacenote replay --client brioche-four --srt fr-FR=sessions/brioche-four/<id>/fr.srt --srt en-US=sessions/brioche-four/<id>/en.srt --speed 8 --offline
```

Prepare clients in the admin panel (http://127.0.0.1:8766/admin; also at `/admin` on a running
interview server):

```bash
pacenote admin
```

### Keys during the interview

| Key | Action |
|---|---|
| `n` / Enter | Next question (or the next phase's first one when this phase is done) |
| `p` | Close the current phase |
| `a` | Mark the current question answered and move on |
| `k` | Skip the current question (no answer yet) |
| `s` | Sync now |
| `c` | FDE console (languages, Wispr paste, possible recipes, Stop & generate plan) |
| `t` | Transcript tail |
| `f` | Full screen |
| `l` | Next display language |

## CLI

| Command | What it does |
|---|---|
| `pacenote run` | Live interview (microphone, every spoken language, the UI) |
| `pacenote replay` | Same app, fed from SRT files (`--srt LOCALE=PATH`, `--speed`) |
| `pacenote finalize --session <id> [--wispr file] [--keep-previous]` | (Re)generate the plan of a saved session |
| `pacenote purge [--session <id>]` | Delete session data (one session or all of a client's) |
| `pacenote check` | Pre-flight: client files, speech models and microphone, Claude CLI login |
| `pacenote soak --minutes 2` | Keep listening for N minutes with the watchdog, no UI or model calls; PASS/FAIL |
| `pacenote new-client <slug> --name "…"` | Scaffold `clients/<slug>/` |
| `pacenote next [<slug> \| --clear]` | Show or set the next client (default when `--client` is not given) |
| `pacenote admin` | Admin panel only |
| `pacenote capabilities [refresh [--brief all]]` | Show the dated knowledge briefs, or refresh them with a web-research run |
| `pacenote metrics seed\|show\|add\|import <slug> …` | Metrics from the plan, dated measurements, trend |
| `pacenote recipes list` / `pacenote recipes record <slug> <recipe_id>` | The recipe library; record a client's measured result as evidence |

`python -m pacenote …` works the same. `pacenote <command> --help` lists every option; environment
variables are in [docs/architecture.md](docs/architecture.md#configuration).

## Before walking into the room

1. The day before, on good Wi-Fi: `pacenote check --client <slug>` twice, then
   `pacenote soak --client <slug> --minutes 2`. First runs download speech models and trigger the
   microphone prompt for your terminal app.
2. Add expected mishearings (people, software, product names) to `clients/<slug>/glossary.md`.
3. In the room: `pacenote run`, test the mic (press `t`), stop, `pacenote purge --client <slug>`.
4. Second screen facing the CEO, full screen (`f`), Do Not Disturb, Mac plugged in
   (`caffeinate -dims`).
5. Read the consent line, click Start only after the CEO agrees.

## Tests

```bash
pytest
```

Real-browser tests need Node and the `playwright` package (`NODE_PATH=/path/to/node_modules pytest`)
and are skipped without them.

## Layout

```
pacenote/          the app (server, session, state, prompts, engines, plan, admin agents, CLI)
  static/             interview, admin and Plan view pages (HTML/CSS/JS, no framework)
  listen/             pacenote_listen.swift: the on-device speech helper
  core/               questions.yaml: core questions merged into every questionnaire
  knowledge/          dated briefs and recipes/<id>.yaml, the recipe library
  locales/            fr.json, en.json, shared with the browser
clients.example/      the fictional example client
docs/                 prd.md, architecture.md
tests/                pytest suite; synthetic SRTs in srt_samples.py, frozen questionnaires in fixtures/
```

## License

MIT. See [LICENSE](LICENSE).
