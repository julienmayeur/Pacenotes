"""Versioned analyses: the transcript version is its last line seq; every analysis (fast pass, full
sync) is stamped with the version it read, and a record (question, slot, theme coverage) is only
overwritten by an analysis that read at least as far as the one that last wrote it. A slow full
sync can no longer overwrite what a newer fast pass wrote meanwhile (review F9)."""

import asyncio
import threading

from fastapi.testclient import TestClient

from pacenote.capture import ReplaySource
from pacenote.contract import FastResult, empty_maps
from pacenote.server import create_app
from pacenote.session import Session, Settings
from pacenote.state import SessionState

from pacenote.questionnaire import load_client

from .conftest import CLIENTS, edit, fill, result, upd

QUIET = Settings(sync_interval=3600, first_sync_delay=3600, poll_interval=3600, fast_enabled=True)


def fast(*updates, coverage=(), slots=()):
    return FastResult.model_validate({"question_updates": list(updates), "coverage": list(coverage),
                                      "slots": list(slots), "maps": {"org": [], "flows": [], "stores": [], "decisions": [], "catalog": []}, "noise": []})


def _open_question(state):
    return state.next_questions[0]


# --- state rules ------------------------------------------------------------------------------------


def test_a_slow_sync_cannot_overwrite_a_newer_fast_pass(state):
    q = _open_question(state)
    other = state.next_questions[1]
    theme = state.get(q).theme
    # the fast pass read up to line 12 and drafted the answer
    state.apply_fast_result(fast(upd(q, "partial", "", piece="Ils ressaisissent tout chaque matin"),
                                 coverage=[{"theme": theme, "score": 0.5, "note": ""}]), read=12)
    assert state.get(q).read == 12 and state.analysis_read == 12 and state.fast_read == 12
    # a full sync that started earlier (read up to line 10) lands afterwards
    rep = state.apply_sync_result(result(
        question_updates=[upd(q, "partial", "Résumé ancien")],
        queue_edits=[edit("reword", other, fr="Nouvelle formulation ?", en="New wording?")],
        coverage=[{"theme": theme, "score": 0.1, "note": "ancien"}]), thread=False, read=10)
    assert state.get(q).answer == "Ils ressaisissent tout chaque matin" and state.get(q).read == 12  # kept
    assert [r.target for r in rep.rejected if r.kind == "stale"] == [q]
    assert state.coverage[theme] >= 0.5  # not lowered by the older read
    assert state.get(other).text("fr") == "Nouvelle formulation ?"  # the sync's own work still applies
    assert state.sync_count == 1 and state.sync_read == 10 and state.analysis_read == 12


def test_equal_or_newer_versions_apply(state):
    q = _open_question(state)
    state.apply_fast_result(fast(upd(q, "partial", "", piece="morceau")), read=10)
    rep = state.apply_sync_result(result(question_updates=[upd(q, "partial", "Résumé au même niveau")]), read=10)
    assert state.get(q).answer == "Résumé au même niveau" and not [r for r in rep.rejected if r.kind == "stale"]
    state.apply_fast_result(fast(upd(q, "partial", "", piece="encore")), read=14)
    state.apply_sync_result(result(question_updates=[upd(q, "partial", "Résumé plus récent")]), read=20)
    assert state.get(q).answer == "Résumé plus récent" and state.get(q).read == 20 and state.analysis_read == 20
    # a stale fast pass (it can only be older if the lanes cross) is refused the same way
    state.apply_fast_result(fast(upd(q, "partial", "", piece="vieux")), read=15)
    assert "vieux" not in state.get(q).answer


def test_slots_keep_the_value_of_the_newer_read():
    state = SessionState.from_questionnaire(load_client(CLIENTS, "brioche-four-slots").questionnaire)
    uc = state.use_cases[0].id
    state.apply_fast_result(fast(slots=[fill(uc, "volume", "40 commandes par jour")]), read=9)
    state.apply_sync_result(result(slots=[fill(uc, "volume", "une dizaine")]), read=7)
    assert state.get_use_case(uc).slots["volume"].value == "40 commandes par jour"
    state.apply_sync_result(result(slots=[fill(uc, "volume", "45 commandes par jour")]), read=9)
    assert state.get_use_case(uc).slots["volume"].value == "45 commandes par jour"


