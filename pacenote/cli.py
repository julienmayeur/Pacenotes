"""Command line: run (live), replay, finalize, purge, new-client, check, capabilities, metrics, recipes."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import sys
import webbrowser
from pathlib import Path

from . import briefs, i18n, speech
from . import metrics as metrics_lib
from . import recipes as recipe_lib
from .capture import CaptureError, ListenCapture, ReplaySource, ensure_helper, locales_map, preflight
from .autofill import DEFAULT_AUTOFILL_MODEL
from .people import DEFAULT_PEOPLE_MODEL
from .finalize import archive_plan, read_wispr
from .llm import DEFAULT_FAST_MODEL, DEFAULT_MODEL, ENGINES, ClaudeCliEngine, ClaudeCliNotFound, SyncFailed, find_claude, make_engine
from .clients import (ClientError, create_client, get_next_client, list_slugs, load_info, resolve_client,
                      set_next_client)
from .questionnaire import load_client
from .session import Session, Settings, new_session_dir, purge

ROOT = Path.cwd()


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--client", default=None,
                   help="client slug (folder under --clients-dir); default: the next client set in /admin")
    p.add_argument("--clients-dir", type=Path, default=Path(os.environ.get("PACENOTE_CLIENTS_DIR", "clients")))
    p.add_argument("--sessions-dir", type=Path, default=Path(os.environ.get("PACENOTE_SESSIONS_DIR", "sessions")))


def _backend_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument("--speech-backend", choices=speech.BACKENDS, default=os.environ.get("PACENOTE_SPEECH_BACKEND", "auto"),
                   help="speech-to-text: auto (default: Apple on-device on macOS, with whisper.cpp as an automatic "
                        "fallback when PACENOTE_WHISPER_MODEL is set; whisper.cpp elsewhere), apple, or whisper")


def _locale_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument("--locales", default=os.environ.get("PACENOTE_LOCALES"),
                   help="spoken languages = speech locales, comma-separated, primary first (e.g. de-DE,en-US; "
                        "default: the questionnaire's speech_locales, else fr-FR,en-US)")


def _language_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument("--interview-language", default=os.environ.get("PACENOTE_INTERVIEW_LANGUAGE"),
                   help="language of the questions, follow-ups, summaries and plan (default: the "
                        "questionnaire's language); missing variants are translated by Claude")
    p.add_argument("--ui-language", default=os.environ.get("PACENOTE_UI_LANGUAGE"),
                   help="FDE screen language (default: the client's language); switchable live with `l`")


def _engine_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument("--engine", choices=ENGINES, default=os.environ.get("PACENOTE_ENGINE", "cli"),
                   help="cli (default): Claude Code CLI on your subscription, no API key; "
                        "api: Anthropic API (ANTHROPIC_API_KEY); offline: keyword heuristics")
    p.add_argument("--offline", action="store_true", help="shortcut for --engine offline")
    p.add_argument("--claude-bin", default=None,
                   help="path to the claude CLI (default: PACENOTE_CLAUDE_BIN, then `claude` on PATH)")
    p.add_argument("--model", default=os.environ.get("PACENOTE_MODEL", DEFAULT_MODEL))
    p.add_argument("--effort", default=os.environ.get("PACENOTE_SYNC_EFFORT", "low"),
                   choices=["low", "medium", "high", "xhigh", "max"], help="effort for live syncs")
    p.add_argument("--plan-effort", default=os.environ.get("PACENOTE_PLAN_EFFORT", "high"),
                   choices=["low", "medium", "high", "xhigh", "max"], help="effort for the final plan")
    p.add_argument("--no-fallbacks", action="store_true",
                   help="--engine api only: disable server-side refusal fallbacks")
    p.add_argument("--fast-model", default=os.environ.get("PACENOTE_FAST_MODEL", DEFAULT_FAST_MODEL),
                   help="model for the fast incremental pass (default claude-haiku-5-5)")


def _server_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument("--host", default=os.environ.get("PACENOTE_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PACENOTE_PORT", "8765")))
    p.add_argument("--interval", type=float, default=_env_float("PACENOTE_SYNC_INTERVAL", 40.0),
                   help="seconds between full syncs (default 40; 30-45 recommended)")
    p.add_argument("--first-sync", type=float, default=_env_float("PACENOTE_FIRST_SYNC_DELAY", 15.0),
                   help="first full sync this many seconds after the first speech (default 15)")
    p.add_argument("--fast-debounce", type=float, default=_env_float("PACENOTE_FAST_DEBOUNCE", 2.5),
                   help="fast pass this many seconds after the last new transcript line (default 2.5)")
    p.add_argument("--no-fast", action="store_true", help="disable the fast incremental pass")
    p.add_argument("--no-browser", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="pacenote", description="Pacenote: pace notes for your interviews")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="live interview: one listening process for every spoken language "
                                     "(fr-FR + en-US by default) and the UI")
    _common(run); _engine_opts(run); _server_opts(run); _locale_opts(run); _language_opts(run); _backend_opts(run)
    run.add_argument("--listen-bin", default=None,
                     help="path to a pacenote-listen build (default: built from the bundled Swift source, cached)")
    run.add_argument("--skip-speech-check", "--skip-yap-check", dest="skip_speech_check", action="store_true",
                     help="don't verify the spoken languages and the microphone before serving (not recommended)")

    rp = sub.add_parser("replay", help="same app, fed from saved SRT files instead of the mic")
    _common(rp); _engine_opts(rp); _server_opts(rp); _language_opts(rp)
    rp.add_argument("--fr", type=Path, help="SRT from the fr-FR instance")
    rp.add_argument("--en", type=Path, help="SRT from the en-US instance")
    rp.add_argument("--srt", action="append", default=[], metavar="LOCALE=PATH",
                    help="SRT of any speech locale, e.g. --srt de-DE=de.srt (repeatable)")
    rp.add_argument("--speed", type=float, default=1.0, help="1 = real time, 10 = 10x, 0 = all at once")
    rp.add_argument("--auto-start", action="store_true", help="skip the consent screen")

    fin = sub.add_parser("finalize", help="(re)generate the plan for a saved session")
    _common(fin); _engine_opts(fin)
    fin.add_argument("--session", required=True, help="session id (folder name) or path")
    fin.add_argument("--wispr", type=Path, help="final Wispr Flow transcript (txt/md/srt) to merge")
    fin.add_argument("--keep-previous", action="store_true",
                     help="keep the existing plan as plan-vN.md / .html / .json before regenerating it")

    pg = sub.add_parser("purge", help="delete session data (transcripts, SRTs, state, plans)")
    pg.add_argument("--client", required=True)
    pg.add_argument("--sessions-dir", type=Path, default=Path(os.environ.get("PACENOTE_SESSIONS_DIR", "sessions")))
    pg.add_argument("--session", help="only this session id (default: all sessions of the client)")
    pg.add_argument("--yes", action="store_true", help="don't ask for confirmation")

    sk = sub.add_parser("soak", help="keep listening (every spoken language) for N minutes and report (no UI, no model calls)")
    sk.add_argument("--client", default=None, help="default: the next client")
    sk.add_argument("--clients-dir", type=Path, default=Path(os.environ.get("PACENOTE_CLIENTS_DIR", "clients")))
    sk.add_argument("--sessions-dir", type=Path, default=Path(os.environ.get("PACENOTE_SESSIONS_DIR", "sessions")))
    sk.add_argument("--minutes", type=float, default=2.0)
    sk.add_argument("--listen-bin", default=None)
    _locale_opts(sk)
    sk.add_argument("--every", type=float, default=15.0, help="status line interval in seconds")

    nc = sub.add_parser("new-client", help="scaffold clients/<slug>/ from the template")
    nc.add_argument("slug")
    nc.add_argument("--name", required=True, help="client display name")
    nc.add_argument("--language", default="fr", help="interview language: fr, en, de, es… (any language code)")
    nc.add_argument("--clients-dir", type=Path, default=Path(os.environ.get("PACENOTE_CLIENTS_DIR", "clients")))

    nx = sub.add_parser("next", help="show or set the next client (used when --client is not given)")
    nx.add_argument("slug", nargs="?", help="client to mark as next; omit to show the current one")
    nx.add_argument("--clear", action="store_true", help="forget the next client")
    nx.add_argument("--clients-dir", type=Path, default=Path(os.environ.get("PACENOTE_CLIENTS_DIR", "clients")))

    ad = sub.add_parser("admin", help="admin panel only (clients, research, pre-plan), no interview")
    ad.add_argument("--clients-dir", type=Path, default=Path(os.environ.get("PACENOTE_CLIENTS_DIR", "clients")))
    ad.add_argument("--sessions-dir", type=Path, default=Path(os.environ.get("PACENOTE_SESSIONS_DIR", "sessions")),
                    help="saved interviews, listed per client with their Plan view")
    ad.add_argument("--host", default=os.environ.get("PACENOTE_HOST", "127.0.0.1"))
    ad.add_argument("--port", type=int, default=int(os.environ.get("PACENOTE_ADMIN_PORT", "8766")))
    ad.add_argument("--claude-bin", default=None)
    ad.add_argument("--research-model", default=os.environ.get("PACENOTE_RESEARCH_MODEL", DEFAULT_MODEL))
    ad.add_argument("--autofill-model", default=os.environ.get("PACENOTE_AUTOFILL_MODEL", DEFAULT_AUTOFILL_MODEL),
                    help="model of the client-sheet Auto-fill agent (default: Sonnet, fast web lookup)")
    ad.add_argument("--people-model", default=os.environ.get("PACENOTE_PEOPLE_MODEL", DEFAULT_PEOPLE_MODEL),
                    help="model of the interviewee research agent (default: Sonnet)")
    ad.add_argument("--no-browser", action="store_true")

    cap = sub.add_parser("capabilities", help="the dated knowledge briefs the plan is written with: show their "
                                              "date, or refresh them with a web-research Claude run")
    cap.add_argument("action", choices=("show", "refresh"), nargs="?", default="show")
    cap.add_argument("--brief", choices=(*briefs.BRIEFS, "all"), default="capabilities",
                     help="which brief to refresh: capabilities (default), fde-practice, or all")
    cap.add_argument("--model", default=os.environ.get("PACENOTE_RESEARCH_MODEL", DEFAULT_MODEL))
    cap.add_argument("--claude-bin", default=None)
    cap.add_argument("-v", "--verbose", action="store_true")

    mt = sub.add_parser("metrics", help="monitor a client's metrics: seed them from the plan, add or import "
                                        "dated measurements, show the trend against baseline and target")
    msub = mt.add_subparsers(dest="action", required=True)

    def _dirs(p: argparse.ArgumentParser) -> None:
        p.add_argument("slug", help="client slug (folder under --clients-dir)")
        p.add_argument("--clients-dir", type=Path, default=Path(os.environ.get("PACENOTE_CLIENTS_DIR", "clients")))
        p.add_argument("--sessions-dir", type=Path, default=Path(os.environ.get("PACENOTE_SESSIONS_DIR", "sessions")))

    m_seed = msub.add_parser("seed", help="metric definitions from the latest plan (measurements are kept)")
    _dirs(m_seed)
    m_seed.add_argument("--session", default=None, help="session id (or folder) whose plan.json to use")
    _dirs(msub.add_parser("show", help="each metric: baseline, target, last value, trend"))
    m_add = msub.add_parser("add", help="add one dated measurement")
    _dirs(m_add)
    m_add.add_argument("metric_id")
    m_add.add_argument("value")
    m_add.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    m_add.add_argument("--note", default="")
    m_add.add_argument("--source", default="manual", help="manual (default), csv, or the system it was read from")
    m_imp = msub.add_parser("import", help="add measurements from a CSV (metric_id,value[,date,source,note])")
    _dirs(m_imp)
    m_imp.add_argument("file", type=Path)

    rc = sub.add_parser("recipes", help="the recipe library: list the recipes, or record an engagement's "
                                        "measured result as evidence (seed -> proven)")
    rsub = rc.add_subparsers(dest="action", required=True)
    rsub.add_parser("list", help="every recipe with its status and evidence count")
    r_rec = rsub.add_parser("record", help="append this client's baseline and latest result to a recipe")
    r_rec.add_argument("slug", help="client slug")
    r_rec.add_argument("recipe_id")
    r_rec.add_argument("--metric", default=None, help="the metric to record (default: every metric tied to the recipe)")
    r_rec.add_argument("--client-name", action="store_true",
                       help="name the client in the evidence (default: an anonymised label)")
    r_rec.add_argument("--clients-dir", type=Path, default=Path(os.environ.get("PACENOTE_CLIENTS_DIR", "clients")))

    ck = sub.add_parser("check", help="pre-flight: client files, speech models + microphone, Claude CLI login (or API key)")
    _common(ck); _locale_opts(ck); _backend_opts(ck)
    ck.add_argument("--listen-bin", default=None)
    ck.add_argument("--no-speech-test", "--no-yap-test", dest="no_speech_test", action="store_true",
                    help="only build/find the speech helper, don't start listening")
    ck.add_argument("--engine", choices=ENGINES, default=os.environ.get("PACENOTE_ENGINE", "cli"))
    ck.add_argument("--claude-bin", default=None)
    ck.add_argument("--model", default=os.environ.get("PACENOTE_MODEL", DEFAULT_MODEL))
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        if args.cmd in ("run", "replay", "finalize", "check", "soak"):
            args.client = resolve_client(args.client, args.clients_dir)  # --client, else the next client
        return _dispatch(args)
    except (ClaudeCliNotFound, ClientError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _dispatch(args) -> int:
    return {
        "run": cmd_run,
        "replay": cmd_replay,
        "finalize": cmd_finalize,
        "purge": cmd_purge,
        "new-client": cmd_new_client,
        "check": cmd_check,
        "next": cmd_next,
        "admin": cmd_admin,
        "soak": cmd_soak,
        "capabilities": cmd_capabilities,
        "metrics": cmd_metrics,
        "recipes": cmd_recipes,
    }[args.cmd](args)


def _brief_line(b) -> str:
    """One status line for a knowledge brief (briefs.py), flagged when older than STALE_DAYS."""
    age = b.age_days()
    line = f"[{'!!' if b.stale() else 'ok'}] {b.name} brief: as of {b.as_of_text}"
    line += " (no date)" if age is None else f" ({age} days old)"
    if b.stale():
        line += " — refresh it: pacenote capabilities refresh" + ("" if b.name == "capabilities" else f" --brief {b.name}")
    return f"{line} · {b.path}"


def cmd_capabilities(args) -> int:
    names = list(briefs.BRIEFS) if args.action == "show" or args.brief == "all" else [args.brief]
    if args.action == "show":
        for name in names:
            b = briefs.load_brief(name)
            print(_brief_line(b) if b else f"[!!] {name}: missing ({briefs.knowledge_dir() / briefs.BRIEFS[name]})")
        return 0
    ok = True
    for name in names:
        print(f"[..] refreshing {name} with {args.model} (web search and fetch only; a few minutes)…", flush=True)

        def step(entry: dict) -> None:
            if entry.get("kind") in ("search", "read", "error"):
                print(f"     {entry.get('kind')}: {entry.get('text', '')[:120]}", flush=True)

        try:
            b = briefs.refresh_brief(name, claude_bin=args.claude_bin, model=args.model, on_step=step)
        except (ClaudeCliNotFound, RuntimeError, ValueError) as exc:
            ok = False
            print(f"[!!] {name}: refresh failed, the brief is unchanged: {exc}")
            continue
        print(f"[ok] {name}: rewritten as of {b.as_of_text}, {len(b.sources)} sources · {b.path}")
    return 0 if ok else 1


def _engine_name(args) -> str:
    return "offline" if args.offline else args.engine


def _engine(args):
    name = _engine_name(args)
    if name == "api" and not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        print("warning: --engine api needs ANTHROPIC_API_KEY; API calls will fail "
              "(the default --engine cli uses your Claude subscription instead)", file=sys.stderr)
    if name == "cli":
        find_claude(args.claude_bin)  # fail fast if the CLI is missing
    return make_engine(name, args.model, args.effort, args.plan_effort, not args.no_fallbacks, args.claude_bin,
                       args.fast_model)


def _settings(args) -> Settings:
    return Settings(sync_interval=args.interval, first_sync_delay=args.first_sync,
                    fast_debounce=args.fast_debounce, fast_enabled=not args.no_fast)


def speech_locales(args, client=None) -> list[str]:
    """--locales / PACENOTE_LOCALES > the questionnaire's speech_locales > fr-FR + en-US."""
    if getattr(args, "locales", None):
        return i18n.normalize_speech(args.locales)
    if client is not None and client.questionnaire.speech_locales:
        return list(client.questionnaire.speech_locales)
    return list(i18n.DEFAULT_SPEECH)


