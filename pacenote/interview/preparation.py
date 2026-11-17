"""What an interview starts from for one client: the questionnaire as it will be asked.

The client's seed questionnaire, plus the app's core questions (phases 1-4, `core/questions.yaml`), plus the
pre-plan's seed questions as a theme, plus the questions of the pace in force (`core/horizons.yaml`), and the
background the model reads (client sheet, interviewer notes, profile cards, pre-plan). `load_client` assembles
it into the `ClientConfig` a session runs on.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from pacenote.shared import i18n
from pacenote.clients.sheet import ClientInfo, linked_summary, load_preplan, slug_from_name
from pacenote.research.horizons import effective_posture, posture_theme, preplan_horizon
from pacenote.people.profiles import compact_profile, load_profiles
from pacenote.people.personas import interviewees
from pacenote.interview.questionnaire import ClientConfig, LadderDef, QuestionDef, Questionnaire, Texts, ThemeDef, load_questionnaire

RUNG_KEYS = ("business", "workflow", "implementation")
PREPLAN_THEME = "pistes-ia"
MAX_SEED_QUESTIONS = 4


def seed_theme(preplan: dict, existing_ids: set[str]) -> ThemeDef | None:
    """The pre-plan's seed questions as an interview theme ("Pistes IA identifiées"), each with
    its ladder, so they are dug Business > Workflow > Implementation like any other question."""
    questions: list[QuestionDef] = []
    taken = set(existing_ids)
    for sq in (preplan.get("seed_questions") or [])[:MAX_SEED_QUESTIONS]:
        fr = str(sq.get("fr") or "").strip()
        en = str(sq.get("en") or "").strip() or fr
        if not fr:
            continue
        base = "pp-" + (slug_from_name(str(sq.get("id") or fr))[:30] or "q")
        qid, n = base, 2
        while qid in taken:
            qid, n = f"{base}-{n}", n + 1
        taken.add(qid)
        expect = sq.get("expect") or {}
        ladder = LadderDef(**{r: [str(x).strip() for x in (expect.get(r) or []) if str(x).strip()][:3] for r in RUNG_KEYS})
        try:
            priority = max(1, min(3, int(sq.get("priority") or 2)))
        except (TypeError, ValueError):
            priority = 2
        questions.append(QuestionDef(id=qid, fr=fr, en=en, priority=priority, expect=ladder))
    if not questions:
        return None
    return ThemeDef(id=PREPLAN_THEME, title=Texts({c: i18n.t("admin.preplanTheme", c) for c in i18n.available()}),
                    phase="deep-dives", questions=questions)


CORE_FILE = Path(__file__).parent / "core" / "questions.yaml"


def load_core_themes(path: Path = CORE_FILE) -> list[ThemeDef]:
    """The app's core questions (phases 1-4: who carries the signal, how it travels, where
    knowledge lives, where expertise decides), as themes with their phase."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return [ThemeDef.model_validate(t) for t in data.get("themes") or []]


def merge_core(q: Questionnaire, core: list[ThemeDef] | None = None) -> Questionnaire:
    """Add the core questions to a client questionnaire. A client question listed in a core
    question's `replaces` (e.g. an old `capacity-size`) keeps its own wording but moves to that core
    question's phase, and the core question is dropped; a core question or theme whose id the
    client already uses is dropped too. `core_questions: false` in the questionnaire opts out."""
    if not q.core_questions:
        return q
    core = load_core_themes() if core is None else core
    client_ids = {x.id for t in q.themes for x in t.questions}
    theme_ids = {t.id for t in q.themes}
    moved: dict[str, str] = {}  # client question id -> phase of the core question it stands for
    added: list[ThemeDef] = []
    for ct in core:
        if ct.id in theme_ids:
            continue
        keep = []
        for cq in ct.questions:
            stand_in = [r for r in cq.replaces if r in client_ids]
            if cq.id in client_ids or stand_in:
                for r in stand_in:
                    moved.setdefault(r, ct.phase_of(cq))
                continue
            keep.append(cq)
        if keep:
            added.append(ct.model_copy(update={"questions": keep}))
    themes = [t.model_copy(update={"questions": [x.model_copy(update={"phase": moved[x.id]}) if x.id in moved else x
                                                 for x in t.questions]}) for t in q.themes]
    return q.model_copy(update={"themes": themes + added})


