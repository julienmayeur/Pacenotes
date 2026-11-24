"""Coverage first: the whole company before one point (coverage.py).

In the first real interview one case took the whole hour: the question on screen changed, the conversation
did not, and most of the company (after-sales, purchasing, logistics, finance, the team) was never
discussed. The model labels each slice with an area of the company; these tests drive the code that decides,
with fictional label sequences.
"""

import time

import pytest

from pacenote.copilot.contract import TopicLabel
from pacenote.copilot.engines import OfflineEngine
from pacenote.copilot.prompts import build_sync_system, build_sync_user
from pacenote.interview import coverage, timebox
from pacenote.interview.questionnaire import Questionnaire
from pacenote.interview.session import Session, Settings
from tests.conftest import real_client, result
from tests.interview.test_interview_layout import _run
from tests.interview.test_questions import jargon
from tests.shared.test_i18n import _node_playwright

E = 1_800_000_000.0
BASE = 20 * 60 / 9 * coverage.ALLOWANCE_FACTOR  # a point's allowance in a 20-minute interview (133 s)


class _Clock:
    def __init__(self, t=0.0):
        self.t = t

    def elapsed(self):
        return self.t


def _session(tmp_path, minutes=20):
    s = Session(client=real_client(), session_dir=tmp_path / "s", engine=OfflineEngine(), source_factory=lambda d: None,
                settings=Settings(sync_interval=3600, first_sync_delay=3600, poll_interval=3600, fast_enabled=False))
    s.state.set_target_minutes(minutes)
    s.state.mode = "interview"
    s.source = _Clock(0.0)
    s.state.started_at = E
    s.state.set_clock(s.interview_clock)
    s.state.start_phase_clock(E)
    return s


def _label(area, point="", same=None, repeating=False, discussed=None, mentioned=None):
    return TopicLabel(area=area, point=point or f"a point about {area}", same_point=bool(same), repeating=repeating,
                      discussed=list(discussed if discussed is not None else ([area] if area != "other" else [])),
                      mentioned=list(mentioned or []))


def _at(s, t):
    s.source.t = t


def _sync(s, t, label):
    """A sync applied at t seconds into the interview, with this topic label."""
    _at(s, t)
    s.state.apply_sync_result(result(topic=label), read=s.transcript.last_seq)


def _say(s, start, end, text):
    s.transcript.add("fr", start, end, text, received_at=time.time())
    s.last_fast_seq = s.transcript.last_seq


# --- the areas ----------------------------------------------------------------------------------------


def test_the_areas_cover_the_whole_company_each_with_a_title_a_label_and_one_question_in_fr_and_en():
    areas = coverage.load_catalog()
    assert [a.id for a in areas] == ["sales", "customer-service", "after-sales", "purchasing", "operations",
                                    "marketing", "finance", "people", "it"]
    for a in areas:
        for lang in ("fr", "en"):
            assert a.title.get(lang) and a.short.get(lang) and a.texts.get(lang).endswith("?"), (a.id, lang)
            assert not jargon(a.texts[lang]), (a.id, jargon(a.texts[lang]))


def test_a_questionnaire_can_narrow_reorder_or_write_its_own_areas():
    base = real_client().questionnaire.model_dump()
    q = Questionnaire.model_validate({**base, "areas": ["finance", "sales", {"id": "fleet", "title": {"fr": "Flotte", "en": "Fleet"},
                                                                            "fr": "Qui gère les véhicules ?", "en": "Who runs the vehicles?"}]})
    got = coverage.resolve(q.areas)
    assert [a.id for a in got] == ["finance", "sales", "fleet"] and got[2].short["en"] == "Fleet"
    with pytest.raises(ValueError, match="unknown area"):
        Questionnaire.model_validate({**base, "areas": ["sales", "nope"]})


# --- coverage from the labels ----------------------------------------------------------------------


def test_the_time_goes_to_the_area_each_slice_is_about_and_a_passing_mention_never_covers(tmp_path):
    s = _session(tmp_path)
    _sync(s, 40, _label("customer-service", mentioned=["finance"]))
    _sync(s, 80, _label("customer-service", same=True))
    st = s.state
    assert st.cov.seconds == {"customer-service": 80.0}
    assert coverage.status(st, "customer-service") == "covered"  # 80 s as the point
    assert coverage.status(st, "finance") == "touched"  # named in passing: touched, never covered
    assert coverage.status(st, "purchasing") == "untouched"
    assert coverage.counts(st) == {"untouched": 7, "touched": 1, "covered": 1}


