"""Multilingual support: locale catalogs and fallback, questionnaire variants, on-the-fly
translation, language switching (API + live screen), configurable speech locales (listening helper mocked)."""

import asyncio
import json
import shutil
import subprocess
import textwrap
import threading
import time
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pacenote import capture, i18n, translate
from pacenote.capture import ListenCapture, locales_map, preflight
from pacenote.cli import main
from pacenote.finalize import assemble_plan
from pacenote.llm import OfflineEngine, offline_plan
from pacenote.prompts import (
    build_fast_system,
    build_fast_user,
    build_plan_system,
    build_sync_system,
    describe_streams,
    plan_headings,
)
from pacenote.questionnaire import load_client, load_questionnaire
from pacenote.server import create_app
from pacenote.session import Session, Settings
from pacenote.state import SessionState

from .conftest import CLIENTS, REAL_CLIENTS, SAMPLES, edit, real_client, result, upd
from .test_listen_capture import FAST, fake_listen, runs

LOCALES = Path(i18n.LOCALES_DIR)


@pytest.fixture
def real_cfg():
    """The real brioche-four client: its questions carry `expect` items ("Ce qui manque")."""
    return real_client()


@pytest.fixture
def real_state(real_cfg):
    return SessionState.from_questionnaire(real_cfg.questionnaire)


# --- catalogs ----------------------------------------------------------------------------------


def test_fr_and_en_locale_files_have_the_same_keys():
    assert {"fr", "en"} <= set(i18n.available())
    fr, en = (set(i18n.flatten(i18n.catalog(c))) for c in ("fr", "en"))
    assert fr == en, f"missing in fr: {sorted(en - fr)}; missing in en: {sorted(fr - en)}"


def test_every_key_used_by_the_ui_exists():
    """Every data-i18n / t("…") key of every screen (interview, admin) is in the English catalog."""
    import re

    keys = set(i18n.flatten(i18n.catalog("en")))
    static = Path(i18n.__file__).parent / "static"
    html = "".join((static / f).read_text(encoding="utf-8") for f in ("index.html", "admin.html"))
    js = "".join((static / f).read_text(encoding="utf-8") for f in ("app.js", "i18n.js", "admin.js"))
    used = set(re.findall(r'data-i18n="([\w.]+)"', html))
    used |= {k for attr in re.findall(r'data-i18n-attr="([^"]+)"', html) for k in re.findall(r":([\w.]+)", attr)}
    used |= set(re.findall(r'label="([\w.]+)"', html))
    used |= set(re.findall(r'\bt\("([\w.]+)"', js)) | set(re.findall(r'\btl\("([\w.]+)"', js))
    used |= {f"focus.rungs.{r}" for r in ("business", "workflow", "implementation", "done")}
    assert used and not (used - keys), sorted(used - keys)


def test_t_interpolates_and_falls_back():
    assert i18n.t("focus.countOf", "fr", n=2, m=5) == "2 sur 5"
    assert i18n.t("focus.countOf", "en", {"n": 1, "m": 3}) == "1 of 3"
    assert i18n.t("focus.now", "de") == "Current question"  # no de.json: English
    assert i18n.t("focus.now", "fr-CA") == "Question en cours"  # regional -> base language
    assert i18n.t("no.such.key", "fr") == "no.such.key"  # visible, never a crash
    assert i18n.t("plan.footer", "fr", stamp="2026-11-02") == "Généré le 2026-11-02 à partir de l'entretien de démarrage."
    assert i18n.fallback_chain("pt-BR") == ["pt-BR", "pt", "en"]


