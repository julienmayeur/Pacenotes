"""Plan output: the integration proposal (structured PlanDoc rendered to Markdown), interview log,
plan.md + plan.json (shown and exported to PDF by the app, see planview.py), Wispr Flow import."""

from __future__ import annotations

import html
import re
from datetime import datetime
from pathlib import Path

import markdown as md_lib
from markdown.extensions import Extension
from markdown.treeprocessors import Treeprocessor

from . import briefs, fsutil
from . import recipes as recipe_lib
from .contract import SLOT_KEYS, Fact, Measure, PlanDoc, PlanUseCase, Recipe
from .prompts import state_headings
from .srt import load_srt, parse_srt
from .state import SessionState


def interview_log_md(state: SessionState) -> str:
    lang = state.language
    tr = state.tr()
    h = state_headings(state)
    titles = {t.id: t.title(lang) for t in state.themes}
    lines = [f"## {h['log']}", ""]
    for t in state.themes:
        qs = [q for q in state.questions if q.theme == t.id and q.status != "removed"]
        if not qs:
            continue
        lines.append(f"### {titles[t.id]} — {int(round(state.coverage.get(t.id, 0) * 100))} %")
        lines.append("")
        for q in qs:
            tag = f" _({tr('plan.addedLive')})_" if q.origin == "added" else ""
            lines.append(f"- **{q.text(lang)}**{tag} — _{tr(f'plan.status.{q.status}')}_")
            if q.answer:
                lines.append(f"  - {q.answer}")
        lines.append("")
    removed = [q for q in state.questions if q.status == "removed"]
    if removed:
        lines.append("### " + tr("plan.removedQuestions"))
        lines.append("")
        lines += [f"- {q.text(lang)}" for q in removed]
        lines.append("")
    if state.passages:
        lines.append("### " + tr("plan.cleanedExcerpts"))
        lines.append("")
        who = {"fde": "FDE", "client": "CEO", "unknown": "?"}
        for p in state.passages:
            lines.append(f"- `{p.start}` **{who.get(p.speaker, p.speaker)}** : {p.text}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# --- the integration proposal --------------------------------------------------------------


def clean_fact(f: Fact) -> Fact:
    """What the plan may show for a slot. Only a stated value with a quote stays a fact; a value
    without a quote becomes "partial" (to confirm); "to_confirm" never carries a value."""
    value, evidence, question = f.value.strip(), f.evidence.strip(), f.question.strip()
    status = f.status
    if status in ("stated", "partial") and not value:
        status = "to_confirm"
    if status == "to_confirm":
        value = evidence = ""
    elif status == "stated" and not evidence:
        status = "partial"
    if status == "stated":
        question = ""
    return Fact(status=status, value=value, evidence=evidence, question=question, ask=f.ask.strip())


def _score(n: int) -> int:
    return max(0, min(5, int(n)))


def clean_status(status: str, evidence: str, has_value: bool = True) -> str:
    """The facts-vs-to_confirm rule for map records and measures: "stated" needs a quote."""
    if not has_value:
        return "to_confirm"
    if status == "stated" and not (evidence or "").strip():
        return "partial"
    return status


def clean_measure(m: Measure) -> Measure:
    value = m.value.strip()
    status = clean_status(m.status, m.evidence, bool(value))
    return Measure(value=value if status != "to_confirm" else "", date=m.date.strip(), status=status,
                   evidence=m.evidence.strip() if status != "to_confirm" else "", question=m.question.strip(),
                   ask=m.ask.strip())


def _metric_of(doc: PlanDoc, u: PlanUseCase):
    return next((m for m in doc.metrics if m.id and m.id == u.metric), None) or next(
        (m for m in doc.metrics if m.use_case == u.id), None)


# --- recipes: what the plan reuses from the library (recipes.py) ---------------------------------------


def _library(library: dict | None) -> dict:
    return recipe_lib.try_library() if library is None else library


def recipe_of(u: PlanUseCase, library: dict | None = None) -> Recipe | None:
    """The opportunity's recipe when it names a library recipe with a fit; None = a new candidate."""
    r = u.recipe
    if r is None or r.fit == "none" or r.id not in _library(library):
        return None
    return r


def recipe_rank(u: PlanUseCase, library: dict | None = None) -> int:
    """3 = strong fit on a recipe proven elsewhere (evidence), 2 = strong fit on a seed, 1 = partial,
    0 = no recipe (a new candidate)."""
    library = _library(library)
    r = recipe_of(u, library)
    if r is None:
        return 0
    if r.fit == "strong":
        return 3 if library[r.id].evidence else 2
    return 1


def _fold_num(text: str) -> str:
    return re.sub(r"[^0-9a-z%]+", " ", (text or "").casefold().replace("typical:", "")).strip()


def is_typical(value: str, metric: "recipe_lib.RecipeMetricDef | None") -> bool:
    """A value that is (part of) a recipe's typical range, or labelled typical: never a client fact."""
    v = _fold_num(value)
    if not v:
        return False
    if (value or "").strip().casefold().startswith("typical"):
        return True
    if metric is None:
        return False
    return any(v and (v == t or (len(v) >= 3 and v in t)) for t in
               (_fold_num(metric.typical_baseline), _fold_num(metric.typical_target)) if t)


def apply_recipes(doc: PlanDoc, library: dict | None = None) -> PlanDoc:
    """Recipe-first plan, enforced whatever the model returned:
    - an opportunity naming an unknown recipe (or fit none) becomes a new candidate (recipe null);
    - a recipe-backed opportunity's metric reuses the recipe's definition for any field left empty;
    - a baseline or target that is a recipe's typical range, without a quote from the interview, is
      blanked to "to confirm": typical ranges never become client facts."""
    library = _library(library)
    doc = doc.model_copy(deep=True)
    for u in doc.use_cases:
        if u.recipe is not None and recipe_of(u, library) is None:
            u.recipe = None
        r = recipe_of(u, library)
        metric = _metric_of(doc, u)
        if metric is None:
            continue
        lib_metric = library[r.id].metric(metric.id) if r is not None else None
        if lib_metric is not None:
            for f in ("name", "unit", "how_measured", "cadence", "owner"):
                if not getattr(metric, f).strip():
                    setattr(metric, f, getattr(lib_metric, f))
        defs = [lib_metric] if lib_metric is not None else (library[r.id].metrics if r is not None else [None])
        for which in ("baseline", "target"):
            m = getattr(metric, which)
            quoted = m.status == "stated" and m.evidence.strip()
            if not quoted and any(is_typical(m.value, d) for d in defs):
                setattr(metric, which, Measure(value="", date="", status="to_confirm", evidence="",
                                               question=m.question, ask=m.ask))
    return doc


def ranked(doc: PlanDoc, library: dict | None = None) -> list[PlanUseCase]:
    """Recipe-backed opportunities first (strong fit proven elsewhere, strong, partial, then new
    candidates): reusing what already worked is the surest return. Then back-office, high-volume,
    measurable work (FDE practice: that is where the return is), then impact x feasibility (both
    justified from the interview); ties keep the model's order."""
    library = _library(library)
    order = {id(u): i for i, u in enumerate(doc.use_cases)}

    def tier(u: PlanUseCase) -> int:
        metric = _metric_of(doc, u)
        measurable = metric is not None and clean_measure(metric.baseline).status == "stated"
        volume = clean_fact(u.slots.volume).status == "stated"
        return int(u.back_office) + int(measurable) + int(volume)

    return sorted(doc.use_cases, key=lambda u: (-recipe_rank(u, library), -tier(u),
                                                -_score(u.impact.score) * _score(u.feasibility.score), order[id(u)]))


def _one_line(text: str) -> str:
    return " ".join((text or "").split()).replace("|", "\\|")


def _cell(text: str) -> str:
    return _one_line(text) or "—"


def open_items(doc: PlanDoc, tr, library: dict | None = None) -> list[tuple[str, str, str]]:
    """Every gap of the proposal as (topic, question, who to ask): missing baselines and targets
    first (no number is ever invented), then the slots that are not stated, the model choice when it
    is open, bottlenecks to confirm, then the general open items."""
    rows: list[tuple[str, str, str]] = []
    unknown = tr("plan.askUnknown")
    for m in doc.metrics:
        for which in ("baseline", "target"):
            ms = clean_measure(getattr(m, which))
            if ms.status == "stated":
                continue
            question = ms.question or tr(f"plan.metricQuestion.{which}", name=m.name)
            rows.append((f"{m.name} — {tr(f'plan.metrics.{which}')}", question, ms.ask or unknown))
    for u in ranked(doc, library):
        for k in SLOT_KEYS:
            f = clean_fact(getattr(u.slots, k))
            if f.status == "stated":
                continue
            question = f.question or (tr("plan.confirmStated") if f.status == "partial" and f.value
                                      else tr(f"plan.slotQuestion.{k}"))
            rows.append((f"{u.title} — {tr(f'plan.fields.{k}')}", question, f.ask or unknown))
        if u.model.provider == "to_confirm":
            rows.append((f"{u.title} — {tr('plan.fields.model')}", u.model.reason.strip() or tr("plan.modelQuestion"),
                         unknown))
    for b in doc.bottlenecks:
        if clean_status(b.status, b.evidence) != "stated" and b.question.strip():
            rows.append((f"{tr('plan.headings.bottlenecks')} — {_one_line(b.what)}", b.question.strip(), b.ask.strip() or unknown))
    for o in doc.open_items:
        if o.question.strip():
            rows.append((o.topic.strip() or tr("plan.toConfirm"), o.question.strip(), o.ask.strip() or unknown))
    return rows


# --- the communication map: Mermaid source in plan.md, drawn offline as SVG (md_to_html) ------------


def _fold_key(text: str) -> str:
    return re.sub(r"\W+", " ", (text or "").casefold()).strip()


def _mm(text: str, n: int = 48) -> str:
    """Text inside a Mermaid quoted label."""
    text = " ".join((text or "").split())
    text = text if len(text) <= n else text[: n - 1] + "…"
    return text.replace('"', "#quot;").replace("|", "/")


def comm_map_mermaid(doc: PlanDoc) -> str:
    """People and departments as nodes, flows as edges labelled what · channel · wait. A flow someone
    waits on is a thick arrow (==>), a confidential one dotted (-.->), reporting lines plain (---)."""
    nodes: dict[str, tuple[str, str]] = {}  # key -> (node id, label)

    def node(name: str, role: str = "") -> str | None:
        key = _fold_key(name)
        if not key:
            return None
        if key not in nodes:
            label = f"{name} · {role}" if role and _fold_key(role) != key else name
            nodes[key] = (f"n{len(nodes) + 1}", label)
        return nodes[key][0]

    for p in doc.current_state.people:
        node(p.name or p.role, p.role if p.name else "")
    edges: list[str] = []
    linked: set[tuple[str, str]] = set()
    for f in doc.current_state.flows:
        a, b = node(f.sender), node(f.receiver)
        if not a or not b or a == b:
            continue
        label = " · ".join(x for x in (f.what, f.channel, f"⏳ {f.wait}" if f.wait.strip() else "") if x.strip())
        arrow = "==>" if (f.wait.strip() or f.who_waits.strip()) else "-.->" if f.confidential == "yes" else "-->"
        edges.append(f'  {a} {arrow}|"{_mm(label, 60)}"| {b}' if label else f"  {a} {arrow} {b}")
        linked |= {(a, b), (b, a)}
    for p in doc.current_state.people:
        a = node(p.name or p.role, p.role if p.name else "")
        b = node(p.reports_to) if p.reports_to.strip() else None
        if a and b and a != b and (a, b) not in linked:
            edges.append(f"  {b} --- {a}")
            linked |= {(a, b), (b, a)}
    if not nodes or not edges:
        return ""
    lines = ["flowchart LR"] + [f'  {nid}["{_mm(label)}"]' for nid, label in nodes.values()] + edges
    return "\n".join(lines)


_MM_NODE = re.compile(r'^\s*(\w+)\["(.*)"\]\s*$')
_MM_EDGE = re.compile(r'^\s*(\w+)\s*(==>|-\.->|-->|---)\s*(?:\|"(.*)"\|\s*)?(\w+)\s*$')


def mermaid_svg(source: str) -> str | None:
    """Draw the communication map without any network or JavaScript: the subset comm_map_mermaid
    writes (quoted nodes, labelled edges), nodes on an ellipse, straight arrows. None when the
    source is not that subset (the Mermaid source is then shown as is)."""
    import math

    lines = [x for x in source.strip().splitlines() if x.strip()]
    if not lines or not lines[0].strip().startswith(("flowchart", "graph")):
        return None
    labels: dict[str, str] = {}
    edges: list[tuple[str, str, str, str]] = []
    for line in lines[1:]:
        if m := _MM_NODE.match(line):
            labels[m.group(1)] = m.group(2).replace("#quot;", '"')
        elif m := _MM_EDGE.match(line):
            edges.append((m.group(1), m.group(2), (m.group(3) or "").replace("#quot;", '"'), m.group(4)))
        else:
            return None
    for a, _, _, b in edges:
        labels.setdefault(a, a)
        labels.setdefault(b, b)
    ids = list(labels)
    if not ids:
        return None
    n = len(ids)
    W, H = 760, (340 if n <= 5 else 460)
    cx, cy = W / 2, H / 2
    rx, ry = W / 2 - 110, H / 2 - 48
    pos = {}
    for i, nid in enumerate(ids):
        if n == 1:
            pos[nid] = (cx, cy)
        else:
            ang = math.pi + 2 * math.pi * i / n
            pos[nid] = (cx + rx * math.cos(ang), cy + ry * math.sin(ang))
    size = {nid: (min(190, 7.2 * len(labels[nid][:30]) + 22), 34) for nid in ids}
    esc = html.escape

    def clip(x0, y0, x1, y1, nid):  # where the segment from the node centre leaves its box
        w, h = size[nid]
        dx, dy = x1 - x0, y1 - y0
        if dx == 0 and dy == 0:
            return x0, y0
        t = min((w / 2) / abs(dx) if dx else 1e9, (h / 2) / abs(dy) if dy else 1e9)
        return x0 + dx * t, y0 + dy * t

    pairs = {(a, b) for a, _, _, b in edges}
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="100%" role="img" '
           f'class="comm-map-svg" font-family="-apple-system, Helvetica, Arial, sans-serif" font-size="11">',
           '<defs><marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
           'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#5b6470"/></marker>'
           '<marker id="arrw" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
           'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#b4561f"/></marker></defs>']
    texts = []
    for a, arrow, label, b in edges:
        (xa, ya), (xb, yb) = pos[a], pos[b]
        if (b, a) in pairs:  # both directions: keep the two arrows apart
            dx, dy = xb - xa, yb - ya
            d = math.hypot(dx, dy) or 1
            ox, oy = -dy / d * 7, dx / d * 7
            xa, ya, xb, yb = xa + ox, ya + oy, xb + ox, yb + oy
        x0, y0 = clip(xa, ya, xb, yb, a)
        x1, y1 = clip(xb, yb, xa, ya, b)
        style = {"==>": 'stroke="#b4561f" stroke-width="2.4" marker-end="url(#arrw)"',
                 "-.->": 'stroke="#5b6470" stroke-width="1.3" stroke-dasharray="5 4" marker-end="url(#arr)"',
                 "-->": 'stroke="#5b6470" stroke-width="1.3" marker-end="url(#arr)"',
                 "---": 'stroke="#c9ced6" stroke-width="1.2"'}[arrow]
        out.append(f'<line x1="{x0:.1f}" y1="{y0:.1f}" x2="{x1:.1f}" y2="{y1:.1f}" {style}/>')
        if label:
            short = label if len(label) <= 44 else label[:43] + "…"
            mx, my = (x0 + x1) / 2, (y0 + y1) / 2
            tw = 5.9 * len(short) + 8
            texts.append(f'<rect x="{mx - tw / 2:.1f}" y="{my - 8:.1f}" width="{tw:.1f}" height="15" rx="3" '
                         f'fill="#ffffff" fill-opacity="0.92"/>'
                         f'<text x="{mx:.1f}" y="{my + 3.5:.1f}" text-anchor="middle" fill="#1b1f24">{esc(short)}</text>')
    for nid in ids:
        (x, y), (w, h) = pos[nid], size[nid]
        label = labels[nid] if len(labels[nid]) <= 30 else labels[nid][:29] + "…"
        out.append(f'<rect x="{x - w / 2:.1f}" y="{y - h / 2:.1f}" width="{w:.1f}" height="{h}" rx="8" '
                   f'fill="#f3f5f8" stroke="#9aa3ae"/>'
                   f'<text x="{x:.1f}" y="{y + 4:.1f}" text-anchor="middle" font-size="12" font-weight="600" '
                   f'fill="#1b1f24">{esc(label)}</text>')
    out += texts + ["</svg>"]
    return "".join(out)