def test_versions_survive_a_save_and_load(state, tmp_path):
    q = _open_question(state)
    state.apply_fast_result(fast(upd(q, "partial", "", piece="x")), read=7)
    state.save(tmp_path / "state.json")
    back = SessionState.load(tmp_path / "state.json")
    assert back.get(q).read == 7 and back.analysis_read == 7 and back.fast_read == 7
    state.save(tmp_path / "state.json")
    raw = (tmp_path / "state.json").read_text()
    old = SessionState.model_validate_json(raw.replace('"read": 7', '"read": 0'))  # older files load too
    assert old.get(q).read == 0


# --- the session: out-of-order completion for real ------------------------------------------------


class SlowSyncEngine:
    """The full sync blocks until released; the fast pass answers at once."""

    name = "slow-sync"

    def __init__(self):
        self.release = threading.Event()
        self.started = threading.Event()

    def fast(self, system, user, *, state, lines):
        cur = state.current_question
        return FastResult(question_updates=[upd(cur.id, "partial", "", piece=lines[-1].text)], slots=[], maps=empty_maps(), coverage=[],
                          noise=[])

    def sync(self, system, user, *, state, delta):
        self.started.set()
        self.release.wait(10)
        cur = state.current_question
        return result(question_updates=[upd(cur.id, "partial", "Résumé de la synchro lente")])

    def write_plan(self, system, user, *, state):
        return "# Plan\n"


def _session(tmp_path, client_cfg, engine):
    s = Session(client=client_cfg, session_dir=tmp_path / "s", engine=engine, settings=QUIET,
                source_factory=lambda d: ReplaySource({}))
    s.state.mode = "interview"
    return s


def test_slow_full_sync_lands_after_a_newer_fast_pass(tmp_path, client_cfg):
    engine = SlowSyncEngine()
    session = _session(tmp_path, client_cfg, engine)
    focus = session.state.focus_id

    async def scenario():
        for i in range(3):
            session.transcript.add("fr", i, i + 1, f"On saisit les commandes à la main, ligne {i}")
        sync = asyncio.create_task(session.sync_once(force=True))  # reads lines 1-3, then hangs
        await asyncio.to_thread(engine.started.wait, 5)
        session.transcript.add("fr", 4, 5, "et ça prend trois heures chaque matin")
        assert await session.fast_once()  # reads up to line 4, lands first
        engine.release.set()
        assert await sync
        return session.state

    st = asyncio.run(scenario())
    q = st.get(focus)
    assert "trois heures" in q.answer and q.answer != "Résumé de la synchro lente"
    assert q.read == 4 and st.fast_read == 4 and st.sync_read == 3 and st.analysis_read == 4
    log = (tmp_path / "s" / "sync_log.jsonl").read_text()
    assert '"kind": "stale"' in log and '"read": 3' in log and '"read": 4' in log


def test_page_refresh_and_restart_show_the_same_reading_level(tmp_path, client_cfg):
    engine = SlowSyncEngine()
    engine.release.set()
    session = _session(tmp_path, client_cfg, engine)
    focus = session.state.focus_id

    async def run():
        for i in range(5):
            session.transcript.add("fr", i, i + 1, f"Ligne {i} sur les commandes des boulangeries")
        session.transcript.save_jsonl(session.session_dir / "transcript.jsonl")
        await session.fast_once()

    asyncio.run(run())
    with TestClient(create_app(session)) as http:
        for _ in range(2):  # the page, then a refresh of it
            st = http.get("/api/state").json()
            assert st["analysis"] == {"transcript": 5, "read": 5, "fast": 5, "sync": 0}
            assert st["current"]["id"] == focus and st["current"]["read"] == 5
    back = Session.resume(client_cfg, session.session_dir, engine)  # the server restarted from its files
    v = back.view()
    assert v["analysis"]["transcript"] == 5 and v["analysis"]["read"] == 5 and v["current"]["read"] == 5


def test_reading_level_text_exists_in_every_ui_language():
    from pacenote import i18n

    for lang in i18n.available():
        assert "42" in i18n.t("focus.readAt", lang, n=42)
        assert "7" in i18n.t("console.readAt", lang, read=7, total=9) and "9" in i18n.t("console.readAt", lang, read=7, total=9)
