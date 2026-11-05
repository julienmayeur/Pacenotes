"""Client-sheet Auto-fill: the web agent (claude -p mocked by a stand-in script), its answer checked
field by field, the registered company vs its linked brands / entities, the candidates step, the
direct fill with per-field provenance (a manual value is never overwritten, across re-runs and
reloads), failure and stop, the endpoints, the new sheet fields (backward compatible), i18n, and
the fill -> double-click edit flow in a real browser."""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from pacenote import i18n
from pacenote.admin import create_admin_app
from pacenote.autofill import (
    DEFAULT_AUTOFILL_MODEL,
    FIELDS,
    LOW_CONFIDENCE,
    AutofillManager,
    AutofillOutput,
    dead_links,
    is_manual,
    mark_dead_sources,
    mark_manual_edits,
    merge_into_sheet,
    parse_output,
)
from pacenote.clients import ClientInfo, client_context, create_client, load_info, save_info

from .conftest import REAL_CLIENTS

FOUND = {
    "status": "fields", "company": "Brioche & Four", "candidates": [], "linked_entities": [],
    "fields": {
        "website": {"value": "https://www.brioche-four.example", "source_url": "https://www.brioche-four.example/", "confidence": 0.95},
        "country": {"value": "France", "source_url": "https://registry.example/brioche-four", "confidence": 0.9},
        "sector": {"value": "Distribution de fours et pétrins Fornaxa",
                   "source_url": "https://www.brioche-four.example/", "confidence": 0.85},
        "headcount": {"value": "20 à 49 salariés", "source_url": "https://registry.example/brioche-four", "confidence": 0.45},
        "leadership": {"value": "Jean Exemple — Président", "source_url": "", "confidence": 0.9},  # no source: dropped
        "hq_city": None,
    },
}
CANDIDATES = {
    "status": "candidates", "company": "", "fields": {}, "linked_entities": [],
    "candidates": [
        {"name": "Brioche & Four (Villebourg)", "website": "https://www.brioche-four.example", "country": "France",
         "description": "Distributeur Fornaxa."},
        {"name": "Brioche & Four (Paris)", "website": "", "country": "France", "description": "Commerce de détail."},
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
     "input": {{"query": "Brioche & Four Fornaxa"}}}}]}}}})
out({{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "name": "WebFetch",
     "input": {{"url": "https://www.brioche-four.example/", "prompt": "x"}}}}]}}}})
if mode == "slow":
    time.sleep(30)
if mode == "fail":
    out({{"type": "result", "subtype": "success", "is_error": True, "result": "Not logged in · Please run /login"}})
    sys.exit(1)