def test_adding_a_language_is_just_a_new_file(tmp_path):
    for code in ("en", "fr"):
        shutil.copy(LOCALES / f"{code}.json", tmp_path / f"{code}.json")
    (tmp_path / "es.json").write_text(json.dumps(
        {"_meta": {"name": "Español"}, "focus": {"now": "Pregunta actual", "countOf": "{{n}} de {{m}}"}}),
        encoding="utf-8")
    assert i18n.available(tmp_path) == ["en", "es", "fr"]
    assert i18n.t("focus.now", "es", locales_dir=tmp_path) == "Pregunta actual"
    assert i18n.t("focus.countOf", "es-MX", locales_dir=tmp_path, n=1, m=2) == "1 de 2"
    assert i18n.t("focus.next", "es", locales_dir=tmp_path) == "Next"  # missing key: English
    assert i18n.resolve_ui("es-AR", locales_dir=tmp_path) == "es"
    assert "focus.next" in i18n.missing_keys("es", ("focus.",), tmp_path)

    app = FastAPI()
    app.include_router(i18n.api_router(tmp_path))
    with TestClient(app) as http:
        langs = http.get("/api/i18n").json()["languages"]
        assert [x["code"] for x in langs] == ["en", "es", "fr"] and langs[1]["label"] == "Español"
        cat = http.get("/api/i18n/es").json()
        assert cat["has_file"] and cat["catalog"]["focus.now"] == "Pregunta actual"
        assert cat["catalog"]["focus.next"] == "Next"  # fallbacks filled in for the browser
        assert http.get("/api/i18n/not a lang").status_code == 404


def test_translator_overlay_only_fills_gaps():
    tr = i18n.Translator("de", {"plan.title": "Kickoff-Plan", "focus.now": "Aktuelle Frage"})
    assert tr("plan.title") == "Kickoff-Plan" and tr("plan.headings.risks") == "Risks"
    fr = i18n.Translator("fr", {"plan.title": "should not win"})
    assert fr("plan.title") == "Proposition d'intégration"


def test_language_tags_and_names():
    assert i18n.speech_locale("de") == "de-DE" and i18n.speech_locale("en_gb") == "en-GB"
    assert i18n.normalize_speech("de, en-US de-DE") == ["de-DE", "en-US"]
    assert i18n.stream_ids(["de-DE", "en-US"]) == {"de": "de-DE", "en": "en-US"}
    assert i18n.stream_ids(["en-US", "en-GB"]) == {"en-us": "en-US", "en-gb": "en-GB"}
    assert i18n.name("de", "fr") == "allemand" and i18n.english_name("de-DE") == "German"
    assert i18n.resolve_ui("de", "fr") == "fr" and i18n.resolve_ui("de") == "en"
    assert i18n.secondary("fr", "fr") == "en" and i18n.secondary("de", "fr") == "fr"
    assert i18n.secondary("en", "en") == "fr"
    with pytest.raises(i18n.LanguageError):
        i18n.normalize_speech("klingon-xx-1234567890")
    with pytest.raises(i18n.LanguageError):
        i18n.normalize_speech([])


# --- questionnaire variants ---------------------------------------------------------------------

MULTI_Q = """\
client: ACME
slug: acme
language: de
speech_locales: [de-DE, en-US]
consent: {de: "Mit Ihrer Zustimmung…", en: "With your consent…"}
themes:
  - id: ops
    title: {de: Betrieb, fr: Opérations, en: Operations}
    questions:
      - id: ops-pain
        priority: 1
        de: "Welche Aufgabe hat letzte Woche am meisten Zeit gekostet?"
        en: "Which task took the most time last week?"
        expect:
          business:
            - {de: "die Aufgabe", en: "the task"}
            - "was es kostet"
      - id: ops-tools
        text: {de: "Welche Werkzeuge?", es: "¿Qué herramientas?"}
"""


def test_questionnaire_accepts_any_language_variant(tmp_path):
    p = tmp_path / "q.yaml"
    p.write_text(MULTI_Q, encoding="utf-8")
    q = load_questionnaire(p)
    assert q.language == "de" and q.speech_locales == ["de-DE", "en-US"]
    pain, tools = q.themes[0].questions
    assert pain.text == {"de": "Welche Aufgabe hat letzte Woche am meisten Zeit gekostet?",
                         "en": "Which task took the most time last week?"}
    assert pain.en.startswith("Which") and pain.fr == ""
    assert tools.text["es"] == "¿Qué herramientas?"
    assert q.consent.get("de").startswith("Mit") and q.consent.get("it").startswith("With")  # fallback

    st = SessionState.from_questionnaire(q)
    assert st.language == "de" and st.ui_language == "en" and st.speech_locales == ["de-DE", "en-US"]
    s = st.get("ops-pain")
    assert s.text("de").startswith("Welche") and s.text("fr").startswith("Which")  # fr missing -> en
    assert s.expect["business"] == ["die Aufgabe", "was es kostet"]  # identity in the source language
    assert s.expect_text("die Aufgabe", "en") == "the task"
    assert s.match_expect("the task") == "die Aufgabe"  # the model copies the English item
    assert st.themes[0].title("fr") == "Opérations" and st.consent_text("de").startswith("Mit")


