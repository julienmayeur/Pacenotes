"""Client folders: info sheet, scaffold, "prochain client", and the pre-plan feeding the interview.

clients/<slug>/
  questionnaire.yaml   the seed questionnaire (themes, questions, ladder `expect`, consent)
  glossary.md          known mishearings
  client.yaml          the client sheet edited in /admin: name, website, sector, country, language,
                       the registered company (legal name, registration number, registered address),
                       company facts (HQ city, headcount, description, products, leadership,
                       LinkedIn, working languages), linked brands / entities, and per-field
                       provenance (agent or manual), all filled by "Auto-fill" (pacenote/autofill.py);
                       who we meet, the FDE's notes, whether the pre-plan feeds the interview
  people/<contact>.json  the interviewee profile cards (pacenote/people.py), regenerated, not in git
  research.md          research with sources            } written by "Lancer la recherche"
  preplan.md           the pre-plan (editable)          } (pacenote/research.py); previous
  preplan.json         use cases + seed questions       } versions go to history/<timestamp>/
  history/<timestamp>/ previous research / pre-plan versions

The next client is stored outside the repo, in ~/.pacenote/config.json (override with
PACENOTE_CONFIG), so it survives pulls and is not committed.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from datetime import datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import fsutil, i18n

from .questionnaire import LadderDef, QuestionDef, Questionnaire, Texts, ThemeDef, valid_slug

RUNG_KEYS = ("business", "workflow", "implementation")
PREPLAN_THEME = "pistes-ia"
MAX_SEED_QUESTIONS = 4


class ClientError(ValueError):
    pass


# --- the client sheet --------------------------------------------------------------------------


class Contact(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = ""
    role: str = ""


class LinkedEntity(BaseModel):
    """A brand or entity linked to the registered company (a distributed brand, the parent, a
    subsidiary, the brand's entity in this country…), with where it was found."""
    model_config = ConfigDict(extra="ignore")
    name: str = ""
    relation: str = "other"  # see autofill.RELATIONS
    website: str = ""
    linkedin_url: str = ""
    source_url: str = ""
    confidence: float | None = None
    by: str = "agent"  # "agent" (filled by Auto-fill) or "manual" (edited by the FDE: never overwritten)


class ClientInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    website: str = ""
    sector: str = ""
    country: str = ""
    language: str = "fr"  # interview language: fr, en, de, es… (see i18n.py)
    # the registered company (legal entity), all optional (older client.yaml files load unchanged)
    legal_name: str = ""
    registration_number: str = ""  # SIREN/SIRET, Companies House number, HRB… with its kind
    registered_address: str = ""
    # company facts, all optional; "Auto-fill" fills them
    hq_city: str = ""
    headcount: str = ""
    description: str = ""
    products: str = ""  # key products / brands, comma-separated
    leadership: str = ""  # public executives: "Name — role; Name — role"
    linkedin_url: str = ""
    working_languages: str = ""  # languages the company works in (≠ `language`, the interview's)
    linked_entities: list[LinkedEntity] = Field(default_factory=list)
    # per company field: {"by": "agent"|"manual", "source_url", "confidence"}. A field with a value
    # and no entry counts as manual: Auto-fill never overwrites it.
    provenance: dict[str, dict] = Field(default_factory=dict)
    removed_entities: list[str] = Field(default_factory=list)  # linked entities the FDE removed (keys)
    contacts: list[Contact] = Field(default_factory=list)
    notes: str = ""  # what we will talk about: context, goals, topics, constraints
    use_preplan: bool = True  # seed the interview with the pre-plan's questions
    updated_at: str | None = None

    @field_validator("language")
    @classmethod
    def _language(cls, v: str) -> str:
        try:
            return i18n.normalize_interview(v)
        except i18n.LanguageError as exc:
            raise ValueError(str(exc)) from exc


def client_dir(clients_dir: Path, slug: str) -> Path:
    if not valid_slug(slug):
        raise ClientError(f"invalid client id {slug!r} (lowercase letters, digits and dashes)")
    return Path(clients_dir) / slug


def list_slugs(clients_dir: Path) -> list[str]:
    root = Path(clients_dir)
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if (p / "questionnaire.yaml").exists() and valid_slug(p.name))


