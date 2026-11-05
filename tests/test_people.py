"""Interviewee research: the web agent (claude -p mocked by a stand-in script), facts checked one by
one (source + confidence required), the privacy filter, homonyms (candidates -> pick -> card), the
card saved straight away with per-fact provenance (manual facts kept across re-runs, removed ones
not brought back), failure and stop, the endpoints, the interview context, i18n, and the
research -> pick -> card -> double-click edit flow in a real browser."""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from pacenote import i18n
from pacenote.admin import create_admin_app
from pacenote.autofill import AutofillManager
from pacenote.clients import ClientInfo, Contact, save_info
from pacenote.people import (
    CATEGORIES,
    DEFAULT_PEOPLE_MODEL,
    PeopleManager,
    PersonOutput,
    compact_profile,
    contact_slug,
    load_profile,
    parse_output,
    privacy_filter,
    sensitive_reason,
)
from pacenote.questionnaire import load_client

from .test_autofill import _node_playwright, _serve, calls, clients  # noqa: F401  (the clients fixture)

SRC = "https://www.brioche-four.example/equipe"
PROFILE = {
    "status": "profile", "person": "Jean Exemple", "candidates": [],
    "facts": [
        {"category": "role", "value": "Président de Brioche & Four", "source_url": SRC, "confidence": 0.95},
        {"category": "tenure", "value": "depuis 2009", "source_url": "https://registry.example/brioche-four", "confidence": 0.9},
        {"category": "career", "value": "Directeur commercial, Fournil Équipement (2001–2009)",
         "source_url": "https://press.example/portrait", "confidence": 0.7},
        {"category": "publication", "value": "Interview : le retour du pain au levain en France",
         "source_url": "https://press.example/interview-levain", "confidence": 0.85},
        {"category": "interest", "value": "Logistique et après-vente des boulangeries",
         "source_url": "https://press.example/interview-levain", "confidence": 0.5},
        {"category": "location", "value": "Villebourg, Île-de-France", "source_url": SRC, "confidence": 0.8},
        {"category": "icebreaker", "value": "Votre interview sur le retour du levain : qu'est-ce qui a changé depuis ?",
         "source_url": "https://press.example/interview-levain", "confidence": 0.8},
        # never kept: no source, too unsure, unknown category, and everything private
        {"category": "education", "value": "HEC", "source_url": "", "confidence": 0.9},
        {"category": "language", "value": "anglais", "source_url": SRC, "confidence": 0.1},
        {"category": "hobby", "value": "golf", "source_url": SRC, "confidence": 0.9},
        {"category": "interest", "value": "Marié, trois enfants", "source_url": SRC, "confidence": 0.9},
        {"category": "interest", "value": "Catholique pratiquant", "source_url": SRC, "confidence": 0.9},
        {"category": "career", "value": "Conseiller municipal (Les Républicains) à Vincennes", "source_url": SRC, "confidence": 0.9},
        {"category": "location", "value": "12 rue des Lilas, 94300 Vincennes", "source_url": SRC, "confidence": 0.9},
        {"category": "interest", "value": "Vacances en famille", "source_url": "https://www.facebook.com/jean.exemple",
         "confidence": 0.9},
    ],
}
CANDIDATES = {
    "status": "candidates", "person": "", "facts": [],
    "candidates": [
        {"name": "Jean Exemple", "headline": "Président, Brioche & Four", "company": "Brioche & Four", "location": "Villebourg, France",
         "url": "https://www.linkedin.com/in/example-jean"},
        {"name": "Jean Exemple", "headline": "Professeur de mathématiques", "company": "Lycée X",
         "location": "8 rue Y, 69001 Lyon", "url": "https://www.facebook.com/jean.exemple.2"},
    ],
}

