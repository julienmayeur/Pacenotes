"""Per-client configuration: seed questionnaire (YAML), glossary.md, consent line."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, RootModel, field_validator, model_validator

from . import i18n
from . import phases as _phases
from .contract import SLOT_KEYS

Lang = str  # any language tag (fr, en, de, es…); validated with i18n.is_language


def _check_texts(v: Any, what: str) -> dict[str, str]:
    if isinstance(v, str):
        raise ValueError(f"{what}: give one entry per language, e.g. {{fr: ..., en: ...}}")
    if not isinstance(v, dict) or not v:
        raise ValueError(f"{what}: needs at least one language variant")
    out = {}
    for k, text in v.items():
        if not i18n.is_language(str(k)):
            raise ValueError(f"{what}: {k!r} is not a language code")
        if not isinstance(text, str):
            raise ValueError(f"{what}: the {k} variant must be text")
        out[i18n.language_of(str(k))] = text
    return out


class Texts(RootModel[dict[str, str]]):
    """One text in several languages: {fr: ..., en: ..., de: ...}. Any language code works;
    missing languages are translated on the fly by Claude (see translate.py)."""

    @model_validator(mode="before")
    @classmethod
    def _langs(cls, v: Any) -> Any:
        return _check_texts(v, "text")

    @property
    def fr(self) -> str:
        return self.root.get("fr", "")

    @property
    def en(self) -> str:
        return self.root.get("en", "")

    def get(self, lang: str, default: str | None = None) -> str:
        for code in i18n.fallback_chain(lang, default):
            if self.root.get(code):
                return self.root[code]
        return next(iter(self.root.values()), "")


Bilingual = Texts  # backwards-compatible name

# An `expect` item: plain text (in the questionnaire language) or {fr: ..., en: ..., de: ...}.
ExpectItem = str | dict[str, str]


class LadderDef(BaseModel):
    """The details a question needs, rung by rung (in the questionnaire language):
    business = the problem or goal; workflow = steps, who does what, handoffs, where it breaks;
    implementation = people and roles, tools (ERP, email, Excel…), data, integrations, volumes."""

    model_config = ConfigDict(extra="forbid")
    business: list[ExpectItem] = Field(default_factory=list)
    workflow: list[ExpectItem] = Field(default_factory=list)
    implementation: list[ExpectItem] = Field(default_factory=list)

    @field_validator("business", "workflow", "implementation")
    @classmethod
    def _items(cls, v: list[ExpectItem]) -> list[ExpectItem]:
        return [x if isinstance(x, str) else _check_texts(x, "expect item") for x in v]


_QUESTION_FIELDS = {"id", "fr", "en", "text", "priority", "expect", "keywords", "use_case", "slots", "phase", "replaces"}


class QuestionDef(BaseModel):
    """Question wording per language: `fr:` / `en:` keys as before, plus any other language as a
    sibling key (`de:`, `es:`…) or a `text: {de: ...}` mapping. At least one is required."""

    model_config = ConfigDict(extra="forbid")
    id: str
    fr: str = ""
    en: str = ""
    text: dict[str, str] = Field(default_factory=dict, description="all language variants, merged")
    priority: int = Field(default=2, description="1 = must-have, 2 = important, 3 = nice-to-have")
    expect: LadderDef = Field(
        default_factory=LadderDef,
        description="Concrete details per rung, shown as 'Ce qui manque' for the current rung.",
    )
    keywords: list[str] = Field(
        default_factory=list,
        description="Only used by the offline (no-API) engine to detect answers.",
    )
    use_case: str | None = Field(
        default=None,
        description="The use case this question drills (id under `use_cases`). Its integration slots become "
                    "the question's 'Ce qui manque'; the answer is sufficient only once they are all filled.",
    )
    slots: list[str] | None = Field(
        default=None,
        description="The integration slots this question gathers. With `use_case`: the slots it requires "
                    "(default: all of them), shown as 'Ce qui manque' and gating 'answered'. Without: the slots "
                    "its answer feeds for whichever workflow the CEO describes (sent to the model; `expect` "
                    "still drives 'Ce qui manque' and sufficiency).",
    )
    phase: str | None = Field(
        default=None,
        description="The interview phase it belongs to (phases.PHASE_IDS); default: its theme's phase, else "
                    "phases.default_phase (questionnaires written before phases load unchanged).",
    )
    replaces: list[str] = Field(
        default_factory=list,
        description="Core questions only: client question ids this one stands for (the client's wording wins).",
    )

    @field_validator("phase")
    @classmethod
    def _phase(cls, v: str | None) -> str | None:
        if v is not None and not _phases.valid(v):
            raise ValueError(f"unknown phase {v!r} (known: {', '.join(_phases.PHASE_IDS)})")
        return v

    @field_validator("slots")
    @classmethod
    def _slots(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        unknown = [x for x in v if x not in SLOT_KEYS]
        if unknown:
            raise ValueError(f"unknown slots {unknown} (known: {', '.join(SLOT_KEYS)})")
        return [k for k in SLOT_KEYS if k in v]  # canonical order

    def required_slots(self) -> list[str]:
        if not self.use_case:
            return []
        return list(self.slots) if self.slots else list(SLOT_KEYS)

    def gathered_slots(self) -> list[str]:
        """The slots this question gathers: the required ones for a use case question, else the
        ones declared under `slots:` (fed to the use case the answer reveals)."""
        return self.required_slots() if self.use_case else list(self.slots or [])

    @model_validator(mode="before")
    @classmethod
    def _gather_languages(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        texts = dict(data.get("text") or {})
        for k in [k for k in data if k not in _QUESTION_FIELDS and i18n.is_language(str(k))]:
            texts[k] = data.pop(k)
        for k in ("fr", "en"):
            if data.get(k):
                texts.setdefault(k, data[k])
        if not texts:
            raise ValueError(f"question {data.get('id')!r}: no wording (give fr:, en:, de:… or text:)")
        texts = _check_texts(texts, f"question {data.get('id')!r}")
        data["text"] = texts
        data["fr"] = texts.get("fr", "")
        data["en"] = texts.get("en", "")
        return data


class UseCaseDef(BaseModel):
    """A workflow we could automate, drilled through the same integration slots as every other
    (see contract.SLOT_KEYS) and written up as one section of the plan."""

    model_config = ConfigDict(extra="forbid")
    id: str
    title: Texts


class ThemeDef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    title: Texts
    phase: str | None = None  # default phase of its questions (see QuestionDef.phase)
    questions: list[QuestionDef]

    @field_validator("phase")
    @classmethod
    def _phase(cls, v: str | None) -> str | None:
        if v is not None and not _phases.valid(v):
            raise ValueError(f"unknown phase {v!r} (known: {', '.join(_phases.PHASE_IDS)})")
        return v

    def phase_of(self, q: QuestionDef) -> str:
        """A question's phase: its own, else the theme's, else the default mapping for old files."""
        return q.phase or self.phase or _phases.default_phase(self.id, q.id, q.use_case)


class Questionnaire(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client: str
    slug: str
    language: Lang = "fr"  # interview language by default (questions, plan); any language code
    ui_language: str | None = None  # FDE screen language by default (else `language`, else English)
    speech_locales: list[str] | None = None  # speech locales, e.g. [de-DE, en-US] (default fr-FR + en-US)
    target_minutes: int = 15
    consent: Texts
    context: str = ""
    use_cases: list[UseCaseDef] = Field(default_factory=list)
    phase_minutes: dict[str, float] | None = None  # minutes per phase (phases.DEFAULT_MINUTES), scaled
    core_questions: bool = True  # merge the app's core questions (phases 1-4) when tailoring
    themes: list[ThemeDef]

    @field_validator("phase_minutes")
    @classmethod
    def _phase_minutes(cls, v: dict[str, float] | None) -> dict[str, float] | None:
        if v is None:
            return None
        unknown = [k for k in v if not _phases.valid(k)]
        if unknown:
            raise ValueError(f"phase_minutes: unknown phases {unknown} (known: {', '.join(_phases.PHASE_IDS)})")
        return {k: max(0.0, float(m)) for k, m in v.items()}

    @field_validator("language")
    @classmethod
    def _language(cls, v: str) -> str:
        return i18n.normalize_interview(v)

    @field_validator("speech_locales")
    @classmethod
    def _speech(cls, v: list[str] | None) -> list[str] | None:
        return i18n.normalize_speech(v) if v else None

    @model_validator(mode="after")
    def _unique_ids(self) -> "Questionnaire":
        theme_ids = [t.id for t in self.themes]
        if len(theme_ids) != len(set(theme_ids)):
            raise ValueError("duplicate theme ids")
        q_ids = [q.id for t in self.themes for q in t.questions]
        dupes = {i for i in q_ids if q_ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate question ids: {sorted(dupes)}")
        uc_ids = [u.id for u in self.use_cases]
        if len(uc_ids) != len(set(uc_ids)):
            raise ValueError("duplicate use case ids")
        for q in (q for t in self.themes for q in t.questions):
            if q.use_case and q.use_case not in uc_ids:
                raise ValueError(f"question {q.id!r}: unknown use case {q.use_case!r}")
        return self


class ClientConfig(BaseModel):
    dir: Path
    questionnaire: Questionnaire  # tailored: seed questionnaire + pre-plan questions + client context
    glossary: str
    base_questionnaire: Questionnaire | None = None  # questionnaire.yaml as written


def load_questionnaire(path: Path) -> Questionnaire:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return Questionnaire.model_validate(data)


def load_client(clients_dir: Path, slug: str, use_preplan: bool = True) -> ClientConfig:
    """use_preplan=False ignores clients/<slug>/preplan.json (local, untracked desk research): the
    questionnaire exactly as written plus the client sheet."""
    cdir = Path(clients_dir) / slug
    qpath = cdir / "questionnaire.yaml"
    if not qpath.exists():
        raise FileNotFoundError(f"no questionnaire at {qpath}")
    gpath = cdir / "glossary.md"
    glossary = gpath.read_text(encoding="utf-8") if gpath.exists() else ""
    from .clients import ClientInfo, load_preplan, tailor_questionnaire  # (avoid an import cycle)

    base = load_questionnaire(qpath)
    info = None
    if (cdir / "client.yaml").exists():
        import yaml as _yaml

        info = ClientInfo.model_validate(_yaml.safe_load((cdir / "client.yaml").read_text(encoding="utf-8")) or {})
    from .people import load_profiles

    tailored = tailor_questionnaire(base, info, load_preplan(cdir) if use_preplan else None,
                                    load_profiles(cdir, info) if info is not None else None)
    return ClientConfig(dir=cdir, questionnaire=tailored, glossary=glossary, base_questionnaire=base)


_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def valid_slug(slug: str) -> bool:
    return bool(_SLUG.match(slug))