def test_other_a_failed_sync_or_an_unknown_id_never_reset_the_point(tmp_path):
    s = _session(tmp_path)
    _sync(s, 40, _label("customer-service"))
    since = s.state.cov.topic["since"]
    _sync(s, 80, _label("other", discussed=[]))  # small talk
    _sync(s, 120, None)  # no speech, or a sync that labelled nothing
    _sync(s, 160, _label("warehouse-robots"))  # not an area id
    assert s.state.cov.topic["area"] == "customer-service" and s.state.cov.topic["since"] == since
    assert coverage.dwell(s.state) == 160  # the point started with the interview: it is still on


def test_a_new_area_starts_a_new_point_unless_the_model_says_it_is_the_same_case(tmp_path):
    s = _session(tmp_path)
    _sync(s, 40, _label("customer-service"))
    _sync(s, 80, _label("it", same=True))  # the same case, through its tools: the point goes on
    assert s.state.cov.topic["since"] == E and s.state.cov.seconds["it"] == 40
    _sync(s, 120, _label("purchasing"))  # a new point: its clock starts at the start of that slice
    assert s.state.cov.topic["area"] == "purchasing" and s.state.cov.topic["since"] == E + 80


# --- drift and the move-on -------------------------------------------------------------------------


def test_the_allowance_is_the_area_share_of_the_interview_with_some_depth(tmp_path):
    for minutes, want in ((20, BASE), (30, 200.0), (5, coverage.MIN_ALLOWANCE), (60, coverage.MAX_ALLOWANCE)):
        assert coverage.allowance(_session(tmp_path / str(minutes), minutes).state) == pytest.approx(want), minutes


def test_one_case_for_an_hour_gets_a_move_on_after_its_allowance_and_again_after_the_cooldown(tmp_path):
    """The real interview's shape: the same point, labelled every 40 s, for an hour."""
    s = _session(tmp_path)
    st = s.state
    moves = []
    for t in range(40, 3601, 10):
        if t % 40 == 0:
            _sync(s, t, _label("customer-service", "dealer calls about order status", same=t > 40))
        _at(s, t)
        if s._maybe_move_on():
            moves.append((t, st.cov.move_on["to"]))
        if (q := coverage.pending_qid(st)) is not None and t - st.cov.move_on["at"] + E >= 60:
            st.advance("test: the interviewer asked it", to=q)  # asked, but the conversation stays put
    assert moves[0][0] == pytest.approx(BASE, abs=10)  # about 2 min into the hour, not 60 min
    assert [m[1] for m in moves[:4]] == ["sales", "after-sales", "purchasing", "operations"]  # untouched, in order
    gaps = [b[0] - a[0] for a, b in zip(moves, moves[1:], strict=False)]
    assert min(gaps) >= coverage.COOLDOWN  # never nagging
    assert all(f"area-{a}" in st.focus_history for _, a in moves)


def test_the_move_on_goes_through_the_queued_next_slot_and_stops_the_digging(tmp_path):
    s = _session(tmp_path)
    st = s.state
    for t in (40, 80, 120, 160, 200):
        _sync(s, t, _label("customer-service", same=t > 40))
    _at(s, 200)
    assert s._maybe_move_on()
    q = st.get("area-sales")
    assert q is not None and q.phase == st.phase and q.priority == 1
    assert st.next_qid() == "area-sales"
    queue = s.ask_queue()
    assert queue[-1] == {"kind": "question", "qid": "area-sales", "source": q.text(st.language)}
    assert "the next area's question is queued" in st.propose_followup("Et combien d'appels par jour ?", by="fast")
    view = s.view()
    item = view["current"]["ask_next"][-1]
    assert item["qid"] == "area-sales" and "ventes et clients" in item["transition"]
    assert view["coverage"]["move_on"]["to"] == "sales"