def load_info(clients_dir: Path, slug: str) -> ClientInfo:
    d = client_dir(clients_dir, slug)
    path = d / "client.yaml"
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return ClientInfo.model_validate(data)
    # no sheet yet: derive the basics from the questionnaire
    q = yaml.safe_load((d / "questionnaire.yaml").read_text(encoding="utf-8")) or {}
    return ClientInfo(name=q.get("client", slug), language=q.get("language", "fr"))


def save_info(clients_dir: Path, slug: str, info: ClientInfo) -> ClientInfo:
    d = client_dir(clients_dir, slug)
    if not (d / "questionnaire.yaml").exists():
        raise ClientError(f"unknown client {slug!r}")
    info = info.model_copy(update={"name": info.name.strip() or slug,
                                   "contacts": [c for c in info.contacts if c.name.strip() or c.role.strip()],
                                   "updated_at": datetime.now().isoformat(timespec="seconds")})
    fsutil.atomic_write_text(
        d / "client.yaml",
        "# Client sheet, edited in /admin. Free to edit by hand too.\n"
        + yaml.safe_dump(info.model_dump(), allow_unicode=True, sort_keys=False, width=100),
    )
    _sync_questionnaire_header(d / "questionnaire.yaml", info)
    return info


def _sync_questionnaire_header(path: Path, info: ClientInfo) -> None:
    """Keep questionnaire.yaml's `client:` and `language:` in line with the sheet, editing those
    two lines in place so the file's comments and layout survive."""
    text = path.read_text(encoding="utf-8")
    current = yaml.safe_load(text) or {}
    new = text
    if current.get("client") != info.name:
        new = re.sub(r"(?m)^client:.*$", lambda m: f"client: {json.dumps(info.name, ensure_ascii=False)}", new, count=1)
    if current.get("language") != info.language:
        new = re.sub(r"(?m)^language:\s*\w+", f"language: {info.language}", new, count=1)
    if new != text:
        path.write_text(new, encoding="utf-8")


def slug_from_name(name: str) -> str:
    s = unicodedata.normalize("NFKD", name.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:40] or "client"


# --- scaffold ----------------------------------------------------------------------------------

