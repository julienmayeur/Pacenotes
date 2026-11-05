"""Interview panel: the thread of past questions (history) with their answer summaries, the live
summary of the current question, absolute session/plan paths, and an intentional capture stop
that is never reported as a capture problem."""

import asyncio
import time

from fastapi.testclient import TestClient

from pacenote.capture import ListenCapture, ReplaySource
from pacenote import cli
from pacenote.contract import FastResult
from pacenote.llm import OfflineEngine
from pacenote.server import create_app
from pacenote.session import Session, Settings

from .conftest import SAMPLES, result, upd
from .test_listen_capture import FAST, fake_listen, runs

QUIET = Settings(sync_interval=3600, first_sync_delay=3600, poll_interval=3600, fast_enabled=False)


def _session(tmp_path, client_cfg, **kw):
    return Session(client=client_cfg, session_dir=tmp_path / "s", engine=OfflineEngine(),
                   source_factory=lambda d: ReplaySource({"fr": SAMPLES / "fr.srt", "en": SAMPLES / "en.srt"}, speed=0),
                   settings=QUIET, **kw)


def _fast(*updates):
    return FastResult.model_validate({"question_updates": list(updates), "slots": [], "maps": {"org": [], "flows": [], "stores": [], "decisions": [], "catalog": []}, "coverage": [], "noise": []})


# --- history: the interview thread above the current question ----------------------------------


def test_history_is_the_questions_that_left_the_focus_in_order(state):
    first = state.focus_id
    state.apply_fast_result(_fast(upd(first, "partial", "", piece="On ressaisit tout à la main")))
    assert state.history() == []  # the current question is never in the thread above itself
    state.advance("FDE pressed next")
    second = state.focus_id
    state.advance("FDE pressed next")
    assert [q.id for q in state.history()] == [first, second]
    assert state.history()[0].answer == "On ressaisit tout à la main"


def test_spontaneous_answer_joins_the_thread_and_the_focus_goes_below_it(state):
    first = state.focus_id
    other = state.next_questions[-1]
    state.apply_fast_result(_fast(upd(other, "partial", "", piece="Le stock est dans Excel")))
    assert [q.id for q in state.history()] == [other]
    time.sleep(0.01)
    state.advance("FDE pressed next")
    assert [q.id for q in state.history()][-1] == first  # the question just left is at the bottom


def test_a_question_back_in_focus_leaves_the_thread_then_returns_at_the_bottom(state):
    first = state.focus_id
    state.advance("next")
    assert first in [q.id for q in state.history()]
    state.focus_id = first  # revisited (e.g. the queue came back to a partial question)
    assert first not in [q.id for q in state.history()]
    time.sleep(0.01)
    state.advance("next")
    assert state.history()[-1].id == first


def test_skipped_focus_stays_in_the_thread_as_skipped(state):
    first = state.focus_id
    assert state.skip(first)
    assert [(q.id, q.status) for q in state.history()] == [(first, "removed")]


def test_history_survives_a_save_and_load(state, tmp_path):
    first = state.focus_id
    state.advance("next")
    state.save(tmp_path / "state.json")
    loaded = type(state).load(tmp_path / "state.json")
    assert [q.id for q in loaded.history()] == [first]


# --- /api/state ---------------------------------------------------------------------------------


def test_view_exposes_the_live_summary_and_the_thread_without_a_recent_block(tmp_path, client_cfg):
    s = _session(tmp_path, client_cfg)
    st = s.state
    first = st.focus_id
    st.apply_fast_result(_fast(upd(first, "partial", "", piece="On ressaisit les commandes")))
    v1 = s.view()["current"]
    assert v1["summary"] == "On ressaisit les commandes" and v1["summary_by"] == "fast" and v1["summary_at"]
    # a later pass makes the summary richer, on the same question
    st.apply_fast_result(_fast(upd(first, "partial", "", piece="Trois personnes, deux heures par jour")))
    v2 = s.view()["current"]
    assert v2["id"] == first and v2["summary"].startswith(v1["summary"]) and len(v2["summary"]) > len(v1["summary"])
    # a full sync rewrites it as a concise summary
    st.apply_sync_result(result(question_updates=[upd(first, "partial", "Ressaisie manuelle : 3 pers., 2 h/j")]),
                         thread=False)
    assert s.view()["current"]["summary"] == "Ressaisie manuelle : 3 pers., 2 h/j"
    assert s.view()["current"]["summary_by"] == "sync"

    st.advance("FDE pressed next")
    view = s.view()
    assert "recent" not in view  # no separate answered/recent block any more
    [h] = view["history"]
    assert h["id"] == first and h["summary"] == "Ressaisie manuelle : 3 pers., 2 h/j" and h["text"]
    assert [e["kind"] for e in h["thread"]][:1] == ["answer"] and not h["skipped"]
    assert view["current"]["id"] != first


