"""The plan's new sections (current state with the communication map, where the signal is lost,
metrics, typed opportunities, timeline), the offline map drawing, and the dated knowledge briefs
(capabilities, FDE practice): staleness, plan prompt, footer, refresh."""

from __future__ import annotations

import json
from datetime import date

import pytest
from fastapi.testclient import TestClient

from pacenote import briefs
from pacenote.cli import main
from pacenote.contract import PlanDoc, empty_maps, parse_plan_doc
from pacenote.finalize import assemble_plan, comm_map_mermaid, md_to_html, mermaid_svg, open_items, ranked, render_plan_md
from pacenote.llm import offline_plan_doc
from pacenote.planview import plan_payload, print_document
from pacenote.prompts import build_plan_system, build_plan_user
from pacenote.state import SessionState

from .conftest import plan_doc, plan_fact, plan_use_case, real_client
from .test_phases import fast, flow, org
from .test_questions import jargon


def state() -> SessionState:
    return SessionState.from_questionnaire(real_client().questionnaire)


def measure(value="", status=None, evidence="", date_="", question="", ask=""):
    return {"value": value, "date": date_, "status": status or ("stated" if value else "to_confirm"),
            "evidence": evidence, "question": question, "ask": ask}


PEOPLE = [{"name": "Claire", "role": "import", "department": "achats", "reports_to": "Marc", "time_split": "60 % à relancer",
           "status": "stated", "evidence": "Claire passe ses journées à relancer"},
          {"name": "Marc", "role": "PDG", "department": "direction", "reports_to": "", "time_split": "",
           "status": "stated", "evidence": "moi, Marc"},
          {"name": "Inès", "role": "commandes", "department": "ventes", "reports_to": "Marc", "time_split": "",
           "status": "partial", "evidence": ""}]
FLOWS = [{"sender": "Marc", "receiver": "Claire", "what": "date de mise à dispo", "channel": "mail", "cadence": "chaque commande",
          "wait": "2 jours", "who_waits": "Claire", "confidential": "yes", "status": "stated",
          "evidence": "elle attend que je lui transfère le mail"},
         {"sender": "Inès", "receiver": "Marc", "what": "état des commandes \"urgentes\"", "channel": "WhatsApp", "cadence": "",
          "wait": "", "who_waits": "", "confidential": "no", "status": "stated", "evidence": "il me l'envoie sur WhatsApp"}]
CURRENT = {"people": PEOPLE, "flows": FLOWS,
           "systems": [{"category": "spreadsheets", "tool": "suivi-commandes.xlsx", "used_by": "Inès", "used_for": "statut",
                        "source_of_truth": "yes", "access_owner": "", "status": "stated", "evidence": "le fichier de Inès"}],
           "stores": [{"what": "comment choisir un transporteur", "where": "head", "owner": "Claire", "how_found": "",
                       "reuse_pain": "personne ne sait quand elle est absente", "status": "stated", "evidence": "c'est Claire qui sait"}],
           "decisions": [{"decision": "réunion du lundi", "owner": "Marc", "frequency": "hebdo", "inputs": "fichier Excel",
                          "prep_time": "1 h", "decide_time": "15 min", "status": "stated", "evidence": "une heure à se remettre à jour"}]}
BOTTLENECKS = [{"kind": "waits_on_ceo", "who_waits": "Claire", "waits_on": "Marc", "what": "date de mise à dispo",
                "how_long": "2 jours", "status": "stated", "evidence": "elle attend que je lui transfère le mail",
                "question": "", "ask": ""},
               {"kind": "ceo_asks_status", "who_waits": "Marc", "waits_on": "Inès", "what": "où en sont les commandes",
                "how_long": "", "status": "to_confirm", "evidence": "", "question": "Combien de fois par semaine ?", "ask": "Marc"}]
METRICS = [{"id": "orders-delay", "use_case": "bakery-orders", "name": "Délai de saisie d'une commande", "unit": "heures",
            "how_measured": "date du mail vs date de création dans l'ERP", "cadence": "hebdomadaire", "owner": "Claire",
            "baseline": measure("24 h", evidence="on les saisit le lendemain", date_="2026-11"),
            "target": measure("", question="Quel délai serait un succès ?", ask="Marc")}]


def full_doc(**kw) -> PlanDoc:
    orders = plan_use_case(volume=plan_fact("40 commandes par jour", "quarante par jour"))
    orders["metric"] = "orders-delay"
    stock = plan_use_case("stock-requests", "Demandes de stock", impact=5, feasibility=5)
    stock["back_office"] = False
    stock["pattern"] = "status_without_asking"
    return parse_plan_doc(plan_doc(orders, stock, current_state=CURRENT, bottlenecks=BOTTLENECKS, metrics=METRICS,
                                   timeline=[{"week": 4, "milestone": "Point d'étape", "proof": "50 commandes rejouées"},
                                             {"week": 1, "milestone": "Première tranche", "proof": "10 commandes"}], **kw))


