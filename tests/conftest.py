from __future__ import annotations

import atexit
import shutil
import tempfile
from pathlib import Path

import pytest

from pacenote.copilot.contract import PLAN_SECTIONS, PlanDelta, SyncResult, empty_maps, empty_read
from pacenote.interview.preparation import load_client
from pacenote.interview.state import SessionState

from .srt_samples import write_samples

ROOT = Path(__file__).resolve().parent.parent
# What a finished session writes next to its client (finalize: the potential pilots, the metrics; research
# runs: the pre-plan, history, profiles). Never part of the committed fixtures.
_GENERATED = ("ambassadors.json", "metrics.yaml", "research.md", "preplan.md", "preplan.json", "history", "people",
              ".*.lock", "__pycache__")


def _run_copy(src: Path) -> Path:
    """A per-run copy of a committed client folder, so a test that finalizes a session never leaves files in the
    working tree for later tests or runs to read (an ambassadors.json left in the fixtures once did)."""
    dst = Path(tempfile.mkdtemp(prefix="pacenote-test-clients-")) / src.name
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(*_GENERATED))
    atexit.register(shutil.rmtree, dst.parent, True)
    return dst


CLIENTS = _run_copy(ROOT / "tests" / "fixtures" / "clients")  # frozen questionnaire with stable ids
REAL_CLIENTS = _run_copy(ROOT / "clients.example")  # the committed, fictional example client (clients/ is git-ignored)
# Synthetic replay transcripts (tests/srt_samples.py), written once per test run: no recording is committed.
SRT_DIR = write_samples(Path(tempfile.mkdtemp(prefix="pacenote-test-srt-")))
atexit.register(shutil.rmtree, SRT_DIR, True)
SAMPLES = SRT_DIR / "clean"
NOISY = SRT_DIR / "noisy"


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    """Never read or write the real ~/.pacenote: the config (the "prochain client") and the local knowledge
    folder (refreshed briefs, recorded recipes)."""
    monkeypatch.setenv("PACENOTE_CONFIG", str(tmp_path / "pacenote-config.json"))
    monkeypatch.setenv("PACENOTE_KNOWLEDGE_DIR", str(tmp_path / "pacenote-knowledge"))
    monkeypatch.delenv("PACENOTE_RECIPES_DIR", raising=False)


@pytest.fixture(autouse=True)
def _fresh_clients():
    """What one test's session wrote next to a client never reaches the next test."""
    yield
    for base in (CLIENTS, REAL_CLIENTS):
        for client in base.iterdir():
            for pattern in _GENERATED[:-1]:
                for f in client.glob(pattern):
                    shutil.rmtree(f, ignore_errors=True) if f.is_dir() else f.unlink(missing_ok=True)


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
        interviewee_read=empty_read(),
        ambassador_probes=[],
        sponsor_probe=None,
        value_framings=[],
        metric_questions=[],
        to_check=[],
        viewpoints=[],
        ask_to=[],
        topic=None,
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


def plan_use_case(uid="bakery-orders", title="Saisie des commandes", impact=4, feasibility=3, horizon="h1", **slots):
    """A contract-valid PlanUseCase dict: every slot "to confirm" unless given (slot=plan_fact(...))."""
    from pacenote.copilot.contract import SLOT_KEYS

    return {"id": uid, "title": title, "horizon": horizon, "pattern": "act_on_behalf",
            "human_decision": "Claire valide les commandes atypiques.",
            "freed": {"who": "", "task": "", "time": "", "evidence": "", "moves_up_to": ""},
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


ADOPTION_KEYS = ("lighthouse", "sponsor", "sponsor_plan", "question", "ask")
WAVE_KEYS = ("volunteers", "majority", "everyone")


def adoption_doc(**kw):
    """A contract-valid AdoptionPlan dict: every field "", no pilot and every wave empty unless given."""
    return {**{k: "" for k in ADOPTION_KEYS}, "pilots": [], **{k: {"plan": "", "trigger": ""} for k in WAVE_KEYS},
            "to_address": [], **kw}


def plan_doc(*use_cases, context="Brioche & Four distribue des fours Fornaxa.", open_items=None, current_state=None, bottlenecks=None,
             metrics=None, timeline=None, recipe_candidates=None, adoption=None):
    return {"context": context,
            "current_state": current_state or {"people": [], "flows": [], "systems": [], "stores": [], "decisions": []},
            "bottlenecks": bottlenecks or [], "metrics": metrics or [], "use_cases": list(use_cases),
            "timeline": timeline or [], "adoption": adoption_doc(**(adoption or {})),
            "open_items": open_items or [],
            "recipe_candidates": recipe_candidates or []}


@pytest.fixture(autouse=True)
def _no_ambassador_file_in_the_repo(monkeypatch):
    """Finalizing a session records the potential ambassadors in its client folder: never in the
    repo's fixtures or example client (tests that check the file work on a copy under tmp_path)."""
    from pacenote.people import pilots

    real = pilots.record

    def guarded(client_path, state, session=""):
        if Path(client_path).resolve().is_relative_to(ROOT):
            return pilots.load(client_path)
        return real(client_path, state, session)

    monkeypatch.setattr(pilots, "record", guarded)


@pytest.fixture(autouse=True)
def _isolated_listen_helper(tmp_path, monkeypatch):
    """Never build into (or run) the real ~/Library/Caches/pacenote speech helper from a test:
    tests pass a stand-in pacenote-listen explicitly."""
    monkeypatch.setenv("PACENOTE_LISTEN_CACHE", str(tmp_path / "cache"))
    monkeypatch.delenv("PACENOTE_LISTEN_BIN", raising=False)


@pytest.fixture(autouse=True)
def _testclient_host(monkeypatch):
    """Starlette's TestClient calls the app as http://testserver: let the same-origin guard
    (web/guard.py) accept that name in tests, like PACENOTE_ALLOWED_HOSTS would for a LAN name."""
    monkeypatch.setenv("PACENOTE_ALLOWED_HOSTS", "testserver")


@pytest.fixture(autouse=True)
def _no_local_speech_backend_config(monkeypatch):
    """A whisper.cpp set up on this Mac (PACENOTE_WHISPER_*, PACENOTE_SPEECH_BACKEND) must not change what the
    tests select: they configure speech backends explicitly (test_speech_backends.py)."""
    for name in ("PACENOTE_WHISPER_BIN", "PACENOTE_WHISPER_MODEL", "PACENOTE_SPEECH_BACKEND"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("pacenote.transcription.speech.WHISPER_NAMES", ("pacenote-test-no-such-whisper",))


def pytest_terminal_summary(terminalreporter):
    """Browser tests skip without Node + the `playwright` package; say so loudly, so a run that silently lost
    them (e.g. NODE_PATH not pointing at a node_modules with playwright) is never read as a full green."""
    skipped = [r for r in terminalreporter.stats.get("skipped", []) if "playwright" in str(r.longrepr)]
    if skipped:
        terminalreporter.write_sep("!", f"{len(skipped)} real-browser tests skipped: Node can't load `playwright`",
                                   yellow=True)
        terminalreporter.write_line("Set NODE_PATH to a node_modules that has playwright (README, Tests), then rerun.")
