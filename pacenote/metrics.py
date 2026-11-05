"""Metric monitoring per client: the plan's metric definitions and dated measurements, so each
engagement is judged on data (baseline -> latest value -> target), and the results feed the recipe
library (`pacenote recipes record`).

clients/<slug>/metrics.yaml (gitignored with the rest of clients/):

    version: 3                # bumped on every write (optimistic lock, see below)
    client: brioche-four
    seeded_from: sessions/brioche-four/20261104-213810/plan.json
    updated_at: '2026-11-12T09:30:00'
    metrics:
    - id: order-entry-minutes
      use_case: bakery-orders
      recipe: customer-order-intake  # the library recipe its opportunity reuses (null = none)
      name: ... unit: ... how_measured: ... cadence: ... owner: ...
      baseline: {value: 12.0, text: 12 minutes par commande, date: '2026-11-04', status: stated}
      target:   {value: 2.0, text: moins de 2 minutes, date: '', status: partial}
      measurements:
      - {date: '2026-11-18', value: 6.5, source: manual, note: first week in shadow mode}

Concurrency: the file is edited from the CLI and from the admin page. Every write goes through
`update()`: it takes an exclusive per-file lock (fcntl, a sibling `.metrics.yaml.lock`), re-reads
the file, and refuses the write (StaleWrite) when its `version` is not the one the editor loaded;
then it bumps the version and writes atomically (fsutil.atomic_write_text). The admin form sends
the version it rendered, so a measurement added from a stale page is rejected (409) instead of
overwriting a change made meanwhile from the CLI or another tab; the CLI loads and writes within one
command, under the same check.
"""

from __future__ import annotations

import contextlib
import csv
import fcntl
import hashlib
import json
import math
import re
from datetime import date, datetime
from pathlib import Path
from typing import Callable

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from . import fsutil
from .contract import Metric as PlanMetric
from .contract import PlanUseCase
from .finalize import clean_measure

METRICS_FILE = "metrics.yaml"
_SOURCE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,39}$")
_NUMBER = re.compile(r"[-+]?\d+(?:[.,]\d+)?")


class MetricsError(ValueError):
    pass


class StaleWrite(MetricsError):
    """metrics.yaml changed since the editor loaded it."""


class UnknownMetric(MetricsError):
    pass


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _iso(v: str) -> str:
    try:
        date.fromisoformat(v)
    except ValueError:
        raise ValueError(f"date {v!r}: use YYYY-MM-DD") from None
    return v


class Point(_Strict):
    """A baseline or target from the plan: the number (when one was said), the text as said."""

    value: float | None = None
    text: str = ""
    date: str = ""
    status: str = "to_confirm"  # stated | partial | to_confirm (finalize.clean_measure)


class Measurement(_Strict):
    date: str
    value: float
    source: str = "manual"  # manual | csv | the system it was read from (erp, outlook…)
    note: str = ""

    @field_validator("date")
    @classmethod
    def _date(cls, v: str) -> str:
        return _iso(v)

    @field_validator("value")
    @classmethod
    def _finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("not a finite number")
        return v

    @field_validator("source")
    @classmethod
    def _source(cls, v: str) -> str:
        if not _SOURCE.match(v):
            raise ValueError(f"source {v!r}: manual, csv or a short system name (lowercase)")
        return v


class TrackedMetric(_Strict):
    id: str
    use_case: str = ""
    recipe: str | None = None
    name: str
    unit: str = ""
    how_measured: str = ""
    cadence: str = ""
    owner: str = ""
    baseline: Point = Point()
    target: Point = Point()
    measurements: list[Measurement] = []

    def series(self) -> list[Measurement]:
        return sorted(self.measurements, key=lambda m: m.date)


class MetricsFile(_Strict):
    version: int = 0
    client: str
    seeded_from: str = ""
    updated_at: str = ""
    metrics: list[TrackedMetric] = []

    def get(self, metric_id: str) -> TrackedMetric:
        m = next((x for x in self.metrics if x.id == metric_id), None)
        if m is None:
            known = ", ".join(x.id for x in self.metrics) or "none: run `pacenote metrics seed` first"
            raise UnknownMetric(f"unknown metric {metric_id!r} (known: {known})")
        return m