# --- the plan --------------------------------------------------------------------------------------


def render_plan_md(doc: PlanDoc, state: SessionState) -> str:
    """The integration proposal: context; the current state (communication map, people, tools and
    where information lives, decisions); where the signal is lost; the metrics; the opportunities
    ranked (back-office, measurable first) and in detail; the engagement timeline; every open item.
    Unknown values are never left empty: each renders as "to confirm" with the question to ask."""
    tr = state.tr()
    h = state_headings(state)
    unknown = tr("plan.askUnknown")

    def fact(f: Fact, k: str) -> str:
        f = clean_fact(f)
        if f.status == "stated":
            return f.value
        question = f.question or (tr("plan.confirmStated") if f.value else tr(f"plan.slotQuestion.{k}"))
        line = tr("plan.confirmLine", question=question, ask=f.ask or unknown)
        return f"{f.value} — {line}" if f.value else line

    def field(label_key: str, value: str) -> str:
        return tr("plan.fieldLine", label=tr(f"plan.fields.{label_key}"), value=value)

    def score(sc) -> str:
        return tr("plan.score", score=_score(sc.score)) if _score(sc.score) else tr("plan.toConfirm")

    def mark(status: str, evidence: str, has_value: bool = True) -> str:
        """A record's tag: nothing for a stated fact, « à confirmer » otherwise."""
        return "" if clean_status(status, evidence, has_value) == "stated" else f" _({tr('plan.toConfirm')})_"

    def yes_no(v: str) -> str:
        return tr(f"plan.yesNo.{v}") if v in ("yes", "no") else "—"

    def measure(m: Measure) -> str:
        m = clean_measure(m)
        if m.status == "to_confirm":
            return tr("plan.toConfirm")
        when = f" ({m.date})" if m.date else ""
        return f"{m.value}{when}" + ("" if m.status == "stated" else f" _({tr('plan.toConfirm')})_")

    def where(w: str) -> str:  # know-how kept in someone's memory
        return tr("plan.current.head") if _fold_key(w) in ("head", "tete", "tête", "in someone s head") else w

    def table(headers: list[str], rows: list[list[str]]) -> list[str]:
        return ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)] + [
            "| " + " | ".join(_cell(c) for c in r) + " |" for r in rows]

    def recipe_line(u: PlanUseCase) -> str:
        r = recipe_of(u, library)
        if r is None:
            return tr("plan.recipe.new")
        lib = library[r.id]
        status = tr("plan.recipe.proven", n=len(lib.evidence)) if lib.evidence else tr("plan.recipe.seed")
        line = tr("plan.recipe.line", title=lib.title_in(state.language), fit=tr(f"plan.recipe.fit.{r.fit}"),
                  status=status)
        gaps = [g.strip() for g in r.gaps if g.strip()]
        return line + (f" — {tr('plan.recipe.gaps', gaps='; '.join(gaps))}" if gaps else "")

    def recipe_cell(u: PlanUseCase) -> str:
        r = recipe_of(u, library)
        return (f"{library[r.id].title_in(state.language)} ({tr(f'plan.recipe.fit.{r.fit}')})" if r is not None
                else tr("plan.recipe.new"))

    library = _library(None)
    cs = doc.current_state
    cases = ranked(doc, library)
    metric_by_case = {u.id: _metric_of(doc, u) for u in cases}
    out = [f"# {h['title']} — {state.client}", "", f"## {h['context']}", "",
           doc.context.strip() or f"_{tr('plan.toComplete')}_", ""]

    # 1. current state: who carries the signal, how it travels, where it lives, who decides
    out += [f"## {h['current_state']}", ""]
    source = comm_map_mermaid(doc)
    if source:
        out += [f"**{tr('plan.current.map')}**", "", tr("plan.current.mapLegend"), "", "```mermaid", source, "```", ""]
    if cs.people:
        out += [f"**{tr('plan.current.people')}**", ""] + table(
            [tr("plan.current.person"), tr("plan.current.role"), tr("plan.current.department"),
             tr("plan.current.reportsTo"), tr("plan.current.timeSplit")],
            [[p.name + mark(p.status, p.evidence, bool(p.name or p.role)), p.role, p.department, p.reports_to,
              p.time_split] for p in cs.people]) + [""]
    if cs.flows:
        out += [f"**{tr('plan.current.flows')}**", ""] + table(
            [tr("plan.current.from"), tr("plan.current.to"), tr("plan.current.what"), tr("plan.current.channel"),
             tr("plan.current.cadence"), tr("plan.current.wait"), tr("plan.current.confidential")],
            [[f.sender, f.receiver, f.what + mark(f.status, f.evidence), f.channel, f.cadence,
              " — ".join(x for x in (f.wait, f.who_waits) if x.strip()), yes_no(f.confidential)] for f in cs.flows]) + [""]
    if cs.systems or cs.stores:
        rows = [[tr(f"plan.catalog.{x.category}"), x.tool + mark(x.status, x.evidence, bool(x.tool)), x.used_by,
                 x.used_for, yes_no(x.source_of_truth), x.access_owner] for x in cs.systems]
        rows += [[tr("plan.current.store"), f"{x.what} — {where(x.where)}" + mark(x.status, x.evidence, bool(x.what)), x.owner,
                  " — ".join(y for y in (x.how_found, x.reuse_pain) if y.strip()), "—", "—"] for x in cs.stores]
        out += [f"**{tr('plan.current.tools')}**", ""] + table(
            [tr("plan.current.category"), tr("plan.current.tool"), tr("plan.current.usedBy"),
             tr("plan.current.usedFor"), tr("plan.current.sourceOfTruth"), tr("plan.current.access")], rows) + [""]
    if cs.decisions:
        out += [f"**{tr('plan.current.decisions')}**", ""] + table(
            [tr("plan.current.decision"), tr("plan.current.owner"), tr("plan.current.frequency"),
             tr("plan.current.inputs"), tr("plan.current.prep"), tr("plan.current.decide")],
            [[d.decision + mark(d.status, d.evidence), d.owner, d.frequency, d.inputs, d.prep_time, d.decide_time]
             for d in cs.decisions]) + [""]
    if not (source or cs.people or cs.flows or cs.systems or cs.stores or cs.decisions):
        out += [f"_{tr('plan.current.none')}_", ""]

    # 2. where the signal is lost
    out += [f"## {h['bottlenecks']}", ""]
    if doc.bottlenecks:
        out += table([tr("plan.bottleneck.kind"), tr("plan.bottleneck.whoWaits"), tr("plan.bottleneck.waitsOn"),
                      tr("plan.bottleneck.what"), tr("plan.bottleneck.howLong"), tr("plan.bottleneck.evidence")],
                     [[tr(f"plan.bottleneckKind.{b.kind}"), b.who_waits, b.waits_on, b.what + mark(b.status, b.evidence),
                       b.how_long, f"« {b.evidence.strip()} »" if b.evidence.strip() else ""] for b in doc.bottlenecks])
    else:
        out.append(f"_{tr('plan.bottleneck.none')}_")
    out.append("")

    # 3. metrics first: what each opportunity will be judged on
    out += [f"## {h['metrics']}", ""]
    if doc.metrics:
        out += table([tr("plan.metrics.name"), tr("plan.metrics.unit"), tr("plan.metrics.howMeasured"),
                      tr("plan.metrics.cadence"), tr("plan.metrics.owner"), tr("plan.metrics.baseline"),
                      tr("plan.metrics.target")],
                     [[m.name, m.unit, m.how_measured, m.cadence, m.owner, measure(m.baseline), measure(m.target)]
                      for m in doc.metrics])
    else:
        out.append(f"_{tr('plan.metrics.none')}_")
    out += ["", f"## {h['use_cases']}", ""]

    # 4. opportunities
    if cases:
        out += [f"| {tr('plan.table.rank')} | {tr('plan.table.useCase')} | {tr('plan.table.pattern')} | "
                f"{tr('plan.table.impact')} | {tr('plan.table.feasibility')} | {tr('plan.table.why')} | "
                f"{tr('plan.table.recipe')} |",
                "|---|---|---|---|---|---|---|"]
        for i, u in enumerate(cases, 1):
            why = tr("plan.scoreWhy", impact=_one_line(u.impact.reason) or tr("plan.toConfirm"),
                     feasibility=_one_line(u.feasibility.reason) or tr("plan.toConfirm"))
            out.append(f"| {i} | {_one_line(u.title)} | {tr(f'plan.patterns.{u.pattern}')} | {score(u.impact)} | "
                       f"{score(u.feasibility)} | {why} | {_one_line(recipe_cell(u))} |")
    else:
        out.append(tr("plan.noUseCases"))
    out += ["", f"## {h['details']}", ""]
    for i, u in enumerate(cases, 1):
        sl = u.slots
        metric = metric_by_case.get(u.id)
        out += [f"### {i}. {u.title.strip()}", "",
                field("pattern", tr(f"plan.patterns.{u.pattern}")), "",
                field("recipe", recipe_line(u)), "",
                field("humanDecision", u.human_decision.strip() or tr("plan.toConfirm")), "",
                field("success", u.success.statement.strip() or tr("plan.toConfirm")), "",
                field("check", u.success.check.strip() or tr("plan.toConfirm")), ""]
        if metric is not None:
            out += [field("metric", tr("plan.metricLine", name=metric.name, baseline=measure(metric.baseline),
                                       target=measure(metric.target))), ""]
        fw = u.first_win
        out += [field("firstWin", tr("plan.firstWinLine", description=fw.description.strip() or tr("plan.toConfirm"),
                                     cases=fw.cases or "?", week=fw.week or "?")), "",
                field("notClaimed", u.not_claimed.strip() or tr("plan.toConfirm")), "",
                field("killCriterion", u.kill_criterion.strip() or tr("plan.toConfirm")), "",
                field("sourcing", tr(f"plan.sourcing.{u.sourcing}") + (f" — {u.sourcing_reason.strip()}"
                                                                         if u.sourcing_reason.strip() else "")), "",
                field("adoption", u.adoption.strip() or tr("plan.toConfirm")), "",
                field("architecture", u.architecture.strip() or tr("plan.toConfirm")), ""]
        if u.steps:
            out += [f"**{tr('plan.fields.steps')}**", ""] + [f"{n}. {x.strip()}" for n, x in enumerate(u.steps, 1)] + [""]
        out += [field("triggerKind", tr(f"plan.triggerKind.{u.trigger_kind}")), ""]
        if u.actions:
            out += [f"**{tr('plan.fields.actions')}**", ""] + [f"- {x.strip()}" for x in u.actions] + [""]
        out += [f"**{tr('plan.fields.dataAccess')}**", "",
                f"- {field('trigger', fact(sl.trigger, 'trigger'))}",
                f"- {field('access', fact(sl.access, 'access'))}",
                f"- {field('tool', fact(sl.tool, 'tool'))}", "",
                field("processing", fact(sl.processing, "processing")), "",
                field("actors", fact(sl.actors, "actors")), "",
                field("distribution", fact(sl.distribution, "distribution")), ""]
        provider = tr(f"plan.providers.{u.model.provider}")
        reason = u.model.reason.strip()
        out += [field("model", f"{provider} — {reason}" if reason else provider), "",
                f"**{tr('plan.fields.result')}**", "",
                f"- {field('baseline', fact(sl.baseline, 'baseline'))}",
                f"- {field('target', fact(sl.target, 'target'))}",
                f"- {field('volume', fact(sl.volume, 'volume'))}", ""]

    # 5. new recipe candidates: drafts for the opportunities no library recipe fits (never auto-added)
    if doc.recipe_candidates:
        out += [f"## {h['recipe_candidates']}", "", f"_{tr('plan.recipe.candidateNote')}_", ""]
        for c in doc.recipe_candidates:
            fl = c.flow
            out += [f"### {c.title.strip() or c.id} (`{c.id}`)", "",
                    field("pattern", tr(f"plan.patterns.{c.type}")), "",
                    field("recipeFlow", " → ".join(x.strip() for x in (fl.trigger, fl.agent_does, fl.writes_to,
                                                                         fl.notifies) if x.strip()) or "—"), "",
                    field("humanDecision", fl.human_decision.strip() or tr("plan.toConfirm")), ""]
            for key, items in (("recipeSignals", c.signals), ("recipePreconditions", c.preconditions),
                               ("recipeMetrics", [f"{m.name} ({m.unit})" if m.unit else m.name for m in c.metrics])):
                items = [x.strip() for x in items if x.strip()]
                if items:
                    out += [f"**{tr(f'plan.fields.{key}')}**", ""] + [f"- {x}" for x in items] + [""]
            out += [field("recipeProof", c.proof.strip() or tr("plan.toConfirm")), "",
                    field("sourcing", tr(f"plan.sourcing.{c.sourcing}")), ""]

    # 6. engagement timeline (week 1 thin slice, week 4 checkpoint, week 8 go / kill)
    steps = [(t.week, t.milestone, t.proof) for t in sorted(doc.timeline, key=lambda t: t.week) if t.milestone.strip()]
    if not steps:
        steps = [(w, tr(f"plan.timeline.default.w{w}"), tr(f"plan.timeline.default.p{w}")) for w in (1, 4, 8)]
    out += [f"## {h['timeline']}", ""] + table(
        [tr("plan.timeline.week"), tr("plan.timeline.milestone"), tr("plan.timeline.proof")],
        [[str(w), m, pr] for w, m, pr in steps]) + [""]

    rows = open_items(doc, tr, library)
    out += [f"## {h['open_items']}", ""]
    if rows:
        out += [f"| {tr('plan.table.topic')} | {tr('plan.table.question')} | {tr('plan.table.ask')} |", "|---|---|---|"]
        out += [f"| {_one_line(a)} | {_one_line(b)} | {_one_line(c)} |" for a, b, c in rows]
    else:
        out.append("—")
    return "\n".join(out).rstrip() + "\n"