def test_legacy_fr_en_questionnaires_load_unchanged(client_cfg):
    q = real_client().questionnaire
    assert q.language == "fr" and q.speech_locales is None
    first = q.themes[0].questions[0]
    assert first.fr.startswith("Si demain") and first.en.startswith("If tomorrow")
    assert q.consent.fr and q.consent.en
    st = SessionState.from_questionnaire(client_cfg.questionnaire)
    assert st.speech_locales == ["fr-FR", "en-US"] and st.ui_language == "fr"


def test_questionnaire_rejects_bad_language_keys(tmp_path):
    p = tmp_path / "q.yaml"
    data = yaml.safe_load(MULTI_Q)
    data["themes"][0]["questions"][1] = {"id": "nothing"}
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ValueError, match="no wording"):
        load_questionnaire(p)
    data["themes"][0]["questions"][1] = {"id": "x", "text": {"not-a-language-123456789": "?"}}
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ValueError, match="not a language code"):
        load_questionnaire(p)


# --- prompts / plan in the interview language -----------------------------------------------------


def test_prompts_follow_the_interview_and_spoken_languages(state, client_cfg):
    state.set_languages(interview="de", speech=["de-DE", "en-US"])
    sync = build_sync_system(state, client_cfg.glossary)
    fast = build_fast_system(state, client_cfg.glossary)
    plan = build_plan_system(state, client_cfg.glossary)
    for text in (sync, fast):
        assert "Output language: German" in text and "German (lines tagged DE)" in text
        assert "French (lines tagged FR)" not in text
    assert "Write every text value in German" in plan and "German (lines tagged DE)" in plan
    one = SessionState.from_questionnaire(client_cfg.questionnaire)
    one.set_languages(speech=["it-IT"])
    assert "One on-device speech recogniser" in describe_streams(one)


def test_queue_edit_in_any_language(state):
    state.set_languages(interview="de")
    state.apply_sync_result(result(queue_edits=[
        edit("add", "import-zoll", "import", None, None, text="Wer macht die Zollabwicklung?"),
        edit("reword", "stock-tools", fr="Le stock est-il dans l'ERP ?", en="Is stock in the ERP?",
             text="Ist der Bestand im ERP?"),
    ]))
    q = state.get("import-zoll")
    assert q.text("de") == "Wer macht die Zollabwicklung?" and q.text_fr == "Wer macht die Zollabwicklung?"
    assert state.get("stock-tools").text("de") == "Ist der Bestand im ERP?"
    assert state.get("stock-tools").text_fr == "Le stock est-il dans l'ERP ?"


def test_plan_and_log_use_the_interview_language(state):
    state.set_languages(interview="en")
    md = assemble_plan(offline_plan(state), state)
    assert "# Integration proposal — Brioche & Four" in md and "## Interview log" in md and "Generated on" in md
    state.set_languages(interview="de")
    state.i18n_overlay["de"] = {"plan.title": "Kickoff-Plan", "plan.headings.log": "Gesprächsprotokoll",
                                "plan.footer": "Erstellt am {{stamp}}."}
    md = assemble_plan(offline_plan(state), state)
    assert md.startswith("# Kickoff-Plan — Brioche & Four") and "## Gesprächsprotokoll" in md and "Erstellt am" in md
    assert plan_headings("de", state.i18n_overlay["de"])["risks"] == "Risks"  # not translated: English


def test_offline_follow_up_is_localised():
    state = SessionState.from_questionnaire(real_client().questionnaire)  # has `expect`
    state.set_languages(interview="en")
    from pacenote.llm import OfflineEngine as OE
    from pacenote.transcript import Segment

    seg = Segment(1, "en", 0, 1, "If orders grew, our order desk would be swamped first.")
    res = OE().sync("", "", state=state, delta=[seg])
    ups = [u for u in res.question_updates if u.follow_up]
    assert ups and all(u.follow_up.startswith("Concretely:") for u in ups)


