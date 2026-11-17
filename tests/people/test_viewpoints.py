"""The views heard in the meeting (pacenote/viewpoints.py): grouped by statement, not by person, each with its
kind, position, topic and verbatim quotes; a quote names an attendee only when the statement itself does.
Interviewer-side only: in the session data and the console, never in git; the plan gets the positions as
points to address, with no name, no quote and no label on anyone. After the meeting, a final transcript with
speaker labels maps the quotes to voices; a plain one leaves the speakers unavailable. Fictional company and
people only; no model call."""

from __future__ import annotations

import asyncio
import json

import pytest

from pacenote.copilot.contract import Viewpoint, parse_plan_doc, parse_sync_result
from pacenote.copilot.engines import offline_plan_doc
from pacenote.copilot.prompts import build_fast_system, build_plan_system, build_sync_system, state_snapshot
from pacenote.interview.state import SessionState
from pacenote.people import viewpoints
from pacenote.plan.finalize import render_plan_md
from tests.conftest import plan_doc, plan_use_case, result
from tests.people.test_attendees import CLERK, EXEC, HEAD, _session, _state


def vp(vid, kind, position, *quotes, topic="", use_case=None) -> dict:
    """A viewpoint as the model returns it; a quote is a text, or (text, speaker, named_by)."""
    qs = [{"text": q, "speaker": "", "named_by": ""} if isinstance(q, str) else
          {"text": q[0], "speaker": q[1], "named_by": q[2]} for q in quotes]
    return {"id": vid, "kind": kind, "position": position, "topic": topic, "use_case": use_case, "quotes": qs}


# three distinct views in one room: one keen, one worried about reliability, one against it
THREE = [
    vp("deja-utilise", "keen", "aller plus loin avec un assistant déjà utilisé",
       ("En tant que PDG, j'utilise déjà un assistant pour préparer mes comités", "a1", "En tant que PDG"),
       topic="comités de direction"),
    vp("fiabilite-extraction", "practical", "fiabilité de l'extraction sur les commandes manuscrites, mise en doute",
       "Les bons manuscrits, aucun logiciel ne les lit correctement", topic="saisie des commandes",
       use_case="bakery-orders"),
    vp("contact-client", "opposed", "garder le contact humain avec les clients",
       "Nos clients veulent parler à quelqu'un, pas à un robot", topic="relation client"),
]


def _three(people=(EXEC, HEAD, CLERK)) -> SessionState:
    st = _state(list(people))
    st.apply_sync_result(result(viewpoints=THREE), read=20)
    return st


# --- grouped by statement, never by person ------------------------------------------------------------------


def test_the_views_heard_are_grouped_by_statement_with_quotes_and_topic():
    st = _three()
    assert list(st.viewpoints) == ["deja-utilise", "fiabilite-extraction", "contact-client"]
    rel = st.viewpoints["fiabilite-extraction"]
    assert rel["kind"] == "practical" and rel["topic"] == "saisie des commandes" and rel["use_case"] == "bakery-orders"
    assert [q["text"] for q in rel["quotes"]] == ["Les bons manuscrits, aucun logiciel ne les lit correctement"]
    assert rel["quotes"][0]["speaker"] == ""  # nothing in the statement says who spoke
    # the same position heard again adds a quote to it (newest first), never a duplicate
    st.apply_sync_result(result(viewpoints=[vp("fiabilite-extraction", "cautious", "",
                                               "Si ça se trompe sur une quantité, c'est nous qui payons",
                                               "Les bons manuscrits, aucun logiciel ne les lit correctement")]), read=30)
    rel = st.viewpoints["fiabilite-extraction"]
    assert rel["kind"] == "cautious" and rel["position"].startswith("fiabilité de l'extraction")
    assert [q["text"] for q in rel["quotes"]] == ["Si ça se trompe sur une quantité, c'est nous qui payons",
                                                   "Les bons manuscrits, aucun logiciel ne les lit correctement"]