def assemble_plan(body_md: str, state: SessionState) -> str:
    body = body_md.strip()
    # Drop any interview-log section the model may have written anyway; ours is authoritative.
    log_heading = re.escape(state_headings(state)["log"])
    body = re.split(rf"(?im)^##\s+{log_heading}\s*$", body)[0].rstrip()
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    tr = state.tr()
    # the capability brief the plan was written with (briefs.py): its date is part of the plan
    footer = f"_{tr('plan.footer', stamp=stamp)} {tr('plan.capabilitiesAsOf', date=briefs.capabilities_as_of())}_"
    return f"{body}\n\n{interview_log_md(state)}\n---\n\n{footer}\n"


_LIST_ITEM = re.compile(r"^(?:[-*+]|\d+[.)])\s")


def loosen_lists(text: str) -> str:
    """Python-Markdown needs a blank line before a list; models often write one straight
    after a paragraph line ("… quatre leviers :\n- a\n- b"), which would render inline."""
    out: list[str] = []
    fence = False
    for line in (text or "").splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
        prev = out[-1] if out else ""
        if (not fence and _LIST_ITEM.match(line) and prev.strip() and not prev[:1].isspace()
                and not _LIST_ITEM.match(prev) and not prev.lstrip().startswith(("|", "#", ">"))):
            out.append("")
        out.append(line)
    return "\n".join(out)