FAKE = r'''#!{python}
import json, os, sys, time
stdin = sys.stdin.read()
open({log!r}, "a").write(json.dumps({{"argv": sys.argv[1:], "stdin": stdin,
    "env": [k for k in os.environ if k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")]}}) + "\n")
mode = {mode!r}
def out(e): print(json.dumps(e), flush=True)
out({{"type": "system", "subtype": "init"}})
out({{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "name": "WebSearch",
     "input": {{"query": "Jean Exemple Brioche & Four"}}}}]}}}})
out({{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "name": "WebFetch",
     "input": {{"url": "https://www.brioche-four.example/equipe", "prompt": "x"}}}}]}}}})
if mode == "slow":
    time.sleep(30)
if mode == "fail":
    out({{"type": "result", "subtype": "success", "is_error": True, "result": "Not logged in · Please run /login"}})
    sys.exit(1)
answer = {profile} if (mode == "profile" or "picked this person" in stdin) else {candidates}
time.sleep({delay})
out({{"type": "result", "subtype": "success", "is_error": False, "result": "", "structured_output": answer}})
'''


def fake_claude(tmp_path, mode="profile", name="claude-person", delay=0.0):
    log = tmp_path / f"{name}.jsonl"
    script = tmp_path / name
    script.write_text(FAKE.format(python=sys.executable, log=str(log), mode=mode, profile=repr(PROFILE),
                                  candidates=repr(CANDIDATES), delay=delay))
    script.chmod(0o755)
    return script, log


def sheet(clients_dir, contacts=(("Jean Exemple", "Président"),)):
    info = ClientInfo(name="Brioche & Four", website="https://www.brioche-four.example", country="France",
                      contacts=[Contact(name=n, role=r) for n, r in contacts])
    save_info(clients_dir, "brioche-four", info)
    return info


# --- the answer, checked fact by fact, then the privacy filter ------------------------------------------


def test_schema_is_strict_and_lists_the_categories():
    schema = PersonOutput.model_json_schema()
    assert set(schema["required"]) == {"status", "person", "candidates", "facts"}
    fact = schema["$defs"]["Fact"]
    assert set(fact["required"]) == {"category", "value", "source_url", "confidence"}
    assert set(fact["properties"]["category"]["enum"]) == set(CATEGORIES) and fact["additionalProperties"] is False


def test_facts_need_a_source_and_a_confidence_and_private_ones_are_dropped():
    r = parse_output(PROFILE)
    assert r["status"] == "profile" and r["person"] == "Jean Exemple"
    got = [(f["category"], f["value"]) for f in r["facts"]]
    assert got == [("role", "Président de Brioche & Four"), ("tenure", "depuis 2009"),
                   ("career", "Directeur commercial, Fournil Équipement (2001–2009)"),
                   ("publication", "Interview : le retour du pain au levain en France"),
                   ("interest", "Logistique et après-vente des boulangeries"),
                   ("location", "Villebourg, Île-de-France"),
                   ("icebreaker", "Votre interview sur le retour du levain : qu'est-ce qui a changé depuis ?")]
    for f in r["facts"]:
        assert f["source_url"].startswith("https://") and 0.3 <= f["confidence"] <= 1 and f["by"] == "agent" and f["id"]
    assert next(f for f in r["facts"] if f["category"] == "interest")["low"] is True
    assert r["dropped"] == {"family": 1, "religion": 1, "politics": 1, "address": 1, "personal_network": 1}
    assert parse_output({"status": "profile", "person": "", "candidates": [], "facts": []})["status"] == "not_found"
    with pytest.raises(ValueError):
        parse_output("nope")


@pytest.mark.parametrize("value,why", [
    ("Marié à Anne Exemple", "family"), ("Father of two daughters", "family"), ("Sa famille vit à Lyon", "family"),
    ("Diagnosed with cancer in 2020", "health"), ("Burn-out en 2019", "health"),
    ("Practising Muslim", "religion"), ("Membre de la paroisse Saint-Marc", "religion"),
    ("Militant au Parti socialiste", "politics"), ("Votes for the Labour Party", "politics"),
    ("Openly gay", "sexuality"), ("D'origine marocaine", "ethnicity"),
    ("Salaire de 250 k€", "finance"), ("Condamné en 2015 pour fraude", "legal"),
    ("Né le 3 mars 1968", "birth"), ("Domicile : Vincennes", "home"),
    ("jean.exemple@gmail.com", "contact"), ("+33 6 12 34 56 78", "contact"),
    ("Habite 12 rue des Lilas", "address"),
])
def test_privacy_filter_drops_sensitive_categories(value, why):
    assert sensitive_reason({"category": "interest", "value": value, "source_url": SRC}) == why


