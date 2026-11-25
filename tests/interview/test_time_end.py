"""The end of the interview's time (timebox.py rules 4-5): a question whose slot is over always moves on, the
closing runs inside the planned length and the interview stops once it is done, and "+5 min" re-plans.

The example questionnaire's closing has two questions (roi-expectation, roi-budget); at 20 minutes its
reserve is 2 x (60 s slot + 30 s grace) = 180 s.
"""

import json

import pytest
from fastapi.testclient import TestClient

from pacenote.interview import coverage, lifecycle, timebox
from pacenote.web.server import create_app
from tests.interview.test_timebox import _say
from tests.interview.test_timebox import _started as _started_session
from tests.shared.test_i18n import _node_playwright


class _Src:
    """The speech clock, and a recogniser that hears nothing more (the tests add the lines themselves)."""

    def __init__(self, t=0.0):
        self.t = t

    def elapsed(self):
        return self.t

    def stop(self):
        pass

    def poll(self, final=False):
        return []

    def problems(self):
        return []


def _started(tmp_path):
    s = _started_session(tmp_path=tmp_path)
    s.source = _Src(0.0)
    return s


def _at(s, t):
    s.source.t = float(t)


def _talk(s, t0, t1, text="et donc on reprend les commandes une par une"):
    """Lines of ~3 s with 0.5 s gaps, none ending a sentence (no move is checked on the way)."""
    t = t0
    while t + 3 <= t1:
        _say(s, t, t + 3, text)
        t += 3.5
    _at(s, t)
    return t


def _end(s, at_boundary=True):
    return lifecycle.end_reason(s.state, speech_now=s.speech_clock(), last_heard=s.speech_clock(),
                                page_seen=False, page_gone_for=None, at_boundary=at_boundary)


def _into_last_closing(s):
    """At 17:01 the closing is due: its two questions go up; returns once the last one is on screen."""
    st = s.state
    _at(s, 1200 - 179)
    st.advance("n")
    assert st.focus_id == "roi-expectation" and st.phase == "return"
    _at(s, 1200 - 90)
    st.advance("n")
    assert st.focus_id == "roi-budget" and timebox.on_last_closing(st)
    s.view()  # the screen shows it (as in the session): what is said from now answers it
    st.ask_seq = s.transcript.last_seq


# --- item 2: a slot that is over always moves on -------------------------------------------------------


def test_with_nothing_said_a_slot_that_is_over_still_moves_on(tmp_path):
    """Nobody says anything after the question went up (or the lines are noise): it used to stay for good."""
    s = _started(tmp_path=tmp_path)
    st = s.state
    first = st.focus_id
    s.view()
    st.ask_seq = 0
    _at(s, st.focus_slot + timebox.HARD - 1)
    assert timebox.slot_over(st) and not s._maybe_retire() and st.focus_id == first
    _at(s, st.focus_slot + timebox.HARD + 0.5)
    assert s._maybe_retire() and st.focus_id != first
    assert s.ask_changes[-1]["reason"] == "time slot over, 90 s past it"


def test_the_tail_of_an_earlier_answer_never_holds_a_question_past_its_slot(tmp_path):
    """After a completion move the old answer can go on with no pause: it held the new question for good."""
    s = _started(tmp_path=tmp_path)
    st = s.state
    first = st.focus_id
    s.view()
    st.ask_seq = 0
    s._lags = [11.5] * 10
    s._tail, s._tail_seq = True, 0
    _talk(s, 1.0, st.focus_slot + timebox.HARD - 2)
    assert not s._maybe_retire() and st.focus_id == first  # the tail still runs, no sentence end
    _talk(s, s.source.t, st.focus_slot + timebox.HARD + 2)
    assert s._maybe_retire() and st.focus_id != first


def test_a_slot_that_is_over_moves_on_by_itself_at_a_sentence_end(tmp_path):
    """The usual live shape: no click, the next line that ends a sentence after the grace moves it."""
    s = _started(tmp_path=tmp_path)
    st = s.state
    first = st.focus_id
    s.view()
    st.ask_seq = 0
    s._lags = [11.5] * 10
    end = _talk(s, 1.0, st.focus_slot + timebox.GRACE + 4)
    assert not s._maybe_retire()
    _say(s, end, end + 3, "et voilà comment ça se passe.")
    _at(s, end + 3.5)
    assert s._maybe_retire() and st.focus_id != first and "sentence end" in s.ask_changes[-1]["reason"]