# Links and images keep only these schemes (or a relative / #anchor target).
_SAFE_URL = re.compile(r"^(https?:|mailto:|#|/(?!/)|\./|\.\./|[^:/?#]+(?:[/?#]|$))", re.IGNORECASE)


def safe_url(url: str) -> bool:
    """http(s), mailto, an anchor or a relative path. Never javascript:, data:, vbscript: or //host."""
    u = re.sub(r"[\x00-\x20]", "", html.unescape(url or ""))  # as the browser reads it: &#106;avascript:
    return bool(u) and bool(_SAFE_URL.match(u))


class _DropUnsafeUrls(Treeprocessor):
    def run(self, root):
        for el in root.iter():
            for attr in ("href", "src"):
                if attr in el.attrib and not safe_url(el.attrib[attr]):
                    el.attrib[attr] = "#" if attr == "href" else ""


class _NoRawHtml(Extension):
    """Markdown from web research and from the models is DATA: raw HTML in it is shown as text
    (Python-Markdown keeps it by default), and javascript:/data: links are neutralised. The
    serializer escapes every text node and attribute, so `<script>` / `<img onerror=…>` never
    reach the page as markup."""

    def extendMarkdown(self, md):
        md.preprocessors.deregister("html_block")
        md.inlinePatterns.deregister("html")
        md.treeprocessors.register(_DropUnsafeUrls(md), "drop_unsafe_urls", 0)