def test_it_moves_up_at_the_next_boundary_unless_the_interviewer_keeps_going(tmp_path):
    s = _session(tmp_path)
    st = s.state
    for t in (40, 80, 120, 160, 200):
        _sync(s, t, _label("customer-service", same=t > 40))
    _say(s, 190.0, 199.0, "et donc les concessionnaires nous rappellent pour savoir")
    _at(s, 200)
    s._maybe_move_on()
    st.ask_seq = s.transcript.last_seq
    _say(s, 200.0, 209.0, "ils appellent tout le temps, c'est le problème principal")
    _at(s, 212)
    assert not s._maybe_retire()  # within the grace, mid-conversation: nothing moves
    _say(s, 220.0, 232.0, "voilà, c'est vraiment ça qui nous prend le plus de temps.")
    _at(s, 235)
    assert s._maybe_retire() and st.focus_id == "area-sales"  # a sentence end after the grace
    assert st.cov.move_on == {} and st.cov.log[-1]["outcome"] == "asked"


def test_keep_going_sets_the_move_on_aside_and_gives_the_point_more_time(tmp_path):
    s = _session(tmp_path)
    st = s.state
    for t in (40, 80, 120, 160, 200):
        _sync(s, t, _label("customer-service", same=t > 40))
    _at(s, 200)
    s._maybe_move_on()
    assert coverage.extend(st)
    assert st.get("area-sales") is None and st.next_qid() != "area-sales"  # left the queue
    assert coverage.allowance(st) == pytest.approx(BASE + coverage.EXTEND)
    _at(s, BASE + coverage.EXTEND - 1)
    assert not coverage.due(st)
    _sync(s, BASE + coverage.EXTEND + 5, _label("customer-service", same=True))
    assert coverage.due(st) and coverage.next_area(st) == "after-sales"  # sales was set aside on this point


def test_a_point_that_repeats_itself_gets_the_move_on_earlier_and_time_still_guarantees_it(tmp_path):
    s = _session(tmp_path)
    st = s.state
    _sync(s, 40, _label("customer-service"))
    _sync(s, 60, _label("customer-service", same=True, repeating=True))
    _at(s, 75)
    assert not coverage.due(st)
    _sync(s, 80, _label("customer-service", same=True, repeating=True))
    _at(s, 85)
    assert coverage.allowance(st) == pytest.approx(BASE * coverage.REPEAT_FACTOR) and coverage.due(st)
    s2 = _session(tmp_path / "b")  # no label at all (every sync failed): the clock alone still moves on
    _at(s2, BASE - 1)
    assert not coverage.due(s2.state)
    _at(s2, BASE + 1)
    assert coverage.due(s2.state)


def test_the_conversation_moving_by_itself_drops_the_pending_move_on(tmp_path):
    s = _session(tmp_path)
    st = s.state
    for t in (40, 80, 120, 160, 200):
        _sync(s, t, _label("customer-service", same=t > 40))
    _at(s, 200)
    s._maybe_move_on()
    _sync(s, 240, _label("finance"))
    assert st.cov.move_on == {} and st.get("area-sales") is None
    assert st.cov.topic["area"] == "finance"


def test_no_move_on_while_the_closing_questions_have_the_floor_and_depth_once_every_area_was_touched(tmp_path):
    s = _session(tmp_path)
    st = s.state
    _at(s, 20 * 60 - timebox.reserve(st) + 1)
    assert timebox.closing_due(st) and not coverage.due(st)
    s2 = _session(tmp_path / "b")
    _sync(s2, 40, _label("customer-service", mentioned=[a for a in coverage.ids(s2.state) if a != "customer-service"]))
    assert coverage.allowance(s2.state) == pytest.approx(BASE * coverage.DEPTH_FACTOR)


# --- what the model and the screen see ---------------------------------------------------------------


def test_the_model_gets_the_areas_and_where_the_conversation_is(tmp_path):
    s = _session(tmp_path)
    _sync(s, 40, _label("customer-service", "dealer calls"))
    system = build_sync_system(s.state, glossary="")
    assert "# Coverage first" in system and "- after-sales: After-sales and warranty" in system
    user = build_sync_user(s.state, [], [], 40)
    assert '"current_area": "customer-service"' in user and '"after-sales": "untouched"' in user