# --- item 3: the closing, then the stop ------------------------------------------------------------------


def test_nothing_is_queued_after_the_closings_last_question(tmp_path):
    s = _started(tmp_path=tmp_path)
    st = s.state
    _into_last_closing(s)
    _at(s, 1200 + 30)
    assert timebox.slot_over(st) and st.next_qid() is None  # no left-over, even past its slot
    assert not timebox.closing_done(st)


def test_the_closing_is_done_when_its_last_answer_is_over_and_then_the_time_ends_it(tmp_path):
    s = _started(tmp_path=tmp_path)
    st = s.state
    s._lags = [2.0] * 10
    _into_last_closing(s)
    # the last closing slot is sized to end GRACE before the time (it gives its grace back to the end)
    assert st.focus_slot == timebox.MIN_SLOT
    end = _talk(s, 1112.0, 1176.0)  # its slot ends at 1170: still talking, mid-sentence
    assert not s._maybe_retire() and st.closing_done_at is None
    _say(s, end, end + 2, "c'est notre objectif.")
    _at(s, end + 2 + 2.5 + 1.6)  # a sure 1.5 s pause after it
    assert s._maybe_retire() and st.closing_done_at is not None
    assert st.focus_id == "roi-budget"  # nothing moved: the closing is the end
    assert timebox.closing_done(st)
    assert _end(s) is None  # time left: the interview goes on (the screen keeps the closing's question)
    _at(s, 1200.5)
    assert _end(s) == "time_up"
    events = [json.loads(x) for x in (s.session_dir / "timing.jsonl").read_text().splitlines()]
    assert [e["event"] for e in events if "event" in e] == ["closing_done"]


def test_a_closing_still_running_at_the_time_goes_on_to_its_end_but_never_past_the_cap(tmp_path):
    s = _started(tmp_path=tmp_path)
    st = s.state
    _into_last_closing(s)
    _at(s, 1200 + 10)
    assert timebox.left(st) < 0 and not timebox.closing_done(st)
    assert _end(s) is None  # mid-closing: no abrupt stop
    _at(s, 1200 + lifecycle.OVERRUN - 1)
    assert _end(s) is None
    _at(s, 1200 + lifecycle.OVERRUN)
    assert _end(s) == "time_up"  # a talkative room never keeps the microphone open for long


def test_once_the_closing_is_done_the_stop_waits_for_a_boundary_at_most_the_grace(tmp_path):
    s = _started(tmp_path=tmp_path)
    st = s.state
    s._lags = [11.5] * 10  # live batches: no pause is sure
    _into_last_closing(s)
    st.closing_done_at = st.now()  # done at 18:30 (e.g. the interviewer moved through it)
    _talk(s, 1150.0, 1203.0)
    assert timebox.left(st) < 0 and not s._end_boundary() and _end(s, s._end_boundary()) is None
    _say(s, 1203.5, 1206.0, "merci beaucoup.")
    _at(s, 1206.5)
    assert s._end_boundary() and _end(s, True) == "time_up"


def test_moving_off_the_last_closing_question_counts_as_the_closing_done(tmp_path):
    s = _started(tmp_path=tmp_path)
    st = s.state
    _into_last_closing(s)
    st.advance("n")  # the interviewer asks a left-over (n)
    assert st.get(st.focus_id).phase != "return" and timebox.closing_done(st)