def test_a_quote_names_a_speaker_only_when_the_statement_itself_does():
    st = _three()
    assert st.viewpoints["deja-utilise"]["quotes"][0]["speaker"] == "a1"  # "En tant que PDG, j'utilise..."
    report = st.apply_sync_result(result(viewpoints=[vp(
        "contact-client", "opposed", "",
        ("Le terrain, c'est la relation", "a2", ""),  # a guess: nothing in the words names him
        ("Moi je trouve que ça déshumanise", "a2", "en tant que directeur commercial"),  # not in the quote
        ("Ça ne marchera jamais chez nous", "a9", "chez nous"))]), read=25)  # nobody by that id
    quotes = st.viewpoints["contact-client"]["quotes"]
    assert [q["speaker"] for q in quotes] == ["", "", "", ""]  # the quotes stay, without a speaker
    assert len([r for r in report.rejected if r.kind == "viewpoint"]) == 3


def test_a_viewpoint_needs_its_words_and_a_stale_read_moves_nothing():
    st = _three()
    report = st.apply_sync_result(result(viewpoints=[
        vp("sans-mots", "cautious", "pas maintenant"),  # no quote
        vp("", "keen", "", "on y va"),  # no id, no position
        vp("contact-client", "open", "pourquoi pas", "Pourquoi pas, au fond")]), read=10)  # older than the read kept
    assert list(st.viewpoints) == ["deja-utilise", "fiabilite-extraction", "contact-client"]
    assert st.viewpoints["contact-client"]["kind"] == "opposed"
    assert len([r for r in report.rejected if r.kind in ("viewpoint", "stale")]) == 3
    with pytest.raises(ValueError):
        Viewpoint.model_validate(vp("x", "driver", "x", "x"))  # no per-person stance words


def test_at_most_eight_viewpoints():
    st = _state([EXEC])
    st.apply_sync_result(result(viewpoints=[vp(f"v{i}", "open", f"position {i}", f"citation {i}")
                                            for i in range(10)]), read=5)
    assert len(st.viewpoints) == viewpoints.MAX_VIEWPOINTS


# --- interviewer-side only -------------------------------------------------------------------------------------


def test_the_console_shows_the_views_and_the_session_data_keeps_them(tmp_path):
    s = _session(tmp_path)
    s.state.apply_sync_result(result(viewpoints=THREE), read=20)
    v = s.view()["viewpoints"]
    assert [x["kind"] for x in v["items"]] == ["keen", "opposed", "practical"]
    keen = v["items"][0]
    assert keen["quotes"][0] == {"text": "En tant que PDG, j'utilise déjà un assistant pour préparer mes comités",
                                 "speaker": "Hélène Varenne (PDG)", "voice": "", "voice_who": ""}
    assert v["items"][2]["quotes"][0]["speaker"] == "" and v["speakers"] == {}
    s.persist()  # sessions/ is git-ignored; nothing goes next to the client
    saved = json.loads((s.session_dir / "state.json").read_text(encoding="utf-8"))
    assert saved["viewpoints"]["contact-client"]["kind"] == "opposed"
    client_files = [p.read_text(encoding="utf-8") for p in s.client.dir.rglob("*") if p.is_file()
                    and p.suffix in (".yaml", ".json", ".md")]
    assert "robot" not in json.dumps(client_files)


def test_the_sync_prompt_groups_statements_and_never_guesses_speakers():
    st = _three()
    sync = " ".join(build_sync_system(st, "", "").split())
    for phrase in ("# The views heard on AI and the change, by statement", "group the statements, never the people",
                   "speaker = an attendee's id only when the statement itself names who speaks",
                   "Never guess who said it from the content, the persona or the order of the lines",
                   "on a concern, stay concrete", "- viewpoints and ask_to:"):
        assert phrase in sync, phrase
    assert "# The views heard" not in build_fast_system(st, "", "")  # the full sync only
    snap = state_snapshot(st)["viewpoints"]
    assert snap[1] == {"id": "fiabilite-extraction", "kind": "practical",
                       "position": "fiabilité de l'extraction sur les commandes manuscrites, mise en doute",
                       "topic": "saisie des commandes", "use_case": "bakery-orders",
                       "quotes": ["Les bons manuscrits, aucun logiciel ne les lit correctement"]}


