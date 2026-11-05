"""Robustness: unexpected errors in the live loops, the finalize guard, atomic writes and the
read-modify-write races on shared files."""

import asyncio
import json
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pacenote import session as session_mod
from pacenote.admin import create_admin_app
from pacenote.capture import ReplaySource
from pacenote.clients import ClientInfo, save_info
from pacenote.server import create_app
from pacenote.session import Session, Settings

from .conftest import REAL_CLIENTS, result

# --- live loops: an unexpected error is reported and backed off, never swallowed -------------------


class _Boom:
    """An engine that fails the way the audit found: not SyncFailed / ContractError."""

    name = "boom"

    def fast(self, system, user, *, state, lines):
        raise RuntimeError("claude vanished")

    def sync(self, system, user, *, state, delta):
        raise OSError("disk full")

    def write_plan(self, system, user, *, state):
        return "# Plan\n"


def _live(tmp_path, client_cfg, engine, name="s"):
    return Session(client=client_cfg, session_dir=tmp_path / name, engine=engine,
                   source_factory=lambda d: ReplaySource({}), settings=Settings(poll_interval=3600))


def test_fast_pass_unexpected_error_is_reported_and_backs_off(tmp_path, client_cfg):
    session = _live(tmp_path, client_cfg, _Boom())

    async def run():
        session.state.mode = "interview"
        session.transcript.add("fr", 0, 1, "On saisit les commandes à la main")
        return await session.fast_once()

    assert asyncio.run(run()) is False
    assert "claude vanished" in session.state.last_fast_error
    assert session._fast_backoff_until > time.monotonic()  # _fast_loop does not respawn it every 0.2 s


def test_sync_unexpected_error_is_reported(tmp_path, client_cfg):
    session = _live(tmp_path, client_cfg, _Boom())

    async def run():
        session.state.mode = "interview"
        session.transcript.add("fr", 0, 1, "On saisit les commandes à la main")
        return await session.sync_once(force=True)

    assert asyncio.run(run()) is False
    assert "disk full" in session.state.last_sync_error
    assert not session.state.sync_in_progress
    assert session.last_synced_seq == 0  # the delta is kept for the next round


# --- finalize: one run at a time, and a failure never leaves the session stuck -----------------------


class _Plan:
    name = "plan-fake"

    def __init__(self, gate=None):
        self.gate = gate
        self.plans = 0

    def sync(self, system, user, *, state, delta):
        if self.gate is not None:
            self.gate.wait(5)
        return result()

    def write_plan(self, system, user, *, state):
        self.plans += 1
        return "# Plan\n"


def test_two_finalize_calls_write_the_plan_once(tmp_path, client_cfg):
    gate = threading.Event()
    engine = _Plan(gate)
    session = _live(tmp_path, client_cfg, engine)

    async def run():
        session.state.mode = "interview"
        session.transcript.add("fr", 0, 1, "On saisit les commandes à la main")  # finalize runs a last sync
        first = asyncio.create_task(session.finalize())
        await asyncio.sleep(0.1)  # the first one is in its last sync
        second = asyncio.create_task(session.finalize())
        await asyncio.sleep(0.1)
        gate.set()
        return await asyncio.gather(first, second)

    first, second = asyncio.run(run())
    assert first and second == {}
    assert engine.plans == 1 and session.state.mode == "done"


def test_second_finalize_post_is_rejected_while_the_first_runs(tmp_path, client_cfg):
    gate = threading.Event()
    session = _live(tmp_path, client_cfg, _Plan(gate))
    session.state.mode = "interview"
    session.transcript.add("fr", 0, 1, "On saisit les commandes à la main")  # finalize runs a last sync
    with TestClient(create_app(session)) as http:
        assert http.post("/api/finalize").status_code == 200
        assert http.post("/api/finalize").status_code == 409  # the last sync is still running
        gate.set()
        for _ in range(100):
            if http.get("/api/state").json()["mode"] == "done":
                break
            time.sleep(0.05)
    assert session.state.mode == "done"


def test_finalize_failure_is_shown_and_can_be_retried(tmp_path, client_cfg, monkeypatch):
    session = _live(tmp_path, client_cfg, _Plan())

    def broken(*a, **k):
        raise OSError("disk full")

    async def run():
        session.state.mode = "interview"
        monkeypatch.setattr(session_mod, "write_plan_files", broken)  # the plan AND the offline draft fail
        assert await session.finalize() == {}
        failed = (session.state.mode, session.state.last_sync_error)
        monkeypatch.undo()
        return failed, await session.finalize()

    (mode, error), files = asyncio.run(run())
    assert mode == "finalize" and "disk full" in error  # not stuck in "generating"
    assert files["md"] and session.state.mode == "done"


