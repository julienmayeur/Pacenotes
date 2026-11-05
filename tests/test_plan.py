"""The plan is a factual integration proposal: a structured PlanDoc (one use case per section, the
same integration slots as the interview) rendered to Markdown + printable HTML. Anything not said
in the interview renders as an open item "to confirm" with the question and who to ask: never empty,
never invented."""

import asyncio
import json
import re

from pacenote import cli
from pacenote.capture import ReplaySource
from pacenote.contract import SLOT_KEYS, Fact, PlanDoc
from pacenote.finalize import archive_plan, clean_fact, open_items, ranked, render_plan_md
from pacenote.planview import print_document
from pacenote.llm import OfflineEngine, offline_plan_doc
from pacenote.prompts import build_plan_system, build_plan_user
from pacenote.questionnaire import load_client
from pacenote.session import Session, Settings
from pacenote.state import SessionState

from .conftest import CLIENTS, fill, plan_doc, plan_fact, plan_use_case, result

QUIET = Settings(sync_interval=3600, first_sync_delay=3600, poll_interval=3600, fast_enabled=False)
FIELDS_FR = ("Architecture", "Étapes", "Déclencheur", "Actions", "Accès aux données", "Source et déclencheur",
             "Accès et droits", "Outil", "Traitement", "Acteurs", "Diffusion", "Modèle", "Résultat attendu",
             "Aujourd'hui", "Objectif", "Volume")


def slots_state() -> SessionState:
    return SessionState.from_questionnaire(load_client(CLIENTS, "brioche-four-slots").questionnaire)


def doc(*cases, **kw) -> PlanDoc:
    return PlanDoc.model_validate(plan_doc(*cases, **kw))


def sections(md: str) -> dict[str, str]:
    """### use-case sections of the rendered plan, by title."""
    body = md.split("## Points à confirmer")[0]
    parts = re.split(r"(?m)^### \d+\. ", body)[1:]
    return {p.splitlines()[0].strip(): p for p in parts}


ORDERS = plan_use_case(
    "bakery-orders", "Saisie des commandes", impact=4, feasibility=4,
    trigger=plan_fact("Mails des boulangeries dans la boîte commandes", "les commandes arrivent par mail"),
    tool=plan_fact("ERP (nom non précisé)", "on a un ERP", status="partial", question="Quel ERP, quelle version ?",
                   ask="Claire"),
    volume=plan_fact("environ une commande par semaine", ""),  # stated without a quote: becomes "to confirm"
    baseline=plan_fact("", "", status="to_confirm", question="", ask=""),  # nothing at all: default question
    target=plan_fact("Doubler les commandes traitées", "doubler le nombre de commandes", status="to_confirm"))
STOCK = plan_use_case("stock-requests", "Demandes de stock", impact=3, feasibility=2)
WARRANTY = plan_use_case("warranty-claims", "Garantie", impact=5, feasibility=5)


def test_every_use_case_section_has_every_field():
    md = render_plan_md(doc(ORDERS, STOCK), slots_state())
    found = sections(md)
    assert set(found) == {"Saisie des commandes", "Demandes de stock"}
    for title, text in found.items():
        for label in FIELDS_FR:
            assert f"**{label}**" in text, (title, label)
        assert "Anthropic (Claude) — Extraction fiable de mails libres." in text  # provider + its reason
        assert "**Déclencheur** : Événement" in text


def test_unknown_values_render_as_open_items_never_empty_or_invented():
    md = render_plan_md(doc(ORDERS), slots_state())
    text = sections(md)["Saisie des commandes"]
    assert "**Source et déclencheur** : Mails des boulangeries dans la boîte commandes" in text  # stated
    assert "ERP (nom non précisé) — **À confirmer** : Quel ERP, quelle version ? (Claire)" in text  # partial
    assert ("environ une commande par semaine — **À confirmer** : Confirmer cette information "
            "(pas de citation de l'entretien)") in text  # stated without a quote is not a fact
    assert ("**Aujourd'hui** : **À confirmer** : Combien de temps cela prend-il aujourd'hui, avec quels délais "
            "ou erreurs ? (interlocuteur à identifier)") in text  # never empty, never a made-up person
    assert "Doubler les commandes" not in md  # a "to_confirm" slot never shows a value
    for line in text.splitlines():
        assert not re.match(r"^-? ?\*\*[^*]+\*\* : ?$", line), line  # no field left empty