@pytest.mark.parametrize("fact", [
    {"category": "career", "value": "VP Sales, Hospitality Group (2010–2014)"},
    {"category": "interest", "value": "Il a la conviction que le SAV fait la fidélité"},
    {"category": "career", "value": "Head of therapeutics marketing, Pharma X"},
    {"category": "publication", "value": "Podcast : former les jeunes boulangers"},
    {"category": "location", "value": "Lyon, Auvergne-Rhône-Alpes"},
    {"category": "mutual_context", "value": "Speaks about Excel-based customer reporting, a workflow we take over"},
])
def test_privacy_filter_keeps_professional_facts(fact):
    kept, dropped = privacy_filter([fact | {"source_url": SRC}])
    assert kept and not dropped


def test_location_is_city_or_region_only_and_no_login_walls():
    assert sensitive_reason({"category": "location", "value": "Paris 75008", "source_url": SRC}) == "address"
    assert sensitive_reason({"category": "location", "value": "Villebourg", "source_url": SRC}) is None
    assert sensitive_reason({"category": "role", "value": "CEO", "source_url": "https://www.linkedin.com/authwall?x=1"}) == "login_wall"
    assert sensitive_reason({"category": "role", "value": "CEO", "source_url": "https://www.instagram.com/x"}) == "personal_network"


def test_homonyms_are_candidates_without_private_details():
    r = parse_output(CANDIDATES)
    assert r["status"] == "candidates" and r["facts"] == []
    assert r["candidates"][0]["url"] == "https://www.linkedin.com/in/example-jean"
    assert r["candidates"][1]["location"] == "" and r["candidates"][1]["url"] == ""  # address and facebook dropped


# --- the agent run ---------------------------------------------------------------------------------