def test_plan_has_the_new_sections_in_order():
    md = render_plan_md(full_doc(), state())
    order = ["## Contexte", "## État des lieux", "## Où l'information se perd", "## Indicateurs",
             "## Opportunités classées", "## Opportunités en détail", "## Calendrier d'engagement", "## Points à confirmer"]
    positions = [md.index(h) for h in order]
    assert positions == sorted(positions)
    assert "```mermaid\nflowchart LR" in md
    assert "| Claire | import | achats | Marc | 60 % à relancer |" in md
    assert "| Inès _(À confirmer)_ |" in md  # no quote: not a fact
    assert "| Tableurs | suivi-commandes.xlsx | Inès | statut | oui |" in md
    assert "comment choisir un transporteur — savoir non écrit (dans la tête)" in md
    assert "| Attend le dirigeant | Claire | Marc | date de mise à dispo | 2 jours | « elle attend" in md
    assert "| Délai de saisie d'une commande | heures | date du mail vs date de création dans l'ERP | hebdomadaire | Claire | 24 h (2026-11) | À confirmer |" in md
    detail = md.split("## Opportunités en détail")[1]
    assert "**Type d'opportunité** : Agir pour le compte de" in detail
    assert "**Ce que l'humain décide** : Claire valide les commandes atypiques." in detail
    assert "**Première preuve** : 50 commandes de la semaine passée rejouées — sur environ 50 cas réels, semaine 2" in detail
    assert "**Critère d'arrêt**" in detail and "**Ce qu'on ne promet pas**" in detail
    assert "**Acheter, configurer ou développer** : Configurer un outil existant" in detail
    assert "**Indicateur** : Délai de saisie d'une commande — aujourd'hui : 24 h (2026-11) · cible : À confirmer" in detail
    timeline = md.split("## Calendrier d'engagement")[1].split("##")[0]
    assert timeline.index("Première tranche") < timeline.index("Point d'étape")  # by week


def test_missing_numbers_become_open_items_never_estimates():
    d = full_doc()
    rows = open_items(d, state().tr())
    assert rows[0] == ("Délai de saisie d'une commande — Cible du pilote", "Quel délai serait un succès ?", "Marc")
    assert any(r[1] == "Combien de fois par semaine ?" for r in rows)  # the bottleneck to confirm


def test_back_office_measurable_high_volume_work_ranks_first():
    d = full_doc()
    # stock scores 5x5 but is front-office with no metric and no stated volume: orders come first
    assert [u.id for u in ranked(d)] == ["bakery-orders", "stock-requests"]


def test_recipe_is_reserved_strict_and_nullable():
    raw = plan_doc(plan_use_case())
    raw["use_cases"][0]["recipe"] = {"id": "mailbox-to-erp", "fit": "partial", "gaps": ["ERP sans API"]}
    assert parse_plan_doc(raw).use_cases[0].recipe.fit == "partial"
    raw["use_cases"][0]["recipe"] = {"id": "x", "fit": "maybe", "gaps": []}
    with pytest.raises(ValueError):
        parse_plan_doc(raw)
    del raw["use_cases"][0]["recipe"]
    with pytest.raises(ValueError):
        parse_plan_doc(raw)  # required (null allowed): the schema stays strict


def test_communication_map_is_drawn_offline():
    src = comm_map_mermaid(full_doc())
    assert src.splitlines()[0] == "flowchart LR"
    assert '==>|"date de mise à dispo · mail · ⏳ 2 jours"|' in src  # someone waits: thick arrow
    assert "#quot;urgentes#quot;" in src  # quotes escaped for Mermaid
    svg = mermaid_svg(src)
    assert svg.startswith("<svg") and svg.count("<line") == 2  # two flows; reporting lines already linked
    assert "Claire · import" in svg and "&quot;urgentes&quot;" in svg
    html = md_to_html(render_plan_md(full_doc(), state()))
    assert '<figure class="comm-map"><svg' in html and "<summary>Mermaid</summary>" in html
    assert "<script" not in html
    assert mermaid_svg("sequenceDiagram\n A->>B: hi") is None
    assert "<details class=\"mermaid-src\" open>" in md_to_html("```mermaid\nsequenceDiagram\n  A->>B: hi\n```")
    assert '<figure class="comm-map"><svg' in print_document(render_plan_md(full_doc(), state()), "t", "fr")


def test_offline_plan_doc_comes_from_the_maps():
    st = state()
    st.apply_fast_result(fast(org=[org(role="import", reports_to="Marc")],
                              flows=[flow(sender="Claire", receiver="Marc", wait="deux jours")]))
    doc = offline_plan_doc(st)
    assert doc.current_state.people[0].name == "Claire" and doc.current_state.people[0].status == "stated"
    assert doc.bottlenecks[0].kind == "waits_on_ceo" and doc.bottlenecks[0].how_long == "deux jours"
    md = render_plan_md(doc, st)
    assert "## État des lieux" in md and "```mermaid" in md