# --- on-the-fly translation ---------------------------------------------------------------------


class TranslatingEngine(OfflineEngine):
    """Offline engine + a fake `translate` (stands in for `claude -p` on the fast model)."""

    def __init__(self, fail: bool = False):
        super().__init__()
        self.calls = []
        self.fail = fail

    def translate(self, system, user, *, schema):
        self.calls.append((system, user))
        if self.fail:
            from pacenote.llm import SyncFailed
            raise SyncFailed("Claude CLI timed out")
        items = json.loads(user.split("<items>", 1)[1].split("</items>", 1)[0])
        lang = user.split("(", 1)[1].split(")", 1)[0]  # "Translate into German (de)."
        return {"items": [{"id": i["id"], "text": f"[{lang}] {i['text']}"} for i in items]}


def test_missing_items_and_apply(real_state):
    state = real_state
    items = translate.missing_items(state, "de")
    kinds = {i["id"].split("|")[0] for i in items}
    assert kinds == {"q", "e", "t", "consent", "k"}
    assert any(i["id"] == "k|plan.title" for i in items)
    assert not translate.missing_items(state, "fr") and not [i for i in translate.missing_items(state, "en")
                                                               if i["id"].startswith("q|")]
    n = translate.translate_state(state, "de", TranslatingEngine())
    assert n == len(items) and not translate.missing_items(state, "de")
    q = state.get("capacity-limit")
    assert q.text("de").startswith("[de] Si demain") and "de" in q.machine
    e = q.expect["business"][0]
    assert q.expect_text(e, "de") == f"[de] {e}" and q.match_expect(f"[de] {e}") == e
    assert state.tr("de")("plan.title") == "[de] Integration proposal"
    assert state.consent_text("de").startswith("[de]")