def check_speech(slug: str, locales: list[str] | None = None, helper_bin: str | None = None,
                 microphone: bool = True, report: dict | None = None) -> bool:
    """capture.preflight: every speech model reserved, installed and loaded; with microphone=True
    (`pacenote check`) the microphone is opened too, exactly like the interview does, then stopped.
    `pacenote run` passes microphone=False: nothing listens before the Start button. Prints the cause
    and the fix on failure. True when at least one language is ready (a language that is not
    ready is reported in `report["failed"]`; the interview starts without it)."""
    locales = list(locales or locales_map(None).values())
    how = "one listening process" if microphone else "models only, the microphone stays off until Start"
    print(f"Checking speech for {' + '.join(locales)} ({how})…", flush=True)
    report = {} if report is None else report
    try:
        secs = preflight(locales, helper_bin=helper_bin, on_progress=lambda m: print(m, flush=True), slug_hint=slug,
                         microphone=microphone, report=report)
    except CaptureError as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return False
    ready = report.get("ready") or locales
    print(f"  ready: {', '.join(ready)} ({secs:.1f}s)", flush=True)
    for loc, why in (report.get("failed") or {}).items():
        print(f"  warning: {loc} is not ready ({why}); the interview starts without it, with a banner on screen",
              file=sys.stderr, flush=True)
    return True