# --- files ------------------------------------------------------------------------------------------


def metrics_path(clients_dir: Path, slug: str) -> Path:
    return Path(clients_dir) / slug / METRICS_FILE


def load(clients_dir: Path, slug: str) -> MetricsFile:
    """The client's metrics (version 0 and no metrics when the file does not exist yet)."""
    path = metrics_path(clients_dir, slug)
    if not path.exists():
        return MetricsFile(client=slug)
    try:
        return MetricsFile.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    except (yaml.YAMLError, ValidationError) as exc:
        raise MetricsError(f"{path}: {exc}") from exc


@contextlib.contextmanager
def _locked(path: Path):
    lock = path.with_name(f".{path.name}.lock")
    with open(lock, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def update(clients_dir: Path, slug: str, change: Callable[[MetricsFile], None],
           expected_version: int | None) -> MetricsFile:
    """Apply `change` to the metrics under the file lock, then write them with version + 1.
    expected_version: the version the editor loaded; another one on disk raises StaleWrite.
    None only for a first write that cannot conflict (seed), never for an edit."""
    path = metrics_path(clients_dir, slug)
    if not path.parent.is_dir():
        raise MetricsError(f"no client folder {path.parent}")
    with _locked(path):
        current = load(clients_dir, slug)
        if expected_version is not None and current.version != expected_version:
            raise StaleWrite(f"metrics.yaml changed since it was loaded (version {expected_version}, now "
                             f"{current.version}): reload and try again")
        new = current.model_copy(deep=True)
        change(new)
        new.version = current.version + 1
        new.updated_at = datetime.now().isoformat(timespec="seconds")
        new = MetricsFile.model_validate(new.model_dump())  # never write an invalid file
        fsutil.atomic_write_text(path, yaml.safe_dump(new.model_dump(), sort_keys=False, allow_unicode=True,
                                                      width=100))
        return new


# --- seed from the plan --------------------------------------------------------------------------------


def parse_number(text: str | float | int | None) -> float | None:
    """The first number in a value as said ("3 h par jour" -> 3.0, "1 200" -> 1200, "2,5" -> 2.5)."""
    if isinstance(text, (int, float)):
        return float(text)
    s = re.sub(r"(?<=\d)[\s  ](?=\d{3}\b)", "", str(text or ""))
    m = _NUMBER.search(s)
    return float(m.group(0).replace(",", ".")) if m else None


def find_plan(sessions_dir: Path, slug: str, session: str | None = None) -> Path:
    """plan.json of the given session, else of the newest session whose plan has metrics."""
    base = Path(sessions_dir) / slug
    if session:
        path = Path(session) / "plan.json" if Path(session).is_dir() else base / session / "plan.json"
        if not path.exists():
            raise MetricsError(f"no plan.json at {path}")
        return path
    for d in sorted((p for p in base.iterdir() if p.is_dir()), reverse=True) if base.is_dir() else []:
        p = d / "plan.json"
        if p.exists():
            try:
                if json.loads(p.read_text(encoding="utf-8")).get("metrics"):
                    return p
            except (OSError, json.JSONDecodeError):
                continue
    raise MetricsError(f"no plan with metrics under {base} (write a plan first: pacenote finalize)")


def _point(measure) -> Point:
    m = clean_measure(measure)
    return Point(value=parse_number(m.value) if m.status != "to_confirm" else None, text=m.value, date=m.date,
                 status=m.status)


def seed(clients_dir: Path, sessions_dir: Path, slug: str, session: str | None = None) -> tuple[MetricsFile, list[str], list[str]]:
    """Metric definitions from the latest plan (or `session`) into metrics.yaml. Measurements are
    always kept; a definition already there is refreshed from the plan, except a baseline or target
    the plan no longer gives a number for. Returns (file, added ids, refreshed ids)."""
    plan_path = find_plan(sessions_dir, slug, session)
    raw = json.loads(plan_path.read_text(encoding="utf-8"))
    recipes = {}
    for u in raw.get("use_cases") or []:
        try:
            uc = PlanUseCase.model_validate(u)
        except ValidationError:
            continue
        if uc.recipe is not None and uc.recipe.fit != "none":
            recipes[uc.id] = uc.recipe.id
    plan_metrics = []
    for m in raw.get("metrics") or []:
        try:
            plan_metrics.append(PlanMetric.model_validate(m))
        except ValidationError as exc:
            raise MetricsError(f"{plan_path}: a metric does not match the plan schema: {exc}") from exc
    if not plan_metrics:
        raise MetricsError(f"{plan_path} has no metrics")
    added, refreshed = [], []

    def change(f: MetricsFile) -> None:
        f.seeded_from = str(plan_path)
        for pm in plan_metrics:
            new = TrackedMetric(id=pm.id, use_case=pm.use_case, recipe=recipes.get(pm.use_case), name=pm.name,
                                unit=pm.unit, how_measured=pm.how_measured, cadence=pm.cadence, owner=pm.owner,
                                baseline=_point(pm.baseline), target=_point(pm.target))
            old = next((x for x in f.metrics if x.id == pm.id), None)
            if old is None:
                f.metrics.append(new)
                added.append(pm.id)
                continue
            for k in ("use_case", "recipe", "name", "unit", "how_measured", "cadence", "owner"):
                setattr(old, k, getattr(new, k) or getattr(old, k))
            for k in ("baseline", "target"):
                if getattr(new, k).status != "to_confirm":
                    setattr(old, k, getattr(new, k))
            refreshed.append(pm.id)

    before = load(clients_dir, slug)
    return update(clients_dir, slug, change, expected_version=before.version), added, refreshed


# --- measurements --------------------------------------------------------------------------------------


def _today() -> str:
    return date.today().isoformat()


def add_measurement(clients_dir: Path, slug: str, metric_id: str, value: float | str, *, when: str | None = None,
                    source: str = "manual", note: str = "", expected_version: int | None = None) -> MetricsFile:
    """Append one dated measurement. expected_version: the version the caller loaded (default: the
    version on disk now, for a one-shot CLI command)."""
    number = parse_number(value) if isinstance(value, str) else float(value)
    if number is None:
        raise MetricsError(f"not a number: {value!r}")
    try:
        point = Measurement(date=when or _today(), value=number, source=source, note=(note or "").strip()[:300])
    except ValidationError as exc:
        raise MetricsError(str(exc.errors()[0]["msg"])) from exc
    if expected_version is None:
        expected_version = load(clients_dir, slug).version
    return update(clients_dir, slug, lambda f: f.get(metric_id).measurements.append(point), expected_version)


def import_csv(clients_dir: Path, slug: str, csv_path: Path) -> tuple[MetricsFile, int, int]:
    """Measurements from a CSV with a header: metric_id, value, and optional date (default today),
    source (default csv), note. Every row is checked first: one bad row writes nothing. A row
    already present (same metric, date, value and source) is skipped, so a re-import is harmless.
    Returns (file, added, skipped)."""
    current = load(clients_dir, slug)
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        missing = {"metric_id", "value"} - set(reader.fieldnames or [])
        if missing:
            raise MetricsError(f"{csv_path}: missing column(s) {', '.join(sorted(missing))}")
        rows, errors = [], []
        for n, row in enumerate(reader, start=2):
            mid = (row.get("metric_id") or "").strip()
            try:
                current.get(mid)
                number = parse_number((row.get("value") or "").strip())
                if number is None:
                    raise MetricsError(f"not a number: {row.get('value')!r}")
                rows.append((mid, Measurement(date=(row.get("date") or "").strip() or _today(), value=number,
                                              source=(row.get("source") or "").strip() or "csv",
                                              note=(row.get("note") or "").strip()[:300])))
            except (MetricsError, ValidationError, ValueError) as exc:
                msg = exc.errors()[0]["msg"] if isinstance(exc, ValidationError) else str(exc)
                errors.append(f"line {n}: {msg}")
    if errors:
        raise MetricsError(f"{csv_path}: nothing imported\n  " + "\n  ".join(errors))
    counts = {"added": 0, "skipped": 0}

    def change(f: MetricsFile) -> None:
        for mid, point in rows:
            m = f.get(mid)
            if any((x.date, x.value, x.source) == (point.date, point.value, point.source) for x in m.measurements):
                counts["skipped"] += 1
                continue
            m.measurements.append(point)
            counts["added"] += 1

    f = update(clients_dir, slug, change, expected_version=current.version)
    return f, counts["added"], counts["skipped"]


# --- reading the trend ---------------------------------------------------------------------------------


def summary(m: TrackedMetric) -> dict:
    """Latest value against baseline and target. direction: "down" when the target is below the
    baseline (less time, fewer errors), "up" otherwise. progress: share of the way from baseline to
    target (1.0 = reached). state: no_data | no_target | reached | on_track | off_track."""
    series = m.series()
    last = series[-1] if series else None
    base, target = m.baseline.value, m.target.value
    out = {"last": last.value if last else None, "last_date": last.date if last else None, "count": len(series),
           "baseline": base, "target": target, "direction": None, "progress": None, "state": "no_data",
           "delta": (series[-1].value - series[-2].value) if len(series) >= 2 else None}
    if last is None:
        return out
    if base is None or target is None or base == target:
        out["state"] = "no_target"
        return out
    down = target < base
    out["direction"] = "down" if down else "up"
    progress = (base - last.value) / (base - target)
    out["progress"] = round(progress, 3)
    out["state"] = "reached" if progress >= 1 else "on_track" if progress > 0 else "off_track"
    return out


def chart_svg(m: TrackedMetric, width: int = 320, height: int = 120) -> str:
    """A small inline SVG: the measurements as a line, the baseline and the target as dashed lines.
    Numbers and dates only (no text to translate, nothing to escape but our own numbers); no script,
    no network. "" when there is nothing to draw."""
    series = m.series()
    levels = [v for v in (m.baseline.value, m.target.value) if v is not None]
    values = [p.value for p in series] + levels
    if not values:
        return ""
    lo, hi = min(values), max(values)
    pad = (hi - lo) * 0.12 or (abs(hi) * 0.1 or 1.0)
    lo, hi = lo - pad, hi + pad
    left, right, top, bottom = 34, 8, 8, 18
    w, h = width - left - right, height - top - bottom

    def y(v: float) -> float:
        return round(top + (hi - v) / (hi - lo) * h, 1)

    days = [date.fromisoformat(p.date).toordinal() for p in series]
    span = (max(days) - min(days)) if days else 0

    def x(i: int) -> float:
        if len(series) == 1 or span == 0:
            return round(left + (w / 2 if len(series) == 1 else i * w / max(1, len(series) - 1)), 1)
        return round(left + (days[i] - min(days)) / span * w, 1)

    def num(v: float) -> str:
        return f"{v:.0f}" if abs(v) >= 100 or float(v).is_integer() else f"{v:.1f}"

    parts = [f'<svg class="metric-chart" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
             f'width="{width}" height="{height}" role="img">']
    for cls, v in (("baseline", m.baseline.value), ("target", m.target.value)):
        if v is not None:
            parts.append(f'<line class="{cls}" x1="{left}" x2="{width - right}" y1="{y(v)}" y2="{y(v)}" '
                         f'stroke-dasharray="4 3"/><text class="{cls}" x="{left - 4}" y="{y(v) + 3}" '
                         f'text-anchor="end">{num(v)}</text>')
    if series:
        pts = " ".join(f"{x(i)},{y(p.value)}" for i, p in enumerate(series))
        if len(series) > 1:
            parts.append(f'<polyline class="series" fill="none" points="{pts}"/>')
        for i, p in enumerate(series):
            parts.append(f'<circle class="point" cx="{x(i)}" cy="{y(p.value)}" r="2.5">'
                         f'<title>{p.date}: {num(p.value)}</title></circle>')
        parts.append(f'<text class="axis" x="{left}" y="{height - 4}">{series[0].date}</text>')
        if len(series) > 1:
            parts.append(f'<text class="axis" x="{width - right}" y="{height - 4}" text-anchor="end">'
                         f'{series[-1].date}</text>')
    parts.append("</svg>")
    return "".join(parts)


def view(f: MetricsFile) -> dict:
    """What the admin Metrics tab shows: the file version (sent back with each new measurement),
    and per metric its definition, measurements (newest first), summary and chart."""
    return {"version": f.version, "seeded_from": f.seeded_from, "updated_at": f.updated_at,
            "metrics": [{**m.model_dump(exclude={"measurements"}),
                         "measurements": [x.model_dump() for x in reversed(m.series())],
                         "summary": summary(m), "svg": chart_svg(m)} for m in f.metrics]}


# --- results back to the recipe library ------------------------------------------------------------------


def anonymised_label(slug: str) -> str:
    """A stable pseudonym for a client in the tracked recipe library (same client -> same label, so
    recording again replaces its entry); `--client-name` keeps the real name instead."""
    return f"client-{hashlib.sha256(slug.encode('utf-8')).hexdigest()[:6]}"


def evidence_for(f: MetricsFile, recipe_id: str, label: str, metric_id: str | None = None) -> list[dict]:
    """One evidence entry per metric tied to the recipe (or `metric_id`): baseline from
    metrics.yaml, the latest measurement as the result, the measurement sources. Raises MetricsError
    when no metric qualifies (no metric for this recipe, no baseline number, no measurement)."""
    chosen = [f.get(metric_id)] if metric_id else [m for m in f.metrics if m.recipe == recipe_id]
    if not chosen:
        raise MetricsError(f"no metric of {f.client} is tied to recipe {recipe_id!r}: pass --metric <id> "
                           f"(metrics: {', '.join(m.id for m in f.metrics) or 'none'})")
    out, problems = [], []
    for m in chosen:
        s = summary(m)
        if m.baseline.value is None:
            problems.append(f"{m.id}: no baseline number (re-seed from a plan that has one)")
            continue
        if s["last"] is None:
            problems.append(f"{m.id}: no measurement yet (pacenote metrics add)")
            continue
        unit = f" {m.unit}" if m.unit else ""
        when = f" ({m.baseline.date})" if m.baseline.date else ""
        series = m.series()
        result = f"{s['last']:g}{unit} ({s['last_date']}), {len(series)} measurement(s) since {series[0].date}"
        if m.target.value is not None:
            result += f"; target {m.target.value:g}{unit}"
        if s["progress"] is not None:
            result += f", {round(s['progress'] * 100)} % of the way"
        out.append({"client": label, "date": _today(), "metric": f"{m.name}{f' ({m.unit})' if m.unit else ''}",
                    "baseline": f"{m.baseline.value:g}{unit}{when}", "result": result,
                    "source": ", ".join(sorted({x.source for x in series}))})
    if not out:
        raise MetricsError("nothing to record: " + "; ".join(problems))
    return out


def show_text(f: MetricsFile) -> str:
    """`pacenote metrics show`: one block per metric."""
    if not f.metrics:
        return f"{f.client}: no metrics yet (pacenote metrics seed {f.client})"

    def val(v: float | None, text: str = "") -> str:
        return "?" if v is None else (f"{v:g}" + (f" ({text})" if text and text != f"{v:g}" else ""))

    lines = [f"{f.client} · metrics.yaml version {f.version}" + (f" · from {f.seeded_from}" if f.seeded_from else "")]
    for m in f.metrics:
        s = summary(m)
        last = f"{s['last']:g} on {s['last_date']}" if s["last"] is not None else "no measurement yet"
        lines += [f"- {m.id}: {m.name}" + (f" [{m.unit}]" if m.unit else "") + (f" · recipe {m.recipe}" if m.recipe else ""),
                  f"    baseline {val(m.baseline.value, m.baseline.text)} · target {val(m.target.value, m.target.text)}"
                  f" · last {last} · {s['state'].replace('_', ' ')}"
                  + (f" ({round(s['progress'] * 100)} % of the way)" if s["progress"] is not None else "")
                  + f" · {s['count']} measurement(s)"]
    return "\n".join(lines)