# --- atomic writes: a failed write never leaves a half-written file --------------------------------


def test_atomic_write_keeps_the_old_file_when_the_write_fails(tmp_path, monkeypatch):
    from pacenote import fsutil

    path = tmp_path / "plan.md"
    fsutil.atomic_write_text(path, "v1")
    assert path.read_text() == "v1"
    fsutil.atomic_write_text(path, "v2 ✓")
    assert path.read_text(encoding="utf-8") == "v2 ✓"

    def boom(*a):
        raise OSError("disk full")

    monkeypatch.setattr(fsutil.os, "replace", boom)
    with pytest.raises(OSError):
        fsutil.atomic_write_text(path, "v3")
    assert path.read_text(encoding="utf-8") == "v2 ✓"
    assert [p.name for p in tmp_path.iterdir()] == ["plan.md"]  # no temp file left behind


@pytest.mark.parametrize("call", ["persist", "wispr", "plan"])
def test_session_files_are_written_atomically(tmp_path, client_cfg, monkeypatch, call):
    from pacenote import fsutil

    session = _live(tmp_path, client_cfg, _Plan())
    written = []
    real = fsutil.atomic_write_text
    monkeypatch.setattr(fsutil, "atomic_write_text", lambda p, t: written.append(Path(p).name) or real(p, t))

    async def run():
        if call == "persist":
            session.persist()
        elif call == "wispr":
            await session.set_wispr("notes")
        else:
            session.state.mode = "interview"
            await session.finalize()

    asyncio.run(run())
    expected = {"persist": {"session.json"}, "wispr": {"wispr.txt", "session.json"}, "plan": {"plan.md"}}[call]
    assert expected <= set(written)


def test_research_admin_and_client_sheet_writes_are_atomic(tmp_path, monkeypatch):
    from pacenote import fsutil
    from pacenote.research import ResearchOutput, write_outputs

    written = []
    real = fsutil.atomic_write_text
    monkeypatch.setattr(fsutil, "atomic_write_text", lambda p, t: written.append(Path(p).name) or real(p, t))
    out = ResearchOutput.model_validate({"summary": "s", "research_md": "# R", "preplan_md": "# P",
                                         "use_cases": [], "seed_questions": [], "sources": []})
    write_outputs(tmp_path, out, "m")
    assert {"research.md", "preplan.md", "preplan.json"} <= set(written)

    clients = tmp_path / "clients"
    shutil.copytree(REAL_CLIENTS / "brioche-four", clients / "brioche-four")
    written.clear()
    http = TestClient(create_admin_app(clients))
    assert http.put("/admin/api/clients/brioche-four/preplan", json={"markdown": "# P"}).status_code == 200
    assert http.put("/admin/api/clients/brioche-four/research-md", json={"markdown": "# R"}).status_code == 200
    save_info(clients, "brioche-four", ClientInfo(name="Brioche & Four"))
    assert written == ["preplan.md", "research.md", "client.yaml"]


# --- translation cache: two translations at once never drop each other's entries ---------------------


def test_translation_cache_keeps_entries_saved_meanwhile(tmp_path, state):
    from pacenote import translate

    path = tmp_path / ".translations" / "cache.json"

    class Engine:
        name = "t"

        def translate(self, system, user, *, schema):
            translate.save_cache(path, {"other": "saved by the display translation meanwhile"})
            items = json.loads(user.split("<items>")[1].split("</items>")[0])
            return {"items": [{"id": i["id"], "text": "DE " + i["text"]} for i in items]}

    found = translate.fetch(state, "de", Engine(), cache_path=path)
    cache = translate.load_cache(path)
    assert found and cache["other"] == "saved by the display translation meanwhile"
    assert len(cache) == len(set(found.values())) + 1  # one entry per distinct source text


# --- people research: an FDE edit made while the agent runs is kept -------------------------------