def test_people_run_sonnet_with_web_tools_only_and_no_api_key(clients, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    info = sheet(clients)
    script, log = fake_claude(tmp_path)
    mgr = PeopleManager(clients, claude_bin=str(script))
    job = mgr.wait(mgr.start("brioche-four", "Jean Exemple", "Président", info, None, "fr").slug, 20)
    assert job.status == "done", job.error
    v = job.view()
    keys = [s.get("key") for s in v["steps"]]
    assert keys[0] == "admin.step.personStart" and "admin.step.search" in keys and "admin.step.personFiltered" in keys
    assert keys[-1] == "admin.step.personReady"
    argv = calls(log)[0]["argv"]
    assert argv[argv.index("--model") + 1] == DEFAULT_PEOPLE_MODEL == "claude-sonnet-5-5"
    assert argv[argv.index("--tools") + 1] == "WebSearch,WebFetch" == argv[argv.index("--allowedTools") + 1]
    assert "--strict-mcp-config" in argv and "--no-session-persistence" in argv
    system = argv[argv.index("--system-prompt") + 1]
    assert "PRIVACY" in system and "Never log in" in system and "city or region only" in system and "in French" in system
    stdin = calls(log)[0]["stdin"]
    assert "Person: Jean Exemple" in stdin and "Role (as the engineer noted it): Président" in stdin
    assert "- website: https://www.brioche-four.example" in stdin
    assert calls(log)[0]["env"] == []  # subscription login only
    # saved straight away, per contact, outside git
    card = load_profile(clients / "brioche-four", "Jean Exemple")
    assert card["status"] == "profile" and len(card["facts"]) == 7 and card["model"] == DEFAULT_PEOPLE_MODEL
    assert (clients / "brioche-four" / "people" / "jean-exemple.json").exists()
    assert sum(card["dropped"].values()) == 5


def _people_system_prompt(clients, tmp_path, name="claude-person", **kw) -> str:
    script, log = fake_claude(tmp_path, name=name)
    mgr = PeopleManager(clients, claude_bin=str(script), **kw)
    job = mgr.wait(mgr.start("brioche-four", "Jean Exemple", "Président", sheet(clients), None, "fr").slug, 20)
    assert job.status == "done", job.error
    argv = calls(log)[0]["argv"]
    return argv[argv.index("--system-prompt") + 1]


def test_mutual_context_skipped_without_engineer_context(clients, tmp_path, monkeypatch):
    monkeypatch.delenv("PACENOTE_ENGINEER_CONTEXT", raising=False)
    system = _people_system_prompt(clients, tmp_path)
    assert "mutual_context: leave this category out" in system
    assert "share with the engineer's side" not in system


def test_mutual_context_uses_engineer_context_from_env_or_argument(clients, tmp_path, monkeypatch):
    monkeypatch.setenv("PACENOTE_ENGINEER_CONTEXT", "Acme Labs (acme.example), a software consultancy")
    system = _people_system_prompt(clients, tmp_path, name="claude-env")
    assert "share with the engineer's side (Acme Labs (acme.example), a software consultancy)" in system
    assert "leave this category out" not in system
    system = _people_system_prompt(clients, tmp_path, name="claude-arg", engineer_context="Globex, an AI studio")
    assert "share with the engineer's side (Globex, an AI studio)" in system


def test_candidates_then_pick_then_card(clients, tmp_path):
    info = sheet(clients)
    script, log = fake_claude(tmp_path, mode="candidates")
    mgr = PeopleManager(clients, claude_bin=str(script))
    job = mgr.wait(mgr.start("brioche-four", "Jean Exemple", "", info).slug, 20)
    assert job.view()["result"]["status"] == "candidates" and job.steps[-1]["key"] == "admin.step.personCandidates"
    card = load_profile(clients / "brioche-four", "Jean Exemple")
    assert card["status"] == "candidates" and len(card["candidates"]) == 2 and card["facts"] == []
    picked = card["candidates"][0]
    job2 = mgr.wait(mgr.start("brioche-four", "Jean Exemple", "", info, picked).slug, 20)
    assert job2.slug != job.slug and job2.view()["result"]["status"] == "profile"
    assert "picked this person" in calls(log)[1]["stdin"] and "linkedin.com/in/example-jean" in calls(log)[1]["stdin"]
    card = load_profile(clients / "brioche-four", "Jean Exemple")
    assert card["status"] == "profile" and card["picked"]["url"] == picked["url"]


def test_failure_or_stop_keeps_the_card_and_the_sheet(clients, tmp_path):
    info = sheet(clients)
    ok, _ = fake_claude(tmp_path)
    mgr = PeopleManager(clients, claude_bin=str(ok))
    assert mgr.wait(mgr.start("brioche-four", "Jean Exemple", "", info).slug, 20).status == "done"
    path = clients / "brioche-four" / "people" / "jean-exemple.json"
    before, sheet_before = path.read_text(), (clients / "brioche-four" / "client.yaml").read_text()
    fail, _ = fake_claude(tmp_path, mode="fail", name="claude-fail")
    m = PeopleManager(clients, claude_bin=str(fail))
    job = m.wait(m.start("brioche-four", "Jean Exemple", "", info, force=True).slug, 20)
    assert job.status == "error" and "Not logged in" in job.error and path.read_text() == before
    slow, _ = fake_claude(tmp_path, mode="slow", name="claude-slow")
    m = PeopleManager(clients, claude_bin=str(slow))
    job = m.start("brioche-four", "Jean Exemple", "", info, force=True)
    deadline = time.time() + 10
    while time.time() < deadline and job.view()["pages"] == 0:
        time.sleep(0.05)
    assert m.stop(job.slug)
    job = m.wait(job.slug, 10)
    assert job.status == "stopped" and job.error is None and path.read_text() == before
    assert (clients / "brioche-four" / "client.yaml").read_text() == sheet_before


# --- edits: manual facts stay, removed ones don't come back -------------------------------------------------


def test_edits_are_manual_and_survive_a_rerun(clients, tmp_path):
    sheet(clients)
    script, log = fake_claude(tmp_path)
    mgr = PeopleManager(clients, claude_bin=str(script))
    http = TestClient(create_admin_app(clients, autofill=AutofillManager(claude_bin=str(script)), people=mgr))
    job = http.post("/admin/api/clients/brioche-four/people", json={"name": "Jean Exemple", "role": "Président"}).json()
    mgr.wait(job["id"], 20)
    card = http.get("/admin/api/clients/brioche-four/people/Jean Exemple").json()
    assert http.get("/admin/api/clients/brioche-four").json()["profiles"]["Jean Exemple"]["facts"] == card["facts"]
    facts = card["facts"]
    role = next(f for f in facts if f["category"] == "role")
    career = next(f for f in facts if f["category"] == "career")
    edited = [f | {"value": "Président-directeur général de Brioche & Four"} if f["id"] == role["id"] else f
              for f in facts if f["id"] != career["id"]]
    card = http.put("/admin/api/clients/brioche-four/people/Jean Exemple", json={"facts": edited, "removed": [career["id"]]}).json()
    new_role = next(f for f in card["facts"] if f["category"] == "role")
    assert new_role["value"] == "Président-directeur général de Brioche & Four" and new_role["by"] == "manual"
    assert career["id"] in card["removed"]
    # a re-run: the manual role stays (the agent's is not added next to it), the removed career stays out
    job = http.post("/admin/api/clients/brioche-four/people", json={"name": "Jean Exemple", "force": True}).json()
    mgr.wait(job["id"], 20)
    card = load_profile(clients / "brioche-four", "Jean Exemple")
    roles = [f for f in card["facts"] if f["category"] == "role"]
    assert [(f["value"], f["by"]) for f in roles] == [("Président-directeur général de Brioche & Four", "manual")]
    assert not any(f["category"] == "career" for f in card["facts"])
    assert "role: Président-directeur général de Brioche & Four" in calls(log)[-1]["stdin"]  # trusted, not searched again
    # the FDE clears his manual role (whitespace only): dropped, not remembered as removed, refilled next run
    facts = card["facts"]
    manual_role = next(f for f in facts if f["category"] == "role")
    card = http.put("/admin/api/clients/brioche-four/people/Jean Exemple",
                    json={"facts": [f | {"value": "  "} if f["id"] == manual_role["id"] else f for f in facts]}).json()
    assert not any(f["category"] == "role" for f in card["facts"]) and manual_role["id"] not in card["removed"]
    saved = load_profile(clients / "brioche-four", "Jean Exemple")  # persisted (what a reload reads)
    assert not any(f["category"] == "role" and f["by"] == "manual" for f in saved["facts"])
    job = http.post("/admin/api/clients/brioche-four/people", json={"name": "Jean Exemple", "force": True}).json()
    mgr.wait(job["id"], 20)
    card = load_profile(clients / "brioche-four", "Jean Exemple")
    assert [(f["value"], f["by"]) for f in card["facts"] if f["category"] == "role"] == [(role["value"], "agent")]
    assert "Président-directeur général" not in calls(log)[-1]["stdin"]  # no longer given as known
    assert not any(f["category"] == "career" for f in card["facts"])  # the × removed career still stays out
    # clearing an agent fact is not a × either: it may come back on the next run
    tenure = next(f for f in card["facts"] if f["category"] == "tenure")
    card = http.put("/admin/api/clients/brioche-four/people/Jean Exemple",
                    json={"facts": [f | {"value": ""} if f["id"] == tenure["id"] else f for f in card["facts"]]}).json()
    assert tenure["id"] not in card["removed"] and not any(f["id"] == tenure["id"] for f in card["facts"])
    job = http.post("/admin/api/clients/brioche-four/people", json={"name": "Jean Exemple", "force": True}).json()
    mgr.wait(job["id"], 20)
    assert any(f["id"] == tenure["id"] for f in load_profile(clients / "brioche-four", "Jean Exemple")["facts"])
    for bad in ({"name": "Jo"}, {"name": "x" * 121}, {"name": "Jean Exemple", "lang": "not a language"},
                {"name": "Jean Exemple", "candidate": {"name": " "}}):
        assert http.post("/admin/api/clients/brioche-four/people", json=bad).status_code in (400, 422), bad
    assert http.put("/admin/api/clients/brioche-four/people/Jean Exemple",
                    json={"facts": [{"category": "religion", "value": "x"}]}).status_code == 400
    assert http.get("/admin/api/clients/brioche-four/people/Nobody Here").status_code == 404
    assert http.get("/admin/api/people/nope").status_code == 404
    assert http.post("/admin/api/people/nope/stop").status_code == 404


def test_the_card_feeds_the_interview_context(clients, tmp_path):
    info = sheet(clients)
    script, _ = fake_claude(tmp_path)
    mgr = PeopleManager(clients, claude_bin=str(script))
    mgr.wait(mgr.start("brioche-four", "Jean Exemple", "Président", info).slug, 20)
    ctx = load_client(clients, "brioche-four", use_preplan=False).questionnaire.context
    assert "Who we meet (public professional profiles" in ctx
    assert "- Jean Exemple (Président): role: Président de Brioche & Four. since: depuis 2009." in ctx
    assert "icebreakers: Votre interview sur le retour du levain" in ctx
    assert compact_profile({"name": "X", "facts": []}) == ""


def test_profiles_never_go_to_git():
    root = Path(__file__).resolve().parent.parent
    out = subprocess.run(["git", "check-ignore", "clients/brioche-four/people/jean-exemple.json"], cwd=root,
                         capture_output=True, text=True)
    assert out.returncode == 0
    assert contact_slug("Jérôme  d'Été") == "jerome-d-ete"


def test_people_model_is_configurable(clients, monkeypatch):
    from pacenote import cli

    seen = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: seen.setdefault("app", app))
    monkeypatch.setenv("PACENOTE_PEOPLE_MODEL", "claude-haiku-5-5")
    assert cli.main(["admin", "--clients-dir", str(clients), "--no-browser"]) == 0
    assert TestClient(seen.pop("app")).get("/admin/api/autofill").json()["people"]["model"] == "claude-haiku-5-5"
    assert cli.main(["admin", "--clients-dir", str(clients), "--no-browser", "--people-model", "claude-opus-5-5"]) == 0
    assert TestClient(seen.pop("app")).get("/admin/api/autofill").json()["people"]["model"] == "claude-opus-5-5"


