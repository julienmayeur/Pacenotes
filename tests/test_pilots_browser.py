"""Real-browser checks (Node + Playwright, skipped without them) of the pilots and the sponsor, with fake data
and no model call: the Plan view with the boss as sponsor alongside several pilots, the Plan view with no
pilot (the boss leads by example), an operator alone (the sponsor is to be confirmed), and the admin's
pre-study without any expected count of pilots. Fixed spare ports 8796 (admin) and 8797 (Plan view).
Fictional people and companies only."""

from __future__ import annotations

import asyncio
import json
import shutil

import pytest
import yaml

from pacenote.admin import create_admin_app
from pacenote.capture import ReplaySource
from pacenote.contract import parse_plan_doc
from pacenote.llm import OfflineEngine
from pacenote.preparation import load_client
from pacenote.server import create_app
from pacenote.session import Session, Settings

from .conftest import CLIENTS, REAL_CLIENTS, fill, plan_doc, plan_use_case, result
from .test_admin import RESULT
from .test_horizons_browser import ADMIN_PORT, APP_PORT, run_node, serve
from .test_i18n import _node_playwright
from .test_phases import maps, org

QUIET = Settings(sync_interval=3600, first_sync_delay=3600, poll_interval=3600, fast_enabled=False)
pytestmark = pytest.mark.skipif(not _node_playwright(), reason="needs node + playwright")

BOSS = {"name": "Hélène Varenne", "role": "PDG", "persona": "executive", "origin": "rule"}
CLERK = {"name": "Léa Morin", "role": "assistante ADV", "persona": "operator", "origin": "rule"}