def test_open_items_collect_every_gap_plus_the_general_ones():
    d = doc(ORDERS, open_items=[{"topic": "Budget", "question": "Quelle enveloppe sur six mois ?", "ask": "PDG"}])
    rows = open_items(d, slots_state().tr())
    stated = {"trigger"}
    assert len(rows) == len(SLOT_KEYS) - len(stated) + 1
    assert ("Saisie des commandes — Outil", "Quel ERP, quelle version ?", "Claire") in rows
    assert rows[-1] == ("Budget", "Quelle enveloppe sur six mois ?", "PDG")
    md = render_plan_md(d, slots_state())
    assert "## Points à confirmer" in md and "| Saisie des commandes — Outil | Quel ERP, quelle version ? | Claire |" in md


def test_use_cases_are_ranked_by_impact_times_feasibility():
    d = doc(STOCK, ORDERS, WARRANTY)
    assert [u.id for u in ranked(d)] == ["warranty-claims", "bakery-orders", "stock-requests"]
    md = render_plan_md(d, slots_state())
    table = md.split("## Opportunités classées")[1].split("## Opportunités en détail")[0]
    assert table.index("Garantie") < table.index("Saisie des commandes") < table.index("Demandes de stock")
    assert "| 1 | Garantie | Agir pour le compte de | 5/5 | 5/5 |" in table
    assert "Impact : Trois heures par jour perdues." in table  # every score carries its justification


def test_clean_fact_only_keeps_quoted_statements_as_facts():
    assert clean_fact(Fact(status="stated", value="Sage", evidence="dans Sage", question="x", ask="")).status == "stated"
    assert clean_fact(Fact(status="stated", value="Sage", evidence="", question="", ask="")).status == "partial"
    assert clean_fact(Fact(status="stated", value=" ", evidence="", question="", ask="")).status == "to_confirm"
    gone = clean_fact(Fact(status="to_confirm", value="40 par jour", evidence="?", question="Combien ?", ask=""))
    assert gone.value == "" and gone.evidence == "" and gone.question == "Combien ?"


def test_no_use_case_and_the_print_document(tmp_path):
    st = slots_state()
    md = render_plan_md(doc(context=""), st)
    assert "Aucun cas d'usage n'a été décrit concrètement" in md and "_À compléter._" in md
    html = print_document(render_plan_md(doc(ORDERS), st), "Proposition", "fr")  # what Export PDF prints
    assert "<table>" in html and "<h3>1. Saisie des commandes</h3>" in html and "@page" in html
    assert "<ol>" in html and "<li>Lire le mail</li>" in html  # steps render as a numbered list


def test_plan_renders_in_the_interview_language():
    st = slots_state()
    st.set_languages(interview="en")
    md = render_plan_md(doc(ORDERS), st)
    assert md.startswith("# Integration proposal — Brioche & Four") and "## Open items to confirm" in md
    assert "**Data access**" in md and "**To confirm**: Quel ERP, quelle version ? (Claire)" in md


# --- generation ---------------------------------------------------------------------------------------


def test_offline_doc_turns_live_slots_into_facts_and_the_rest_into_open_items():
    st = slots_state()
    st.apply_sync_result(result(slots=[fill("bakery-orders", "tool", "ERP Sage", "on saisit tout dans Sage")]))
    d = offline_plan_doc(st)
    assert [u.id for u in d.use_cases] == ["bakery-orders"]  # only use cases the interview touched
    u = d.use_cases[0]
    assert u.slots.tool.status == "stated" and u.slots.tool.evidence == "on saisit tout dans Sage"
    assert all(getattr(u.slots, k).status == "to_confirm" for k in SLOT_KEYS if k != "tool")
    md = render_plan_md(d, st)
    assert "**Outil** : ERP Sage" in md and "Fournisseur à confirmer" in md