def test_view_exposes_absolute_session_and_plan_paths(tmp_path, client_cfg, monkeypatch):
    monkeypatch.chdir(tmp_path)
    s = Session(client=client_cfg, session_dir="sessions/x/1", engine=OfflineEngine(),
                source_factory=lambda d: ReplaySource({}), settings=QUIET)
    view = s.view()
    assert view["session_dir"] == str(tmp_path.resolve() / "sessions" / "x" / "1")
    assert view["plan_files"] == {} and not view["plan_ready"]

    printed = []
    s.on_plan_written = printed.append
    files = asyncio.run(s.finalize())
    view = s.view()
    assert view["plan_ready"] and view["plan_files"] == files == printed[0]
    assert all(p.startswith(str(tmp_path.resolve())) and p.endswith(("plan.md", "plan.json"))
               for p in files.values())
    assert set(files) == {"md", "json"}  # plan.json: the structured proposal; no plan.html


def test_cli_plan_message_lists_both_absolute_paths(tmp_path):
    files = {"md": str(tmp_path / "s" / "plan.md"), "json": str(tmp_path / "s" / "plan.json")}
    out = cli._plan_written(files)
    assert "Plan written" in out and files["md"] in out and files["json"] in out and "/plan" in out


def test_cli_serve_wires_the_plan_announcement(tmp_path, client_cfg, capsys, monkeypatch):
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    s = _session(tmp_path, client_cfg)

    class Args:
        host, port, no_browser = "127.0.0.1", 8799, True

    cli._serve(s, Args, auto_start=False)
    assert "Session:  " + str((tmp_path / "s").resolve()) in capsys.readouterr().out
    s.on_plan_written({"md": "/abs/plan.md", "json": "/abs/plan.json"})
    assert "/abs/plan.md" in capsys.readouterr().out


# --- intentional stop is not a capture problem --------------------------------------------------


def test_stopped_capture_reports_no_problem_and_never_restarts(tmp_path):
    helper, log = fake_listen(tmp_path)
    cap = ListenCapture(tmp_path / "sess", helper_bin=str(helper), **FAST)
    cap.start()
    cap.stop()
    status = cap.stream_status()
    assert set(status) == {"fr", "en"}
    assert all(v["stopped"] and not v["alive"] and v["problem"] is None for v in status.values())
    assert cap.problems() == []
    assert cap.maintain() == []  # the watchdog never restarts a helper stopped on purpose
    assert len(runs(log)) == 1


def test_ending_the_interview_shows_no_restarting_capture_problem(tmp_path, client_cfg):
    helper, log = fake_listen(tmp_path)

    async def run():
        s = Session(client=client_cfg, session_dir=tmp_path / "s", engine=OfflineEngine(),
                    source_factory=lambda d: ListenCapture(d, helper_bin=str(helper), **FAST),
                    settings=Settings(sync_interval=3600, first_sync_delay=3600, poll_interval=0.05,
                                      fast_enabled=False))
        await s.start()
        await asyncio.sleep(0.3)
        await s.finalize()  # "FDE ended the interview"
        await asyncio.sleep(0.3)
        s.ingest_once()  # a late poll must not resurrect the problem nor restart listening
        view = s.view()
        await s.shutdown()
        return s, view

    s, view = asyncio.run(run())
    assert view["mode"] == "done" and view["enough_data_reason"] == "FDE ended the interview"
    assert view["capture_problems"] == []
    assert view["listening"]["stopped"]
    assert all(x["stopped"] and x["problem"] is None for x in view["listening"]["streams"])
    assert len(runs(log)) == 1  # one process for fr + en; no restart after the stop


def test_http_state_after_finalize_has_paths_and_no_capture_problem(tmp_path, client_cfg):
    s = _session(tmp_path, client_cfg)
    with TestClient(create_app(s)) as http:
        http.post("/api/start")
        http.post("/api/finalize")
        for _ in range(100):
            view = http.get("/api/state").json()
            if view["mode"] == "done":
                break
            time.sleep(0.05)
    assert view["mode"] == "done" and view["capture_problems"] == []
    assert view["plan_files"]["md"].startswith(view["session_dir"])
