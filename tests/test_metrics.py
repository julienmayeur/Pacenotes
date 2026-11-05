"""Metric monitoring: metrics.yaml seeded from the plan, dated measurements (CLI, CSV, admin), the
trend against baseline and target, and the optimistic lock that refuses stale writes."""

from __future__ import annotations

import json
import shutil

import pytest
import yaml
from fastapi.testclient import TestClient

from pacenote import metrics as M
from pacenote.admin import create_admin_app
from pacenote.cli import main

from .conftest import REAL_CLIENTS, plan_doc, plan_use_case


def measure(value="", evidence="", status=None, when=""):
    return {"value": value, "date": when, "status": status or ("stated" if value else "to_confirm"),
            "evidence": evidence, "question": "" if value else "Combien ?", "ask": ""}


def plan_metric(mid="order-entry-minutes", use_case="bakery-orders", baseline=None, target=None):
    return {"id": mid, "use_case": use_case, "name": "Temps de saisie d'une commande", "unit": "minutes",
            "how_measured": "chronométré sur un échantillon", "cadence": "hebdomadaire", "owner": "Claire",
            "baseline": baseline or measure("12 minutes par commande", "douze minutes", when="2026-11-04"),
            "target": target or measure("moins de 2 minutes", "", status="partial")}


def write_plan(sessions, session_id, *metrics, recipe="customer-order-intake"):
    d = sessions / "brioche-four" / session_id
    d.mkdir(parents=True)
    u = plan_use_case()
    u["recipe"] = {"id": recipe, "fit": "strong", "gaps": []} if recipe else None
    (d / "plan.json").write_text(json.dumps(plan_doc(u, metrics=list(metrics))), encoding="utf-8")
    return d


@pytest.fixture
def dirs(tmp_path):
    clients, sessions = tmp_path / "clients", tmp_path / "sessions"
    shutil.copytree(REAL_CLIENTS / "brioche-four", clients / "brioche-four")
    return clients, sessions


@pytest.fixture
def seeded(dirs):
    clients, sessions = dirs
    write_plan(sessions, "20261104-100000", plan_metric(), plan_metric("same-day-rate", baseline=measure(),
                                                                       target=measure("95 %", "95 %")))
    M.seed(clients, sessions, "brioche-four")
    return clients, sessions


def test_seed_from_the_latest_plan_with_metrics(dirs):
    clients, sessions = dirs
    write_plan(sessions, "20261104-100000", plan_metric())
    (sessions / "brioche-four" / "20261105-090000").mkdir()  # a newer session without a plan
    old = sessions / "brioche-four" / "20261106-090000"  # a newer plan written before metrics existed
    old.mkdir()
    (old / "plan.json").write_text(json.dumps({"context": "", "use_cases": [], "open_items": []}))
    f, added, refreshed = M.seed(clients, sessions, "brioche-four")
    assert added == ["order-entry-minutes"] and refreshed == [] and f.version == 1
    assert f.seeded_from.endswith("20261104-100000/plan.json")
    m = f.metrics[0]
    assert (m.use_case, m.recipe, m.unit, m.owner) == ("bakery-orders", "customer-order-intake", "minutes", "Claire")
    assert (m.baseline.value, m.baseline.text, m.baseline.date, m.baseline.status) == (
        12.0, "12 minutes par commande", "2026-11-04", "stated")
    assert (m.target.value, m.target.status) == (2.0, "partial")
    on_disk = yaml.safe_load(M.metrics_path(clients, "brioche-four").read_text())
    assert on_disk["version"] == 1 and on_disk["metrics"][0]["measurements"] == []


def test_reseed_keeps_measurements_and_numbers_the_plan_no_longer_gives(seeded):
    clients, sessions = seeded
    M.add_measurement(clients, "brioche-four", "order-entry-minutes", 9, when="2026-11-12")
    write_plan(sessions, "20261120-100000", plan_metric(baseline=measure(), target=measure("1 minute", "une minute")),
               plan_metric("errors") | {"name": "Erreurs"}, recipe=None)
    f, added, refreshed = M.seed(clients, sessions, "brioche-four")
    assert added == ["errors"] and refreshed == ["order-entry-minutes"]
    m = f.get("order-entry-minutes")
    assert [x.value for x in m.measurements] == [9.0]  # never lost
    assert m.baseline.value == 12.0  # the newer plan has no baseline number: the old one is kept
    assert m.target.value == 1.0 and m.target.status == "stated"
    assert m.recipe == "customer-order-intake"  # a plan without a recipe does not erase it
    assert [x.id for x in f.metrics] == ["order-entry-minutes", "same-day-rate", "errors"]
    f2, _, _ = M.seed(clients, sessions, "brioche-four", session="20261104-100000")  # an explicit session
    assert f2.seeded_from.endswith("20261104-100000/plan.json") and f2.version == f.version + 1


