"""Admin panel: clients (create, edit), the "prochain client", the research + pre-plan run through
the Claude CLI (mocked here), and the pre-plan feeding the interview."""

import json
import shutil
import sys
import time

import pytest
import yaml
from fastapi.testclient import TestClient

from pacenote.admin import create_admin_app
from pacenote.capture import ReplaySource
from pacenote.cli import main
from pacenote.clients import (
    load_core_themes,
    PREPLAN_THEME,
    ClientError,
    get_next_client,
    resolve_client,
    slug_from_name,
)
from pacenote.llm import OfflineEngine
from pacenote.prompts import build_fast_system, build_sync_system
from pacenote.questionnaire import load_client
from pacenote.research import ResearchJob, ResearchManager, _progress
from pacenote.server import create_app
from pacenote.session import Session, Settings
from pacenote.state import SessionState

from .conftest import REAL_CLIENTS, SAMPLES


@pytest.fixture
def clients(tmp_path):
    root = tmp_path / "clients"
    root.mkdir()
    shutil.copytree(REAL_CLIENTS / "brioche-four", root / "brioche-four")
    for f in ("preplan.md", "preplan.json", "research.md"):
        (root / "brioche-four" / f).unlink(missing_ok=True)
    shutil.rmtree(root / "brioche-four" / "history", ignore_errors=True)
    return root


# --- prochain client ----------------------------------------------------------------------------


def test_next_client_is_the_default_and_client_flag_overrides(clients, tmp_path, monkeypatch, capsys):
    assert get_next_client() is None
    with pytest.raises(ClientError, match="no client given"):
        resolve_client(None, clients)
    main(["new-client", "acme", "--name", "ACME", "--clients-dir", str(clients)])
    assert main(["next", "acme", "--clients-dir", str(clients)]) == 0
    assert get_next_client() == "acme"
    config = json.loads((tmp_path / "pacenote-config.json").read_text())  # PACENOTE_CONFIG (conftest)
    assert config == {"next_client": "acme"}
    assert resolve_client(None, clients) == "acme"
    assert resolve_client("brioche-four", clients) == "brioche-four"  # --client wins
    capsys.readouterr()
    # pacenote check without --client uses the next client
    helper = tmp_path / "pacenote-listen"  # a stand-in: `check` must not build the real speech helper here
    helper.write_text("#!/bin/sh\n")
    helper.chmod(0o755)
    main(["check", "--clients-dir", str(clients), "--no-speech-test", "--listen-bin", str(helper), "--engine", "offline"])
    assert "questionnaire: ACME" in capsys.readouterr().out
    main(["check", "--client", "brioche-four", "--clients-dir", str(clients), "--no-speech-test", "--listen-bin", str(helper), "--engine", "offline"])
    assert "questionnaire: Brioche & Four" in capsys.readouterr().out
    # a next client whose folder is gone, and an unknown one
    shutil.rmtree(clients / "acme")
    with pytest.raises(ClientError, match="has no folder"):
        resolve_client(None, clients)
    assert main(["next", "nope", "--clients-dir", str(clients)]) == 2
    assert main(["next", "--clear"]) == 0 and get_next_client() is None


def test_run_without_client_and_without_next_explains(clients, capsys):
    code = main(["run", "--clients-dir", str(clients), "--no-browser"])
    assert code == 2 and "pacenote admin" in capsys.readouterr().err


# --- create / edit ---------------------------------------------------------------------------------


def test_admin_create_and_edit_a_client(clients):
    http = TestClient(create_admin_app(clients))
    assert "Administration" in http.get("/admin").text
    d = http.post("/admin/api/clients", json={"name": "Société Atlantique Bois", "language": "fr"}).json()
    slug = d["slug"]
    assert slug == slug_from_name("Société Atlantique Bois") == "societe-atlantique-bois"
    for f in ("questionnaire.yaml", "glossary.md", "client.yaml"):
        assert (clients / slug / f).exists(), f
    cfg = load_client(clients, slug)  # the scaffold is a valid, ladder-style questionnaire
    assert cfg.questionnaire.client == "Société Atlantique Bois"
    assert all(q.all_expect() for q in SessionState.from_questionnaire(cfg.questionnaire).questions)
    assert http.post("/admin/api/clients", json={"name": "Société Atlantique Bois"}).status_code == 400  # exists
    assert http.post("/admin/api/clients", json={"name": "x", "slug": "Bad Slug"}).status_code == 400

    info = {"name": "Atlantique Bois", "website": "https://example.org", "sector": "Négoce de bois",
            "country": "France", "language": "en",
            "contacts": [{"name": "Marie Durand", "role": "PDG"}, {"name": "", "role": ""}],
            "notes": "Parler des devis et du stock.", "use_preplan": False}
    d = http.put(f"/admin/api/clients/{slug}", json=info).json()
    assert d["info"]["contacts"] == [{"name": "Marie Durand", "role": "PDG"}]  # empty rows dropped
    sheet = yaml.safe_load((clients / slug / "client.yaml").read_text())
    assert sheet["website"] == "https://example.org" and sheet["notes"] == "Parler des devis et du stock."
    q = (clients / slug / "questionnaire.yaml").read_text()
    assert 'client: "Atlantique Bois"' in q and "language: en" in q
    assert q.startswith("# Seed questionnaire")  # comments survive the edit
    listing = http.get("/admin/api/clients").json()
    assert {c["slug"] for c in listing["clients"]} == {"brioche-four", slug}
    assert http.put("/admin/api/clients/nope", json=info).status_code == 404

    assert http.post("/admin/api/next", json={"slug": slug}).json()["next"] == slug
    assert get_next_client() == slug
    assert next(c for c in http.get("/admin/api/clients").json()["clients"] if c["slug"] == slug)["next"]