def test_plan_prompt_carries_the_dated_briefs_and_the_maps():
    st = state()
    st.apply_fast_result(fast(org=[org()]))
    system = build_plan_system(st, "", "")
    cap = briefs.load_brief("capabilities")
    assert f"capability brief, as of {cap.as_of_text}" in system
    assert "<capability_brief>" in system and "computer use" in system.lower()
    assert "<fde_practice>" in system and "kill criteria" in system
    assert "never claim a capability it does not contain" in system
    assert "route_distil" in system and "decision_brief" in system and "NEVER invent a number" in system
    user = build_plan_user(st, [], None)
    assert "<map_evidence" in user and "Claire s'occupe de l'import" in user


def test_plan_footer_states_the_capability_date(tmp_path):
    st = state()
    md = assemble_plan(render_plan_md(full_doc(), st), st)
    assert f"Capacités des agents : état au {briefs.load_brief('capabilities').as_of_text}." in md


def test_briefs_are_dated_sourced_and_flag_staleness(tmp_path, monkeypatch, capsys):
    for name in briefs.BRIEFS:
        b = briefs.load_brief(name)
        assert b.as_of is not None and b.sources and b.body.startswith("# ")
        assert not b.stale(b.as_of)
        assert b.stale(date.fromordinal(b.as_of.toordinal() + 31))
    assert "(unconfirmed)" in briefs.load_brief("capabilities").body
    assert "vendor-reported" in briefs.load_brief("fde-practice").body
    old = tmp_path / "k"
    old.mkdir()
    (old / "capabilities.md").write_text("---\nas_of: 2026-01-01\nsources: []\n---\n# Old\n", encoding="utf-8")
    (old / "fde-practice.md").write_text("no front matter\n", encoding="utf-8")
    monkeypatch.setenv("PACENOTE_KNOWLEDGE_DIR", str(old))
    assert {b.name for b in briefs.stale_briefs(date(2026, 11, 4))} == {"capabilities", "fde-practice"}
    assert main(["capabilities", "show"]) == 0
    out = capsys.readouterr().out
    assert "[!!] capabilities brief: as of 2026-01-01" in out and "pacenote capabilities refresh" in out


def test_plan_view_warns_when_the_brief_is_old(tmp_path, monkeypatch):
    d = tmp_path / "sessions" / "brioche-four" / "20261104-100000"
    d.mkdir(parents=True)
    (d / "plan.md").write_text("# Plan\n", encoding="utf-8")
    assert plan_payload(d)["capabilities"]["stale"] is False
    k = tmp_path / "k"
    k.mkdir()
    (k / "capabilities.md").write_text("---\nas_of: 2025-01-01\n---\n# Old\n", encoding="utf-8")
    monkeypatch.setenv("PACENOTE_KNOWLEDGE_DIR", str(k))
    assert plan_payload(d)["capabilities"] == {"as_of": "2025-01-01", "stale": True}


def test_capabilities_refresh_rewrites_the_brief_with_sources(tmp_path, monkeypatch):
    k = tmp_path / "k"
    k.mkdir()
    for name, fname in briefs.BRIEFS.items():
        (k / fname).write_text((briefs.KNOWLEDGE_DIR / fname).read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("PACENOTE_KNOWLEDGE_DIR", str(k))
    seen = {}

    def runner(job, cmd, prompt, timeout, prefix):
        seen.update(cmd=cmd, prompt=prompt)
        job.step("search", "computer use GA")
        return {"body_md": "# What agents can do today\n\n- New item (unconfirmed)\n",
                "sources": [{"title": "Release notes", "url": "https://example.com/notes"}]}

    steps = []
    b = briefs.refresh_brief("capabilities", claude_bin="/bin/echo", on_step=steps.append, today=date(2026, 12, 1),
                             runner=runner)
    assert b.as_of == date(2026, 12, 1) and b.sources == [{"title": "Release notes", "url": "https://example.com/notes"}]
    assert "New item" in b.body and steps and steps[0]["kind"] == "search"
    cmd = seen["cmd"]
    assert cmd[cmd.index("--tools") + 1] == "WebSearch,WebFetch" and "--json-schema" in cmd
    assert "Current brief (as of 2026-11-04)" in seen["prompt"]

    def broken(job, cmd, prompt, timeout, prefix):
        raise RuntimeError("Claude CLI error")

    before = (k / "capabilities.md").read_text(encoding="utf-8")
    with pytest.raises(RuntimeError):
        briefs.refresh_brief("capabilities", claude_bin="/bin/echo", runner=broken)
    assert (k / "capabilities.md").read_text(encoding="utf-8") == before  # a failed run changes nothing


def test_plan_texts_shown_to_the_client_use_no_ai_jargon_in_questions():
    from pacenote import i18n

    for lang in i18n.available():
        cat = i18n.flatten(i18n.catalog(lang))
        for k in ("plan.metricQuestion.baseline", "plan.metricQuestion.target"):
            assert not jargon(cat[k]), (lang, k)