def test_seed_without_a_plan_explains(dirs):
    clients, sessions = dirs
    with pytest.raises(M.MetricsError, match="no plan with metrics"):
        M.seed(clients, sessions, "brioche-four")
    with pytest.raises(M.MetricsError, match="no plan.json"):
        M.seed(clients, sessions, "brioche-four", session="nope")


def test_add_measurement_bumps_the_version_and_validates(seeded):
    clients, _ = seeded
    f = M.add_measurement(clients, "brioche-four", "order-entry-minutes", "6,5", when="2026-11-18", note="semaine 1")
    assert f.version == 2 and f.get("order-entry-minutes").measurements[0].model_dump() == {
        "date": "2026-11-18", "value": 6.5, "source": "manual", "note": "semaine 1"}
    for kwargs, message in (({"metric_id": "nope", "value": 1}, "unknown metric"),
                            ({"metric_id": "order-entry-minutes", "value": "beaucoup"}, "not a number"),
                            ({"metric_id": "order-entry-minutes", "value": 1, "when": "18/11/2026"}, "date"),
                            ({"metric_id": "order-entry-minutes", "value": 1, "source": "Mon ERP!"}, "source"),
                            ({"metric_id": "order-entry-minutes", "value": float("nan")}, "finite")):
        with pytest.raises(M.MetricsError, match=message):
            M.add_measurement(clients, "brioche-four", **kwargs)
    assert M.load(clients, "brioche-four").version == 2  # nothing written by the failures


def test_a_stale_write_is_refused_and_changes_nothing(seeded):
    clients, _ = seeded
    loaded = M.load(clients, "brioche-four")  # e.g. the admin page rendered this version
    M.add_measurement(clients, "brioche-four", "order-entry-minutes", 8, when="2026-11-12")  # the CLI writes meanwhile
    before = M.metrics_path(clients, "brioche-four").read_text()
    with pytest.raises(M.StaleWrite, match="changed since it was loaded"):
        M.add_measurement(clients, "brioche-four", "order-entry-minutes", 7, expected_version=loaded.version)
    assert M.metrics_path(clients, "brioche-four").read_text() == before
    M.add_measurement(clients, "brioche-four", "order-entry-minutes", 7, expected_version=loaded.version + 1)  # fresh: ok


def test_csv_import_checks_every_row_first_and_skips_duplicates(seeded, tmp_path):
    clients, _ = seeded
    good = tmp_path / "good.csv"
    good.write_text("metric_id,date,value,source,note\n"
                    "order-entry-minutes,2026-11-12,9,erp,\n"
                    "order-entry-minutes,2026-11-19,\"4,5\",erp,après la semaine 2\n"
                    "same-day-rate,2026-11-19,88,,\n", encoding="utf-8")
    f, added, skipped = M.import_csv(clients, "brioche-four", good)
    assert (added, skipped) == (3, 0)
    assert [(x.value, x.source) for x in f.get("order-entry-minutes").series()] == [(9.0, "erp"), (4.5, "erp")]
    assert f.get("same-day-rate").measurements[0].source == "csv"  # default source
    assert M.import_csv(clients, "brioche-four", good)[1:] == (0, 3)  # re-import is harmless
    bad = tmp_path / "bad.csv"
    bad.write_text("metric_id,value,date\norder-entry-minutes,3,2026-11-26\nnope,1,\norder-entry-minutes,x,\n",
                   encoding="utf-8")
    version = M.load(clients, "brioche-four").version
    with pytest.raises(M.MetricsError) as exc:
        M.import_csv(clients, "brioche-four", bad)
    assert "line 3: unknown metric 'nope'" in str(exc.value) and "line 4: not a number" in str(exc.value)
    assert M.load(clients, "brioche-four").version == version  # one bad row writes nothing
    (tmp_path / "cols.csv").write_text("metric,value\n", encoding="utf-8")
    with pytest.raises(M.MetricsError, match="missing column"):
        M.import_csv(clients, "brioche-four", tmp_path / "cols.csv")


def test_summary_reads_the_trend_against_baseline_and_target():
    m = M.TrackedMetric(id="t", name="Temps", baseline=M.Point(value=12), target=M.Point(value=2))
    assert M.summary(m)["state"] == "no_data"
    m.measurements = [M.Measurement(date="2026-11-12", value=10), M.Measurement(date="2026-11-19", value=7)]
    s = M.summary(m)
    assert (s["direction"], s["progress"], s["state"], s["last"], s["delta"]) == ("down", 0.5, "on_track", 7.0, -3.0)
    m.measurements.append(M.Measurement(date="2026-11-26", value=1.5))
    assert M.summary(m)["state"] == "reached"
    up = M.TrackedMetric(id="u", name="Taux", baseline=M.Point(value=70), target=M.Point(value=95),
                         measurements=[M.Measurement(date="2026-11-12", value=65)])
    assert M.summary(up)["direction"] == "up" and M.summary(up)["state"] == "off_track"
    assert M.summary(M.TrackedMetric(id="v", name="x", measurements=up.measurements))["state"] == "no_target"