def test_admin_is_mounted_in_the_interview_app(clients):
    cfg = load_client(clients, "brioche-four")
    s = Session(client=cfg, session_dir=clients.parent / "s", engine=OfflineEngine(),
                source_factory=lambda d: ReplaySource({"fr": SAMPLES / "fr.srt", "en": SAMPLES / "en.srt"}, speed=0),
                settings=Settings(poll_interval=3600))
    with TestClient(create_app(s)) as http:
        assert http.get("/admin").status_code == 200
        assert http.get("/admin/api/clients").json()["clients"][0]["slug"] == "brioche-four"
        assert 'href="/admin"' in http.get("/").text  # linked from the interview screen


# --- research + pre-plan (claude -p mocked) -----------------------------------------------------------

RESULT = {
    "summary": "Brioche & Four distribue Fornaxa en France via ~600 boulangeries.",
    "research_md": "# Brioche & Four\n\nDistributeur Fornaxa [1].\n\n## Sources\n[1] Fornaxa — https://www.fornaxa.example",
    "preplan_md": "# Pré-plan\n\n## Cas d'usage\n| cas | valeur |\n|---|---|\n| Saisie des commandes | 5 |",
    "use_cases": [{"title": "Saisie automatique des commandes des boulangeries", "description": "Mail -> ERP",
                   "value": 5, "feasibility": 4, "kind": "quick_win", "already_done": False}],
    "seed_questions": [{"id": "orders-email", "fr": "Racontez-moi la dernière commande reçue par mail.",
                        "en": "Walk me through the last order received by email.", "priority": 1,
                        "use_case": "Saisie automatique des commandes des boulangeries",
                        "expect": {"business": ["combien de commandes par mail"],
                                   "workflow": ["qui les ressaisit", "où ça coince"],
                                   "implementation": ["dans quel ERP", "format des mails"]}}],
    "sources": [{"title": "Fornaxa", "url": "https://www.fornaxa.example"}],
}

FAKE_CLAUDE = r'''#!{python}
import json, os, sys, time
open({log!r}, "a").write(json.dumps({{"argv": sys.argv[1:], "stdin": sys.stdin.read(),
    "env": [k for k in os.environ if k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")]}}) + "\n")
mode = {mode!r}
def out(e): print(json.dumps(e), flush=True)
out({{"type": "system", "subtype": "init"}})
out({{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "name": "WebSearch",
     "input": {{"query": "Brioche & Four Fornaxa distributeur"}}}}]}}}})
out({{"type": "user", "message": {{"content": [{{"type": "tool_result", "content": "results"}}]}}}})
out({{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "name": "WebFetch",
     "input": {{"url": "https://www.fornaxa.example", "prompt": "x"}}}}]}}}})
out({{"type": "user", "message": {{"content": [{{"type": "tool_result", "is_error": True, "content": "403"}}]}}}})
if mode == "slow":
    time.sleep(30)
out({{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "name": "StructuredOutput", "input": {{}}}}]}}}})
if mode == "fail":
    out({{"type": "result", "subtype": "success", "is_error": True, "result": "Not logged in · Please run /login"}})
else:
    out({{"type": "result", "subtype": "success", "is_error": False, "result": "", "structured_output": {result}}})
'''


def fake_claude(tmp_path, mode="ok", name="claude"):
    log = tmp_path / f"{name}.jsonl"
    script = tmp_path / name
    script.write_text(FAKE_CLAUDE.format(python=sys.executable, log=str(log), mode=mode, result=repr(RESULT)))
    script.chmod(0o755)
    return script, log


