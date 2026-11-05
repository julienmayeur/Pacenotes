"""ClaudeCliEngine: `claude -p` subprocess on the subscription login (subprocess mocked by a
stand-in `claude` script that records argv/env/stdin and prints a canned JSON result)."""

import asyncio
import json
import sys
import threading
import time

import pytest

from pacenote.capture import ReplaySource
from pacenote.cli import main
from pacenote.contract import PlanDoc, SyncResult
from pacenote.llm import ClaudeCliEngine, ClaudeCliNotFound, ClaudeEngine, SyncFailed, find_claude, make_engine
from pacenote.session import Session, Settings

from .conftest import REAL_CLIENTS, SAMPLES, plan_doc, plan_use_case, result, upd
from .test_contract import VALID

FAKE = """#!{python}
import json, os, sys, time
record = {{"argv": sys.argv[1:], "stdin": sys.stdin.read(),
          "env": {{k: v for k, v in os.environ.items() if k.startswith(("ANTHROPIC", "CLAUDE_CODE"))}},
          "cwd": os.getcwd()}}
with open({log!r}, "a") as fh:
    fh.write(json.dumps(record) + "\\n")
mode = {mode!r}
if mode == "sleep":
    time.sleep(30)
if mode == "garbage":
    print("Error: something exploded")
    sys.exit(1)
print(json.dumps({payload}))
"""


def make_fake(tmp_path, payload: dict, mode: str = "ok"):
    log = tmp_path / "calls.jsonl"
    script = tmp_path / "claude"
    script.write_text(FAKE.format(python=sys.executable, log=str(log), mode=mode, payload=repr(payload)))
    script.chmod(0o755)
    return script, log


def calls(log):
    return [json.loads(line) for line in log.read_text().splitlines()]


def ok_payload(**kw):
    base = {"type": "result", "subtype": "success", "is_error": False, "result": "", "duration_ms": 1200}
    base.update(kw)
    return base


def test_sync_command_line_env_and_structured_output(tmp_path, state, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-leak")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-should-not-leak")
    script, log = make_fake(tmp_path, ok_payload(result=json.dumps(VALID), structured_output=VALID))
    engine = ClaudeCliEngine(model="claude-opus-5-5", sync_effort="low", claude_bin=str(script))
    out = engine.sync("SYSTEM PROMPT", "USER PROMPT", state=state, delta=[])
    assert isinstance(out, SyncResult) and out.question_updates[0].question_id == "import-flow"

    (call,) = calls(log)
    argv = call["argv"]
    assert argv[0] == "-p"
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--model") + 1] == "claude-opus-5-5"
    assert argv[argv.index("--effort") + 1] == "low"
    assert argv[argv.index("--system-prompt") + 1] == "SYSTEM PROMPT"
    assert argv[argv.index("--tools") + 1] == ""  # all tools disabled
    assert "--strict-mcp-config" in argv and "--no-session-persistence" in argv
    assert "--bare" not in argv  # --bare would force API-key auth
    schema = json.loads(argv[argv.index("--json-schema") + 1])
    assert set(schema["required"]) == set(SyncResult.model_fields)
    assert call["stdin"].startswith("USER PROMPT")  # prompt via stdin, not argv
    assert "USER PROMPT" not in " ".join(argv)
    # subscription login only: API credentials are stripped from the child environment
    assert "ANTHROPIC_API_KEY" not in call["env"] and "ANTHROPIC_AUTH_TOKEN" not in call["env"]
    assert call["env"].get("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC") == "1"
    assert "pacenote-claude-" in call["cwd"]  # empty working dir, no project CLAUDE.md picked up


def test_text_result_goes_through_the_repair_path(tmp_path, state):
    text = "Voici la mise à jour :\n```json\n" + json.dumps(VALID) + "\n```"
    script, _ = make_fake(tmp_path, ok_payload(result=text))
    out = ClaudeCliEngine(claude_bin=str(script)).sync("S", "U", state=state, delta=[])
    assert out.next_questions == VALID["next_questions"]
    script, _ = make_fake(tmp_path, ok_payload(result="Sure. " + json.dumps(VALID) + " Done."))
    assert ClaudeCliEngine(claude_bin=str(script)).sync("S", "U", state=state, delta=[]).enough_data is False


@pytest.mark.parametrize(
    "payload,mode,message",
    [
        (ok_payload(result="not json at all"), "ok", "contract"),
        (ok_payload(result=json.dumps({**VALID, "extra": 1})), "ok", "contract"),
        (ok_payload(is_error=True, subtype="success", result="Not logged in · Please run /login"), "ok", "Not logged in"),
        (ok_payload(subtype="error_max_turns", result=""), "ok", "error_max_turns"),
        ({}, "garbage", "without JSON"),
    ],
)
def test_failures_raise_sync_failed(tmp_path, state, payload, mode, message):
    script, _ = make_fake(tmp_path, payload, mode)
    with pytest.raises(SyncFailed, match=message):
        ClaudeCliEngine(claude_bin=str(script)).sync("S", "U", state=state, delta=[])


def test_timeout_kills_the_subprocess(tmp_path, state):
    script, _ = make_fake(tmp_path, ok_payload(), mode="sleep")
    engine = ClaudeCliEngine(claude_bin=str(script), sync_timeout=0.5)
    started = time.monotonic()
    with pytest.raises(SyncFailed, match="timed out"):
        engine.sync("S", "U", state=state, delta=[])
    assert time.monotonic() - started < 10
    assert not engine._active