found = json.load(open(os.environ["FAKE_ANSWER"])) if os.environ.get("FAKE_ANSWER") else {found}
answer = found if (mode == "fields" or "picked this company" in stdin) else {candidates}
time.sleep({delay})
out({{"type": "result", "subtype": "success", "is_error": False, "result": "", "structured_output": answer}})
'''


def fake_claude(tmp_path, mode="fields", name="claude", delay=0.0):
    log = tmp_path / f"{name}.jsonl"
    script = tmp_path / name
    script.write_text(FAKE.format(python=sys.executable, log=str(log), mode=mode, found=repr(FOUND),
                                  candidates=repr(CANDIDATES), delay=delay))
    script.chmod(0o755)
    return script, log


def calls(log: Path) -> list[dict]:
    return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []


@pytest.fixture
def clients(tmp_path):
    root = tmp_path / "clients"
    root.mkdir()
    shutil.copytree(REAL_CLIENTS / "brioche-four", root / "brioche-four")
    for f in ("preplan.md", "preplan.json", "research.md"):
        (root / "brioche-four" / f).unlink(missing_ok=True)
    shutil.rmtree(root / "brioche-four" / "history", ignore_errors=True)
    return root


# --- the answer, checked field by field ----------------------------------------------------------------


def test_schema_lists_every_sheet_field_and_is_strict():
    schema = AutofillOutput.model_json_schema()
    assert set(schema["required"]) == {"status", "company", "candidates", "fields", "linked_entities"}
    assert set(schema["$defs"]["LinkedEntityOut"]["required"]) == {"name", "relation", "website", "linkedin_url",
                                                                  "source_url", "confidence"}
    fields = schema["$defs"]["FoundFields"]
    assert set(fields["properties"]) == set(FIELDS) and fields["additionalProperties"] is False
    assert set(FIELDS) <= set(ClientInfo.model_fields)  # every value lands in a real sheet field
    assert {"legal_name", "registration_number", "registered_address"} <= set(FIELDS)


def test_parse_output_keeps_only_sourced_confident_fields():
    r = parse_output(FOUND)
    assert r["status"] == "fields" and r["company"] == "Brioche & Four"
    assert set(r["fields"]) == {"website", "country", "sector", "headcount"}  # unsourced leadership dropped
    assert r["fields"]["website"] == {"value": "https://www.brioche-four.example",
                                      "source_url": "https://www.brioche-four.example/", "confidence": 0.95, "low": False}
    assert r["fields"]["headcount"]["low"] is True and 0.45 < LOW_CONFIDENCE  # kept, flagged
    assert r["linked_entities"] == [] and r["moved"] == []  # brioche-four.example is Brioche & Four's own site
    r = parse_output({"status": "fields", "company": "X", "candidates": [], "fields": {
        "website": {"value": "brioche-four dot com", "source_url": "https://a.example", "confidence": 0.9},   # not a URL
        "linkedin_url": {"value": "https://facebook.com/bf", "source_url": "https://a.example", "confidence": 0.9},
        "country": {"value": "France", "source_url": "https://a.example", "confidence": 0.1},       # too unsure
        "sector": {"value": "  Fours\n et pétrins ", "source_url": "https://a.example", "confidence": 85},  # a percentage
        "products": {"value": "", "source_url": "https://a.example", "confidence": 0.9},           # empty
        "hq_city": {"value": "Villebourg", "source_url": "https://a.example", "confidence": "n/a"},  # no confidence
        "nonsense": {"value": "x", "source_url": "https://a.example", "confidence": 1},            # not a field
    }})
    assert r["fields"] == {"sector": {"value": "Fours et pétrins", "source_url": "https://a.example",
                                      "confidence": 0.85, "low": False}}
    assert parse_output({"status": "fields", "company": "", "candidates": [], "fields": {},
                         "linked_entities": []})["status"] == "not_found"
    with pytest.raises(ValueError):
        parse_output(["not", "a", "dict"])


def test_parse_output_candidates():
    r = parse_output(CANDIDATES)
    assert r["status"] == "candidates" and r["fields"] == {} and len(r["candidates"]) == 2
    assert r["candidates"][1] == {"name": "Brioche & Four (Paris)", "website": "", "country": "France",
                                  "description": "Commerce de détail."}
    bad = parse_output({"status": "candidates", "company": "", "fields": {}, "candidates": [
        {"name": "A", "website": "not a url", "country": "", "description": ""}, {"name": ""}, "junk"]})
    assert bad["candidates"] == [{"name": "A", "website": "", "country": "", "description": ""}]


# --- the registered company vs its linked brands / entities ---------------------------------------------

REGISTERED = {"value": "BRIOCHE & FOUR SAS", "source_url": "https://registry.example/brioche-four", "confidence": 0.95}
SIREN = {"value": "SIREN 412 345 678", "source_url": "https://registry.example/brioche-four", "confidence": 0.95}
ADDRESS = {"value": "12 rue de l'Industrie, 99100 Villebourg", "source_url": "https://registry.example/brioche-four",
           "confidence": 0.9}
FORNAXA = {"name": "Fornaxa France", "relation": "brand_entity", "website": "",
          "linkedin_url": "https://www.linkedin.com/company/example-fornaxa-france", "source_url": "https://for-naxa.example/distributeurs",
          "confidence": 0.8}
BRAND_AS_WEBSITE = {
    "status": "fields", "company": "Brioche & Four", "candidates": [], "linked_entities": [FORNAXA],
    "fields": {
        "legal_name": REGISTERED, "registration_number": SIREN, "registered_address": ADDRESS,
        # the model returned the brand's site and LinkedIn page as the company's
        "website": {"value": "https://www.for-naxa.example", "source_url": "https://for-naxa.example/distributeurs", "confidence": 0.8},
        "linkedin_url": {"value": "https://www.linkedin.com/company/example-fornaxa-france",
                         "source_url": "https://www.linkedin.com/company/example-fornaxa-france", "confidence": 0.8},
        # ...while the company's own pages were read for other fields
        "sector": {"value": "Distribution de fours", "source_url": "https://www.brioche-four.example/qui-sommes-nous", "confidence": 0.85},
        "headcount": {"value": "20 à 49 salariés", "source_url": "https://www.linkedin.com/company/example-brioche-four/about/",
                      "confidence": 0.7},
        "products": {"value": "Fornaxa, Pétrinox", "source_url": "https://www.brioche-four.example/qui-sommes-nous", "confidence": 0.8},
    },
}


def test_registration_fields_are_parsed():
    r = parse_output(BRAND_AS_WEBSITE)
    assert r["fields"]["legal_name"]["value"] == "BRIOCHE & FOUR SAS"
    assert r["fields"]["registration_number"]["value"] == "SIREN 412 345 678"
    assert r["fields"]["registered_address"]["value"].endswith("Villebourg")


def test_a_brand_site_returned_as_website_moves_to_linked_entities_and_the_own_site_wins():
    r = parse_output(BRAND_AS_WEBSITE)
    assert r["fields"]["website"]["value"] == "https://www.brioche-four.example"  # the company's own, found in its sources
    assert r["fields"]["website"]["source_url"] == "https://www.brioche-four.example/qui-sommes-nous"
    assert r["fields"]["linkedin_url"]["value"] == "https://www.linkedin.com/company/example-brioche-four"
    cf = next(e for e in r["linked_entities"] if e["name"] == "Fornaxa France")
    assert cf["relation"] == "brand_entity" and cf["website"] == "https://www.for-naxa.example"  # kept, not discarded
    assert cf["linkedin_url"] == "https://www.linkedin.com/company/example-fornaxa-france"
    assert cf["source_url"] == "https://for-naxa.example/distributeurs" and cf["confidence"] == 0.8
    assert set(r["moved"]) == {"website", "linkedin_url"}


def test_no_own_site_keeps_the_brand_as_a_linked_entity_and_the_website_empty():
    data = json.loads(json.dumps(BRAND_AS_WEBSITE))
    data["linked_entities"] = []  # the model didn't even list the brand: `products` names it
    for f in ("sector", "products", "headcount"):
        data["fields"][f]["source_url"] = "https://press.example/article"
    del data["fields"]["linkedin_url"]
    data["fields"]["website"]["value"] = "https://www.fornaxa.example"
    r = parse_output(data)
    assert "website" not in r["fields"]
    assert r["linked_entities"] == [{"name": "Fornaxa", "relation": "distributed_brand", "website": "https://www.fornaxa.example",
                                     "linkedin_url": "", "source_url": "https://for-naxa.example/distributeurs",
                                     "confidence": 0.8, "low": False}]
    # a site nothing ties to someone else stays the company's
    data["fields"]["website"]["value"] = "https://www.fours-importes.example"
    assert parse_output(data)["fields"]["website"]["value"] == "https://www.fours-importes.example"


def test_linked_entities_are_checked_like_fields():
    r = parse_output({"status": "fields", "company": "Brioche & Four", "candidates": [], "fields": {}, "linked_entities": [
        {"name": "Fornaxa", "relation": "Distributed brand", "website": "https://fornaxa.example", "linkedin_url": "https://x.example/cf",
         "source_url": "https://brioche-four.example", "confidence": 0.9},
        {"name": "Ghost", "relation": "parent", "website": "", "linkedin_url": "", "source_url": "", "confidence": 0.9},
        {"name": "Unsure", "relation": "parent", "website": "", "linkedin_url": "", "source_url": "https://a.example", "confidence": 0.1},
        {"name": "Odd", "relation": "cousin", "website": "nope", "linkedin_url": "", "source_url": "https://a.example", "confidence": 0.5},
    ]})
    assert r["status"] == "fields"  # linked entities alone are an answer
    assert [(e["name"], e["relation"], e["website"], e["linkedin_url"], e["low"]) for e in r["linked_entities"]] == [
        ("Fornaxa", "distributed_brand", "https://fornaxa.example", "", False), ("Odd", "other", "", "", True)]


# --- straight into the sheet, never over a manual value ---------------------------------------------------


def test_merge_fills_empty_and_agent_fields_never_manual_ones():
    info = ClientInfo(name="Brioche & Four", country="Belgique", sector="old agent value",
                      provenance={"sector": {"by": "agent", "source_url": "https://old.example", "confidence": 0.5}})
    r = parse_output(BRAND_AS_WEBSITE)
    new, written = merge_into_sheet(info, r)
    assert new.country == "Belgique"  # typed (no provenance, a value): manual
    assert new.sector == "Distribution de fours" and new.provenance["sector"]["by"] == "agent"  # agent: refreshed
    assert new.provenance["sector"]["source_url"] == "https://www.brioche-four.example/qui-sommes-nous"
    assert new.website == "https://www.brioche-four.example" and new.legal_name == "BRIOCHE & FOUR SAS"
    assert new.provenance["legal_name"] == {"by": "agent", "source_url": "https://registry.example/brioche-four",
                                            "confidence": 0.95}
    assert [(e.name, e.by) for e in new.linked_entities] == [("Fornaxa France", "agent")]
    assert "country" not in written and "sector" in written and "linked:fornaxa france" in written
    # the FDE edits the website and removes the brand: a re-run changes neither
    edited = new.model_copy(update={"website": "https://gd.example", "provenance": new.provenance | {"website": {"by": "manual"}},
                                    "linked_entities": [], "removed_entities": ["fornaxa france"]})
    again, _ = merge_into_sheet(edited, r)
    assert again.website == "https://gd.example" and again.linked_entities == []
    # a manual field cleared by the FDE (empty or whitespace) is no longer his: the next run fills it
    for blank in ("", "   \t "):
        cleared = again.model_copy(update={"headcount": blank, "provenance": again.provenance | {"headcount": {"by": "manual"}}})
        refilled, written = merge_into_sheet(cleared, r)
        assert refilled.headcount == r["fields"]["headcount"]["value"] and "headcount" in written
        assert refilled.provenance["headcount"]["by"] == "agent"
    # a cleared field the agent has nothing for keeps no stale manual provenance
    bare = again.model_copy(update={"hq_city": " ", "provenance": again.provenance | {"hq_city": {"by": "manual"}}})
    assert "hq_city" not in merge_into_sheet(bare, {"status": "fields", "fields": {}})[0].provenance
    assert merge_into_sheet(info, {"status": "candidates", "fields": {}})[0] is info


def test_a_form_edit_marks_the_field_manual():
    old = ClientInfo(name="X", website="https://a.example",
                     provenance={"website": {"by": "agent", "source_url": "https://a.example", "confidence": 0.9}})
    new = mark_manual_edits(old, old.model_copy(update={"website": "https://b.example"}))
    assert new.provenance["website"] == {"by": "manual"}
    same = mark_manual_edits(old, old.model_copy())
    assert same.provenance["website"]["by"] == "agent"  # untouched: still the agent's


def test_clearing_a_field_resets_its_provenance():
    old = ClientInfo(name="X", website="https://a.example", legal_name="X SAS", headcount="20",
                     provenance={"website": {"by": "manual"}, "legal_name": {"by": "manual"},
                                 "headcount": {"by": "agent", "source_url": "https://a.example", "confidence": 0.9}})
    # cleared from the form (the page also sends {"by": "manual"}): empty or whitespace only, no longer his
    new = mark_manual_edits(old, old.model_copy(update={"website": "", "legal_name": "  \n ", "headcount": "",
                                                        "provenance": old.provenance | {"website": {"by": "manual"}}}))
    assert not {"website", "legal_name", "headcount"} & set(new.provenance)
    assert not is_manual(new, "website") and not is_manual(new, "legal_name")
    assert not is_manual(old.model_copy(update={"website": " "}), "website")  # empty even if still marked manual
    assert is_manual(old, "website") and is_manual(ClientInfo(name="X", sector="typed"), "sector")


def test_a_source_page_that_is_not_there_downgrades_its_suggestion():
    r = mark_dead_sources(parse_output(FOUND), {"https://registry.example/brioche-four"})
    assert r["fields"]["country"]["dead_source"] and r["fields"]["country"]["low"]
    assert r["fields"]["country"]["confidence"] < LOW_CONFIDENCE  # out of "accept all"
    assert "dead_source" not in r["fields"]["website"] and not r["fields"]["website"]["low"]


def test_dead_links_counts_only_404_410_and_unresolvable_hosts():
    import http.server

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response({"/ok": 200, "/gone": 404, "/old": 410, "/wall": 403, "/busy": 429}[self.path])
            self.end_headers()

        def log_message(self, *a):
            pass

    import socketserver

    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)  # not HTTPServer: its getfqdn() can stall
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        urls = [f"{base}/{p}" for p in ("ok", "gone", "old", "wall", "busy")] + ["http://no-such-host.invalid/x"]
        assert dead_links(urls, allow_private=True) == {f"{base}/gone", f"{base}/old", "http://no-such-host.invalid/x"}
        assert dead_links([]) == set()
    finally:
        srv.shutdown()
        srv.server_close()


def test_the_run_checks_its_sources(tmp_path):
    script, _ = fake_claude(tmp_path)
    seen = []
    mgr = AutofillManager(claude_bin=str(script),
                          check_links=lambda urls: seen.extend(urls) or {"https://www.brioche-four.example/"})
    job = mgr.wait(mgr.start("Brioche & Four").slug, 20)
    fields = job.view()["result"]["fields"]
    assert set(seen) == {"https://www.brioche-four.example/", "https://registry.example/brioche-four"}
    assert fields["website"]["dead_source"] and fields["website"]["low"] and not fields["country"]["low"]
    assert "admin.step.autofillChecking" in [s.get("key") for s in job.steps]

# --- the agent run ------------------------------------------------------------------------------------


def test_autofill_runs_sonnet_with_web_tools_only_and_no_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    script, log = fake_claude(tmp_path)
    mgr = AutofillManager(claude_bin=str(script))
    job = mgr.start("Brioche & Four", {"country": "France", "nonsense": "x"}, None, "fr")
    job = mgr.wait(job.slug, 20)
    assert job.status == "done", job.error
    v = job.view()
    assert v["result"]["status"] == "fields" and "website" in v["result"]["fields"]
    keys = [s.get("key") for s in v["steps"]]
    assert keys[0] == "admin.step.autofillStart" and "admin.step.search" in keys and "admin.step.read" in keys
    assert keys[-1] == "admin.step.autofillReady" and v["searches"] == 1 and v["pages"] == 1
    call = calls(log)[0]
    argv = call["argv"]
    assert argv[argv.index("--model") + 1] == DEFAULT_AUTOFILL_MODEL == "claude-sonnet-5-5"
    assert argv[argv.index("--tools") + 1] == "WebSearch,WebFetch"
    assert argv[argv.index("--allowedTools") + 1] == "WebSearch,WebFetch"
    assert "--strict-mcp-config" in argv and "--no-session-persistence" in argv
    assert json.loads(argv[argv.index("--json-schema") + 1])["required"] == ["status", "company", "candidates", "fields",
                                                                             "linked_entities"]
    system = argv[argv.index("--system-prompt") + 1]
    assert "REGISTERED company" in system and "linked_entities" in system and "registration_number" in system
    assert "Write the descriptive values" in argv[argv.index("--system-prompt") + 1]
    assert "in French" in argv[argv.index("--system-prompt") + 1]
    assert "Company name: Brioche & Four" in call["stdin"] and "- country: France" in call["stdin"]
    assert "nonsense" not in call["stdin"]
    wanted = re.search(r"Fields to find: (.*)", call["stdin"]).group(1)
    assert "country" not in wanted.split(", ") and "website" in wanted
    assert call["env"] == []  # subscription login only


def test_candidates_then_the_chosen_one_fills_the_sheet(tmp_path):
    script, log = fake_claude(tmp_path, mode="candidates")
    mgr = AutofillManager(claude_bin=str(script))
    job = mgr.wait(mgr.start("Brioche & Four", {}, None, "fr").slug, 20)
    r = job.view()["result"]
    assert r["status"] == "candidates" and [c["name"] for c in r["candidates"]] == ["Brioche & Four (Villebourg)", "Brioche & Four (Paris)"]
    assert job.steps[-1]["key"] == "admin.step.autofillCandidates" and job.steps[-1]["params"] == {"n": 2}

    picked = r["candidates"][0]
    job2 = mgr.wait(mgr.start("Brioche & Four", {}, picked, "fr").slug, 20)
    assert job2.slug != job.slug and job2.view()["result"]["status"] == "fields"
    stdin = calls(log)[1]["stdin"]
    assert "picked this company" in stdin and "https://www.brioche-four.example" in stdin


def test_a_schema_retry_is_not_shown_as_an_unreachable_source():
    from pacenote.autofill import AutofillJob
    from pacenote.research import _progress

    job = AutofillJob(slug="x")
    _progress(job, {"type": "user", "message": {"content": [{"type": "tool_result", "is_error": True,
               "content": "Output does not match required schema: /fields/website"}]}}, "autofillWriting")
    _progress(job, {"type": "user", "message": {"content": [{"type": "tool_result", "is_error": True,
               "content": "403 Forbidden"}]}}, "autofillWriting")
    assert [s["key"] for s in job.steps] == ["admin.step.reformat", "admin.step.unreachable"]


def test_same_name_again_reuses_the_run_and_force_runs_again(tmp_path):
    script, log = fake_claude(tmp_path, delay=1.0)
    mgr = AutofillManager(claude_bin=str(script))
    a = mgr.start("Brioche & Four", {}, None, "fr")
    b = mgr.start("  BRIOCHE & FOUR   ", {}, None, "fr")  # while it runs
    assert a is b
    mgr.wait(a.slug, 20)
    c = mgr.start("Brioche & Four", {"website": "https://x.example"}, None, "fr")  # just done: cached
    assert c is a
    assert len(calls(log)) == 1
    d = mgr.wait(mgr.start("Brioche & Four", {}, None, "fr", force=True).slug, 20)  # the button
    assert d is not a and len(calls(log)) == 2
    mgr.wait(mgr.start("Brioche & Four", {}, None, "en").slug, 20)  # another language: another run
    assert len(calls(log)) == 3


def test_failed_or_stopped_autofill_leaves_the_sheet_alone(clients, tmp_path):
    before = (clients / "brioche-four" / "client.yaml").read_text() if (clients / "brioche-four" / "client.yaml").exists() else None
    fail, _ = fake_claude(tmp_path, mode="fail", name="claude-fail")
    mgr = AutofillManager(claude_bin=str(fail))
    job = mgr.wait(mgr.start("Brioche & Four").slug, 20)
    assert job.status == "error" and "Not logged in" in job.error and job.view()["result"] is None
    slow, _ = fake_claude(tmp_path, mode="slow", name="claude-slow")
    mgr = AutofillManager(claude_bin=str(slow))
    job = mgr.start("Brioche & Four")
    deadline = time.time() + 10
    while time.time() < deadline and job.view()["pages"] == 0:
        time.sleep(0.05)
    assert job.status == "running" and mgr.stop(job.slug)
    job = mgr.wait(job.slug, 10)
    assert job.status == "stopped" and job.error is None and job.view()["result"] is None
    after = (clients / "brioche-four" / "client.yaml").read_text() if (clients / "brioche-four" / "client.yaml").exists() else None
    assert before == after  # the agent never writes the sheet


def test_typing_on_stops_the_oldest_runs(tmp_path):
    slow, _ = fake_claude(tmp_path, mode="slow", name="claude-slow")
    mgr = AutofillManager(claude_bin=str(slow))
    first = mgr.start("GD")
    second = mgr.start("GD Fr")
    third = mgr.start("Brioche & Four")
    assert mgr.wait(first.slug, 10).status == "stopped"
    assert second.status == "running" and third.status == "running"  # at most two at a time
    for j in (second, third):
        mgr.stop(j.slug)
        mgr.wait(j.slug, 10)


# --- endpoints --------------------------------------------------------------------------------------


def test_autofill_endpoints(clients, tmp_path):
    script, _ = fake_claude(tmp_path)
    mgr = AutofillManager(claude_bin=str(script))
    http = TestClient(create_admin_app(clients, autofill=mgr))
    info = http.get("/admin/api/autofill").json()
    assert info["enabled"] is True and info["model"] == "claude-sonnet-5-5" and info["fields"] == list(FIELDS)
    job = http.post("/admin/api/autofill", json={"name": "Brioche & Four", "hints": {"country": "France"}, "lang": "fr"}).json()
    assert job["status"] == "running" and job["id"]
    mgr.wait(job["id"], 20)
    done = http.get(f"/admin/api/autofill/{job['id']}").json()
    assert done["status"] == "done"
    assert done["result"]["fields"]["sector"]["source_url"] == "https://www.brioche-four.example/"
    assert http.post(f"/admin/api/autofill/{job['id']}/stop").json() == {"stopped": False}
    for bad in ({"name": " "}, {"name": "x"}, {"name": "x" * 121}, {"name": "Brioche & Four", "lang": "not a language"},
                {"name": "Brioche & Four", "hints": {"password": "x"}}, {"name": "Brioche & Four", "candidate": {"name": " "}},
                {"hints": {}}):
        assert http.post("/admin/api/autofill", json=bad).status_code in (400, 422), bad
    assert http.get("/admin/api/autofill/nope").status_code == 404
    assert http.post("/admin/api/autofill/nope/stop").status_code == 404
    assert http.post("/admin/api/clients/brioche-four/autofill/nope/apply").status_code == 404


def test_apply_writes_the_answer_into_the_sheet_and_keeps_manual_values(clients, tmp_path, monkeypatch):
    answer = tmp_path / "answer.json"
    answer.write_text(json.dumps(BRAND_AS_WEBSITE))
    monkeypatch.setenv("FAKE_ANSWER", str(answer))
    script, _ = fake_claude(tmp_path)
    mgr = AutofillManager(claude_bin=str(script))
    http = TestClient(create_admin_app(clients, autofill=mgr))
    sheet = http.get("/admin/api/clients/brioche-four").json()["info"]
    assert sheet["country"] == "France" and sheet["provenance"] == {}  # typed before Auto-fill existed
    job = http.post("/admin/api/autofill", json={"name": "Brioche & Four"}).json()
    mgr.wait(job["id"], 20)
    d = http.post(f"/admin/api/clients/brioche-four/autofill/{job['id']}/apply", json={}).json()
    info = d["info"]
    assert info["website"] == "https://www.brioche-four.example" and info["legal_name"] == "BRIOCHE & FOUR SAS"
    assert info["registration_number"] == "SIREN 412 345 678" and info["provenance"]["website"]["by"] == "agent"
    assert info["linked_entities"][0]["name"] == "Fornaxa France" and info["linked_entities"][0]["by"] == "agent"
    assert info["sector"].startswith("Distribution de matériel de boulangerie")  # typed: kept
    saved = yaml.safe_load((clients / "brioche-four" / "client.yaml").read_text())
    assert saved["provenance"]["legal_name"]["source_url"] == "https://registry.example/brioche-four"  # persisted
    assert saved["linked_entities"][0]["website"] == "https://www.for-naxa.example"
    # the FDE edits a field from the page (PUT, value changed): it becomes manual
    info["legal_name"] = "Brioche & Four SAS (édité)"
    assert http.put("/admin/api/clients/brioche-four", json=info).json()["info"]["provenance"]["legal_name"] == {"by": "manual"}
    # a re-run, after a reload (a new app on the same folder): the manual value stays
    mgr2 = AutofillManager(claude_bin=str(script))
    http2 = TestClient(create_admin_app(clients, autofill=mgr2))
    job = http2.post("/admin/api/autofill", json={"name": "Brioche & Four", "force": True}).json()
    mgr2.wait(job["id"], 20)
    d = http2.post(f"/admin/api/clients/brioche-four/autofill/{job['id']}/apply", json={}).json()
    assert d["info"]["legal_name"] == "Brioche & Four SAS (édité)" and "legal_name" not in d["written"]
    assert d["info"]["website"] == "https://www.brioche-four.example"
    # the FDE clears his manual legal name (whitespace only): provenance reset, persisted, refilled next run
    info = d["info"] | {"legal_name": "   "}
    info["provenance"] = info["provenance"] | {"legal_name": {"by": "manual"}}
    assert "legal_name" not in http2.put("/admin/api/clients/brioche-four", json=info).json()["info"]["provenance"]
    assert "legal_name" not in yaml.safe_load((clients / "brioche-four" / "client.yaml").read_text())["provenance"]
    mgr3 = AutofillManager(claude_bin=str(script))
    http3 = TestClient(create_admin_app(clients, autofill=mgr3))  # after a reload
    job = http3.post("/admin/api/autofill", json={"name": "Brioche & Four", "force": True}).json()
    mgr3.wait(job["id"], 20)
    d = http3.post(f"/admin/api/clients/brioche-four/autofill/{job['id']}/apply", json={}).json()
    assert d["info"]["legal_name"] == "BRIOCHE & FOUR SAS" and "legal_name" in d["written"]
    assert d["info"]["provenance"]["legal_name"]["by"] == "agent"
    assert d["info"]["sector"].startswith("Distribution de matériel de boulangerie")  # typed: still kept
    # the legal name is edited again by hand: it survives a re-run and a reload
    info = d["info"] | {"legal_name": "Brioche & Four SAS (édité)"}
    http3.put("/admin/api/clients/brioche-four", json=info)
    http4 = TestClient(create_admin_app(clients, autofill=mgr3))
    job = http4.post("/admin/api/autofill", json={"name": "Brioche & Four", "force": True}).json()
    mgr3.wait(job["id"], 20)
    d = http4.post(f"/admin/api/clients/brioche-four/autofill/{job['id']}/apply", json={}).json()
    assert d["info"]["legal_name"] == "Brioche & Four SAS (édité)" and d["info"]["provenance"]["legal_name"] == {"by": "manual"}
    ctx = client_context("", load_info(clients, "brioche-four"), None)
    assert "Registered company: Brioche & Four SAS (édité)" in ctx and "Fornaxa France (brand entity" in ctx


def test_autofill_model_is_configurable(clients, tmp_path, monkeypatch):
    from pacenote import cli

    seen = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: seen.setdefault("app", app))
    monkeypatch.setenv("PACENOTE_AUTOFILL_MODEL", "claude-haiku-5-5")
    assert cli.main(["admin", "--clients-dir", str(clients), "--no-browser"]) == 0
    assert TestClient(seen.pop("app")).get("/admin/api/autofill").json()["model"] == "claude-haiku-5-5"
    assert cli.main(["admin", "--clients-dir", str(clients), "--no-browser", "--autofill-model", "claude-opus-5-5"]) == 0
    assert TestClient(seen.pop("app")).get("/admin/api/autofill").json()["model"] == "claude-opus-5-5"


# --- the sheet fields ------------------------------------------------------------------------------------


def test_new_sheet_fields_are_optional_and_round_trip(tmp_path):
    root = tmp_path / "clients"
    create_client(root, "acme", "ACME")
    # an older client.yaml (before the company fields) loads unchanged, with empty defaults
    (root / "acme" / "client.yaml").write_text(yaml.safe_dump(
        {"name": "ACME", "website": "https://acme.example", "sector": "Bois", "country": "France", "language": "fr",
         "contacts": [], "notes": "", "use_preplan": True}))
    info = load_info(root, "acme")
    assert info.website == "https://acme.example" and all(getattr(info, f) == "" for f in FIELDS if f not in
                                                           ("website", "sector", "country"))
    http = TestClient(create_admin_app(root))
    sheet = http.get("/admin/api/clients/acme").json()["info"]
    assert set(FIELDS) <= set(sheet) and sheet["linked_entities"] == [] and sheet["provenance"] == {}
    sheet |= {"hq_city": "Nantes", "headcount": "20–49", "description": "Négoce de bois.", "products": "Chêne, hêtre",
              "leadership": "Marie Durand — PDG", "linkedin_url": "https://www.linkedin.com/company/example-acme",
              "working_languages": "français"}
    assert http.put("/admin/api/clients/acme", json=sheet).json()["info"]["hq_city"] == "Nantes"
    saved = yaml.safe_load((root / "acme" / "client.yaml").read_text())
    assert saved["linkedin_url"] == "https://www.linkedin.com/company/example-acme" and saved["products"] == "Chêne, hêtre"
    ctx = client_context("", load_info(root, "acme"), None)
    assert "Head office: Nantes" in ctx and "Leadership: Marie Durand — PDG" in ctx  # feeds the interview
    save_info(root, "acme", ClientInfo(name="ACME"))  # and the old shape still saves
    assert load_info(root, "acme").hq_city == ""


# --- i18n -------------------------------------------------------------------------------------------------


AF_KEYS = ("admin.autofill.", "admin.step.autofill", "admin.hqCity", "admin.headcount", "admin.description",
           "admin.products", "admin.leadership", "admin.linkedin", "admin.workingLanguages", "admin.legalName",
           "admin.registrationNumber", "admin.registeredAddress", "admin.registered", "admin.linked.", "admin.cell.")


def test_autofill_keys_exist_in_every_locale():
    en = {k for k in i18n.flatten(i18n.catalog("en")) if k.startswith(AF_KEYS)}
    assert len(en) > 40
    assert not {k for k in en if k.split(".")[-1] in ("accept", "acceptAll", "dismiss", "suggested")}  # no accept UI
    for code in i18n.available():
        raw = json.loads((Path(i18n.LOCALES_DIR) / f"{code}.json").read_text(encoding="utf-8"))
        have = {k for k in i18n.flatten(raw)}
        assert not (en - have), (code, sorted(en - have))
    assert i18n.t("admin.step.autofillStart", "fr", name="Brioche & Four", model="m") == "Recherche de Brioche & Four sur le web (m)…"


def test_admin_js_shows_no_hard_coded_text():
    """Every visible string of the admin script goes through t(): no literal words between tags of
    its HTML templates, nor assigned to textContent."""
    js = (Path(i18n.__file__).parent / "static" / "admin.js").read_text(encoding="utf-8")
    code = re.sub(r"^\s*//.*$", "", js, flags=re.M)
    templates = re.findall(r"`([^`]*)`", code)
    stripped = [re.sub(r"\$\{(?:[^{}]|\{[^{}]*\})*\}", "", x) for x in templates]
    words = [w for x in stripped for w in re.findall(r">([^<>]*[A-Za-zÀ-ÿ]{3,}[^<>]*)<", x)]
    assert not words, words
    assert not re.findall(r"textContent\s*=\s*[\"'][^\"']*[A-Za-zÀ-ÿ]{3,}", code)


# --- the browser: the agent fills, the FDE double-clicks to edit (Playwright, when available) ----------------

BROWSER = r"""
const { chromium } = require('playwright');
(async () => {
  const [base, shots] = process.argv.slice(2);
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1280, height: 1500 } });
  const errors = [];
  p.on('pageerror', e => errors.push(e.message));
  p.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  const out = {};
  const cell = (k) => `#info-form .cell[data-field="${k}"]`;
  const val = (k) => p.$eval(cell(k) + ' .cv', e => e.textContent);
  const saved = () => p.waitForResponse(r => r.url().includes('/admin/api/clients/brioche-four') && r.request().method() === 'PUT');
  await p.goto(base + '/admin?client=brioche-four');
  await p.waitForFunction(() => document.querySelector('#f-name').value === 'Brioche & Four');
  await p.waitForFunction(() => !document.querySelector('#autofill').classList.contains('hidden'));
  out.button = await p.textContent('#autofill-run');
  // the FDE types the country first (double-click, type, Enter): it must never be overwritten
  await p.dblclick(cell('country'));
  await p.fill(cell('country') + ' .inline', 'Belgique');
  let w = saved(); await p.keyboard.press('Enter'); await w;
  await p.click('#autofill-run');
  await p.waitForSelector('#autofill-candidates:not(.hidden) li');
  out.candidates = await p.$$eval('#autofill-candidate-list li b', xs => xs.map(x => x.textContent));
  await p.click('#autofill-candidate-list li button');
  await p.waitForFunction(() => document.querySelector('#info-form .cell[data-field="website"] .cv').textContent !== '');
  out.noAcceptUi = await p.evaluate(() => document.querySelectorAll('.suggest, [data-act], #autofill-accept-all').length === 0);
  out.filled = { website: await val('website'), legal: await val('legal_name'), siren: await val('registration_number'),
                 country: await val('country'), sector: await val('sector') };
  out.status = await p.textContent('#autofill-status');
  out.source = await p.getAttribute(cell('website') + ' a.src', 'href');
  out.legalLow = await p.$eval(cell('legal_name'), e => e.classList.contains('low'));
  out.linked = await p.$$eval('#linked-rows tr', rows => rows.map(r => [...r.querySelectorAll('td.cell .cv')].map(x => x.textContent)));
  out.linkedLinks = await p.$$eval('#linked-rows a.src', xs => xs.map(x => x.getAttribute('href')));
  if (shots) await p.screenshot({ path: shots, fullPage: true });
  // double-click edit: Enter saves, the field becomes manual
  await p.dblclick(cell('sector'));
  await p.fill(cell('sector') + ' .inline', 'Fours et pétrins');
  w = saved(); await p.keyboard.press('Enter'); await w;
  // Esc cancels
  await p.dblclick(cell('headcount'));
  await p.fill(cell('headcount') + ' .inline', 'zzz');
  await p.keyboard.press('Escape');
  out.afterEsc = await val('headcount');
  // remove a linked entity (× on hover)
  await p.hover('#linked-rows tr');
  w = saved(); await p.click('#linked-rows tr button[data-rm]'); await w;
  out.linkedAfterRemove = await p.$$eval('#linked-rows tr', rows => rows.length);
  // a re-run: manual values stay, the removed entity doesn't come back
  await p.click('#autofill-run');
  await p.waitForSelector('#autofill-candidates:not(.hidden) li');
  const applied = p.waitForResponse(r => r.url().includes('/apply'));
  await p.click('#autofill-candidate-list li button');
  await applied;
  await p.waitForTimeout(300);
  out.rerun = { sector: await val('sector'), country: await val('country'), website: await val('website'),
                linked: await p.$$eval('#linked-rows tr', rows => rows.length) };
  // reload: the same values, and the provenance (manual) still there
  await p.reload();
  await p.waitForFunction(() => document.querySelector('#f-name').value === 'Brioche & Four');
  await p.waitForSelector(cell('sector'));
  out.reload = { sector: await val('sector'), country: await val('country'),
                 sectorTitle: await p.getAttribute(cell('sector'), 'title') };
  out.navs = await p.evaluate(() => performance.getEntriesByType('navigation').length);
  // the FDE clears his sector (only spaces): no longer his, the next run fills it again
  await p.dblclick(cell('sector'));
  await p.fill(cell('sector') + ' .inline', '   ');
  w = saved(); await p.keyboard.press('Enter'); await w;
  out.clearedTitle = await p.getAttribute(cell('sector'), 'title');
  await p.click('#autofill-run');
  await p.waitForSelector('#autofill-candidates:not(.hidden) li');
  const refilled = p.waitForResponse(r => r.url().includes('/apply'));
  await p.click('#autofill-candidate-list li button');
  await refilled;
  await p.waitForTimeout(300);
  out.refill = { sector: await val('sector'), country: await val('country'),
                 linked: await p.$$eval('#linked-rows tr', rows => rows.length) };
  out.errors = errors;
  console.log(JSON.stringify(out));
  await b.close();
})().catch(e => { console.error(e); process.exit(1); });
"""

DEGRADED = r"""
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage();
  const errors = [];
  p.on('pageerror', e => errors.push(e.message));
  await p.route('**/admin/api/autofill', r => r.fulfill({ status: 404, body: '{"detail":"Not Found"}' }));
  await p.goto(process.argv[2] + '/admin?client=brioche-four');
  await p.waitForFunction(() => document.querySelector('#f-name').value === 'Brioche & Four');
  await p.fill('#f-name', 'Brioche & Four SAS');
  await p.waitForTimeout(1800);
  const hidden = await p.$eval('#autofill', e => e.classList.contains('hidden'));
  console.log(JSON.stringify({ hidden, errors }));
  await b.close();
})().catch(e => { console.error(e); process.exit(1); });
"""


def _node_playwright() -> bool:
    if not shutil.which("node"):
        return False
    return subprocess.run(["node", "-e", "require('playwright')"], capture_output=True).returncode == 0


def _serve(app):
    import uvicorn

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    return server, th, f"http://127.0.0.1:{port}"


@pytest.mark.skipif(not _node_playwright(), reason="needs node + playwright + chromium")
def test_browser_agent_fills_then_double_click_edit(clients, tmp_path, monkeypatch):
    (clients / "brioche-four" / "client.yaml").write_text("name: Brioche & Four\nlanguage: fr\n")  # a fresh sheet
    answer = tmp_path / "answer.json"
    answer.write_text(json.dumps(BRAND_AS_WEBSITE))
    monkeypatch.setenv("FAKE_ANSWER", str(answer))
    script, log = fake_claude(tmp_path, mode="candidates", delay=0.3)
    mgr = AutofillManager(claude_bin=str(script), check_links=lambda urls: {"https://registry.example/brioche-four"})
    server, th, base = _serve(create_admin_app(clients, autofill=mgr))
    try:
        js = tmp_path / "check.js"
        js.write_text(BROWSER, encoding="utf-8")
        out = subprocess.run(["node", str(js), base, os.environ.get("PACENOTE_SHOT_AUTOFILL", "")], capture_output=True,
                             text=True, timeout=120, cwd=tmp_path)
        assert out.returncode == 0, out.stderr
        r = json.loads(out.stdout.strip().splitlines()[-1])
        assert r["errors"] == []
        assert r["button"] and "admin." not in r["button"]
        assert r["candidates"] == ["Brioche & Four (Villebourg)", "Brioche & Four (Paris)"]
        assert r["noAcceptUi"] is True  # no suggestion boxes, no accept / dismiss
        assert r["filled"] == {"website": "https://www.brioche-four.example", "legal": "BRIOCHE & FOUR SAS",
                               "siren": "SIREN 412 345 678", "country": "Belgique", "sector": "Distribution de fours"}
        assert r["status"].startswith("Filled ") and "admin." not in r["status"]
        assert r["source"] == "https://www.brioche-four.example/qui-sommes-nous"
        assert r["legalLow"] is True  # its registry page 404s: a subtle hint, not a badge
        assert r["linked"] == [["Fornaxa France", "Brand entity (country)", "for-naxa.example",
                                "linkedin.com/company/example-fornaxa-france"]]
        assert "https://www.for-naxa.example" in r["linkedLinks"]
        assert r["afterEsc"] == "20 à 49 salariés" and r["linkedAfterRemove"] == 0
        assert r["rerun"] == {"sector": "Fours et pétrins", "country": "Belgique", "website": "https://www.brioche-four.example",
                              "linked": 0}
        assert r["reload"]["sector"] == "Fours et pétrins" and r["reload"]["country"] == "Belgique"
        assert "Auto-fill" in r["reload"]["sectorTitle"] and r["navs"] == 1
        assert r["clearedTitle"] != r["reload"]["sectorTitle"]  # cleared: no longer shown as the FDE's
        # cleared → refilled by the agent; the typed country and the removed entity are untouched
        assert r["refill"] == {"sector": "Distribution de fours", "country": "Belgique", "linked": 0}
        saved = yaml.safe_load((clients / "brioche-four" / "client.yaml").read_text())
        assert saved["provenance"]["sector"]["by"] == "agent" and saved["provenance"]["country"] == {"by": "manual"}
        assert saved["provenance"]["website"]["by"] == "agent" and saved["removed_entities"] == ["fornaxa france"]
        assert len(calls(log)) == 4  # candidates, chosen company, forced candidates ×2 (the pick reuses its answer)
        assert "- country: Belgique" in calls(log)[0]["stdin"]  # what the FDE typed is given as known
    finally:
        server.should_exit = True
        th.join(timeout=10)


@pytest.mark.skipif(not _node_playwright(), reason="needs node + playwright + chromium")
def test_browser_without_autofill_endpoint_keeps_the_form_working(clients, tmp_path):
    script, log = fake_claude(tmp_path)
    server, th, base = _serve(create_admin_app(clients, autofill=AutofillManager(claude_bin=str(script))))
    try:
        js = tmp_path / "degraded.js"
        js.write_text(DEGRADED, encoding="utf-8")
        out = subprocess.run(["node", str(js), base], capture_output=True, text=True, timeout=60, cwd=tmp_path)
        assert out.returncode == 0, out.stderr
        r = json.loads(out.stdout.strip().splitlines()[-1])
        assert r == {"hidden": True, "errors": []} and calls(log) == []  # no button, no run, no error
    finally:
        server.should_exit = True
        th.join(timeout=10)