# argv: url, optional screenshot path (the adoption plan, for a human to look at)
PLAN_SCRIPT = r"""
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1100, height: 900 } });
  const errors = [];
  p.on('pageerror', (e) => errors.push(String(e)));
  await p.goto(process.argv[2] + '/plan');
  await p.waitForSelector('#plan-doc h1');
  const r = await p.evaluate(() => {
    const h2 = [...document.querySelectorAll('#plan-doc h2')];
    const start = h2.find((x) => /adoption/i.test(x.textContent));
    const part = [];
    for (let n = start; n && !(n !== start && n.tagName === 'H2'); n = n.nextElementSibling) part.push(n);
    return { adoption: part.map((n) => n.textContent).join('\n'), text: document.querySelector('#plan-doc').textContent,
             pilots: part.flatMap((n) => n.tagName === 'UL' ? [...n.querySelectorAll(':scope > li')].map((li) => li.textContent) : []) };
  });
  if (process.argv[3]) {
    const box = await p.evaluate(() => {
      const h2 = [...document.querySelectorAll('#plan-doc h2')];
      const i = h2.findIndex((x) => /adoption/i.test(x.textContent));
      const top = h2[i].getBoundingClientRect().top + window.scrollY - 12;
      const end = h2[i + 1] ? h2[i + 1].getBoundingClientRect().top + window.scrollY : document.body.scrollHeight;
      const doc = document.querySelector('#plan-doc').getBoundingClientRect();
      return { x: doc.left - 12, y: top, width: doc.width + 24, height: end - top };
    });
    await p.screenshot({ path: process.argv[3], clip: box, fullPage: true });
  }
  console.log(JSON.stringify({ ...r, errors }));
  await b.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""


class _BarePlanEngine(OfflineEngine):
    """A written plan that names nobody: the pilots and the sponsor come from the interview (finalize)."""

    def write_plan_doc(self, system, user, state=None):
        return parse_plan_doc(plan_doc(
            plan_use_case("bakery-orders", "Saisie des commandes", horizon="h1"),
            plan_use_case("sav", "Pré-diagnostic SAV", horizon="h2", feasibility=3),
            adoption={"lighthouse": "bakery-orders"}))


def _session(tmp_path, stamp: str, people) -> Session:
    s = Session(client=load_client(CLIENTS, "brioche-four-slots"), session_dir=tmp_path / "s" / stamp,
                engine=_BarePlanEngine(), source_factory=lambda d: ReplaySource({}), settings=QUIET)
    s.state.set_attendees([dict(p) for p in people])
    return s


def several_pilots_session(tmp_path) -> Session:
    """The boss and three people the interview described as keen, one of them a whole team."""
    s = _session(tmp_path, "20261117-100000", [BOSS, CLERK])
    s.state.apply_sync_result(result(
        maps=maps(org=[org(rid="claire", person="Claire", role="assistante ADV",
                           adoption_signal="elle essaie toujours les nouveautés en premier", evidence="Claire à l'ADV"),
                       org(rid="marc", person="Marc", role="magasinier",
                           adoption_signal="c'est lui qui a montré le scanner aux autres", evidence="Marc au dépôt"),
                       org(rid="ventes", person="toute l'équipe ventes", role="",
                           adoption_signal="ils sont tous partants pour essayer", evidence="l'équipe ventes")]),
        slots=[fill("bakery-orders", "actors", "Claire", "c'est Claire qui saisit")]))
    return s


def no_pilot_session(tmp_path) -> Session:
    s = _session(tmp_path, "20261117-110000", [BOSS])
    s.state.posture = "explorer"
    return s


def plan_in_browser(tmp_path, s: Session, shot: str = "") -> dict:
    asyncio.run(s.finalize())
    with serve(create_app(s), APP_PORT) as url:
        return run_node(tmp_path, PLAN_SCRIPT, url, *([shot] if shot else []))


def test_plan_view_names_the_boss_as_sponsor_alongside_several_pilots(tmp_path):
    r = plan_in_browser(tmp_path, several_pilots_session(tmp_path))
    assert r["errors"] == []
    assert [x.split(" — ")[0] for x in r["pilots"]] == ["Claire", "Marc", "toute l'équipe ventes (en groupe)"]
    assert "« c'est lui qui a montré le scanner aux autres »" in r["pilots"][1]
    a = r["adoption"]
    assert "Sponsor : PDG, qui montre l'exemple" in a and a.index("Sponsor") < a.index("Pilotes")
    assert "Le sponsor (PDG) l'utilise d'abord sur son propre travail, avec les pilotes" in a
    for leak in ("Varenne", "Hélène", "Morin", "en général", "Opérationnel"):
        assert leak not in a, leak
    assert "Dans la réunion : PDG — Dirigeant; assistante ADV — Opérationnel" in r["text"]


def test_plan_view_with_no_pilot_the_boss_leads_by_example(tmp_path):
    r = plan_in_browser(tmp_path, no_pilot_session(tmp_path))
    assert r["errors"] == [] and r["pilots"] == []
    a = r["adoption"]
    assert "Sponsor : PDG, qui montre l'exemple : l'utilise d'abord sur son propre travail" in a
    assert "Le sponsor (PDG) ouvre la voie sur son propre travail" in a and "en premier ? (PDG)" in a
    for leak in ("Varenne", "Hélène", "explorer", "Explorateur"):
        assert leak not in r["text"], leak
    for leak in ("en général", "en retard", "âgé", "réticen"):  # no count, no pace, no label on anyone
        assert leak not in a, leak


def test_plan_view_with_an_operator_alone_asks_the_leadership_for_a_sponsor(tmp_path):
    r = plan_in_browser(tmp_path, _session(tmp_path, "20261117-120000", [CLERK]))
    assert r["errors"] == []
    assert "Sponsor : à confirmer avec la direction." in r["adoption"] and "assistante ADV, qui" not in r["adoption"]


ADMIN_SCRIPT = r"""
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1280, height: 1000 } });
  const errors = [];
  p.on('pageerror', (e) => errors.push(String(e)));
  await p.addInitScript(() => localStorage.setItem('pacenote.ui-language', 'en'));
  await p.goto(process.argv[2] + '/admin?client=brioche-four');
  await p.waitForSelector('#client:not(.hidden)');
  await p.click('#tabs button[data-tab="prestudy"]');
  await p.waitForFunction(() => document.querySelector('#prestudy').textContent.length > 20);
  const r = await p.evaluate(() => ({ text: document.querySelector('#prestudy').textContent,
    hint: !!document.querySelector('#prestudy .pilots-hint') }));
  console.log(JSON.stringify({ ...r, errors }));
  await b.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""


def test_admin_prestudy_shows_no_expected_count_even_from_an_old_preplan(tmp_path):
    shutil.copytree(REAL_CLIENTS / "brioche-four", tmp_path / "clients" / "brioche-four")
    d = tmp_path / "clients" / "brioche-four"
    meta = {k: RESULT[k] for k in ("summary", "use_cases", "seed_questions", "sources", "prestudy")}
    meta["prestudy"] = {**meta["prestudy"], "workforce": "mature", "workforce_note": "rapport annuel (fictif)"}
    (d / "preplan.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    (d / "preplan.md").write_text("# Pré-plan\n", encoding="utf-8")
    info = yaml.safe_load((d / "client.yaml").read_text(encoding="utf-8")) or {}
    (d / "client.yaml").write_text(yaml.safe_dump({**info, "headcount": "800"}, allow_unicode=True), encoding="utf-8")
    with serve(create_admin_app(tmp_path / "clients"), ADMIN_PORT) as url:
        r = run_node(tmp_path, ADMIN_SCRIPT, url)
    assert r["errors"] == [] and not r["hint"]
    for leak in ("Workforce", "Pilots to expect", "for 800 people", "rapport annuel"):
        assert leak not in r["text"], leak