def test_the_closing_done_and_the_end_in_the_session_loop(tmp_path):
    """The loop: the last closing answer ends, the time comes, the interview ends with reason time_up and the
    closing logged done, listening stopped."""
    import asyncio

    s = _started(tmp_path=tmp_path)
    st = s.state
    s._lags = [2.0] * 10
    _into_last_closing(s)
    _say(s, 1115.0, 1170.0, "notre budget est de dix mille euros.")
    _at(s, 1176.0)
    assert s._maybe_retire() and timebox.closing_done(st)
    _at(s, 1200.2)
    assert s._end_due() == "time_up"

    async def run():
        s.request_finalize(s._end_due())
        await asyncio.wait_for(s._plan_task, 20)

    asyncio.run(run())
    assert st.mode == "done" and st.end_reason == "time_up" and st.plan_files
    log = [json.loads(x) for x in (s.session_dir / "sync_log.jsonl").read_text().splitlines()]
    end = next(e for e in log if e.get("kind") == "end")
    assert end["closing_done"] is True and any(e.get("kind") == "closing_done" for e in log)


# --- item 4: +5 min --------------------------------------------------------------------------------------


def test_plus_five_is_offered_only_in_the_last_five_minutes(tmp_path):
    s = _started(tmp_path=tmp_path)
    _at(s, 1200 - 301)
    assert s.view()["time"]["extend"] is False
    _at(s, 1200 - 300)
    assert s.view()["time"]["extend"] is True
    _at(s, 1200 + 20)
    assert s.view()["time"]["extend"] is True  # still, while a closing runs past the time


def test_plus_five_before_the_closing_moves_the_closing_and_replans_the_step_and_the_slot(tmp_path):
    s = _started(tmp_path=tmp_path)
    st = s.state
    _at(s, 1200 - 290)
    st.advance("n", to="core-who-comes")  # a question goes up with 4:50 left
    budget, slot, allowance = st.phase_budgets[st.phase], st.focus_slot, coverage.allowance(st)
    _at(s, 1200 - 250)
    st.extend_time()
    assert st.target_minutes == 25 and timebox.total(st) == 1500 and st.extensions == [950.0]
    assert st.phase_budgets[st.phase] > budget and st.focus_slot > slot  # re-planned on the new length
    assert coverage.allowance(st) > allowance  # 1500 s over 9 areas
    _at(s, 1200 - 179)
    assert not timebox.closing_due(st)  # the closing's reserve moved with the end
    _at(s, 1500 - 179)
    assert timebox.closing_due(st)
    st.extend_time()
    assert st.target_minutes == 30 and len(st.extensions) == 2  # several clicks add up


def test_plus_five_in_the_closing_gives_the_floor_back_then_the_closing_comes_again(tmp_path):
    s = _started(tmp_path=tmp_path)
    st = s.state
    _at(s, 1200 - 179)
    st.advance("n")
    assert st.focus_id == "roi-expectation"
    _at(s, 1200 - 150)
    st.extend_time()  # 7:30 left, the closing needs 3:00 at most
    assert st.closing_reopened and timebox.closing_reopened(st) and st.phase_done("return")
    _at(s, 1200 - 60)  # its slot is over
    nxt = st.next_qid()
    assert nxt is not None and st.get(nxt).phase != "return" and nxt not in st.focus_history[:-1]
    st.advance("time slot over", to=nxt)
    assert st.phase != "return" and not st.closing_reopened
    _at(s, 1500 - 89)  # roi-budget still to ask: 90 s reserve
    assert timebox.closing_due(st) and st.upcoming_order()[0] == "roi-budget"


def test_plus_five_in_the_closing_with_nothing_left_to_ask_stretches_the_closing(tmp_path):
    s = _started(tmp_path=tmp_path)
    st = s.state
    for q in st.questions:
        if q.phase != "return" and q.id != st.focus_id:
            st.focus_history.append(q.id)  # every other question went up once
    st.cov.areas = []  # no area left to go to
    _at(s, 1200 - 179)
    st.advance("n")
    slot = st.focus_slot
    st.extend_time()
    assert not st.closing_reopened and st.focus_slot > slot