def test_write_plan_doc_asks_for_the_plan_schema_at_plan_effort(tmp_path, state):
    doc = plan_doc(plan_use_case())
    script, log = make_fake(tmp_path, ok_payload(result=json.dumps(doc), structured_output=doc))
    out = ClaudeCliEngine(claude_bin=str(script), plan_effort="high").write_plan_doc("PLAN SYS", "DATA", state=state)
    assert isinstance(out, PlanDoc) and out.use_cases[0].id == "bakery-orders"
    argv = calls(log)[0]["argv"]
    assert argv[argv.index("--effort") + 1] == "high"
    schema = json.loads(argv[argv.index("--json-schema") + 1])
    assert set(schema["required"]) == {"context", "current_state", "bottlenecks", "metrics", "use_cases", "timeline",
                                       "open_items", "recipe_candidates"}


def test_find_claude(tmp_path, monkeypatch):
    script, _ = make_fake(tmp_path, ok_payload())
    monkeypatch.delenv("PACENOTE_CLAUDE_BIN", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert find_claude() == str(script)
    monkeypatch.setenv("PACENOTE_CLAUDE_BIN", str(script))
    monkeypatch.setenv("PATH", "/nonexistent")
    assert find_claude() == str(script)
    monkeypatch.setenv("PACENOTE_CLAUDE_BIN", str(tmp_path / "missing"))
    with pytest.raises(ClaudeCliNotFound):
        find_claude()


def test_engine_selection(monkeypatch):
    assert make_engine().name == "claude-cli"  # default
    assert make_engine("offline").name == "offline"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    assert isinstance(make_engine("api"), ClaudeEngine)
    with pytest.raises(ValueError):
        make_engine("gpt")


def test_engine_flag_and_env_default(monkeypatch):
    from pacenote.cli import build_parser

    args = build_parser().parse_args(["run", "--client", "x"])
    assert args.engine == "cli" and args.effort == "low"
    monkeypatch.setenv("PACENOTE_ENGINE", "api")
    assert build_parser().parse_args(["run", "--client", "x"]).engine == "api"


def test_check_pings_the_cli_instead_of_requiring_an_api_key(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    script, log = make_fake(tmp_path, ok_payload(result="ok"))
    helper = tmp_path / "pacenote-listen"
    helper.write_text("#!/bin/sh\n")
    helper.chmod(0o755)
    code = main(["check", "--client", "brioche-four", "--clients-dir", str(REAL_CLIENTS), "--listen-bin", str(helper), "--no-speech-test",
                 "--claude-bin", str(script)])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "logged in" in out and "needs ANTHROPIC_API_KEY" not in out
    assert calls(log)[0]["stdin"] == "reply ok"

    bad, _ = make_fake(tmp_path, ok_payload(is_error=True, result="Invalid API key · Please run /login"))
    code = main(["check", "--client", "brioche-four", "--clients-dir", str(REAL_CLIENTS), "--listen-bin", str(helper), "--no-yap-test",
                 "--claude-bin", str(bad)])
    assert code == 1 and "not logged in" in capsys.readouterr().out


def test_run_fails_fast_without_claude(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PACENOTE_CLAUDE_BIN", str(tmp_path / "nope"))
    code = main(["replay", "--client", "brioche-four", "--clients-dir", str(REAL_CLIENTS), "--fr", str(SAMPLES / "fr.srt"), "--en", str(SAMPLES / "en.srt"),
                 "--sessions-dir", str(tmp_path / "s"), "--no-browser"])
    assert code == 2 and "Claude Code CLI not found" in capsys.readouterr().err


# --- cadence: one call at a time, skipped ticks, duration -----------------------------


class SlowEngine:
    name = "slow"

    def __init__(self, seconds):
        self.seconds = seconds
        self.running = 0
        self.max_running = 0
        self.calls = 0
        self.lock = threading.Lock()

    def sync(self, system, user, *, state, delta):
        with self.lock:
            self.running += 1
            self.calls += 1
            self.max_running = max(self.max_running, self.running)
        time.sleep(self.seconds)
        with self.lock:
            self.running -= 1
        return result(question_updates=[upd("strategy-success", "partial", "x")])

    def write_plan(self, system, user, *, state):
        return "# Plan\n"


def test_slow_calls_skip_ticks_and_never_overlap(tmp_path, client_cfg):
    async def run():
        clock = [1000.0]
        engine = SlowEngine(0.6)
        session = Session(
            client=client_cfg,
            session_dir=tmp_path / "cadence",
            engine=engine,
            source_factory=lambda d: ReplaySource({"fr": SAMPLES / "fr.srt", "en": SAMPLES / "en.srt"},
                                                  speed=1.0, clock=lambda: clock[0]),
            settings=Settings(sync_interval=0.1, first_sync_delay=0, poll_interval=0.02, min_new_chars=1,
                              fast_enabled=False),
        )
        await session.start()
        for _ in range(20):  # new transcript keeps arriving every 0.1 s
            clock[0] += 15
            await asyncio.sleep(0.1)
        view = session.view()
        await session.finalize()  # waits for the in-flight call, then one last sync
        await session.shutdown()
        return engine, session, view

    engine, session, view = asyncio.run(run())
    assert engine.max_running == 1
    assert session.state.skipped_ticks >= 2
    assert session.state.last_sync_seconds is not None and session.state.last_sync_seconds >= 0.6
    assert {"sync_running", "last_sync_seconds", "skipped_ticks"} <= set(view)