def test_chart_is_inline_svg_with_baseline_and_target_lines():
    m = M.TrackedMetric(id="t", name="Temps", baseline=M.Point(value=12), target=M.Point(value=2),
                        measurements=[M.Measurement(date="2026-11-19", value=7), M.Measurement(date="2026-11-12", value=10)])
    svg = M.chart_svg(m)
    assert svg.startswith("<svg") and svg.endswith("</svg>") and "<script" not in svg and "http" not in svg.replace(
        'xmlns="http://www.w3.org/2000/svg"', "")
    assert 'class="baseline"' in svg and 'class="target"' in svg and 'class="series"' in svg
    assert svg.count('class="point"') == 2 and "<title>2026-11-12: 10</title>" in svg
    assert M.chart_svg(M.TrackedMetric(id="e", name="vide")) == ""


def test_parse_number():
    assert M.parse_number("3 h par jour") == 3.0 and M.parse_number("1 200 commandes") == 1200.0
    assert M.parse_number("2,5") == 2.5 and M.parse_number("aucune") is None and M.parse_number(4) == 4.0


def test_cli_seed_add_import_show(dirs, tmp_path, capsys):
    clients, sessions = dirs
    write_plan(sessions, "20261104-100000", plan_metric())
    common = ["--clients-dir", str(clients), "--sessions-dir", str(sessions)]
    assert main(["metrics", "show", "brioche-four", *common]) == 0
    assert "no metrics yet" in capsys.readouterr().out
    assert main(["metrics", "seed", "brioche-four", *common]) == 0
    assert "1 new (order-entry-minutes)" in capsys.readouterr().out
    assert main(["metrics", "add", "brioche-four", "order-entry-minutes", "7", "--date", "2026-11-19", "--note", "s1",
                 *common]) == 0
    csv_file = tmp_path / "m.csv"
    csv_file.write_text("metric_id,date,value\norder-entry-minutes,2026-11-26,4\n", encoding="utf-8")
    assert main(["metrics", "import", "brioche-four", str(csv_file), *common]) == 0
    assert "imported 1 measurement(s)" in capsys.readouterr().out
    assert main(["metrics", "show", "brioche-four", *common]) == 0
    out = capsys.readouterr().out
    assert "order-entry-minutes: Temps de saisie d'une commande [minutes] · recipe customer-order-intake" in out
    assert "baseline 12 (12 minutes par commande) · target 2 (moins de 2 minutes) · last 4 on 2026-11-26" in out
    assert "on track (80 % of the way) · 2 measurement(s)" in out
    assert main(["metrics", "add", "brioche-four", "nope", "1", *common]) == 2
    assert "unknown metric" in capsys.readouterr().err
    assert main(["metrics", "show", "acme", *common]) == 2


def test_admin_metrics_tab_api_with_the_lock_and_the_same_origin_guard(seeded):
    clients, sessions = seeded
    with TestClient(create_admin_app(clients, sessions_dir=sessions), base_url="http://localhost:8766") as http:
        data = http.get("/admin/api/clients/brioche-four/metrics").json()
        assert data["version"] == 1 and [m["id"] for m in data["metrics"]] == ["order-entry-minutes", "same-day-rate"]
        m = data["metrics"][0]
        assert m["summary"]["state"] == "no_data" and m["svg"].startswith("<svg") and m["measurements"] == []
        url = "/admin/api/clients/brioche-four/metrics/order-entry-minutes/measurements"
        same = {"Origin": "http://localhost:8766"}
        r = http.post(url, json={"value": 6.5, "date": "2026-11-19", "note": "semaine 1", "version": 1}, headers=same)
        assert r.status_code == 200 and r.json()["version"] == 2
        assert r.json()["metrics"][0]["measurements"][0] == {"date": "2026-11-19", "value": 6.5, "source": "manual",
                                                              "note": "semaine 1"}
        assert r.json()["metrics"][0]["summary"]["state"] == "on_track"
        # the same page posts again with the version it rendered before: refused, nothing written
        stale = http.post(url, json={"value": 5, "version": 1}, headers=same)
        assert stale.status_code == 409 and "changed since it was loaded" in stale.json()["detail"]
        assert len(M.load(clients, "brioche-four").get("order-entry-minutes").measurements) == 1
        assert http.post("/admin/api/clients/brioche-four/metrics/nope/measurements", json={"value": 1, "version": 2},
                         headers=same).status_code == 404
        assert http.post(url, json={"value": 1, "date": "hier", "version": 2}, headers=same).status_code == 400
        assert http.post(url, json={"version": 2}, headers=same).status_code == 422
        # another web page can never write
        evil = http.post(url, json={"value": 1, "version": 2}, headers={"Origin": "https://evil.example"})
        assert evil.status_code == 403
        assert M.load(clients, "brioche-four").version == 2
        assert http.get("/admin/api/clients/nope/metrics").status_code == 404