def test_plus_five_over_http_logs_in_timing_jsonl_and_is_refused_outside_the_interview(tmp_path):
    s = _started(tmp_path=tmp_path)
    s.state.mode = "consent"
    with TestClient(create_app(s)) as http:
        assert http.post("/api/more-time").status_code == 409
        s.state.mode = "interview"
        _at(s, 1200 - 120)
        r = http.post("/api/more-time")
        assert r.status_code == 200 and r.json()["time"]["total"] == 1500 and r.json()["time"]["extended"] == 1
        assert r.json()["time"]["extend"] is False  # 7 minutes left now: the button hides again
    rows = [json.loads(x) for x in (s.session_dir / "timing.jsonl").read_text().splitlines()]
    ev = [r for r in rows if r.get("event") == "extend_time"]
    assert len(ev) == 1 and ev[0]["by"] == 300 and ev[0]["left_before"] == 120 and ev[0]["left"] == 420
    assert ev[0]["total"] == 1500 and ev[0]["clock"] == 1080


# --- the button, in a real browser -------------------------------------------------------------------------

_MORE_JS = """
const { chromium } = require('playwright');
(async () => {
  const url = process.argv[2];
  const b = await chromium.launch();
  const p = await b.newPage();
  const net = [];
  p.on('response', (r) => { if (r.url().includes('/api/more-time')) net.push(r.status()); });
  p.on('console', (m) => net.push('console:' + m.text()));
  const shown = () => p.evaluate(() => !document.getElementById('ctl-more').classList.contains('hidden'));
  const total = () => p.evaluate(async () => (await (await fetch('/api/state')).json()).time.total);
  await p.goto(url);
  await p.waitForSelector('#start');
  const before = await shown();
  await p.click('#start');
  await p.waitForFunction(() => !document.getElementById('ctl-more').classList.contains('hidden'));
  const label = await p.evaluate(() => document.getElementById('ctl-more').textContent);
  await p.click('#ctl-more');
  for (let i = 0; i < 100 && (await total()) !== 1500; i++) await p.waitForTimeout(100);
  const afterClick = { total: await total(), shown: await shown() };
  await p.keyboard.press('m');
  for (let i = 0; i < 100 && (await total()) !== 1800; i++) await p.waitForTimeout(100);
  await p.waitForFunction(() => document.getElementById('ctl-more').classList.contains('hidden'));
  const afterKey = { total: await total(), shown: await shown() };
  await b.close();
  console.log(JSON.stringify({ before, label, afterClick, afterKey, net }));
})().catch((e) => { console.error(e); process.exit(1); });
"""


@pytest.mark.skipif(not _node_playwright(), reason="needs node + playwright + chromium")
def test_the_plus_five_button_and_its_key_in_a_real_browser(tmp_path, client_cfg, monkeypatch):
    """Hidden before the interview; shown once the time left is within EXTEND_SHOWN (made 1550 s here so the
    20-minute interview shows it at once); a click adds 5 minutes, the m key 5 more, and it hides once more
    than EXTEND_SHOWN is left."""
    import os
    import socket
    import subprocess
    import threading

    import uvicorn

    from tests.interview.test_listening_lifecycle import _live, _until
    from tests.transcription.test_listen_capture import fake_listen

    monkeypatch.setattr(timebox, "EXTEND_SHOWN", 1550.0)
    script, _log = fake_listen(tmp_path)
    session = _live(tmp_path, client_cfg, script)
    session.state.set_target_minutes(20)  # (the frozen fixture questionnaire says 15)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(create_app(session), host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    js = tmp_path / "more.js"
    js.write_text(_MORE_JS, encoding="utf-8")
    try:
        assert _until(lambda: server.started)
        out = subprocess.run(["node", str(js), f"http://127.0.0.1:{port}/"], capture_output=True, text=True,
                             timeout=60, env=dict(os.environ))
        assert out.returncode == 0, out.stderr
        seen = json.loads(out.stdout.strip().splitlines()[-1])
    finally:
        server.should_exit = True
        th.join(10)
    assert seen["before"] is False and seen["label"].startswith("+5 min")
    assert seen["net"] == [200, 200]  # one request per click / key press, no console error
    assert seen["afterClick"] == {"total": 1500, "shown": True}
    assert seen["afterKey"] == {"total": 1800, "shown": False}
    assert session.state.target_minutes == 30 and len(session.state.extensions) == 2


# --- found on the replay of this morning's interview -------------------------------------------------------