def test_people_rerun_keeps_an_edit_made_while_it_ran(tmp_path):
    from pacenote.people import PeopleManager, load_profile, save_profile_edit

    from .test_people import fake_claude, sheet

    clients = tmp_path / "clients"
    shutil.copytree(REAL_CLIENTS / "brioche-four", clients / "brioche-four")
    info = sheet(clients)
    script, _ = fake_claude(tmp_path)
    cdir = clients / "brioche-four"
    first = PeopleManager(clients, claude_bin=str(script))
    assert first.wait(first.start("brioche-four", "Jean Exemple", "", info).slug, 20).status == "done"

    def fde_edits_meanwhile(urls):  # runs after the agent answered, before the card is merged
        card = load_profile(cdir, "Jean Exemple")
        save_profile_edit(cdir, "Jean Exemple",
                          card["facts"] + [{"category": "interest", "value": "Passionné de voile"}], [])
        return set()

    again = PeopleManager(clients, claude_bin=str(script), check_links=fde_edits_meanwhile)
    job = again.wait(again.start("brioche-four", "Jean Exemple", "", info, force=True).slug, 20)
    assert job.status == "done", job.error
    values = [f["value"] for f in load_profile(cdir, "Jean Exemple")["facts"]]
    assert "Passionné de voile" in values


# --- small ones -----------------------------------------------------------------------------------


def test_claude_engine_removes_its_empty_workdir_on_close():
    from pacenote.llm import ClaudeCliEngine

    engine = ClaudeCliEngine()
    workdir = Path(engine._workdir)
    assert workdir.is_dir()
    engine.cancel()  # server shutdown
    assert not workdir.exists()
    out = engine.run([sys.executable, "-c", "import json; print(json.dumps({'result': 'ok'}))"], "", 10)
    assert out.text == "ok"  # still usable after: the empty cwd comes back
    engine.cancel()


def test_claude_stream_drains_stderr_while_reading_stdout(tmp_path):
    from pacenote.research import ResearchJob, run_claude_stream

    script = tmp_path / "chatty-claude"
    script.write_text(f"""#!{sys.executable}
import json, sys
sys.stdin.read()
sys.stderr.write("warning: " + "x" * 300_000 + "\\n")  # more than a pipe buffer, before any stdout
sys.stderr.flush()
print(json.dumps({{"type": "result", "subtype": "success", "structured_output": {{"ok": True}}}}))
""")
    script.chmod(0o755)
    started = time.monotonic()
    data = run_claude_stream(ResearchJob(slug="x"), [str(script)], "prompt", 5, "pacenote-test-")
    assert data == {"ok": True} and time.monotonic() - started < 4  # not killed by the timeout


def test_interview_app_passes_claude_bin_to_the_admin_agents(tmp_path, client_cfg, monkeypatch):
    from pacenote import server

    seen = {}
    for name in ("ResearchManager", "AutofillManager", "PeopleManager"):
        def spy(*a, _name=name, _real=getattr(server, name), **k):
            seen[_name] = k.get("claude_bin")
            return _real(*a, **k)

        monkeypatch.setattr(server, name, spy)
    server.create_app(_live(tmp_path, client_cfg, _Plan()), claude_bin="/opt/claude")
    assert seen == {"ResearchManager": "/opt/claude", "AutofillManager": "/opt/claude", "PeopleManager": "/opt/claude"}


def _http_server(routes, hits):
    import http.server
    import socketserver

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            code, location = routes.get(self.path, (200, None))
            self.send_response(code)
            if location:
                self.send_header("Location", location)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_dead_links_never_fetches_private_or_local_addresses(monkeypatch):
    from pacenote import autofill

    hits = []
    srv, base = _http_server({"/gone": (404, None)}, hits)
    try:
        urls = [f"{base}/gone", "http://10.0.0.1/admin", "http://169.254.169.254/latest/meta-data", "file:///etc/hosts"]
        assert autofill.dead_links(urls, timeout=1) == set()  # not fetched: no claim either way
        assert hits == []
    finally:
        srv.shutdown()
        srv.server_close()


def test_dead_links_does_not_follow_a_redirect_to_a_private_address(monkeypatch):
    from pacenote import autofill

    inner_hits, outer_hits = [], []
    inner, inner_base = _http_server({}, inner_hits)
    outer, outer_base = _http_server({"/r": (302, f"{inner_base}/secret")}, outer_hits)
    monkeypatch.setattr(autofill, "_private_url", lambda url: url.startswith(inner_base))  # outer = "public"
    try:
        assert autofill.dead_links([f"{outer_base}/r"], timeout=2) == set()
        assert outer_hits == ["/r"] and inner_hits == []
    finally:
        for s in (inner, outer):
            s.shutdown()
            s.server_close()