def test_the_screen_shows_a_quiet_strip_of_areas(tmp_path):
    s = _session(tmp_path)
    _sync(s, 40, _label("customer-service", mentioned=["it"]))
    _sync(s, 90, _label("customer-service", same=True))
    v = s.view()["coverage"]
    assert v["total"] == 9 and v["covered"] == 1 and v["touched"] == 1
    cs = next(a for a in v["areas"] if a["id"] == "customer-service")
    assert cs["current"] and cs["status"] == "covered" and cs["short"] == "Service client"
    assert v["move_on"] is None


COVERAGE_SCRIPT = r"""
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  p.on('pageerror', (e) => errors.push(String(e)));
  await p.goto(process.argv[2]);
  await p.waitForFunction(() => document.querySelectorAll('#areas li').length > 0);
  await p.waitForTimeout(400);
  const read = () => p.evaluate(() => {
    const visible = (el) => !!el && el.getClientRects().length > 0;
    const li = document.querySelector('#ask-next li.move-on');
    return {
      title: document.querySelector('#areas-title').textContent.replace(/\s+/g, ' ').trim(),
      areas: [...document.querySelectorAll('#areas li')].map((x) => [x.textContent, x.className]),
      dotColors: [...new Set([...document.querySelectorAll('#areas li')].map((x) => getComputedStyle(x).color))].length,
      moveOn: li ? { transition: li.querySelector('.an-transition').textContent, text: li.querySelector('.an-text').textContent,
                     keep: visible(li.querySelector('.an-keep')) ? li.querySelector('.an-keep').textContent : null } : null,
    };
  });
  const before = await read();
  await p.keyboard.press('x');
  await p.waitForFunction(() => !document.querySelector('#ask-next li.move-on'), null, { timeout: 8000 });
  const after = await read();
  console.log(JSON.stringify({ before, after, errors }));
  await b.close();
})().catch(e => { console.error(e); process.exit(1); });
"""


@pytest.mark.skipif(not _node_playwright(), reason="needs node + playwright + chromium")
def test_the_screen_shows_the_areas_and_the_move_on_line_and_x_keeps_going(tmp_path):
    s = _session(tmp_path)
    for t in (40, 80, 120, 160, 200):
        _sync(s, t, _label("customer-service", "dealer calls", same=t > 40, mentioned=["it"] if t == 40 else []))
    _at(s, 200)
    assert s._maybe_move_on()
    r = _run(tmp_path, s, COVERAGE_SCRIPT)
    assert r["errors"] == []
    b = r["before"]
    assert b["title"] == "Tour de l'entreprise · 2/9 abordés"
    assert b["areas"][1] == ["Service client", "covered current"] and b["areas"][-1] == ["Informatique", "touched"]
    assert b["areas"][0] == ["Ventes", "untouched"] and b["dotColors"] <= 3  # quiet greys, no alarm colour
    assert b["moveOn"]["transition"].startswith("Merci, c'est très clair.") and "ventes et clients" in b["moveOn"]["transition"]
    assert b["moveOn"]["text"].startswith("Côté ventes") and b["moveOn"]["keep"] == "Continuerx"
    assert r["after"]["moveOn"] is None  # x: the move-on is set aside, the point gets more time
    assert s.state.cov.topic["extra"] == coverage.EXTEND


def test_a_followed_move_on_starts_the_new_point_when_its_question_went_up(tmp_path):
    s = _session(tmp_path)
    st = s.state
    for t in (40, 80, 120, 160):
        _sync(s, t, _label("customer-service", same=t > 40))
    _at(s, 160)
    s._maybe_move_on()
    _at(s, 170)
    st.advance("test: asked", to="area-sales")
    _sync(s, 205, _label("sales"))  # the slice from 160 to 205 is mostly about sales, from 170 on
    assert st.cov.topic["area"] == "sales" and st.cov.topic["since"] == E + 170


def test_greetings_before_the_first_area_do_not_count_against_it(tmp_path):
    s = _session(tmp_path)
    _sync(s, 40, _label("other", "how the interview works", discussed=[]))
    _sync(s, 80, _label("other", "how the interview works", same=True, discussed=[]))
    _sync(s, 130, _label("customer-service"))
    st = s.state
    assert st.cov.topic["since"] == E + 80 and coverage.dwell(st) == 50
    _at(s, 80 + BASE - 1)
    assert not coverage.due(st)
    _at(s, 80 + BASE + 1)
    assert coverage.due(st)