# --- the plan: the diversity of views, neutrally -----------------------------------------------------------------


def test_the_plan_writer_gets_positions_never_quotes_or_speakers():
    st = _three()
    snap = json.dumps(state_snapshot(st, for_plan=True), ensure_ascii=False)
    assert "fiabilité de l'extraction" in snap
    for leak in ("robot", "En tant que PDG", "Varenne", '"a1"', "quotes", "speaker"):
        assert leak not in snap, leak
    plan = " ".join(build_plan_system(st, "").split())
    assert "adoption.to_address = the points the cautious, opposed and practical views raise" in plan
    assert "never who raised it, no name, no role, no label on anyone" in plan


def test_the_plan_lists_the_points_to_address_in_wave_2_with_no_one_named():
    st = _three()
    md = render_plan_md(offline_plan_doc(st), st)  # no writer points: the concerns' positions, as topics
    wave2 = md[md.index("2. **"):md.index("3. **")]
    assert "Points soulevés pendant la réunion, à traiter :" in wave2
    assert "fiabilité de l'extraction sur les commandes manuscrites, mise en doute (saisie des commandes)" in wave2
    assert "garder le contact humain avec les clients (relation client)" in wave2
    assert "assistant déjà utilisé" not in md  # a keen view is not a point to address
    for leak in ("Varenne", "Ardent", "Morin", "robot", "Opposé", "Prudent", "réticen"):
        assert leak not in md, leak


def test_a_written_point_that_names_or_labels_someone_is_dropped():
    st = _three()
    doc = parse_plan_doc(plan_doc(plan_use_case("bakery-orders", "Saisie des commandes", horizon="h1"), adoption={
        "lighthouse": "bakery-orders", "to_address": [
            "la fiabilité de l'extraction sur les bons manuscrits", "rassurer Paul sur le contact client",
            "les commerciaux réticents", "qui garde la validation finale"]}))
    from pacenote.plan.finalize import apply_horizons

    md = render_plan_md(apply_horizons(doc, st), st)
    assert "la fiabilité de l'extraction sur les bons manuscrits" in md and "qui garde la validation finale" in md
    assert "Paul" not in md and "réticents" not in md


def test_no_view_heard_no_points():
    st = _state([EXEC])
    md = render_plan_md(offline_plan_doc(st), st)
    assert "Points soulevés" not in md


# --- after the meeting: speakers only from a transcript that has them ----------------------------------------------

LABELLED = """\
Hélène Varenne: En tant que PDG, j'utilise déjà un assistant pour préparer mes comités.
Speaker 2: Les bons manuscrits, aucun logiciel ne les lit correctement, je vous le dis.
Hélène Varenne: On peut essayer sur les commandes par mail d'abord.
Speaker 3: Nos clients veulent parler à quelqu'un, pas à un robot.
Speaker 2: Et si ça se trompe sur une quantité ?
"""


def test_a_labelled_transcript_maps_each_quote_to_its_voice():
    st = _three()
    got = viewpoints.refine(st, LABELLED, "pasted")
    assert got["status"] == "mapped" and got["mapped"] == 3
    assert got["labels"] == ["Hélène Varenne", "Speaker 2", "Speaker 3"]
    v = {x["id"]: x for x in viewpoints.view(st)["items"]}
    assert v["deja-utilise"]["quotes"][0]["voice_who"] == "Hélène Varenne (PDG)"  # a label that is an attendee
    assert v["fiabilite-extraction"]["quotes"][0]["voice"] == "Speaker 2"  # an anonymous voice stays anonymous
    assert v["fiabilite-extraction"]["quotes"][0]["voice_who"] == ""
    assert v["contact-client"]["quotes"][0]["voice"] == "Speaker 3"