class _PlanEngine(OfflineEngine):
    def __init__(self):
        super().__init__()
        self.prompts = []

    def write_plan_doc(self, system, user, *, state):
        self.prompts.append((system, user))
        return PlanDoc.model_validate(plan_doc(ORDERS))


def test_finalize_writes_the_proposal_and_its_json(tmp_path):
    engine = _PlanEngine()
    s = Session(client=load_client(CLIENTS, "brioche-four-slots"), session_dir=tmp_path / "s", engine=engine,
                source_factory=lambda d: ReplaySource({}), settings=QUIET)
    s.state.apply_sync_result(result(slots=[fill("bakery-orders", "volume", "40 par semaine", "quarante par semaine")]))
    files = asyncio.run(s.finalize())
    assert set(files) == {"md", "json"} and not (tmp_path / "s" / "plan.html").exists()  # no plan.html any more
    md = (tmp_path / "s" / "plan.md").read_text(encoding="utf-8")
    assert md.startswith("# Proposition d'intégration — Brioche & Four") and "## Journal de l'entretien" in md
    saved = PlanDoc.model_validate_json((tmp_path / "s" / "plan.json").read_text(encoding="utf-8"))
    assert saved.use_cases[0].id == "bakery-orders"
    system, user = engine.prompts[0]
    assert "quarante par semaine" in user and '"volume": "40 par semaine"' in user  # live slots reach the plan


def test_plan_prompt_states_the_fact_rule_the_slots_and_the_providers():
    st = slots_state()
    system = build_plan_system(st, "", "Brioche & Four importe des Fornaxa.")
    assert "FACTUAL INTEGRATION PROPOSAL" in system and "NEVER invent" in system
    assert all(f"- {k} (" in system for k in SLOT_KEYS)
    assert "anthropic, openai or xai" in system and "impact and feasibility" in system
    assert "hypotheses, never facts" in system
    user = build_plan_user(st, [], None)
    assert "<slot_evidence" in user and json.loads(user.split("<state>\n", 1)[1].split("\n</state>")[0])["use_cases"]


def test_cli_finalize_keeps_the_previous_plan(tmp_path, capsys):
    sdir = tmp_path / "sessions" / "brioche-four-slots" / "1"
    s = Session(client=load_client(CLIENTS, "brioche-four-slots"), session_dir=sdir, engine=OfflineEngine(),
                source_factory=lambda d: ReplaySource({}), settings=QUIET)
    s.persist()
    (sdir / "plan.md").write_text("# old plan\n", encoding="utf-8")
    (sdir / "plan.html").write_text("<p>old</p>\n", encoding="utf-8")
    code = cli.main(["finalize", "--offline", "--client", "brioche-four-slots", "--clients-dir", str(CLIENTS),
                     "--sessions-dir", str(tmp_path / "sessions"), "--session", "1", "--keep-previous"])
    assert code == 0
    assert (sdir / "plan-v1.md").read_text(encoding="utf-8") == "# old plan\n"
    assert (sdir / "plan.html").read_text(encoding="utf-8") == "<p>old</p>\n"  # an old plan.html is left alone
    assert not (sdir / "plan-v1.html").exists()
    assert (sdir / "plan.md").read_text(encoding="utf-8").startswith("# Proposition d'intégration")
    out = capsys.readouterr().out
    assert "plan-v1.md" in out and "plan.json" in out and "plan.html" not in out
    (sdir / "plan.pdf").write_bytes(b"%PDF-1.4 exported")
    assert archive_plan(sdir) == [str(sdir / "plan-v2.md"), str(sdir / "plan-v2.json"), str(sdir / "plan-v2.pdf")]
    assert (sdir / "plan.html").exists()
