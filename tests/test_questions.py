"""The questions the CEO hears: concrete, capacity-first, no AI jargon, each mapped to its slots.

The CEO meets a forward-deployed engineer for the first time and does not know how AI could help,
so every question asks about his work (who, which software or file, how often, how long), never
about technology, and feeds the integration slots the plan is written from.
"""

from __future__ import annotations

import json
import re

import pytest

from pacenote import i18n
from pacenote.clients import create_client
from pacenote.contract import SLOT_KEYS
from pacenote.prompts import QUESTION_STYLE, build_fast_system, build_fast_user, build_sync_system, build_sync_user
from pacenote.questionnaire import Questionnaire, load_client
from pacenote.state import SessionState

from .conftest import CLIENTS, real_client

# Words a CEO new to AI should never hear in a question (FR and EN).
BANNED = [
    re.compile(r"\b(IA|AI)\b"),  # case-sensitive: "j'ai", "mai" are fine
    *(re.compile(p, re.I) for p in (
        r"intelligence artificielle", r"artificial intelligence", r"\bagents?\b", r"automat",
        r"\bLLMs?\b", r"chat ?gpt", r"\bGPT", r"\b(chat)?bots?\b", r"\brobots?\b", r"algorithm",
        r"machine learning", r"\bprompts?\b", r"\bcopilot",
    )),
]


def jargon(text: str) -> list[str]:
    return [m.group(0) for p in BANNED for m in p.finditer(text)]


def _texts(q: Questionnaire) -> list[tuple[str, str]]:
    """Every text the interview asks or shows as a question: wording, ladder items, use case and
    theme titles, in every language given."""
    out: list[tuple[str, str]] = []
    for u in q.use_cases:
        out += [(f"use case {u.id} ({lang})", t) for lang, t in u.title.root.items()]
    for theme in q.themes:
        out += [(f"theme {theme.id} ({lang})", t) for lang, t in theme.title.root.items()]
        for qd in theme.questions:
            out += [(f"{qd.id} ({lang})", t) for lang, t in qd.text.items()]
            for rung in ("business", "workflow", "implementation"):
                for item in getattr(qd.expect, rung):
                    values = [item] if isinstance(item, str) else list(item.values())
                    out += [(f"{qd.id} expect/{rung}", v) for v in values]
    return out


@pytest.fixture
def template_questionnaire(tmp_path):
    create_client(tmp_path, "acme", "ACME")
    return load_client(tmp_path, "acme").questionnaire


def _questionnaires(template: Questionnaire) -> dict[str, Questionnaire]:
    return {"brioche-four": real_client().questionnaire,
            "brioche-four-slots": load_client(CLIENTS, "brioche-four-slots").questionnaire,
            "template": template}


def test_the_jargon_check_catches_ai_words_and_nothing_else():
    assert jargon("Comment l'IA pourrait-elle vous aider ?") == ["IA"]
    assert jargon("Which tasks could an agent automate?") == ["agent", "automat"]
    assert jargon("Un LLM ou un chatbot ?") == ["LLM", "chatbot"]
    assert jargon("Qu'est-ce que j'ai fait en mai, dans quel logiciel, combien de temps ?") == []
    assert jargon("Who re-keyed it, in which software, and how long did it take?") == []


def test_no_question_uses_ai_jargon(template_questionnaire):
    for name, q in _questionnaires(template_questionnaire).items():
        hits = [(where, text, jargon(text)) for where, text in _texts(q) if jargon(text)]
        assert not hits, f"{name}: {hits}"


def test_fallback_questions_of_the_plan_use_no_ai_jargon():
    for lang in i18n.available():  # asked to the client for every slot still "to confirm"
        cat = i18n.flatten(i18n.catalog(lang))
        for k in SLOT_KEYS:
            assert not jargon(cat[f"plan.slotQuestion.{k}"]), (lang, k)


def test_every_question_maps_to_its_slots(template_questionnaire):
    for name, q in _questionnaires(template_questionnaire).items():
        questions = [qd for t in q.themes for qd in t.questions]
        for qd in questions:
            slots = qd.gathered_slots()
            assert set(slots) <= set(SLOT_KEYS), (name, qd.id)
            if qd.priority == 1:
                assert slots, f"{name}: priority question {qd.id} maps to no slot"
            if qd.use_case:
                assert slots == qd.required_slots(), (name, qd.id)  # required: "Ce qui manque" and the gate
        assert {s for qd in questions for s in qd.gathered_slots()} >= {"baseline", "target"}, name


def test_example_client_feeds_every_slot_and_every_question_is_mapped():
    q = real_client().questionnaire
    questions = [qd for t in q.themes for qd in t.questions]
    assert all(qd.gathered_slots() for qd in questions)  # not only the priority ones
    assert {s for qd in questions for s in qd.gathered_slots()} == set(SLOT_KEYS)


def test_interviews_open_on_capacity_and_get_the_target_from_it(template_questionnaire):
    for name, q in _questionnaires(template_questionnaire).items():
        first_theme = q.themes[0]
        assert first_theme.id == "capacity", name
        first = first_theme.questions[0]
        assert first.priority == 1 and not first.use_case
        assert {"baseline", "target"} <= set(first.gathered_slots()), name  # target from the capacity talk
        assert "dans un an" not in first.fr.lower() and "a year from now" not in first.en.lower()
        # the target is never asked in the abstract: questions gathering it talk about time saved or more volume
        for qd in (x for t in q.themes for x in t.questions if "target" in x.gathered_slots() and not x.use_case):
            assert re.search(r"deux fois|doubl|en plus", qd.fr), (name, qd.id)


def test_the_prompts_carry_the_wording_rules_and_the_slots_to_gather():
    st = SessionState.from_questionnaire(real_client().questionnaire)
    glossary = real_client().glossary
    assert "capacity" in QUESTION_STYLE and "last week" in QUESTION_STYLE and "No AI vocabulary" in QUESTION_STYLE
    for system in (build_sync_system(st, glossary), build_fast_system(st, glossary)):
        assert QUESTION_STYLE in system and "gathers_slots" in system
    assert st.current_question.id == "capacity-limit"
    snap = json.loads(build_sync_user(st, [], [], 0).split("<state>\n", 1)[1].split("\n</state>", 1)[0])
    assert snap["current_question"]["gathers_slots"] == st.current_question.gathers
    assert set(st.current_question.gathers) >= {"baseline", "target"}
    fast = json.loads(build_fast_user(st, [], []).split("<current_question>\n", 1)[1].split("\n</current_question>", 1)[0])
    assert fast["gathers_slots"] == st.current_question.gathers
    assert "missing_slots" not in fast  # no use case: the ladder, not the slot gate, decides "answered"