def _serve(session: Session, args, auto_start: bool) -> int:
    import uvicorn

    from .server import create_app

    if session.on_plan_written is None:  # the plan is written from the UI: say where, absolute
        session.on_plan_written = lambda files: print(_plan_written(files), flush=True)
    app = create_app(session, auto_start=auto_start, bind_host=args.host, claude_bin=getattr(args, "claude_bin", None))
    url = f"http://{args.host}:{args.port}/"
    print(f"\n  FDE kickoff — {session.state.client}\n  UI:       {url}\n  Admin:    {url}admin\n"
          f"  Session:  {session.session_dir}\n"
          f"  Engine:   {session.engine.name}\n  Stop:     Ctrl-C (stops listening and saves)\n")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def _plan_written(files: dict[str, str]) -> str:
    """Where the plan is (plan.md + plan.json); it is read and exported to PDF in the app's Plan view."""
    return ("\n  Plan written:\n" + "".join(f"    {files[k]}\n" for k in ("md", "json") if files.get(k))
            + "  Plan view: /plan on the interview screen, or `pacenote admin` → the client → Sessions\n")


def _apply_languages(session: Session, args, speech: list[str] | None = None) -> None:
    try:
        session.state.set_languages(ui=args.ui_language, interview=args.interview_language, speech=speech)
    except i18n.LanguageError as exc:
        raise SystemExit(f"error: {exc}") from exc
    session.persist()