def test_a_pending_move_on_gives_way_to_the_closing_when_it_is_due(tmp_path):
    """Replay: a move-on queued at 15:27 went up at 15:56 and held the closing (due 15:30) until 16:19."""
    s = _started(tmp_path=tmp_path)
    st = s.state
    st.cov.topic = {"area": "customer-service", "since": st.started_at, "point": "x"}
    _at(s, 1200 - 215)
    assert coverage.raise_move_on(st) is not None and coverage.pending_qid(st) is not None
    _at(s, 1200 - 205)  # the closing is due (210 s reserve)
    assert timebox.closing_due(st)
    coverage.tick(st)
    assert coverage.pending_qid(st) is None and st.cov.log[-1]["outcome"] == "closing time"
    assert st.upcoming_order()[0] == "roi-expectation"


def test_in_the_closing_a_sentence_end_after_the_slot_moves_on_without_grace(tmp_path):
    """Replay: each closing question took its slot plus a 30 s grace plus the next batch (92-116 s): the
    closing ended 79 s past the time. In the closing the first sentence end after the slot is the boundary."""
    s = _started(tmp_path=tmp_path)
    st = s.state
    s._lags = [11.5] * 10
    _at(s, 1200 - 209)
    st.advance("n")
    s.view()
    st.ask_seq = s.transcript.last_seq
    assert st.focus_id == "roi-expectation"
    slot_end = 1200 - 209 + st.focus_slot
    end = _talk(s, 1200 - 208, slot_end + 2)
    assert not s._maybe_retire()  # the slot is over, mid-sentence: the loop checks on every poll
    _say(s, end, end + 3, "on vise un retour en un an.")
    _at(s, end + 3.5)
    assert s._maybe_retire() and st.focus_id == "roi-budget"
    assert s.ask_changes[-1]["reason"] == "time slot over, sentence end"


def test_a_move_on_after_plus_five_in_the_closing_is_no_closing_question(tmp_path):
    """Replay: an area raised after a +5 in the closing was put in the closing's step, and counted as one."""
    s = _started(tmp_path=tmp_path)
    st = s.state
    _at(s, 1200 - 209)
    st.advance("n")
    st.extend_time()
    assert timebox.closing_reopened(st)
    st.cov.topic = {"area": "customer-service", "since": st.started_at, "point": "x"}
    qid = coverage.raise_move_on(st)
    assert qid is not None and st.get(qid).phase != "return"
    assert timebox.closing_left(st) == ["roi-budget"]


def test_closing_questions_already_asked_keep_no_time_in_the_reserve(tmp_path):
    """Replay: after +5 in the closing, the two closing questions already asked (left open: nobody answered
    them) still counted in the reserve, and the closing came back at 20:09 of 25 instead of about 23:00."""
    s = _started(tmp_path=tmp_path)
    st = s.state
    _at(s, 1200 - 209)
    st.advance("n")  # roi-expectation goes up and stays open
    st.extend_time()
    st.advance("n")  # the floor goes back: a question never asked
    assert st.get(st.focus_id).phase != "return"
    assert timebox.reserve(st) == timebox.MIN_SLOT + timebox.GRACE + timebox.GRACE  # roi-budget only


def test_after_plus_five_the_closing_question_on_screen_keeps_its_slot(tmp_path):
    """Replay: the click 6 s after a closing question went up let a move-on replace it 23 s later."""
    s = _started(tmp_path=tmp_path)
    st = s.state
    st.cov.topic = {"area": "customer-service", "since": st.started_at, "point": "x"}  # a long point
    _at(s, 1200 - 209)
    st.advance("n")
    _at(s, 1200 - 200)
    st.extend_time()
    assert timebox.closing_reopened(st) and not coverage.due(st)  # its slot runs: no move-on yet
    _at(s, 1200 - 209 + st.focus_slot + 1)
    assert coverage.due(st)  # its slot is over: the floor goes back