def test_research_writes_the_preplan_with_progress_and_keeps_versions(clients, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    script, log = fake_claude(tmp_path)
    mgr = ResearchManager(clients, claude_bin=str(script))
    (clients / "brioche-four" / "client.yaml").write_text(yaml.safe_dump(
        {"name": "Brioche & Four", "website": "https://brioche-four.example", "notes": "Parler des commandes par mail."}))
    mgr.start("brioche-four")
    job = mgr.wait("brioche-four", 20)
    assert job.status == "done", job.error
    d = clients / "brioche-four"
    assert (d / "preplan.md").read_text().startswith("# Pré-plan")
    assert "https://www.fornaxa.example" in (d / "research.md").read_text()
    meta = json.loads((d / "preplan.json").read_text())
    assert meta["model"] == "claude-opus-5-5" and meta["seed_questions"][0]["id"] == "orders-email"
    texts = [s["text"] for s in job.steps]
    assert "Recherche : Brioche & Four Fornaxa distributeur" in texts
    assert "Lecture : https://www.fornaxa.example" in texts
    assert any(t.startswith("Source inaccessible") for t in texts) and any("Pré-plan prêt" in t for t in texts)
    call = json.loads(log.read_text().splitlines()[0])
    argv = call["argv"]
    assert argv[0] == "-p" and argv[argv.index("--model") + 1] == "claude-opus-5-5"  # Opus
    assert argv[argv.index("--output-format") + 1] == "stream-json" and "--verbose" in argv
    assert argv[argv.index("--tools") + 1] == "WebSearch,WebFetch"  # web research, nothing else
    assert argv[argv.index("--allowedTools") + 1] == "WebSearch,WebFetch"
    assert "seed_questions" in json.loads(argv[argv.index("--json-schema") + 1])["required"]
    assert "Company: Brioche & Four" in call["stdin"] and "Parler des commandes par mail." in call["stdin"]
    assert call["env"] == []  # subscription login only, no API key

    # re-run: the previous version is kept in history/
    mgr.start("brioche-four")
    job = mgr.wait("brioche-four", 20)
    assert job.status == "done" and job.archived
    hist = d / "history" / job.archived
    assert sorted(p.name for p in hist.iterdir()) == ["preplan.json", "preplan.md", "research.md"]


def test_failed_or_stopped_research_keeps_the_previous_preplan(clients, tmp_path):
    d = clients / "brioche-four"
    (d / "preplan.md").write_text("# Ancien pré-plan\n")
    script, _ = fake_claude(tmp_path, mode="fail", name="claude-fail")
    mgr = ResearchManager(clients, claude_bin=str(script))
    mgr.start("brioche-four")
    job = mgr.wait("brioche-four", 20)
    assert job.status == "error" and "Not logged in" in job.error
    assert (d / "preplan.md").read_text() == "# Ancien pré-plan\n" and not (d / "history").exists()

    slow, _ = fake_claude(tmp_path, mode="slow", name="claude-slow")
    mgr = ResearchManager(clients, claude_bin=str(slow))
    mgr.start("brioche-four")
    deadline = time.time() + 10
    while time.time() < deadline and not any(s["kind"] == "read" for s in mgr.status("brioche-four")["steps"]):
        time.sleep(0.05)
    assert mgr.status("brioche-four")["status"] == "running" and mgr.status("brioche-four")["pages"] == 1  # live progress
    assert mgr.stop("brioche-four")
    job = mgr.wait("brioche-four", 10)
    assert job.status == "stopped" and (d / "preplan.md").read_text() == "# Ancien pré-plan\n"


def test_admin_research_api_and_editing(clients, tmp_path):
    script, _ = fake_claude(tmp_path)
    mgr = ResearchManager(clients, claude_bin=str(script))
    http = TestClient(create_admin_app(clients, manager=mgr))
    assert http.post("/admin/api/clients/brioche-four/research").json()["status"] == "running"
    mgr.wait("brioche-four", 20)
    assert http.get("/admin/api/clients/brioche-four/research").json()["status"] == "done"
    d = http.get("/admin/api/clients/brioche-four").json()
    assert "<table>" in d["preplan_html"] and d["seed_questions"][0]["fr"].startswith("Racontez-moi")
    assert d["use_cases"][0]["kind"] == "quick_win" and d["research_model"] == "claude-opus-5-5"
    r = http.put("/admin/api/clients/brioche-four/preplan", json={"markdown": "# Pré-plan corrigé\n\n- un point"})
    assert "<h1>Pré-plan corrigé</h1>" in r.json()["html"]
    assert (clients / "brioche-four" / "preplan.md").read_text().startswith("# Pré-plan corrigé")
    # second run -> a version in history; restoring it brings the generated pre-plan back
    http.post("/admin/api/clients/brioche-four/research")
    mgr.wait("brioche-four", 20)
    versions = http.get("/admin/api/clients/brioche-four").json()["history"]
    assert len(versions) == 1
    assert "corrigé" in http.get(f"/admin/api/clients/brioche-four/history/{versions[0]['version']}").json()["preplan_html"]
    restored = http.post(f"/admin/api/clients/brioche-four/history/{versions[0]['version']}/restore").json()
    assert "corrigé" in restored["preplan_md"] and len(restored["history"]) == 2


# --- the pre-plan feeds the interview -------------------------------------------------------------------


def _with_preplan(clients, use=True):
    d = clients / "brioche-four"
    meta = {k: RESULT[k] for k in ("summary", "use_cases", "seed_questions", "sources")}
    (d / "preplan.json").write_text(json.dumps(meta, ensure_ascii=False))
    (d / "client.yaml").write_text(yaml.safe_dump({"name": "Brioche & Four", "sector": "Distribution Fornaxa",
                                                   "contacts": [{"name": "Marc", "role": "PDG"}],
                                                   "notes": "Le PDG veut parler des commandes.", "use_preplan": use},
                                                  allow_unicode=True))


def test_preplan_seed_questions_join_the_interview_on_the_ladder(clients):
    _with_preplan(clients)
    cfg = load_client(clients, "brioche-four")
    themes = [t.id for t in cfg.questionnaire.themes]
    assert themes[1] == PREPLAN_THEME  # right after the business framing
    core = len(load_core_themes())  # the app's core questions (phases 1-4) are merged in too
    assert len(cfg.questionnaire.themes) == len(cfg.base_questionnaire.themes) + 1 + core
    assert next(t for t in cfg.questionnaire.themes if t.id == PREPLAN_THEME).phase == "deep-dives"
    st = SessionState.from_questionnaire(cfg.questionnaire)
    q = st.get("pp-orders-email")
    assert q is not None and q.priority == 1
    assert q.expect == {"business": ["combien de commandes par mail"], "workflow": ["qui les ressaisit", "où ça coince"],
                        "implementation": ["dans quel ERP", "format des mails"]}
    assert st.focus_id == "capacity-limit"  # the interview still starts with the capacity question
    ctx = cfg.questionnaire.context
    assert "Distribution Fornaxa" in ctx and "Marc (PDG)" in ctx and "Le PDG veut parler des commandes." in ctx
    assert "Saisie automatique des commandes des boulangeries (value 5/5, feasibility 4/5, quick win)" in ctx
    assert "hypotheses" in ctx
    assert "Pre-plan from desk research" in build_sync_system(st, cfg.glossary, ctx)
    assert "Le PDG veut parler des commandes." in build_fast_system(st, cfg.glossary, ctx)


def test_preplan_can_be_kept_out_of_the_interview(clients):
    _with_preplan(clients, use=False)
    cfg = load_client(clients, "brioche-four")
    assert PREPLAN_THEME not in [t.id for t in cfg.questionnaire.themes]
    assert "Pre-plan" not in cfg.questionnaire.context and "Le PDG veut parler" in cfg.questionnaire.context


def test_research_progress_shows_writing_as_soon_as_it_starts():
    job = ResearchJob(slug="x")
    _progress(job, {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "WebSearch", "input": {"query": "Brioche & Four Fornaxa"}}]}})
    _progress(job, {"type": "user", "message": {"content": [
        {"type": "tool_result", "is_error": True, "content": "proxy refused"}]}})
    _progress(job, {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"text": "…"}}})
    _progress(job, {"type": "stream_event", "event": {
        "type": "content_block_start", "content_block": {"type": "tool_use", "name": "StructuredOutput"}}})
    # the complete block arrives later: not repeated
    _progress(job, {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "StructuredOutput", "input": {}}]}})
    assert [s["kind"] for s in job.steps] == ["search", "warn", "write"]
    assert job.steps[0]["text"] == "Recherche : Brioche & Four Fornaxa"
    assert job.steps[2]["text"].startswith("Rédaction du pré-plan")


def test_markdown_list_right_after_a_paragraph_renders_as_a_list():
    from pacenote.finalize import md_to_html

    html = md_to_html("Quatre leviers :\n- les commandes ;\n- la garantie.\n\n| a | b |\n|---|---|\n| 1 | 2 |\n")
    assert "<ul>" in html and html.count("<li>") == 2 and "<table>" in html
    assert md_to_html("```\nx :\n- y\n```").count("<li>") == 0