def cmd_run(args) -> int:
    client = load_client(args.clients_dir, args.client)
    say = lambda m: print(m, flush=True)  # noqa: E731
    binary = None
    try:
        locales = speech_locales(args, client)
        plan = speech.select(args.speech_backend)
        if plan.primary == "apple":
            try:
                binary = ensure_helper(args.listen_bin, on_progress=say, slug_hint=args.client)
            except CaptureError as exc:
                if not plan.fallback:
                    raise
                print(f"warning: {str(exc).splitlines()[0]}; using the whisper.cpp fallback", file=sys.stderr)
                plan = speech.Plan("whisper")
    except CaptureError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except i18n.LanguageError as exc:
        print(f"error: --locales: {exc}", file=sys.stderr)
        return 2
    engine = _engine(args)
    # warm-up without the microphone: listening (and the transcript) only starts on the Start button
    if plan.primary == "apple" and not args.skip_speech_check and not check_speech(args.client, locales, binary,
                                                                                   microphone=False):
        if not plan.fallback:
            print(f"Not starting: no spoken language ({', '.join(locales)}) is ready. Fix the problem above, then run "
                  "`pacenote run` again.", file=sys.stderr)
            return 3
        print("warning: Apple speech is not ready; the interview will use the whisper.cpp fallback", file=sys.stderr)
        plan = speech.Plan("whisper")
    print(f"Speech: {plan.primary}" + (f" (automatic fallback: {plan.fallback})" if plan.fallback else ""), flush=True)
    sdir = new_session_dir(args.sessions_dir, args.client)
    session = Session(
        client=client,
        session_dir=sdir,
        engine=engine,
        source_factory=lambda d: speech.make_source(plan, d, locales, helper_bin=binary, on_progress=say,
                                                    slug_hint=args.client),
        settings=_settings(args),
    )
    _apply_languages(session, args, locales)
    return _serve(session, args, auto_start=False)