TEMPLATE_Q = """\
# Seed questionnaire for {name_plain}. Edit freely; see clients.example/brioche-four/questionnaire.yaml for a
# complete example and the writing rules. Start from capacity (what stops them from taking more
# customers, orders or a new market, what eats the team's time), ask for a real case from last
# week (who, which software or file, how often, how long, where the information comes from and
# goes), and never use AI vocabulary in a question. Each question is dug down a ladder
# (business -> workflow -> implementation): `expect` lists the concrete details each rung needs
# ("Ce qui manque" on screen) and `slots` the integration slots its answer feeds. The interview
# runs in phases (`phase:`): this file holds the opening (wealth), the walkthroughs (deep-dives) and
# the return; the four phases in between ship with the app and are merged in.
client: {name}
slug: {slug}
language: {language}          # interview language (questions, follow-ups, plan): fr, en, de, es…
speech_locales: {speech}   # speech locales listening to the room, primary first
target_minutes: 15
consent:
  fr: >-
    Avec votre accord, cet échange est transcrit en direct sur cet ordinateur pour en tirer
    un plan d'action. L'audio n'est jamais enregistré ni envoyé ; seul le texte est analysé.
  en: >-
    With your consent, this conversation is transcribed live on this computer to build an
    action plan. Audio is never recorded or sent anywhere; only the text is analysed.
context: >-
  One paragraph of background for the model (business, size, why we are here).
themes:
  - id: capacity
    phase: wealth
    title: {{fr: Capacité & croissance, en: Capacity & growth}}
    questions:
      - id: capacity-limit
        priority: 1
        slots: [baseline, target, actors, processing, tool, volume]
        fr: "Si demain vous aviez deux fois plus de clients ou de commandes, qui dans l'équipe serait débordé en premier, et sur quelle tâche précise ? Qu'est-ce que cette personne fait de ses journées aujourd'hui ?"
        en: "If tomorrow you had twice as many customers or orders, who on the team would be swamped first, and on which exact task? What does that person spend their days on today?"
        expect:
          business: ["la tâche qui coincerait en premier", "le temps qu'elle prend aujourd'hui (heures par semaine)",
                     "ce que vous feriez du temps gagné (clients ou commandes en plus)"]
          workflow: ["qui la fait (nom, poste)", "ce que cette personne fait, sur un cas réel de la semaine dernière"]
          implementation: ["le logiciel ou le fichier utilisé, par son nom", "combien de fois par jour ou par semaine"]
        keywords: [débordé, doubler, coince, embaucher, recruter, capacité, swamped, twice]
      - id: capacity-time
        priority: 1
        slots: [baseline, trigger, actors, distribution, tool, volume]
        fr: "Prenez la semaine dernière : sur quelle tâche répétitive vos équipes ont-elles passé le plus d'heures — ressaisir des commandes, répondre au téléphone, remplir des tableaux Excel, relancer des fournisseurs ? Qui l'a faite, combien d'heures au total, et dans quel logiciel ?"
        en: "Take last week: which repetitive task took your teams the most hours — re-keying orders, answering the phone, filling in Excel sheets, chasing suppliers? Who did it, how many hours in total, and in which software?"
        expect:
          business: ["la tâche qui a pris le plus d'heures", "le nombre d'heures par semaine, et combien de personnes"]
          workflow: ["qui l'a faite (nom, poste)", "d'où arrive l'information (mail, téléphone, portail, fichier)",
                     "où elle repart ensuite (logiciel, mail, tableau, client)"]
          implementation: ["le logiciel ou le fichier, par son nom", "combien de fois par jour ou par semaine"]
        keywords: [temps, heures, ressaisie, manuel, excel, semaine]
      - id: capacity-gain
        priority: 1
        phase: return
        slots: [baseline, target]
        fr: "Si cette tâche prenait deux fois moins de temps, qu'en feriez-vous concrètement : combien de clients ou de commandes en plus, ou quelle embauche évitée ? Et en combien de mois voulez-vous voir la différence ?"
        en: "If that task took half the time, what would you concretely do with it: how many more customers or orders, or which hire avoided? And within how many months do you want to see the difference?"
        expect:
          business: ["ce que le temps gagné permettrait (clients, commandes, embauche évitée)", "en combien de mois"]
          workflow: ["sur quelle tâche, et pour qui, le gain doit se voir"]
          implementation: ["comment vous le verrez (quel chiffre, dans quel outil)"]
        keywords: [moitié, deux fois moins, gain, mois, embauche]
"""

TEMPLATE_G = """\
# Glossary — {name}

Known mishearings from the speech recognisers. Format: what is heard → what was said.

- "5D", "EV", "FD", "effet dé" → **FDE** (forward-deployed engineer)
"""


def create_client(clients_dir: Path, slug: str, name: str, language: str = "fr") -> ClientInfo:
    """Scaffold clients/<slug>/ (questionnaire.yaml, glossary.md, client.yaml)."""
    d = client_dir(clients_dir, slug)
    if d.exists():
        raise ClientError(f"{d} already exists")
    try:
        lang = i18n.normalize_interview(language or "fr")
    except i18n.LanguageError as exc:
        raise ClientError(str(exc)) from exc
    # spoken languages: the interview language first, then English (or French for an English client)
    speech = i18n.normalize_speech([lang, i18n.secondary(lang)])
    name = name.strip() or slug
    d.mkdir(parents=True)
    (d / "questionnaire.yaml").write_text(
        TEMPLATE_Q.format(name=json.dumps(name, ensure_ascii=False), name_plain=name, slug=slug, language=lang,
                          speech="[" + ", ".join(speech) + "]"),
        encoding="utf-8")
    (d / "glossary.md").write_text(TEMPLATE_G.format(name=name), encoding="utf-8")
    return save_info(clients_dir, slug, ClientInfo(name=name, language=lang))