# --- i18n -------------------------------------------------------------------------------------------------


def test_person_keys_exist_in_every_locale_and_no_hard_coded_text():
    en = {k for k in i18n.flatten(i18n.catalog("en")) if k.startswith(("admin.person.", "admin.step.person"))}
    assert {f"admin.person.cat.{c}" for c in CATEGORIES} <= en and "admin.person.privacy" in en
    for code in i18n.available():
        raw = json.loads((Path(i18n.LOCALES_DIR) / f"{code}.json").read_text(encoding="utf-8"))
        assert not (en - set(i18n.flatten(raw))), code
    js = (Path(i18n.__file__).parent / "static" / "admin.js").read_text(encoding="utf-8")
    assert "admin.person.privacy" in js and not re.search(r"title=\"[A-Za-z]{3,}", js)


# --- the browser: research -> pick -> card -> double-click edit ----------------------------------------------

BROWSER = r"""
const { chromium } = require('playwright');
(async () => {
  const [base, shots] = process.argv.slice(2);
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1280, height: 1600 } });
  const errors = [];
  p.on('pageerror', e => errors.push(e.message));
  p.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  const out = {};
  await p.goto(base + '/admin?client=brioche-four');
  await p.waitForFunction(() => document.querySelector('#f-name').value === 'Brioche & Four');
  await p.waitForFunction(() => !document.querySelector('#autofill').classList.contains('hidden'));
  out.buttonTitle = await p.getAttribute('#f-contacts li .c-research', 'title');
  await p.click('#f-contacts li .c-research');
  await p.waitForSelector('.pcard .candidates li');
  out.candidates = await p.$$eval('.pcard .candidates li .muted', xs => xs.map(x => x.textContent));
  await p.click('.pcard .candidates li button');
  await p.waitForSelector('.pcard dl.facts dd');
  out.labels = await p.$$eval('.pcard dl.facts dt', xs => xs.map(x => x.textContent));
  out.facts = await p.$$eval('.pcard dl.facts dd .cv', xs => xs.map(x => x.textContent));
  out.sources = await p.$$eval('.pcard dl.facts dd a.src', xs => xs.length);
  out.privacy = await p.textContent('.pcard .privacy');
  out.noAcceptUi = await p.evaluate(() => document.querySelectorAll('[data-act], .suggest').length === 0);
  if (shots) await p.screenshot({ path: shots, fullPage: true });
  const dd = '.pcard dl.facts dd:first-of-type';
  await p.dblclick(dd);
  await p.fill(dd + ' .inline', 'PDG de Brioche & Four');
  const w = p.waitForResponse(r => r.url().includes('/people/') && r.request().method() === 'PUT');
  await p.keyboard.press('Enter');
  await w;
  out.edited = await p.textContent(dd + ' .cv');
  await p.reload();
  await p.waitForSelector('.pcard dl.facts dd');
  out.afterReload = await p.textContent('.pcard dl.facts dd .cv');
  // the FDE clears his edited role: dropped (not a ×), the next run fills it again
  await p.dblclick(dd);
  await p.fill(dd + ' .inline', '  ');
  const c = p.waitForResponse(r => r.url().includes('/people/') && r.request().method() === 'PUT');
  await p.keyboard.press('Enter');
  out.clearedPut = (await c).request().postDataJSON();
  out.afterClear = await p.$$eval('.pcard dl.facts dd .cv', xs => xs.map(x => x.textContent));
  out.errors = errors;
  console.log(JSON.stringify(out));
  await b.close();
})().catch(e => { console.error(e); process.exit(1); });
"""


