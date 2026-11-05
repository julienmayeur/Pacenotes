from __future__ import annotations

import atexit
import shutil
import tempfile
from pathlib import Path

import pytest

from pacenote.contract import PLAN_SECTIONS, PlanDelta, SyncResult, empty_maps
from pacenote.questionnaire import load_client
from pacenote.state import SessionState

from .srt_samples import write_samples

ROOT = Path(__file__).resolve().parent.parent
CLIENTS = ROOT / "tests" / "fixtures" / "clients"  # frozen questionnaire with stable ids
REAL_CLIENTS = ROOT / "clients.example"  # the committed, fictional example client (clients/ is git-ignored)
# Synthetic replay transcripts (tests/srt_samples.py), written once per test run: no recording is committed.
SRT_DIR = write_samples(Path(tempfile.mkdtemp(prefix="pacenote-test-srt-")))
atexit.register(shutil.rmtree, SRT_DIR, True)
SAMPLES = SRT_DIR / "clean"
NOISY = SRT_DIR / "noisy"


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    """Never read or write the real ~/.pacenote/config.json (the "prochain client")."""
    monkeypatch.setenv("PACENOTE_CONFIG", str(tmp_path / "pacenote-config.json"))


def real_client(slug: str = "brioche-four"):
    """The real client as committed: never the local, untracked pre-plan (clients/<slug>/preplan.json),
    which adds a theme of questions and would make these tests depend on the machine."""
    return load_client(REAL_CLIENTS, slug, use_preplan=False)


@pytest.fixture
def client_cfg():
    return load_client(CLIENTS, "brioche-four")


@pytest.fixture
def state(client_cfg):
    return SessionState.from_questionnaire(client_cfg.questionnaire)


def result(**kw) -> SyncResult:
    """A contract-valid SyncResult with empty defaults, overridable per test."""
    base = dict(
        question_updates=[],
        slots=[],
        maps=empty_maps(),
        recipe_matches=[],
        queue_edits=[],
        next_questions=[],
        coverage=[],
        plan_delta=PlanDelta(**{s: [] for s in PLAN_SECTIONS}),
        clean_passages=[],
        enough_data=False,
        enough_data_reason="",
    )
    base.update(kw)
    return SyncResult.model_validate(base)


def upd(qid, status, answer="", evidence="", covered=None, rung=None, follow_up="", vague=False, piece=None):
    return {"question_id": qid, "status": status, "answer": answer,
            "answer_piece": answer if piece is None else piece, "evidence": evidence,
            "covered": covered or [], "rung": rung or ("done" if status == "answered" else "business"),
            "follow_up": follow_up, "vague": vague}


def edit(op, qid=None, theme=None, fr=None, en=None, after=None, reason="test", expect=None, text=None,
         use_case=None, use_case_title=None):
    return {"op": op, "question_id": qid, "theme": theme, "text_fr": fr, "text_en": en, "text": text,
            "after_id": after, "expect": expect, "use_case": use_case, "use_case_title": use_case_title,
            "reason": reason}


def fill(use_case, slot, value, evidence=""):
    return {"use_case": use_case, "slot": slot, "value": value, "evidence": evidence or value}


def plan_fact(value="", evidence="", status=None, question="", ask=""):
    return {"status": status or ("stated" if value else "to_confirm"), "value": value, "evidence": evidence,
            "question": question, "ask": ask}


def plan_use_case(uid="bakery-orders", title="Saisie des commandes", impact=4, feasibility=3, **slots):
    """A contract-valid PlanUseCase dict: every slot "to confirm" unless given (slot=plan_fact(...))."""
    from pacenote.contract import SLOT_KEYS

    return {"id": uid, "title": title, "pattern": "act_on_behalf",
            "human_decision": "Claire valide les commandes atypiques.",
            "success": {"statement": "Les commandes sont saisies le jour même.",
                        "check": "Délai de saisie avant / après, vérifié par Claire."},
            "metric": "", "not_claimed": "Aucune réduction d'effectif.",
            "first_win": {"description": "50 commandes de la semaine passée rejouées", "cases": 50, "week": 2},
            "kill_criterion": "Moins de 80 % des commandes correctes à la semaine 4.",
            "sourcing": "configure", "sourcing_reason": "Connecteur de messagerie existant.",
            "adoption": "Claire l'utilise chaque jour.", "back_office": True, "recipe": None,
            "architecture": "Un agent surveille la boîte commandes et saisit chaque commande dans l'ERP.",
            "steps": ["Lire le mail", "Extraire la commande", "Saisir dans l'ERP"], "trigger_kind": "event",
            "actions": ["extraire", "saisir"], "slots": {k: slots.get(k) or plan_fact() for k in SLOT_KEYS},
            "model": {"provider": "anthropic", "reason": "Extraction fiable de mails libres."},
            "impact": {"score": impact, "reason": "Trois heures par jour perdues."},
            "feasibility": {"score": feasibility, "reason": "Boîte mail accessible."}}


def plan_doc(*use_cases, context="Brioche & Four distribue des fours Fornaxa.", open_items=None, current_state=None, bottlenecks=None,
             metrics=None, timeline=None, recipe_candidates=None):
    return {"context": context,
            "current_state": current_state or {"people": [], "flows": [], "systems": [], "stores": [], "decisions": []},
            "bottlenecks": bottlenecks or [], "metrics": metrics or [], "use_cases": list(use_cases),
            "timeline": timeline or [], "open_items": open_items or [],
            "recipe_candidates": recipe_candidates or []}


@pytest.fixture(autouse=True)
def _isolated_listen_helper(tmp_path, monkeypatch):
    """Never build into (or run) the real ~/Library/Caches/pacenote speech helper from a test:
    tests pass a stand-in pacenote-listen explicitly."""
    monkeypatch.setenv("PACENOTE_LISTEN_CACHE", str(tmp_path / "cache"))
    monkeypatch.delenv("PACENOTE_LISTEN_BIN", raising=False)


@pytest.fixture(autouse=True)
def _testclient_host(monkeypatch):
    """Starlette's TestClient calls the app as http://testserver: let the same-origin guard
    (web_guard.py) accept that name in tests, like PACENOTE_ALLOWED_HOSTS would for a LAN name."""
    monkeypatch.setenv("PACENOTE_ALLOWED_HOSTS", "testserver")


@pytest.fixture(autouse=True)
def _no_local_speech_backend_config(monkeypatch):
    """A whisper.cpp set up on this Mac (PACENOTE_WHISPER_*, PACENOTE_SPEECH_BACKEND) must not change what the
    tests select: they configure speech backends explicitly (test_speech_backends.py)."""
    for name in ("PACENOTE_WHISPER_BIN", "PACENOTE_WHISPER_MODEL", "PACENOTE_SPEECH_BACKEND"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("pacenote.speech.WHISPER_NAMES", ("pacenote-test-no-such-whisper",))