def cmd_replay(args) -> int:
    client = load_client(args.clients_dir, args.client)
    try:
        locs = {"fr-FR": args.fr, "en-US": args.en} | {
            i18n.speech_locale(k): Path(v) for k, _, v in (x.partition("=") for x in args.srt)}
        locs = {k: v for k, v in locs.items() if v}
    except i18n.LanguageError as exc:
        print(f"error: --srt: {exc} (expected LOCALE=PATH, e.g. de-DE=de.srt)", file=sys.stderr)
        return 2
    if not locs:
        print("error: give the SRT files to replay: --fr X --en Y, or --srt de-DE=X --srt en-US=Y", file=sys.stderr)
        return 2
    files = {sid: locs[loc] for sid, loc in i18n.stream_ids(locs).items()}
    sdir = new_session_dir(args.sessions_dir, args.client)
    for p in files.values():
        if not p.exists():
            print(f"error: {p} not found", file=sys.stderr)
            return 2

    def factory(d: Path):
        # keep a copy of the input SRTs alongside the session, like a live run would
        for stream, src in files.items():
            shutil.copyfile(src, d / f"{stream}.srt")
        return ReplaySource(files, speed=args.speed)

    session = Session(client=client, session_dir=sdir, engine=_engine(args), source_factory=factory,
                      settings=_settings(args))
    _apply_languages(session, args, list(locs))
    return _serve(session, args, auto_start=args.auto_start)