def tailor_questionnaire(q: Questionnaire, info: ClientInfo | None, preplan: dict | None,
                         profiles: dict[str, dict] | None = None) -> Questionnaire:
    """The interview's questionnaire for this client: the seed questionnaire plus the app's core
    questions (phases 1-4, merge_core), plus the pre-plan's seed questions as a theme right after the
    first one (parked in the deep dives), and a richer background for the model (client sheet, interviewer
    notes, pre-plan summary and use cases)."""
    q = merge_core(q)
    themes = list(q.themes)
    use = preplan is not None and (info is None or info.use_preplan)
    if use:
        theme = seed_theme(preplan, {x.id for t in q.themes for x in t.questions})
        if theme is not None and PREPLAN_THEME not in {t.id for t in themes}:
            themes.insert(min(1, len(themes)), theme)
    # the posture: the interviewer's override, else the pre-study's score, else the questionnaire's own
    posture, origin = effective_posture(info.posture_override if info is not None else "", preplan if use else None)
    posture = posture or q.posture
    rationale = q.posture_rationale
    if origin == "prestudy":
        rationale = str(((preplan or {}).get("prestudy") or {}).get("rationale") or "").strip()
    elif origin == "manual":
        rationale = ""
    extra = posture_theme(posture) if q.core_questions else None
    if extra is not None:
        taken = {x.id for t in themes for x in t.questions}
        keep = [x for x in extra.questions if x.id not in taken]
        if keep and extra.id not in {t.id for t in themes}:
            themes.append(extra.model_copy(update={"questions": keep}))
    return q.model_copy(update={"themes": themes, "posture": posture, "posture_rationale": rationale,
                                "context": client_context(q.context, info, preplan if use else None, profiles)})


# what create_client wrote as `context:` before 2026-11-17: a placeholder, never background
TEMPLATE_CONTEXT = "One paragraph of background for the model (business, size, why we are here)."


def client_context(base: str, info: ClientInfo | None, preplan: dict | None,
                   profiles: dict[str, dict] | None = None) -> str:
    """profiles: the interviewee profile cards by contact name (people.load_profiles), given to
    the model in a compact form."""
    base = "" if base.strip() == TEMPLATE_CONTEXT else base  # an untouched scaffold: no background
    parts = [base.strip()] if base.strip() else []
    if info is not None:
        sheet = [f"{label}: {value}" for label, value in (
            ("Registered company", info.legal_name), ("Registration number", info.registration_number),
            ("Registered address", info.registered_address), ("Sector", info.sector), ("Website", info.website), ("Country", info.country),
            ("Head office", info.hq_city), ("Headcount", info.headcount), ("About", info.description),
            ("Products and brands", info.products), ("Leadership", info.leadership),
            ("Working languages", info.working_languages)) if value]
        linked = linked_summary(info)
        if linked:
            sheet.append(f"Linked brands and entities: {linked}")
        people = "; ".join(f"{c.name} ({c.role})" if c.role else (c.name or c.role) for c in info.contacts if c.name or c.role)
        if people:
            sheet.append(f"People we meet: {people}")
        if sheet:
            parts.append("Client sheet. " + ". ".join(sheet) + ".")
        if profiles:
            cards = [compact_profile(p) for p in profiles.values()]
            cards = [c for c in cards if c]
            if cards:
                parts.append("Who we meet (public professional profiles, from desk research):\n" + "\n".join(cards))
        if info.notes.strip():
            parts.append("interviewer notes (what we want to talk about):\n" + info.notes.strip())
    if preplan:
        summary = str(preplan.get("summary") or "").strip()
        cases = preplan.get("use_cases") or []
        lines = [f"- {c.get('title')} (value {c.get('value')}/5, feasibility {c.get('feasibility')}/5, "
                 f"{'quick win' if c.get('kind') == 'quick_win' else 'bigger bet'}, {preplan_horizon(c)})"
                 for c in cases[:6] if c.get("title")]
        if summary or lines:
            parts.append("Pre-plan from desk research (hypotheses to confirm in the interview, not facts):\n"
                         + (summary + "\n" if summary else "") + "\n".join(lines))
    return "\n\n".join(parts)


def load_client(clients_dir: Path, slug: str, use_preplan: bool = True) -> ClientConfig:
    """use_preplan=False ignores clients/<slug>/preplan.json (local, untracked desk research): the
    questionnaire exactly as written plus the client sheet."""
    cdir = Path(clients_dir) / slug
    qpath = cdir / "questionnaire.yaml"
    if not qpath.exists():
        raise FileNotFoundError(f"no questionnaire at {qpath}")
    gpath = cdir / "glossary.md"
    glossary = gpath.read_text(encoding="utf-8") if gpath.exists() else ""
    base = load_questionnaire(qpath)
    info = None
    if (cdir / "client.yaml").exists():
        info = ClientInfo.model_validate(yaml.safe_load((cdir / "client.yaml").read_text(encoding="utf-8")) or {})
    preplan = load_preplan(cdir) if use_preplan else None
    tailored = tailor_questionnaire(base, info, preplan, load_profiles(cdir, info) if info is not None else None)
    return ClientConfig(dir=cdir, questionnaire=tailored, glossary=glossary, base_questionnaire=base,
                        interviewees=interviewees(info, preplan) if info is not None else [])