_AUTO_JS = """
const { chromium } = require('playwright');
(async () => {
  const url = process.argv[2];
  const b = await chromium.launch();
  const p = await b.newPage();
  p.on('dialog', (d) => d.accept());
  await p.goto(url);
  await p.waitForSelector('#start');
  await p.click('#start');
  const seen = [];
  const t0 = Date.now();
  for (;;) {  // no key is pressed, nothing is clicked: watch the question on screen change by itself
    const s = await p.evaluate(() => ({ q: document.getElementById('current').textContent,
      ended: document.getElementById('listen').classList.contains('ended') }));
    if (s.q && seen[seen.length - 1] !== s.q) seen.push(s.q);
    if (s.ended || Date.now() - t0 > 90000) break;
    await p.waitForTimeout(100);
  }
  const banner = await p.evaluate(() => ({ shown: !document.getElementById('banner').classList.contains('hidden'),
    mic: document.getElementById('banner-mic-label').textContent }));
  await p.waitForURL(/\\/plan/, { timeout: 60000 });
  await b.close();
  console.log(JSON.stringify({ questions: seen.length, banner, plan: true }));
})().catch((e) => { console.error(e); process.exit(1); });
"""


@pytest.mark.skipif(not _node_playwright(), reason="needs node + playwright + chromium")
def test_no_click_the_questions_move_the_closing_runs_then_the_interview_stops_in_a_real_browser(
        tmp_path, monkeypatch):
    """A replay (3 minutes of speech at 6x) with the times shrunk (a 150 s interview, 15 s slots, 5 s grace):
    nobody touches the screen. The questions move on by themselves, the closing's questions go up, and once
    the closing is done the time ends the interview: listening stops, the plan is written and opens."""
    import os
    import socket
    import subprocess
    import threading

    import uvicorn

    from pacenote.copilot.engines import OfflineEngine
    from pacenote.interview.session import Session, Settings
    from pacenote.transcription.capture import ReplaySource
    from tests.conftest import SAMPLES, real_client
    from tests.interview.test_listening_lifecycle import _until

    monkeypatch.setattr(timebox, "total", lambda st: 150.0)
    monkeypatch.setattr(timebox, "MIN_SLOT", 15.0)
    monkeypatch.setattr(timebox, "GRACE", 5.0)
    monkeypatch.setattr(timebox, "HARD", 20.0)
    monkeypatch.setattr(lifecycle, "OVERRUN", 40.0)
    src = ReplaySource({"fr": SAMPLES / "fr.srt", "en": SAMPLES / "en.srt"}, speed=6.0)
    session = Session(client=real_client(), session_dir=tmp_path / "s", engine=OfflineEngine(), source_factory=lambda d: src,
                      settings=Settings(sync_interval=3600, first_sync_delay=3600, poll_interval=0.05, fast_enabled=False))
    for q in session.state.questions:  # the closing must go up (the keyword engine could find it answered)
        q.keywords = [] if q.phase == "return" else q.keywords
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(create_app(session), host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    js = tmp_path / "auto.js"
    js.write_text(_AUTO_JS, encoding="utf-8")
    try:
        assert _until(lambda: server.started)
        out = subprocess.run(["node", str(js), f"http://127.0.0.1:{port}/"], capture_output=True, text=True,
                             timeout=180, env=dict(os.environ))
        assert out.returncode == 0, out.stderr
        seen = json.loads(out.stdout.strip().splitlines()[-1])
    finally:
        server.should_exit = True
        th.join(10)
    st = session.state
    log = [json.loads(x) for x in (session.session_dir / "sync_log.jsonl").read_text().splitlines()]
    moves = [e for e in log if e.get("kind") == "question"]
    assert seen["questions"] >= 3 and len(moves) >= 2  # moved by themselves: nothing was pressed
    assert all(not m["reason"].startswith(("interviewer", "next question clicked")) for m in moves)
    closing = [m["focus"] for m in moves if st.get(m["focus"]).phase == "return"]
    assert closing == ["roi-expectation", "roi-budget"]
    end = next(e for e in log if e.get("kind") == "end")
    assert st.end_reason == "time_up" and end["closing_done"] is True
    assert any(e.get("kind") == "closing_done" for e in log)
    assert seen["banner"]["shown"] and seen["banner"]["mic"]  # "listening stopped" on the end banner
    assert st.mode == "done" and st.plan_files and (session.session_dir / "plan.md").exists()
    assert not session.capture_open