def cmd_finalize(args) -> int:
    client = load_client(args.clients_dir, args.client)
    sdir = Path(args.session)
    if not sdir.exists():
        sdir = args.sessions_dir / args.client / args.session
    if not (sdir / "state.json").exists():
        print(f"error: no session at {sdir}", file=sys.stderr)
        return 2
    session = Session.resume(client, sdir, _engine(args))
    if args.keep_previous:
        for path in archive_plan(sdir):
            print(f"  kept the previous plan as {Path(path).resolve()}")
    if args.wispr:
        session.wispr_text = read_wispr(args.wispr)
        (sdir / "wispr.txt").write_text(session.wispr_text, encoding="utf-8")
        session.state.wispr_merged = True
    files = asyncio.run(session.finalize())
    print(_plan_written(files).strip("\n"))
    if session.state.last_sync_error:
        print(f"note: {session.state.last_sync_error}", file=sys.stderr)
    return 0


def _client_folder(args) -> bool:
    if args.slug not in list_slugs(args.clients_dir):
        print(f"error: no client {args.slug!r} under {args.clients_dir}", file=sys.stderr)
        return False
    return True


def cmd_metrics(args) -> int:
    if not _client_folder(args):
        return 2
    try:
        if args.action == "seed":
            f, added, refreshed = metrics_lib.seed(args.clients_dir, args.sessions_dir, args.slug, args.session)
            print(f"seeded from {f.seeded_from}: {len(added)} new ({', '.join(added) or '-'}), "
                  f"{len(refreshed)} refreshed ({', '.join(refreshed) or '-'}) · version {f.version}")
            print(metrics_lib.show_text(f))
        elif args.action == "show":
            print(metrics_lib.show_text(metrics_lib.load(args.clients_dir, args.slug)))
        elif args.action == "add":
            f = metrics_lib.add_measurement(args.clients_dir, args.slug, args.metric_id, args.value, when=args.date,
                                            source=args.source, note=args.note)
            print(f"added to {args.metric_id} · version {f.version}")
            print(metrics_lib.show_text(f))
        elif args.action == "import":
            f, added, skipped = metrics_lib.import_csv(args.clients_dir, args.slug, args.file)
            print(f"imported {added} measurement(s), skipped {skipped} already there · version {f.version}")
    except metrics_lib.MetricsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