@pytest.mark.skipif(not _node_playwright(), reason="needs node + playwright + chromium")
def test_browser_research_pick_card(clients, tmp_path):
    sheet(clients)
    script, log = fake_claude(tmp_path, mode="candidates", delay=0.3)
    auto, _ = fake_claude(tmp_path, mode="fail", name="claude-autofill")  # Auto-fill not under test here
    server, th, base = _serve(create_admin_app(clients, autofill=AutofillManager(claude_bin=str(auto)),
                                               people=PeopleManager(clients, claude_bin=str(script))))
    try:
        js = tmp_path / "people.js"
        js.write_text(BROWSER, encoding="utf-8")
        out = subprocess.run(["node", str(js), base, os.environ.get("PACENOTE_SHOT_PEOPLE", "")], capture_output=True,
                             text=True, timeout=120, cwd=tmp_path)
        assert out.returncode == 0, out.stderr
        r = json.loads(out.stdout.strip().splitlines()[-1])
        assert r["errors"] == []
        assert r["buttonTitle"] == "Research this person"
        assert r["candidates"] == ["Président, Brioche & Four", "Professeur de mathématiques"]
        assert r["labels"][0] == "Current role" and "Icebreakers" in r["labels"]
        assert r["facts"][0] == "Président de Brioche & Four" and len(r["facts"]) == 7 and r["sources"] == 7
        assert not any(x in " ".join(r["facts"]) for x in ("Marié", "Catholique", "rue des Lilas", "Républicains"))
        assert r["privacy"].startswith("Professional, public information only") and "5 personal details left out" in r["privacy"]
        assert r["noAcceptUi"] is True
        assert r["edited"] == "PDG de Brioche & Four" and r["afterReload"] == "PDG de Brioche & Four"
        assert r["clearedPut"]["removed"] == [] and "PDG de Brioche & Four" not in r["afterClear"]
        card = load_profile(clients / "brioche-four", "Jean Exemple")
        assert not any(f["category"] == "role" for f in card["facts"]) and card["removed"] == []
        assert len(calls(log)) == 2  # the homonyms, then the chosen person
        assert yaml.safe_load((clients / "brioche-four" / "client.yaml").read_text())["contacts"][0]["name"] == "Jean Exemple"
    finally:
        server.should_exit = True
        th.join(timeout=10)