def test_translation_cache_avoids_a_second_call(tmp_path, client_cfg):
    cache = tmp_path / "cache.json"
    eng = TranslatingEngine()
    s1 = SessionState.from_questionnaire(client_cfg.questionnaire)
    translate.translate_state(s1, "de", eng, cache_path=cache)
    first = len(eng.calls)
    assert first == -(-len(translate.missing_items(SessionState.from_questionnaire(client_cfg.questionnaire), "de"))
                       // translate.CHUNK)  # parallel chunks of CHUNK texts
    s2 = SessionState.from_questionnaire(client_cfg.questionnaire)
    translate.translate_state(s2, "de", eng, cache_path=cache)
    assert len(eng.calls) == first and s2.get("strategy-success").text("de") == s1.get("strategy-success").text("de")


def test_engine_without_translate_keeps_the_fallback(state):
    with pytest.raises(NotImplementedError):
        translate.translate_state(state, "de", OfflineEngine())
    assert state.get("strategy-success").text("de").startswith("A year")  # English fallback


def test_cli_engine_translates_through_claude_p(tmp_path, monkeypatch):
    """The translation call goes through the Claude Code CLI (subscription), never an API key."""
    from pacenote.llm import ClaudeCliEngine

    seen = {}
    claude = tmp_path / "claude"
    claude.write_text("#!/bin/sh\nenv > " + str(tmp_path / "env.txt") + "\necho \"$@\" > " + str(tmp_path / "args.txt")
                      + "\ncat > /dev/null\necho '{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":false,"
                      "\"result\":\"\",\"structured_output\":{\"items\":[{\"id\":\"q|a\",\"text\":\"Hallo\"}]}}'\n")
    claude.chmod(0o755)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-be-forwarded")
    out = ClaudeCliEngine(claude_bin=str(claude)).translate("sys", "user", schema=translate.TranslationResult.model_json_schema())
    assert out == {"items": [{"id": "q|a", "text": "Hallo"}]}
    args = (tmp_path / "args.txt").read_text()
    assert "-p" in args.split() and "--json-schema" in args and "claude-haiku-5-5" in args
    assert "ANTHROPIC_API_KEY" not in (tmp_path / "env.txt").read_text()


# --- switching languages in a session --------------------------------------------------------------


def _session(tmp_path, client_cfg, engine=None):
    return Session(
        client=client_cfg,
        session_dir=tmp_path / "web",
        engine=engine or TranslatingEngine(),
        source_factory=lambda d: ReplayForTests(),
        settings=Settings(sync_interval=3600, poll_interval=0.05),
    )


class ReplayForTests(capture.ReplaySource):
    def __init__(self):
        super().__init__({"fr": SAMPLES / "fr.srt", "en": SAMPLES / "en.srt"}, speed=0)
        self.locales = None

    def configure_locales(self, locales):
        self.locales = list(locales)


def test_switching_languages_over_the_api_keeps_the_interview_state(tmp_path, real_cfg):
    session = _session(tmp_path, real_cfg)
    with TestClient(create_app(session)) as http:
        st = http.get("/api/state").json()
        assert st["ui_language"] == "fr" and st["language"] == "fr"  # default: the client's language
        assert st["languages"]["speech"] == ["fr-FR", "en-US"] and st["languages"]["streams"] == {"fr": "fr-FR", "en": "en-US"}
        st = http.post("/api/language", json={"speech": ["de-DE", "en-US"]}).json()
        assert st["languages"]["speech"] == ["de-DE", "en-US"]
        st = http.post("/api/start").json()
        assert session.source.locales == ["de-DE", "en-US"]  # the capture got the picked locales
        session.state.apply_sync_result(result(question_updates=[upd(
            "capacity-limit", "partial", "Délais divisés par deux", piece="Délais divisés par deux",
            follow_up="Qui décroche ces appels aujourd'hui ?")]))
        before = http.get("/api/state").json()
        assert "alt" not in before["current"] and "secondary" not in before["consent"]  # one language
        assert "secondary" not in before["languages"]
        fast_before = build_fast_system(session.state, "")

        # display language: labels AND question texts / follow-ups / items, one language at a time
        st = http.post("/api/language", json={"ui": "en"}).json()
        assert st["ui_language"] == "en" and st["language"] == "fr"
        assert st["current"]["text"].startswith("If tomorrow")  # the questionnaire's own English
        _wait(lambda: http.get("/api/state").json()["current"]["follow_up"].startswith("[en] "))
        st = http.get("/api/state").json()
        fu = [e for e in st["current"]["thread"] if e["kind"] == "followup"]
        assert fu and fu[-1]["text"] == "[en] Qui décroche ces appels aujourd'hui ?"
        assert [e for e in st["current"]["thread"] if e["kind"] == "answer"] == [
            e for e in before["current"]["thread"] if e["kind"] == "answer"]  # what was said stays as said
        assert st["status"]["must_open"] and not any(t.startswith("Si demain") for t in st["status"]["must_open"])
        # ...but generation is untouched: the follow-up is stored and prompted in French
        stored = session.state.get("capacity-limit").thread[-1]
        assert stored.text == "Qui décroche ces appels aujourd'hui ?" and stored.lang == "fr"
        assert build_fast_system(session.state, "") == fast_before
        assert session.state.speech_locales == ["de-DE", "en-US"] and session.source.locales == ["de-DE", "en-US"]
        assert http.post("/api/language", json={"ui": "fr"}).json()["current"]["text"] == before["current"]["text"]
        http.post("/api/language", json={"ui": "en"})
        assert http.post("/api/language", json={"ui": "xx"}).status_code == 400
        assert http.post("/api/language", json={"speech": ["fr-FR"]}).status_code == 409  # capture is running

        # interview language: translated in the background, then everything follows
        st = http.post("/api/language", json={"interview": "de"}).json()
        deadline = time.time() + 5
        while session.state.translation.get("status") != "done" and time.time() < deadline:
            time.sleep(0.05)
        st = http.get("/api/state").json()
        cur = st["current"]
        assert st["language"] == "de" and cur["id"] == before["current"]["id"]
        q = session.state.get(cur["id"])
        assert q.text("de").startswith("[de] Si demain") and "de" in q.machine
        assert all(q.expect_text(e, "de").startswith("[de] ") for e in q.all_expect())
        assert cur["text"].startswith("If tomorrow")  # the screen stays in the display language (en)
        assert st["languages"]["translation"]["status"] == "done"
        assert st["languages"]["overlay"]["interview.bannerDone"].startswith("[de]")
        assert session.state.consent_text("de").startswith("[de]")
        assert "Output language: German" in build_fast_system(session.state, "")
        assert "[de] " in build_fast_user(session.state, [], [])

        # back to French: the questionnaire's own text again, no new translation needed
        st = http.post("/api/language", json={"interview": "fr", "ui": "fr"}).json()
        assert st["current"]["text"].startswith("Si demain") and not st["current"]["translated"]


def _wait(cond, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while not cond():
        assert time.time() < deadline, "timed out"
        time.sleep(0.05)


def test_display_language_never_touches_generation_or_capture(tmp_path, real_cfg):
    """The display selector (S.ui_language) is display only: the interview language (prompts,
    plan, generated follow-ups) and the speech locales stay exactly as they were."""
    session = _session(tmp_path, real_cfg)

    async def go():
        st = session.state
        sync0, fast0 = build_sync_system(st, real_cfg.glossary), build_fast_system(st, real_cfg.glossary)
        speech0, streams0 = list(st.speech_locales), st.languages.stream_ids()
        await session.set_languages(ui="en")
        assert st.language == "fr" and st.ui_language == "en"
        assert build_sync_system(st, real_cfg.glossary) == sync0 and build_fast_system(st, real_cfg.glossary) == fast0
        assert st.speech_locales == speech0 and st.languages.stream_ids() == streams0
        # a follow-up generated now is generated (and stored, and prompted) in French
        st.apply_sync_result(result(question_updates=[upd(st.focus_id, "partial", "x", piece="x",
                                                          follow_up="Combien de commandes par jour ?")]))
        e = st.current_question.thread[-1]
        assert e.kind == "followup" and e.lang == "fr" and e.text == "Combien de commandes par jour ?"
        assert "Combien de commandes par jour ?" in build_fast_user(st, [], [])
        session._ensure_display_translation()
        await session._display_task
        assert e.text == "Combien de commandes par jour ?" and e.texts == {"en": "[en] Combien de commandes par jour ?"}
        v = session.view()
        assert v["current"]["follow_up"] == "[en] Combien de commandes par jour ?" and v["language"] == "fr"
        # back to the interview language: the generated text itself, no translation involved
        await session.set_languages(ui="fr")
        assert session.view()["current"]["follow_up"] == "Combien de commandes par jour ?"
        await session.shutdown()

    asyncio.run(go())


def test_display_translation_without_a_translating_engine_is_not_retried(tmp_path, real_cfg):
    session = _session(tmp_path, real_cfg, OfflineEngine())

    async def go():
        st = session.state
        st.apply_sync_result(result(question_updates=[upd(st.focus_id, "partial", "x", piece="x",
                                                          follow_up="Combien de commandes par jour ?")]))
        await session.set_languages(ui="en")
        await session._display_task
        task = session._display_task
        session._ensure_display_translation()
        assert session._display_task is task  # nothing new to try: no loop of failing calls
        assert session.view()["current"]["follow_up"] == "Combien de commandes par jour ?"  # as generated
        await session.shutdown()

    asyncio.run(go())


def test_failed_translation_keeps_fallback_texts(tmp_path, client_cfg):
    session = _session(tmp_path, client_cfg, TranslatingEngine(fail=True))

    async def go():
        await session.set_languages(interview="de")
        await session._translate_task
        return session.view()

    st = asyncio.run(go())
    assert st["languages"]["translation"]["status"] == "error" and "timed out" in st["languages"]["translation"]["error"]
    assert session.state.current_question.text("de").startswith("A year")  # English fallback, the interview goes on
    assert st["current"]["text"].startswith("Dans un an")  # the screen: the display language (fr)


def test_ui_language_survives_a_reload(tmp_path, client_cfg):
    session = _session(tmp_path, client_cfg)
    with TestClient(create_app(session)) as http:
        http.post("/api/language", json={"ui": "en"})
    assert SessionState.load(tmp_path / "web" / "state.json").ui_language == "en"


# --- configurable speech locales (listening helper mocked) -----------------------------------------


def test_locales_map():
    assert locales_map(None) == {"fr": "fr-FR", "en": "en-US"} == capture.DEFAULT_LOCALES
    assert locales_map(["de-DE", "en-US"]) == {"de": "de-DE", "en": "en-US"}
    assert locales_map("de,en") == {"de": "de-DE", "en": "en-US"}


def test_preflight_any_locales_in_one_start(tmp_path):
    helper, log = fake_listen(tmp_path)
    msgs = []
    preflight(["de-DE", "en-US"], helper_bin=str(helper), on_progress=msgs.append)
    assert [r["argv"] for r in runs(log)] == [["listen", "--locale", "de-DE", "--locale", "en-US"]]
    assert "de-DE: model loaded" in "\n".join(msgs) and "fr-FR" not in "\n".join(msgs)


def test_capture_runs_the_selected_locales(tmp_path):
    helper, log = fake_listen(tmp_path)
    cap = ListenCapture(tmp_path / "sess", helper_bin=str(helper), **FAST)
    cap.configure_locales(["de-DE", "en-US"])
    cap.start()
    try:
        assert [r["argv"] for r in runs(log)] == [["listen", "--locale", "de-DE", "--locale", "en-US"]]
        assert set(cap.stream_status()) == {"de", "en"} and all(v["alive"] for v in cap.stream_status().values())
        with pytest.raises(capture.CaptureError):
            cap.configure_locales(["fr-FR"])
    finally:
        cap.stop()
    assert {c.stream for c in cap.poll(final=True)} == {"de", "en"}
    assert (tmp_path / "sess" / "de.srt").exists() and (tmp_path / "sess" / "en.srt").exists()


def test_pacenote_check_uses_the_selected_locales(tmp_path, capsys):
    helper, log = fake_listen(tmp_path)
    code = main(["check", "--client", "brioche-four", "--clients-dir", str(CLIENTS), "--listen-bin", str(helper),
                 "--engine", "offline", "--locales", "de-DE,en-US"])
    out = capsys.readouterr().out
    assert code == 0 and "spoken languages: de-DE, en-US" in out
    assert "de-DE and en-US: speech models installed and loaded, microphone live" in out
    assert [r["argv"] for r in runs(log)] == [["listen", "--locale", "de-DE", "--locale", "en-US"]]


def test_questionnaire_speech_locales_are_the_default_for_check(tmp_path, capsys):
    cdir = tmp_path / "clients" / "acme"
    cdir.mkdir(parents=True)
    (cdir / "questionnaire.yaml").write_text(MULTI_Q, encoding="utf-8")
    helper, log = fake_listen(tmp_path)
    code = main(["check", "--client", "acme", "--clients-dir", str(tmp_path / "clients"), "--listen-bin", str(helper),
                 "--engine", "offline"])
    assert code == 0 and "spoken languages: de-DE, en-US" in capsys.readouterr().out


# --- the live screen (real browser, when Chromium + Playwright are available) ---------------------

BROWSER_SCRIPT = r"""
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage();
  const errors = [];
  p.on('pageerror', e => errors.push(e.message));
  await p.goto(process.argv[2]);
  await p.waitForFunction(() => document.querySelector('#start').textContent === 'Commencer');
  await p.click('#consent-ui button[data-lang="en"]');       // switcher on the consent screen
  await p.waitForFunction(() => document.querySelector('#start').textContent === 'Start');
  await p.click('#start');
  await p.waitForFunction(() => document.querySelector('#consent').classList.contains('hidden')
    && document.querySelector('#current').textContent.startsWith('A year'));
  const snap = () => p.evaluate(() => ({
    label: document.querySelector('[data-i18n="focus.theme"]').textContent,
    q: document.querySelector('#current').textContent, lang: document.documentElement.lang }));
  const en = await snap();
  await p.keyboard.press('l');                                 // keyboard switch, no reload
  await p.waitForFunction(() => document.documentElement.lang === 'fr'
    && document.querySelector('#current').textContent.startsWith('Dans un an'));
  const fr = await snap();
  const navs = await p.evaluate(() => performance.getEntriesByType('navigation').length);
  console.log(JSON.stringify({ en, fr, navs, errors }));
  await b.close();
})().catch(e => { console.error(e); process.exit(1); });
"""


def _node_playwright() -> bool:
    if not shutil.which("node"):
        return False
    return subprocess.run(["node", "-e", "require('playwright')"], capture_output=True).returncode == 0


@pytest.mark.skipif(not _node_playwright(), reason="needs node + playwright + chromium")
def test_browser_switches_language_instantly(tmp_path, client_cfg):
    import socket

    import uvicorn

    session = _session(tmp_path, client_cfg, OfflineEngine())
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(create_app(session), host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    try:
        deadline = time.time() + 10
        while not server.started and time.time() < deadline:
            time.sleep(0.05)
        script = tmp_path / "check.js"
        script.write_text(BROWSER_SCRIPT, encoding="utf-8")
        out = subprocess.run(["node", str(script), f"http://127.0.0.1:{port}/"], capture_output=True, text=True,
                             timeout=90, cwd=tmp_path)
        assert out.returncode == 0, out.stderr
        r = json.loads(out.stdout.strip().splitlines()[-1])
        assert r["errors"] == []
        assert r["en"]["label"] == "Study theme" and r["fr"]["label"] == "Thème d'étude"
        # one display language for labels AND the question; the interview language stays French
        assert r["en"]["q"].startswith("A year") and r["fr"]["q"].startswith("Dans un an")
        assert session.state.language == "fr"
        assert r["navs"] == 1  # one page load: switching never reloads
        assert session.state.ui_language == "fr"
    finally:
        server.should_exit = True
        th.join(timeout=10)


# --- the admin panel uses the same component --------------------------------------------------------


@pytest.mark.parametrize("page", ["index.html", "admin.html", "plan.html"])
def test_pages_have_no_hard_coded_strings_and_use_the_shared_helper(page):
    import re

    static = Path(i18n.__file__).parent / "static"
    html = (static / page).read_text(encoding="utf-8")
    assert '<script src="/static/i18n.js"></script>' in html and '<language-select ' in html and 'bind="ui"' in html
    body = re.sub(r"<!--.*?-->|<(script|style|title)[^>]*>.*?</\1>", "", html, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", body)
    words = [w for w in re.findall(r"[A-Za-zÀ-ÿ]{3,}", text) if w not in ("Pacenote",)]
    assert not words, words  # every visible string comes from the locale files


def test_admin_research_steps_carry_catalog_keys(tmp_path):
    from pacenote.research import ResearchJob, _progress

    job = ResearchJob(slug="acme", started_at=time.time())
    job.lang = "en"
    _progress(job, {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "WebSearch", "input": {"query": "ACME GmbH"}}]}})
    step = job.steps[-1]
    assert step["key"] == "admin.step.search" and step["params"] == {"query": "ACME GmbH"}
    assert step["text"] == "Search: ACME GmbH"
    assert i18n.t(step["key"], "fr", step["params"]) == "Recherche : ACME GmbH"


def test_admin_clients_take_any_interview_language(tmp_path):
    from pacenote.clients import create_client, load_info

    info = create_client(tmp_path, "acme", "ACME GmbH", "de")
    assert info.language == "de"
    q = load_client(tmp_path, "acme").questionnaire
    assert q.language == "de" and q.speech_locales == ["de-DE", "en-US"]
    assert load_info(tmp_path, "acme").language == "de"
    from pacenote.clients import ClientError
    with pytest.raises(ClientError):
        create_client(tmp_path, "bad", "Bad", "not a language")
    fr = create_client(tmp_path, "gd", "GD", "fr")
    assert fr.language == "fr" and load_client(tmp_path, "gd").questionnaire.speech_locales == ["fr-FR", "en-US"]


def test_standalone_admin_app_serves_the_catalogs(tmp_path):
    from pacenote.admin import create_admin_app

    with TestClient(create_admin_app(tmp_path)) as http:
        assert http.get("/admin").status_code == 200
        assert "fr" in [x["code"] for x in http.get("/api/i18n").json()["languages"]]
        assert http.get("/api/i18n/fr").json()["catalog"]["admin.sheet"] == "Fiche client"