def test_a_live_quote_with_mishearings_still_finds_its_line():
    st = _state([EXEC, HEAD])
    st.apply_sync_result(result(viewpoints=[vp("fiab", "practical", "fiabilité",
                                               "les bons manuscrit aucun logiciel les lit correctement")]), read=3)
    viewpoints.refine(st, LABELLED)
    assert st.viewpoints["fiab"]["quotes"][0]["voice"]["label"] == "Speaker 2"


@pytest.mark.parametrize("text", [
    "En tant que PDG, j'utilise déjà un assistant.\nLes bons manuscrits, aucun logiciel ne les lit.",  # dictation
    "Note: à relancer.\nLes bons manuscrits, aucun logiciel ne les lit.\nNos clients veulent parler à quelqu'un.\n"
    "Et si ça se trompe ?\nOn verra.",  # one stray "Note:" line is not a speaker
    "",
])
def test_a_plain_transcript_leaves_the_speakers_unavailable(text):
    st = _three()
    got = viewpoints.refine(st, text)
    assert got["status"] == "unavailable" and got["mapped"] == 0
    assert all("voice" not in q for v in st.viewpoints.values() for q in v["quotes"])


def test_pasting_the_final_transcript_refines_the_speakers(tmp_path):
    s = _session(tmp_path)
    s.state.apply_sync_result(result(viewpoints=THREE), read=20)
    asyncio.run(s.set_wispr(LABELLED))
    assert s.view()["viewpoints"]["speakers"]["status"] == "mapped"
    asyncio.run(s.set_wispr("Un texte dicté, sans intervenants."))
    assert s.view()["viewpoints"]["speakers"]["status"] == "unavailable"
    asyncio.run(s.set_wispr(""))
    assert s.view()["viewpoints"]["speakers"] == {}
    snap = json.dumps(state_snapshot(s.state, for_plan=True), ensure_ascii=False)
    assert "Speaker" not in snap and "voice" not in snap  # the voices never reach the plan writer


# --- old data still loads --------------------------------------------------------------------------------------


def test_a_session_saved_with_stances_and_an_old_sync_reply_still_load(tmp_path):
    st = _state([EXEC, HEAD])
    data = json.loads(st.model_dump_json())
    data["attendees"][1]["stance"] = {"label": "opposed", "confidence": 0.7, "evidence": ["pas pour nous"], "read": 3}
    data.pop("viewpoints"), data.pop("viewpoint_speakers")
    old = SessionState.model_validate(data)
    assert old.viewpoints == {} and old.attendees[1]["role"] == "Directeur commercial"
    raw = json.loads(result().model_dump_json())
    raw.pop("viewpoints")
    raw["attendee_stances"] = [{"attendee": "a2", "stance": "opposed", "evidence": "non", "confidence": 0.5,
                                "attributed": "self"}]
    assert parse_sync_result(raw).viewpoints == []
    doc = json.loads(offline_plan_doc(st).model_dump_json())
    doc["adoption"].pop("to_address")  # a plan.json written before the points to address
    assert parse_plan_doc(doc).adoption.to_address == []


@pytest.mark.parametrize("lang", ["en", "fr"])
def test_every_viewpoint_string_exists_in_both_languages(lang):
    from pacenote.shared.i18n import catalog

    loc = catalog(lang)
    assert set(loc["viewpoints"]["kind"]) == set(viewpoints.KINDS)
    for key in ("viewpoints", "viewpointsNote", "viewpointsEmpty", "speakersMapped", "speakersUnavailable", "voice"):
        assert loc["console"][key], key
    assert loc["plan"]["adoption"]["toAddress"]