# --- prochain client -------------------------------------------------------------------------------


def config_path() -> Path:
    return Path(os.environ.get("PACENOTE_CONFIG") or Path.home() / ".pacenote" / "config.json")


def _read_config() -> dict:
    p = config_path()
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, json.JSONDecodeError):
        return {}


def get_next_client() -> str | None:
    return _read_config().get("next_client") or None


def set_next_client(slug: str | None) -> None:
    cfg = _read_config()
    if slug:
        cfg["next_client"] = slug
    else:
        cfg.pop("next_client", None)
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def resolve_client(arg: str | None, clients_dir: Path) -> str:
    """--client wins; otherwise the "prochain client"; otherwise a clear error."""
    if arg:
        return arg
    nxt = get_next_client()
    if nxt and (Path(clients_dir) / nxt / "questionnaire.yaml").exists():
        return nxt
    if nxt:
        raise ClientError(f"the next client {nxt!r} has no folder under {clients_dir}; pass --client "
                          "or choose the next client in the admin panel (pacenote admin)")
    raise ClientError("no client given: pass --client <slug>, or mark the next client in the admin panel "
                      "(pacenote admin) or with `pacenote next <slug>`")


# --- the pre-plan feeding the interview ------------------------------------------------------------


def load_preplan(client_path: Path) -> dict | None:
    p = Path(client_path) / "preplan.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    except (OSError, json.JSONDecodeError):
        return None


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


def linked_summary(info: ClientInfo) -> str:
    out = []
    for e in info.linked_entities:
        if not e.name.strip():
            continue
        extra = ", ".join(x for x in (e.relation.replace("_", " ") if e.relation else "", e.website, e.linkedin_url) if x)
        out.append(f"{e.name} ({extra})" if extra else e.name)
    return "; ".join(out)


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
    first one (parked in the deep dives), and a richer background for the model (client sheet, FDE
    notes, pre-plan summary and use cases)."""
    q = merge_core(q)
    themes = list(q.themes)
    use = preplan is not None and (info is None or info.use_preplan)
    if use:
        theme = seed_theme(preplan, {x.id for t in q.themes for x in t.questions})
        if theme is not None and PREPLAN_THEME not in {t.id for t in themes}:
            themes.insert(min(1, len(themes)), theme)
    return q.model_copy(update={"themes": themes,
                                "context": client_context(q.context, info, preplan if use else None, profiles)})


def client_context(base: str, info: ClientInfo | None, preplan: dict | None,
                   profiles: dict[str, dict] | None = None) -> str:
    """profiles: the interviewee profile cards by contact name (people.load_profiles), given to
    the model in a compact form."""
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
        people = "; ".join(f"{c.name} ({c.role})" if c.role else c.name for c in info.contacts if c.name or c.role)
        if people:
            sheet.append(f"People we meet: {people}")
        if sheet:
            parts.append("Client sheet. " + ". ".join(sheet) + ".")
        if profiles:
            from .people import compact_profile  # (avoid an import cycle)

            cards = [compact_profile(p) for p in profiles.values()]
            cards = [c for c in cards if c]
            if cards:
                parts.append("Who we meet (public professional profiles, from desk research):\n" + "\n".join(cards))
        if info.notes.strip():
            parts.append("FDE notes (what we want to talk about):\n" + info.notes.strip())
    if preplan:
        summary = str(preplan.get("summary") or "").strip()
        cases = preplan.get("use_cases") or []
        lines = [f"- {c.get('title')} (value {c.get('value')}/5, feasibility {c.get('feasibility')}/5, "
                 f"{'quick win' if c.get('kind') == 'quick_win' else 'bigger bet'})" for c in cases[:6] if c.get("title")]
        if summary or lines:
            parts.append("Pre-plan from desk research (hypotheses to confirm in the interview, not facts):\n"
                         + (summary + "\n" if summary else "") + "\n".join(lines))
    return "\n\n".join(parts)