_MERMAID_BLOCK = re.compile(r"^```mermaid[ \t]*\n(.*?)\n```[ \t]*$", re.M | re.S)


def md_to_html(text: str) -> str:
    """Markdown to safe HTML. A ```mermaid block (the communication map) is drawn as an inline SVG
    by mermaid_svg (no network, no script, same in the Plan view and the PDF), with its source kept
    under a collapsed "Mermaid" details; a block outside that subset shows as source only."""
    blocks: list[str] = []

    def hold(m: re.Match) -> str:
        blocks.append(m.group(1))
        return f"\nFDEMERMAIDBLOCK{len(blocks) - 1}\n"

    body = md_lib.markdown(loosen_lists(_MERMAID_BLOCK.sub(hold, text or "")),
                           extensions=["tables", "sane_lists", _NoRawHtml()])
    for i, src in enumerate(blocks):
        svg = mermaid_svg(src)
        figure = (f'<figure class="comm-map">{svg or ""}<details class="mermaid-src"{"" if svg else " open"}>'
                  f"<summary>Mermaid</summary><pre><code>{html.escape(src)}</code></pre></details></figure>")
        body = body.replace(f"<p>FDEMERMAIDBLOCK{i}</p>", figure)
    return body


def write_plan_files(session_dir: Path, plan_md: str, state: SessionState, doc: PlanDoc | None = None) -> dict[str, str]:
    """plan.md (and plan.json, the structured proposal) are the plan: the app shows plan.md in its
    Plan view and exports it to PDF on demand (planview.py). No plan.html is written."""
    md_path = session_dir / "plan.md"
    fsutil.atomic_write_text(md_path, plan_md)
    files = {"md": str(md_path)}
    if doc is not None:  # the structured intermediate, same slot schema as the interview
        json_path = session_dir / "plan.json"
        fsutil.atomic_write_text(json_path, doc.model_dump_json(indent=2))
        files["json"] = str(json_path)
    return files


ARCHIVED = ("md", "json", "pdf")


def archive_plan(session_dir: Path) -> list[str]:
    """Keep the current plan.md / plan.json / plan.pdf as plan-vN.* (first free N) before a
    regeneration. Returns the new paths. A plan.html left by an older version is not touched."""
    current = [p for p in (session_dir / f"plan.{ext}" for ext in ARCHIVED) if p.exists()]
    if not current:
        return []
    n = 1
    while any((session_dir / f"plan-v{n}.{ext}").exists() for ext in (*ARCHIVED, "html")):
        n += 1
    moved = []
    for p in current:
        target = session_dir / f"plan-v{n}{p.suffix}"
        p.rename(target)
        moved.append(str(target))
    return moved


def read_wispr(path: Path) -> str:
    """Wispr Flow export: plain text/markdown as-is; SRT is flattened to text."""
    raw = Path(path).read_text(encoding="utf-8", errors="replace")
    if "-->" in raw and parse_srt(raw):
        return "\n".join(c.text for c in load_srt(path))
    return raw.strip()