def cmd_recipes(args) -> int:
    try:
        if args.action == "list":
            lib = recipe_lib.load_library()
            if not lib:
                print(f"no recipes in {recipe_lib.recipes_dir()}")
            for r in lib.values():
                print(f"{r.id:<28} {r.status:<6} evidence {len(r.evidence)}  {r.type:<22} {r.title.en}")
            return 0
        if not _client_folder(args):
            return 2
        label = (load_info(args.clients_dir, args.slug).name or args.slug) if args.client_name \
            else metrics_lib.anonymised_label(args.slug)
        f = metrics_lib.load(args.clients_dir, args.slug)
        entries = [recipe_lib.Evidence.model_validate(e)
                   for e in metrics_lib.evidence_for(f, args.recipe_id, label, args.metric)]
        r = recipe_lib.record_evidence(args.recipe_id, entries)
    except (recipe_lib.RecipeError, metrics_lib.MetricsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for e in entries:
        print(f"recorded for {e.client}: {e.metric}: {e.baseline} -> {e.result}")
    print(f"{r.id}: {r.status}, {len(r.evidence)} evidence entr{'y' if len(r.evidence) == 1 else 'ies'} · "
          f"{recipe_lib.recipes_dir() / (r.id + '.yaml')}")
    return 0


def cmd_purge(args) -> int:
    target = args.sessions_dir / args.client / (args.session or "")
    if not target.exists():
        print(f"nothing to purge at {target}")
        return 0
    if not args.yes:
        reply = input(f"Permanently delete {target} (transcripts, SRTs, state, plans)? [y/N] ")
        if reply.strip().lower() not in ("y", "yes", "o", "oui"):
            print("aborted")
            return 1
    for p in purge(args.sessions_dir, args.client, args.session):
        print(f"deleted {p}")
    return 0


def cmd_new_client(args) -> int:
    try:
        create_client(args.clients_dir, args.slug, args.name, args.language)
    except ClientError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    d = args.clients_dir / args.slug
    print(f"created {d}/questionnaire.yaml, {d}/glossary.md and {d}/client.yaml")
    return 0


def cmd_next(args) -> int:
    if args.clear:
        set_next_client(None)
        print("next client cleared")
        return 0
    if args.slug:
        if args.slug not in list_slugs(args.clients_dir):
            print(f"error: no client {args.slug!r} under {args.clients_dir}", file=sys.stderr)
            return 2
        set_next_client(args.slug)
        print(f"next client: {args.slug}")
        return 0
    print(get_next_client() or "(no next client set)")
    return 0


def cmd_admin(args) -> int:
    import uvicorn

    from .admin import create_admin_app

    app = create_admin_app(args.clients_dir, claude_bin=args.claude_bin, model=args.research_model,
                           sessions_dir=args.sessions_dir, autofill_model=args.autofill_model,
                           people_model=args.people_model, bind_host=args.host)
    url = f"http://{args.host}:{args.port}/admin"
    print(f"\n  FDE kickoff — administration\n  Admin: {url}\n  Clients: {args.clients_dir.resolve()}\n")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_soak(args) -> int:
    """Live stability test: start listening (fr-FR + en-US by default) exactly like `pacenote run`,
    keep them running for --minutes with the watchdog on, print a status line every --every
    seconds, then PASS/FAIL."""
    import time as _time
    from datetime import datetime

    try:
        binary = ensure_helper(args.listen_bin, on_progress=lambda m: print(m, flush=True), slug_hint=args.client)
    except CaptureError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        client = load_client(args.clients_dir, args.client)
    except (FileNotFoundError, ValueError):
        client = None
    try:
        streams = locales_map(speech_locales(args, client))
    except i18n.LanguageError as exc:
        print(f"error: --locales: {exc}", file=sys.stderr)
        return 2
    sdir = args.sessions_dir / args.client / f"soak-{datetime.now():%Y%m%d-%H%M%S}"
    cap = ListenCapture(sdir, dict(streams), binary, on_progress=lambda m: print(m, flush=True),
                        slug_hint=args.client)
    names = " and ".join(i18n.english_name(x) for x in streams.values())
    print(f"Starting {' + '.join(streams.values())} (speak {names} now and then)…", flush=True)
    try:
        cap.start()
    except CaptureError as exc:
        print(f"\nFAIL: {exc}", file=sys.stderr)
        return 1
    lines = {k: 0 for k in streams}
    started = _time.monotonic()
    next_report = started + args.every
    try:
        while _time.monotonic() - started < args.minutes * 60:
            for note in cap.maintain():
                print(f"  watchdog: {note}", flush=True)
            for cue in cap.poll():
                lines[cue.stream] = lines.get(cue.stream, 0) + 1
            if _time.monotonic() >= next_report:
                print(_soak_line(cap, lines, _time.monotonic() - started), flush=True)
                next_report += args.every
            _time.sleep(0.5)
        for cue in cap.poll():
            lines[cue.stream] = lines.get(cue.stream, 0) + 1
        status = cap.stream_status()
    finally:
        cap.stop()
    print(_soak_line(cap, lines, _time.monotonic() - started, status), flush=True)
    restarts = sum(v.get("restarts", 0) for v in status.values())
    ok = all(v["alive"] for v in status.values()) and len(status) == len(streams)
    which = "both languages" if len(streams) == 2 else f"all {len(streams)} languages" if len(streams) > 2 else "the language"
    print(f"{'PASS' if ok else 'FAIL'}: {which} {'alive' if ok else 'NOT alive'} after "
          f"{args.minutes:g} min; watchdog restarts: {restarts}; transcript lines: "
          + ", ".join(f"{streams[k]} {n}" for k, n in lines.items()))
    for v in status.values():
        if v["problem"]:
            print(f"  {v['problem']}", file=sys.stderr)
    print(f"(logs and SRTs in {sdir}; delete with `pacenote purge --client {args.client} --session {sdir.name}`)")
    return 0 if ok else 1


def _soak_line(cap, lines: dict, elapsed: float, status: dict | None = None) -> str:
    status = status or cap.stream_status()
    parts = []
    for stream, v in status.items():
        state = ("alive" if v["alive"] else "stopped" if v.get("stopped")
                 else "STOPPED" if v.get("gave_up") else "restarting")
        parts.append(f"{v['locale']} {state} ({lines.get(stream, 0)} lines, {v.get('restarts', 0)} restarts)")
    m, sec = divmod(int(elapsed), 60)
    return f"  {m:02d}:{sec:02d}  " + "  ".join(parts)


def cmd_check(args) -> int:
    ok = True
    try:
        client = load_client(args.clients_dir, args.client)
        q = client.questionnaire
        n = sum(len(t.questions) for t in q.themes)
        print(f"[ok] questionnaire: {q.client}, {len(q.themes)} themes, {n} questions, language={q.language}")
        print(f"[{'ok' if client.glossary.strip() else '!!'}] glossary.md "
              f"({len(client.glossary.splitlines())} lines)")
        for name in briefs.BRIEFS:  # the plan relies on these dated briefs, not on what a model remembers
            b = briefs.load_brief(name)
            print(_brief_line(b) if b else f"[!!] {name} brief missing")
    except Exception as exc:
        ok = False
        client = None
        print(f"[!!] client files: {exc}")
    try:
        locales = speech_locales(args, client)
        print(f"[ok] spoken languages: {', '.join(locales)}")
    except i18n.LanguageError as exc:
        print(f"[!!] --locales: {exc}")
        return 1
    try:
        plan = speech.select(args.speech_backend)
    except CaptureError as exc:
        print(f"[!!] speech backend: {exc}")
        return 1
    whisper_bin, whisper_model, why = speech.whisper_config()
    if "whisper" in (plan.primary, plan.fallback):
        print(f"[ok] whisper.cpp{' (automatic fallback)' if plan.fallback else ''}: {whisper_bin}, model {whisper_model}")
    else:
        print(f"[..] whisper.cpp fallback off ({why}); see README > Speech backends")
    if plan.primary == "whisper":  # whisper-stream only listens to the microphone: nothing more to check here
        if args.engine == "cli":
            ok = _check_claude_cli(args) and ok
        return 0 if ok else 1
    try:
        binary = ensure_helper(args.listen_bin, on_progress=lambda m: print(m, flush=True), slug_hint=args.client)
        print(f"[ok] speech helper: {binary}")
        if not args.no_speech_test:
            report: dict = {}
            if check_speech(args.client, locales, binary, report=report):
                ready, failed = report.get("ready") or locales, report.get("failed") or {}
                print(f"[ok] {' and '.join(ready)}: speech models installed and loaded, microphone live")
                for loc, why in failed.items():  # `pacenote run` would start without it: fix it the day before
                    ok = False
                    print(f"[!!] {loc}: not ready ({why}); an interview would start without it")
            else:
                ok = False
                print("[!!] speech check failed (details above)")
    except CaptureError as exc:
        ok = False
        print(f"[!!] {exc}")
    if args.engine == "cli":
        ok = _check_claude_cli(args) and ok
    elif args.engine == "api":
        if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            print("[ok] ANTHROPIC_API_KEY is set (--engine api)")
        else:
            ok = False
            print("[!!] --engine api needs ANTHROPIC_API_KEY (the default --engine cli does not)")
    else:
        print("[ok] --engine offline: no model calls")
    print(f"[..] engine: {args.engine} · model: {args.model}")
    return 0 if ok else 1


def _check_claude_cli(args) -> bool:
    try:
        binary = find_claude(args.claude_bin)
    except ClaudeCliNotFound as exc:
        print(f"[!!] {exc}")
        return False
    print(f"[ok] claude CLI: {binary}")
    print("[..] asking the CLI to reply 'ok' (uses your Claude subscription, ANTHROPIC_API_KEY is not passed)…")
    try:
        res = ClaudeCliEngine(model=args.model, claude_bin=binary).ping()
    except SyncFailed as exc:
        print(f"[!!] claude CLI is not working or not logged in: {exc}")
        print("     run `claude` once in a terminal and log in with your Claude account (/login)")
        return False
    print(f"[ok] claude CLI logged in and answering ({res.seconds:.1f}s round trip: {res.text.strip()[:40]!r})")
    return True


if __name__ == "__main__":
    sys.exit(main())
