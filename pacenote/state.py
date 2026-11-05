"""Interview state and the rules for applying a model result to it.

Focus (the question on screen):
  * `focus_id` is the question being asked. It is LOCKED while it is being answered: no
    model result can replace, reorder, reword or remove it. A rewording proposed for it is
    shown as a follow-up probe under it instead. It only changes when the FDE moves on
    (n / Enter, "answered", "skip"), or after it is answered and the room has been silent
    for a few seconds (decided by the session, which knows about silence).
  * `next_questions` is the upcoming list (focus excluded).

Per question, the interview digs down a ladder: business (the problem or goal) -> workflow
(steps, who does what, handoffs, where it breaks) -> implementation (people and roles, tools,
ERP, email, Excel, data, integrations, volumes) -> done. `expect` lists the concrete details
each rung needs; `covered` = those heard so far (only grows); `rung` only moves down the ladder.
The `thread` under the original question alternates captured answer pieces and ONE follow-up
question at a time: answer piece, follow-up, answer piece, follow-up… A pending follow-up is
never swapped under the FDE; the next one is appended only after the next answer piece.

Locking rules (enforced here, whatever the model returns):
  * "answered" locks a question: status, answer and wording never change again; it cannot be
    removed or moved, and it never reappears in the upcoming list.
  * "answered" == ladder done. A question with `expect` items can only get there once at least
    half of all its expected details are covered; otherwise it stays "partial" (the FDE can
    still mark it, or move on with n / Enter).
  * Use cases and integration slots (contract.SLOT_KEYS): a question attached to a use case gets
    its required slots as `expect` items (labels from the locale files). Those items are covered
    only when the use case's slot holds a value the CEO actually gave (`slots` in a model
    result), never because the model lists them as covered; the question is sufficient (can be
    answered) only when ALL its required slots are filled. Slot values only grow, and they are
    shared by every question on the same use case. A new use case discovered live (an "add" queue
    edit naming an unknown use case) gets its own full slot set.
  * Status only moves forward: pending -> partial -> answered.
  * Queue edits (add / reword / reorder / remove) only target questions that are not yet
    answered and are not the current focus; "remove" only targets *pending* questions.
  * Status updates are applied before queue edits, so an edit that targets a question locked
    in the same sync is rejected.
  * Phases (phases.py): every question belongs to one of seven ordered phases. The focus moves
    phase by phase: a phase ends when its questions are answered or skipped, when its maps hold
    enough facts (once one of its questions was asked), when its time budget is spent, or when the
    FDE skips it (`p`). The phase only changes at a focus change, so the current card is never
    interrupted. Queue edits may only add / reword / reorder / remove questions of the current
    phase; an "add" for a new use case is parked in the deep dives. Answers, slots and maps from
    any phase are always captured: what the CEO says early is kept, the focus does not jump.
  * Maps (contract.MapsUpdate): org, flows, stores, decisions and the tool catalog, merged per
    record id field by field, with the same optimistic versioning as the slots.
  * Recipe matches (contract.RecipeMatch): library recipes the CEO's stories fit, one record per
    (recipe, use case), same optimistic versioning; heard signals only grow, `missing` is the latest
    view minus what was heard; unknown recipes and use cases are rejected. Applied after the queue
    edits, so a use case added in the same sync can hold a match.
Every rejected change is recorded in `edit_log` with the reason.
"""

from __future__ import annotations

import json
import math
import re
import time
import unicodedata
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from . import fsutil, i18n
from . import phases as P
from . import recipes as recipe_lib
from .contract import (CATALOG_CATEGORIES, MAP_KINDS, PLAN_SECTIONS, RUNGS, SLOT_KEYS, SLOT_RUNG, FastResult,
                       MapsUpdate, QuestionUpdate, RecipeMatch, SlotFill, SyncResult)
from .questionnaire import Questionnaire

Status = Literal["pending", "partial", "answered", "removed"]
Mode = Literal["consent", "interview", "finalize", "generating", "done"]

_RANK = {"pending": 0, "partial": 1, "answered": 2}
MAX_ADDS_PER_SYNC = 3
UPCOMING_COUNT = 3  # questions shown after the current one
NEXT_COUNT = UPCOMING_COUNT  # backwards-compatible name
MAX_THREAD = 40
MAX_MAP_RECORDS = 40  # per map: a runaway model never floods the state
MAX_RECIPE_MATCHES = 24
MAX_RECIPE_ITEMS = 12  # heard signals / missing items kept per match
_RUNG_RANK = {"business": 0, "workflow": 1, "implementation": 2, "done": 3}


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


class ThreadEntry(BaseModel):
    kind: Literal["answer", "followup"]
    text: str  # as generated / heard, in `lang`: what the prompts and the plan always read
    rung: str
    at: float
    by: str = "fast"
    lang: str = ""  # language `text` was generated in (the interview language at the time)
    texts: dict[str, str] = Field(default_factory=dict)  # display-only translations of a follow-up

    def shown(self, lang: str, default: str) -> str:
        """The text in display language `lang` (`default` = the language of entries without one).
        Answer pieces stay as heard; a follow-up not translated yet shows as generated."""
        if self.kind != "followup" or (self.lang or default) == lang:
            return self.text
        return self.texts.get(lang) or self.text


def _empty_ladder() -> dict[str, list[str]]:
    return {r: [] for r in RUNGS}


class QuestionState(BaseModel):
    id: str
    theme: str
    text_fr: str = ""
    text_en: str = ""
    texts: dict[str, str] = Field(default_factory=dict)  # wording per language (fr/en mirrored)
    machine: list[str] = Field(default_factory=list)  # languages whose wording Claude translated
    priority: int = 2
    status: Status = "pending"
    answer: str = ""
    evidence: str = ""
    expect: dict[str, list[str]] = Field(default_factory=_empty_ladder)  # rung -> details (source language)
    expect_i18n: dict[str, dict[str, str]] = Field(default_factory=dict)  # lang -> {item -> translation}
    covered: list[str] = Field(default_factory=list)
    rung: Literal["business", "workflow", "implementation", "done"] = "business"
    thread: list[ThreadEntry] = Field(default_factory=list)
    vague: bool = False
    origin: Literal["seed", "added", "reworded", "manual"] = "seed"
    keywords: list[str] = Field(default_factory=list)
    previous_texts: list[str] = Field(default_factory=list)
    answered_at_sync: int | None = None
    updated_at: float | None = None  # epoch of the last answer/status/detail change
    updated_by: str | None = None  # "fast" | "sync" | "manual"
    read: int = 0  # transcript version (line seq) the analysis that last changed it had read
    thread_at: float | None = None  # when it joined the interview thread above the focus (left the
    #                                 focus, or answered spontaneously while another was asked)
    use_case: str | None = None  # the use case this question drills (its slots are the expect items)
    slot_map: dict[str, str] = Field(default_factory=dict)  # expect item (source language) -> slot key
    gathers: list[str] = Field(default_factory=list)  # no use case: slots its answer feeds (questionnaire `slots:`)
    phase: str = P.DEEP_DIVES  # interview phase (phases.PHASE_IDS); sessions saved before phases: one phase

    @property
    def locked(self) -> bool:
        return self.status == "answered"

    @property
    def open(self) -> bool:
        return self.status in ("pending", "partial")

    @model_validator(mode="after")
    def _mirror_fr_en(self) -> "QuestionState":
        for lang in ("fr", "en"):
            own = getattr(self, f"text_{lang}")
            if own:
                self.texts[lang] = own
            elif self.texts.get(lang):
                setattr(self, f"text_{lang}", self.texts[lang])
        return self

    def has_text(self, lang: str) -> bool:
        return bool(self._variant(lang))

    def _variant(self, lang: str) -> str:
        if lang in ("fr", "en"):
            return getattr(self, f"text_{lang}") or self.texts.get(lang, "")
        return self.texts.get(lang, "")

    def text(self, lang: str) -> str:
        """Wording in `lang`, else the closest fallback (base language, English, French, any)."""
        for code in (*i18n.fallback_chain(lang), "fr"):
            v = self._variant(code)
            if v:
                return v
        return next((v for v in self.texts.values() if v), "")

    def set_text(self, lang: str, text: str, machine: bool = False) -> None:
        self.texts[lang] = text
        if lang in ("fr", "en"):
            setattr(self, f"text_{lang}", text)
        if machine and lang not in self.machine:
            self.machine.append(lang)
        elif not machine and lang in self.machine:
            self.machine.remove(lang)

    def expect_text(self, item: str, lang: str) -> str:
        """An expect item (stored in the source language) as shown/sent in `lang`."""
        return self.expect_i18n.get(lang, {}).get(item, item)

    def expect_in(self, lang: str) -> dict[str, list[str]]:
        return {r: [self.expect_text(e, lang) for e in self.expect.get(r, [])] for r in RUNGS}

    def all_expect(self) -> list[str]:
        return [e for r in RUNGS for e in self.expect.get(r, [])]

    def rung_items(self, rung: str | None = None) -> list[str]:
        return list(self.expect.get(rung or self.rung, []))

    def missing(self, rung: str | None = None) -> list[str]:
        """Details still missing on the current rung (or on `rung`)."""
        return [e for e in self.rung_items(rung) if e not in self.covered]

    def missing_all(self) -> list[str]:
        return [e for e in self.all_expect() if e not in self.covered]

    def required_covered(self) -> int:
        """Details needed before the question can be answered: every slot item, plus half of the
        other expect items."""
        slots = [e for e in self.all_expect() if e in self.slot_map]
        others = [e for e in self.all_expect() if e not in self.slot_map]
        return len(slots) + (math.ceil(len(others) / 2) if others else 0)

    def missing_slots(self) -> list[str]:
        """Slot keys this question still needs, in canonical order."""
        missing = {self.slot_map[e] for e in self.all_expect() if e in self.slot_map and e not in self.covered}
        return [k for k in SLOT_KEYS if k in missing]

    def sufficient(self) -> bool:
        """Enough concrete detail to mark it answered: all required slots filled and at least half
        of the other expect items heard (a question without expect items is always sufficient)."""
        others = [e for e in self.all_expect() if e not in self.slot_map]
        return not self.missing_slots() and (
            sum(1 for e in others if e in self.covered) >= (math.ceil(len(others) / 2) if others else 0))

    @property
    def follow_up(self) -> str:
        """The follow-up question to ask now (the last thread entry, if it is a follow-up)."""
        return self.thread[-1].text if self.thread and self.thread[-1].kind == "followup" else ""

    def match_expect(self, item: str) -> str | None:
        """Map a detail named by the model onto this question's expect items (tolerant). Slot items
        never match: a slot is covered only by a value (see SessionState.apply_slot_fills)."""
        f = _fold(item)
        if not f:
            return None
        for e in self.all_expect():
            if e in self.slot_map:
                continue
            # the model copies items in the interview language: match translations too
            for variant in (e, *(tr[e] for tr in self.expect_i18n.values() if tr.get(e))):
                fe = _fold(variant)
                if fe and (f == fe or (len(f) >= 4 and f in fe) or (len(fe) >= 4 and fe in f)):
                    return e
        return None


class SlotValue(BaseModel):
    value: str
    evidence: str = ""
    at: float | None = None
    by: str | None = None  # "fast" | "sync"
    read: int = 0  # transcript version (line seq) the analysis that wrote it had read


class UseCaseState(BaseModel):
    """A workflow to automate and its integration slots (contract.SLOT_KEYS), filled live."""

    id: str
    titles: dict[str, str] = Field(default_factory=dict)
    origin: Literal["seed", "added"] = "seed"
    slots: dict[str, SlotValue] = Field(default_factory=dict)

    def title(self, lang: str) -> str:
        for code in (*i18n.fallback_chain(lang), "fr"):
            if self.titles.get(code):
                return self.titles[code]
        return next((v for v in self.titles.values() if v), self.id)

    def filled(self, key: str) -> bool:
        v = self.slots.get(key)
        return bool(v and v.value.strip())

    def missing(self) -> list[str]:
        return [k for k in SLOT_KEYS if not self.filled(k)]


class MapRecord(BaseModel):
    """One fact of a map (contract.MapsUpdate): fields as said, merged field by field."""

    id: str
    fields: dict[str, str] = Field(default_factory=dict)
    evidence: str = ""
    at: float | None = None
    by: str | None = None  # "fast" | "sync"
    read: int = 0  # transcript version the analysis that last wrote it had read
    phase: str = ""  # the phase in focus when it was first heard


class RecipeMatchState(BaseModel):
    """A library recipe the interview fits so far (contract.RecipeMatch), per (recipe, use case)."""

    recipe_id: str
    use_case: str | None = None
    fit: str = "partial"
    heard_signals: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    at: float | None = None
    by: str | None = None
    read: int = 0  # transcript version the analysis that last wrote it had read


def _map_slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", _fold(text)).strip("-")[:48]


# the fields that make up a record's identity when the model gives no id
_MAP_KEY_FIELDS = {"org": ("person", "role"), "flows": ("sender", "receiver", "what"), "stores": ("what", "where"),
                   "decisions": ("decision",), "catalog": ("category", "tool")}


class ThemeState(BaseModel):
    id: str
    title_fr: str = ""
    title_en: str = ""
    titles: dict[str, str] = Field(default_factory=dict)  # every language (fr/en mirrored)

    def title(self, lang: str) -> str:
        variants = {**self.titles, **{k: v for k, v in (("fr", self.title_fr), ("en", self.title_en)) if v}}
        for code in (*i18n.fallback_chain(lang), "fr"):
            if variants.get(code):
                return variants[code]
        return next(iter(variants.values()), self.id)


class EditLogEntry(BaseModel):
    sync: int
    kind: str  # "status" | "fast" | "gate" | op name | "focus"
    target: str | None
    applied: bool
    detail: str


class PassageLog(BaseModel):
    sync: int
    start: str
    speaker: str
    lang: str
    text: str


class ApplyReport(BaseModel):
    applied: list[EditLogEntry] = Field(default_factory=list)
    rejected: list[EditLogEntry] = Field(default_factory=list)


class SessionState(BaseModel):
    client: str
    slug: str
    language: str  # interview language: questions, follow-ups, summaries, plan (see i18n.py)
    target_minutes: int
    consent_fr: str = ""
    consent_en: str = ""
    consent: dict[str, str] = Field(default_factory=dict)  # every language (fr/en mirrored)
    source_language: str = ""  # language the questionnaire was written in (expect items)
    ui_language: str = ""  # FDE screen language ("" = same as `language` when it has a locale file)
    speech_locales: list[str] = Field(default_factory=lambda: list(i18n.DEFAULT_SPEECH))  # yap locales
    i18n_overlay: dict[str, dict[str, str]] = Field(default_factory=dict)  # lang -> machine-translated keys
    translation: dict = Field(default_factory=dict)  # {lang, status: running|done|error, count, error}
    themes: list[ThemeState]
    questions: list[QuestionState]  # list order == queue order
    use_cases: list[UseCaseState] = Field(default_factory=list)
    maps: dict[str, list[MapRecord]] = Field(default_factory=lambda: {k: [] for k in MAP_KINDS})
    recipe_matches: list[RecipeMatchState] = Field(default_factory=list)
    # phases (phases.py): the current one, when it started, seconds spent in the ones left, budgets
    phase: str | None = None
    phase_since: float | None = None
    phase_spent: dict[str, float] = Field(default_factory=dict)
    phase_budgets: dict[str, float] = Field(default_factory=dict)  # seconds per phase
    phases_skipped: list[str] = Field(default_factory=list)  # phases the FDE ended with `p`
    focus_id: str | None = None  # the question being asked (locked while answered)
    focus_since: float | None = None
    focus_history: list[str] = Field(default_factory=list)
    next_questions: list[str] = Field(default_factory=list)  # upcoming, focus excluded
    coverage: dict[str, float] = Field(default_factory=dict)
    coverage_notes: dict[str, str] = Field(default_factory=dict)
    plan_notes: dict[str, list[str]] = Field(default_factory=lambda: {s: [] for s in PLAN_SECTIONS})
    passages: list[PassageLog] = Field(default_factory=list)
    edit_log: list[EditLogEntry] = Field(default_factory=list)
    enough_data: bool = False
    enough_data_reason: str = ""
    mode: Mode = "consent"
    sync_count: int = 0
    started_at: float | None = None  # epoch seconds, set when consent is given
    last_sync_at: float | None = None
    last_sync_error: str | None = None
    last_sync_seconds: float | None = None  # duration of the last model call
    sync_in_progress: bool = False
    skipped_ticks: int = 0  # ticks skipped because the previous call was still running
    fast_count: int = 0
    last_fast_seconds: float | None = None  # duration of the last fast model call
    last_fast_latency: float | None = None  # newest speech line ingested -> fast result on screen
    last_fast_error: str | None = None
    first_speech_at: float | None = None  # epoch of the first transcript line
    capture_problems: list[str] = Field(default_factory=list)
    capture_progress: str = ""  # last start-up message (speech model download, retries)
    plan_files: dict[str, str] = Field(default_factory=dict)
    wispr_merged: bool = False
    # Versioning (optimistic locking): the transcript version is its last line seq; every analysis
    # is stamped with the version it read, and each record (question, slot, theme coverage) keeps
    # the version of the analysis that last wrote it. An analysis never overwrites a record written
    # by one that read further (a slow full sync vs a newer fast pass); equal versions apply.
    analysis_read: int = 0  # highest transcript version any applied analysis had read
    fast_read: int = 0
    sync_read: int = 0
    coverage_read: dict[str, int] = Field(default_factory=dict)

    # --- construction / persistence ---------------------------------------
    @classmethod
    def from_questionnaire(cls, q: Questionnaire) -> "SessionState":
        lang = q.language
        st = cls(
            client=q.client,
            slug=q.slug,
            language=lang,
            source_language=lang,
            ui_language=i18n.resolve_ui(q.ui_language, lang),
            speech_locales=list(q.speech_locales or i18n.DEFAULT_SPEECH),
            target_minutes=q.target_minutes,
            consent_fr=q.consent.fr,
            consent_en=q.consent.en,
            consent=dict(q.consent.root),
            themes=[ThemeState(id=t.id, title_fr=t.title.fr, title_en=t.title.en, titles=dict(t.title.root))
                    for t in q.themes],
            # phase by phase, the questionnaire's order kept inside a phase
            questions=sorted((_question_from_def(qd, t.id, lang, t.phase_of(qd)) for t in q.themes for qd in t.questions),
                             key=lambda x: P.PHASE_INDEX[x.phase]),
            use_cases=[UseCaseState(id=u.id, titles=dict(u.title.root)) for u in q.use_cases],
            phase_budgets=P.budgets(q.target_minutes, q.phase_minutes),
        )
        st.coverage = st.computed_coverage()
        st._refill_next()
        return st

    def save(self, path: Path) -> None:
        fsutil.atomic_write_text(path, self.model_dump_json(indent=2))

    @classmethod
    def load(cls, path: Path) -> "SessionState":
        return cls.model_validate(json.loads(path.read_text(encoding="utf-8")))

    # --- languages (rules in i18n.py) ------------------------------------------
    @property
    def languages(self) -> i18n.LanguageState:
        return i18n.LanguageState(ui=self.ui_language or i18n.resolve_ui(None, self.language),
                                  interview=self.language, speech=self.speech_locales)

    def set_languages(self, ui: str | None = None, interview: str | None = None,
                      speech: list[str] | str | None = None) -> i18n.LanguageState:
        """Validate and apply a language change (raises i18n.LanguageError). Speech locales can
        only change before capture starts (the session enforces it)."""
        # validate everything first: a bad value changes nothing
        if ui is not None and ui not in i18n.available():
            raise i18n.LanguageError(f"no locale file for screen language {ui!r} "
                                     f"(available: {', '.join(i18n.available())})")
        interview = i18n.normalize_interview(interview) if interview is not None else None
        speech = i18n.normalize_speech(speech) if speech is not None else None
        if ui is not None:
            self.ui_language = ui
        if interview is not None:
            self.language = interview
        if speech is not None:
            self.speech_locales = speech
        return self.languages

    def tr(self, lang: str | None = None) -> i18n.Translator:
        """Translator for the interview language (or `lang`), with Claude's overlay for it."""
        lang = lang or self.language
        return i18n.Translator(lang, self.i18n_overlay.get(lang))

    def consent_text(self, lang: str) -> str:
        variants = {**self.consent, **{k: v for k, v in (("fr", self.consent_fr), ("en", self.consent_en)) if v}}
        for code in (*i18n.fallback_chain(lang), "fr"):
            if variants.get(code):
                return variants[code]
        return next(iter(variants.values()), "")

    # --- queries -----------------------------------------------------------
    def get(self, qid: str | None) -> QuestionState | None:
        if qid is None:
            return None
        return next((q for q in self.questions if q.id == qid), None)

    def get_use_case(self, uid: str | None) -> UseCaseState | None:
        if not uid:
            return None
        return next((u for u in self.use_cases if u.id == uid), None)

    def apply_slot_fills(self, fills: list[SlotFill], ok, reject, by: str, read: int | None = None) -> None:
        """Fill use-case slots with what the CEO said (values only grow), then refresh the slot
        items covered by every question on those use cases. `read`: the transcript version the
        analysis read (default: the newest applied); a slot written by a newer read is kept."""
        read = self.analysis_read if read is None else read
        for f in fills:
            uc = self.get_use_case(f.use_case)
            if uc is None:
                reject("slot", f.use_case, f"unknown use case (slot {f.slot})")
                continue
            value = f.value.strip()
            if not value:
                continue
            old = uc.slots.get(f.slot)
            if old is not None and old.value == value:
                continue
            if old is not None and old.read > read:
                reject("stale", uc.id, f"{f.slot}: read at line {read}, kept the value read at line {old.read}")
                continue
            uc.slots[f.slot] = SlotValue(value=value, evidence=f.evidence.strip() or (old.evidence if old else ""),
                                         at=time.time(), by=by, read=read)
            ok("slot", uc.id, f"{f.slot}: {value[:80]}")
        self.sync_slot_coverage()

    def sync_slot_coverage(self) -> None:
        for q in self.questions:
            uc = self.get_use_case(q.use_case)
            if uc is None or not q.slot_map:
                continue
            filled = {e for e, k in q.slot_map.items() if uc.filled(k)}
            q.covered = [e for e in q.all_expect() if e in filled or (e not in q.slot_map and e in q.covered)]

    def queue(self) -> list[QuestionState]:
        """Open questions in queue order."""
        return [q for q in self.questions if q.open]

    # --- maps ------------------------------------------------------------------------
    def map_records(self, kind: str) -> list[MapRecord]:
        return self.maps.setdefault(kind, [])

    def apply_map_updates(self, maps: MapsUpdate, ok, reject, by: str, read: int | None = None) -> None:
        """Merge map facts per record id, field by field: a new non-empty field replaces the old one,
        an empty one (or "unknown") keeps it. A record written by an analysis that read further is
        kept (optimistic versioning, like the slots). Whatever the phase in focus: facts are always
        captured."""
        read = self.analysis_read if read is None else read
        for kind in MAP_KINDS:
            records = self.map_records(kind)
            for fact in getattr(maps, kind):
                data = fact.model_dump()
                evidence = str(data.pop("evidence", "")).strip()
                rid = _map_slug(str(data.pop("id", "")))
                values = {k: str(v).strip() for k, v in data.items()
                          if str(v).strip() and str(v).strip() not in ("unknown", "none")}
                if kind == "catalog":
                    values["category"] = str(data.get("category"))
                if not rid:
                    rid = _map_slug(" ".join(values.get(f, "") for f in _MAP_KEY_FIELDS[kind]))
                substance = [k for k in values if k != "category"]
                if not rid or not substance:
                    continue
                old = next((r for r in records if r.id == rid), None)
                if old is not None and old.read > read:
                    reject("stale", f"{kind}:{rid}", f"read at line {read}, kept the record read at line {old.read}")
                    continue
                if old is None:
                    if len(records) >= MAX_MAP_RECORDS:
                        reject("map", f"{kind}:{rid}", f"more than {MAX_MAP_RECORDS} {kind} records")
                        continue
                    old = MapRecord(id=rid, phase=self.current_phase or "")
                    records.append(old)
                merged = {**old.fields, **values}
                if merged == old.fields and (not evidence or evidence == old.evidence):
                    continue
                old.fields = merged
                old.evidence = evidence or old.evidence
                old.at, old.by, old.read = time.time(), by, max(old.read, read)
                ok("map", f"{kind}:{rid}", "; ".join(f"{k}={v}" for k, v in values.items())[:120])

    # --- recipe matches ---------------------------------------------------------------
    def apply_recipe_matches(self, matches: list[RecipeMatch], ok, reject, by: str, read: int | None = None,
                             known: set[str] | None = None) -> None:
        """Merge recipe matches per (recipe, use case): fit replaced, heard signals only grow, missing =
        the newest view minus what was heard. A match written by an analysis that read further is
        kept (optimistic versioning). `known`: the library's recipe ids (default: the library)."""
        read = self.analysis_read if read is None else read
        known = set(recipe_lib.try_library()) if known is None else known
        for m in matches:
            rid = m.recipe_id.strip()
            uc = (m.use_case or "").strip() or None
            target = f"{rid}@{uc or '-'}"
            if rid not in known:
                reject("recipe", target, "unknown recipe")
                continue
            if uc is not None and self.get_use_case(uc) is None:
                reject("recipe", target, f"unknown use case {uc!r}")
                continue
            old = next((r for r in self.recipe_matches if r.recipe_id == rid and r.use_case == uc), None)
            if old is not None and old.read > read:
                reject("stale", target, f"read at line {read}, kept the match read at line {old.read}")
                continue
            if old is None:
                if m.fit == "none":
                    continue  # nothing to withdraw
                if len(self.recipe_matches) >= MAX_RECIPE_MATCHES:
                    reject("recipe", target, f"more than {MAX_RECIPE_MATCHES} recipe matches")
                    continue
                old = RecipeMatchState(recipe_id=rid, use_case=uc)
                self.recipe_matches.append(old)
            heard = list(old.heard_signals)
            for x in (s.strip() for s in m.heard_signals):
                if x and x not in heard:
                    heard.append(x)
            missing = []
            for x in (s.strip() for s in m.missing):
                if x and x not in heard and x not in missing:
                    missing.append(x)
            new = (m.fit, heard[:MAX_RECIPE_ITEMS], missing[:MAX_RECIPE_ITEMS])
            if new == (old.fit, old.heard_signals, old.missing):
                continue
            old.fit, old.heard_signals, old.missing = new
            old.at, old.by, old.read = time.time(), by, max(old.read, read)
            ok("recipe", target, f"{m.fit}; missing: {', '.join(old.missing)[:100]}")

    def active_recipe_matches(self) -> list[RecipeMatchState]:
        """Matches still standing (fit strong or partial), strong first, then the most signals heard."""
        rank = {"strong": 0, "partial": 1}
        live = [m for m in self.recipe_matches if m.fit in rank]
        return sorted(live, key=lambda m: (rank[m.fit], -len(m.heard_signals)))

    def map_count(self, kind: str) -> int:
        return len(self.map_records(kind))

    def maps_enough(self, phase: str) -> bool:
        """The phase's maps hold enough facts to move on (phases.MAP_ENOUGH)."""
        kinds = P.PHASE_MAPS.get(phase)
        if not kinds:
            return False
        for kind in kinds:
            if kind == "catalog":
                if len({r.fields.get("category") for r in self.map_records("catalog")}) < P.MAP_ENOUGH["catalog"]:
                    return False
            elif self.map_count(kind) < P.MAP_ENOUGH[kind]:
                return False
        return True

    def map_gaps(self, phase: str | None = None) -> list[str]:
        """What the current phase's maps still miss, as locale keys (`maps.gap.*`, `maps.catalog.*`):
        a hidden checklist the follow-ups aim at, in story form, never read out as a list."""
        phase = phase or self.current_phase
        org, flows = self.map_records("org"), self.map_records("flows")
        stores, decisions = self.map_records("stores"), self.map_records("decisions")
        cats = {r.fields.get("category") for r in self.map_records("catalog")}
        gaps: list[str] = []

        def need(cond: bool, key: str) -> None:
            if cond:
                gaps.append(key)

        if phase == "signal-carriers":
            need(len(org) < P.MAP_ENOUGH["org"], "gap.people")
            need(not any(r.fields.get("reports_to") for r in org), "gap.reportsTo")
            need(not any(r.fields.get("department") for r in org), "gap.departments")
        elif phase == "signal-flow":
            need(not any(r.fields.get("ceo_pattern") == "waits_on_ceo" for r in flows), "gap.waitsOnCeo")
            need(not any(r.fields.get("ceo_pattern") == "ceo_asks_status" for r in flows), "gap.ceoStatus")
            need(not any(r.fields.get("wait") for r in flows), "gap.wait")
            need(not any(r.fields.get("confidential") for r in flows), "gap.confidential")
            gaps += [f"catalog.{c}" for c in ("internal_messaging", "external_channels") if c not in cats]
        elif phase == "knowledge":
            need(len(stores) < P.MAP_ENOUGH["stores"], "gap.stores")
            need(not any(_fold(r.fields.get("where", "")) in ("head", "tete") for r in stores), "gap.unwritten")
            need(not any(r.fields.get("how_found") for r in stores), "gap.howFound")
            gaps += [f"catalog.{c}" for c in ("shared_storage", "spreadsheets", "core_systems") if c not in cats]
        elif phase == "decisions":
            need(not decisions, "gap.decision")
            need(not any(r.fields.get("prep_time") for r in decisions), "gap.prepTime")
            need(not any(r.fields.get("time_split") for r in org), "gap.timeSplit")
            gaps += [f"catalog.{c}" for c in ("task_tracking", "reporting") if c not in cats]
        return gaps

    # --- phases ------------------------------------------------------------------------
    @property
    def current_phase(self) -> str | None:
        cur = self.get(self.focus_id)
        return cur.phase if cur is not None else self.phase

    def phase_elapsed(self, now: float | None = None) -> float:
        if self.phase_since is None:
            return 0.0
        return max(0.0, (now or time.time()) - self.phase_since)

    def phase_budget_spent(self, phase: str | None = None, now: float | None = None) -> bool:
        phase = phase or self.current_phase
        budget = self.phase_budgets.get(phase or "", 0.0)
        return bool(phase == self.phase and budget and self.phase_since is not None
                    and self.phase_elapsed(now) >= budget)

    def phase_done(self, phase: str | None, now: float | None = None) -> bool:
        """The phase can hand over: nothing left to ask in it, the FDE skipped it, its time budget is
        spent, or its maps hold enough facts once one of its questions was asked."""
        if phase is None:
            return True
        if phase in self.phases_skipped:
            return True
        if not any(q.open and q.phase == phase for q in self.questions):
            return True
        if self.phase_budget_spent(phase, now):
            return True
        asked = any((q := self.get(i)) is not None and q.phase == phase for i in self.focus_history)
        return asked and self.maps_enough(phase)

    def start_phase_clock(self, now: float | None = None) -> None:
        """Called when the interview starts (consent given): the first phase's clock starts."""
        self.phase = self.current_phase
        self.phase_since = now or time.time()

    def _enter_phase(self, phase: str | None, reason: str) -> None:
        if phase == self.phase:
            return
        now = time.time()
        if self.phase is not None and self.phase_since is not None:
            self.phase_spent[self.phase] = round(self.phase_spent.get(self.phase, 0.0) + now - self.phase_since, 1)
        old, self.phase = self.phase, phase
        self.phase_since = now if self.phase_since is not None or self.started_at else None
        self.edit_log.append(EditLogEntry(sync=self.sync_count, kind="phase", target=phase, applied=True,
                                          detail=f"{old} -> {phase} ({reason})"))

    def upcoming_order(self, exclude: str | None = None) -> list[str]:
        """Open questions in the order the focus will take them: the current phase first (unless it
        is done), in the upcoming list's order then the queue's, questions not asked yet before ones
        already visited; then the later phases in order; earlier phases only at the very end."""
        current = self.phase or self.current_phase
        visited = set(self.focus_history)
        cands = [q for q in self.queue() if q.id not in (exclude, self.focus_id)]
        rank = {i: n for n, i in enumerate(self.next_questions)}
        cur_idx = P.PHASE_INDEX.get(current or "", 0)
        current_open = not self.phase_done(current)

        def key(q: QuestionState) -> tuple:
            idx = P.PHASE_INDEX[q.phase]
            seen = q.id in visited
            if idx == cur_idx and current_open and not seen:
                group = 0  # the current phase, questions not asked yet
            elif idx > cur_idx and not seen:
                group = 1  # the next phases, in order
            else:
                group = 2  # left-overs (visited, earlier or ended phases): only at the very end
            return (group, seen, idx if group else 0, rank.get(q.id, len(rank)) if group == 0 else 0)

        order = sorted(cands, key=key)  # stable: queue order inside each group
        return [q.id for q in order]

    def skip_phase(self) -> str | None:
        """The FDE ends the current phase (`p`): the focus moves to the next phase's first question.
        The current card joins the thread as with `n`."""
        cur = self.current_phase
        if cur is None:
            return None
        if cur not in self.phases_skipped:
            self.phases_skipped.append(cur)
        return self.advance("FDE skipped to the next phase")

    def theme_ids(self) -> set[str]:
        return {t.id for t in self.themes}

    def computed_coverage(self) -> dict[str, float]:
        """Per theme: answered = 1, partial = share of expected details heard (at least 0.25)."""
        cov: dict[str, float] = {}
        for t in self.themes:
            qs = [q for q in self.questions if q.theme == t.id and q.status != "removed"]
            if not qs:
                cov[t.id] = 0.0
                continue
            score = 0.0
            for q in qs:
                items = q.all_expect()
                if q.status == "answered":
                    score += 1.0
                elif q.status == "partial":
                    score += max(0.25, len(q.covered) / len(items)) if items else 0.5
            cov[t.id] = round(min(1.0, score / len(qs)), 2)
        return cov

    @property
    def current_question(self) -> QuestionState | None:
        return self.get(self.focus_id)

    def overview(self) -> dict:
        """"Où on en est": must-have questions still open and their missing details."""
        must = [q for q in self.questions if q.priority == 1 and q.open]
        return {
            "must_open": [q.id for q in must],
            "missing_details": sum(len(q.missing_all()) for q in must),
            "answered": sum(1 for q in self.questions if q.status == "answered"),
            "open": len(self.queue()),
            "coverage": round(sum(self.coverage.values()) / len(self.coverage), 2) if self.coverage else 0.0,
        }

    # --- the rules -----------------------------------------------------------
    def apply_sync_result(self, result: SyncResult, thread: bool = True, read: int | None = None) -> ApplyReport:
        """thread=False when a fast pass owns the thread: the full sync then does not append
        answer pieces (it sees a larger, overlapping slice and would duplicate them).
        `read`: the transcript version (last line seq) this sync read; None = the newest applied.
        Questions, slots and theme coverage written meanwhile by an analysis that read further
        (a fast pass) are kept; queue edits, upcoming list, plan notes and enough_data (the full
        sync's own) apply."""
        read = self.analysis_read if read is None else read
        sync_no = self.sync_count + 1
        report = ApplyReport()

        def ok(kind: str, target: str | None, detail: str) -> None:
            report.applied.append(EditLogEntry(sync=sync_no, kind=kind, target=target, applied=True, detail=detail))

        def reject(kind: str, target: str | None, detail: str) -> None:
            report.rejected.append(EditLogEntry(sync=sync_no, kind=kind, target=target, applied=False, detail=detail))

        # 0. integration slots of the use cases (before statuses: they gate "answered"), and the maps
        self.apply_slot_fills(result.slots, ok, reject, by="sync", read=read)
        self.apply_map_updates(result.maps, ok, reject, by="sync", read=read)
        phase = self.current_phase

        # 1. status updates / extracted answers / covered details / rung / thread
        self._apply_updates(result.question_updates, sync_no, ok, reject, by="sync", pieces=thread, read=read)

        # 2. queue edits (never on answered questions, never on the current focus)
        adds = 0
        for e in result.queue_edits:
            if e.op == "add":
                if adds >= MAX_ADDS_PER_SYNC:
                    reject("add", e.question_id, f"more than {MAX_ADDS_PER_SYNC} additions in one sync")
                    continue
                if e.theme not in self.theme_ids():
                    reject("add", e.question_id, f"unknown theme {e.theme!r}")
                    continue
                parked = bool((e.use_case or "").strip())  # a workflow heard: its walkthrough waits in the deep dives
                target_phase = P.DEEP_DIVES if parked else phase
                theme_phase = self.theme_phase(e.theme)
                if not parked and theme_phase is not None and theme_phase != phase:
                    reject("phase", e.question_id, f"theme {e.theme!r} belongs to phase {theme_phase}, "
                                                   f"the interview is in {phase}")
                    continue
                fr, en = (e.text_fr or "").strip(), (e.text_en or "").strip()
                own = (e.text or "").strip()
                if not fr and not en and not own:
                    reject("add", e.question_id, "empty question text")
                    continue
                new_id = self._unique_id(e.question_id or f"{e.theme}-x")
                ladder = _empty_ladder()
                if e.expect is not None:
                    for r in RUNGS:
                        ladder[r] = [x.strip() for x in getattr(e.expect, r) if x.strip()][:4]
                q = QuestionState(
                    id=new_id, theme=e.theme, text_fr=fr or en or own, text_en=en or fr or own, origin="added",
                    priority=2, expect=ladder, phase=target_phase or P.DEEP_DIVES,
                )
                if own:
                    q.set_text(self.language, own)
                if (e.use_case or "").strip():
                    uc = self._use_case_for_add(e.use_case, e.use_case_title or own or fr or en)
                    attach_slots(q, uc.id, list(SLOT_KEYS), self.source_language or self.language, self.tr)
                    if uc.origin == "added" and not any(x.use_case == uc.id for x in self.questions):
                        ok("use_case", uc.id, f"new use case: {uc.title(self.language)}")
                anchor = self.get(e.after_id)
                if parked and target_phase != phase and (anchor is None or anchor.phase != target_phase):
                    self._park(q)
                else:
                    self._insert_after(q, e.after_id if anchor is not None and anchor.phase == q.phase else None)
                adds += 1
                ok("add", new_id, e.reason + (" (parked in the deep dives)" if parked and target_phase != phase else ""))
                continue

            q = self.get(e.question_id)
            if q is None:
                reject(e.op, e.question_id, "unknown question id")
                continue
            if q.locked:
                reject(e.op, q.id, "question is answered/locked")
                continue
            if q.status == "removed":
                reject(e.op, q.id, "question was removed")
                continue
            if q.phase != phase:
                reject("phase", q.id, f"{e.op}: the question belongs to phase {q.phase}, the interview is in {phase}")
                continue
            if q.id == self.focus_id:
                if e.op == "reword":
                    text = (e.text or (e.text_fr if self.language == "fr" else e.text_en if self.language == "en"
                                       else None) or e.text_en or e.text_fr or "").strip()
                    if text and self._append_followup(q, text, by="sync", lang=self.language):
                        ok("reword", q.id, "current question: rewording added to the thread as a follow-up")
                    else:
                        reject("reword", q.id, "current question: a follow-up is already pending")
                    continue
                reject(e.op, q.id, "current question is locked while it is being answered")
                continue

            if e.op == "reword":
                fr, en = (e.text_fr or "").strip(), (e.text_en or "").strip()
                own = (e.text or "").strip()
                if not fr and not en and not own:
                    reject("reword", q.id, "empty question text")
                    continue
                q.previous_texts.append(q.text(self.language))
                for lang in [k for k in q.texts if k not in ("fr", "en")]:
                    q.texts.pop(lang)  # other variants describe the old wording
                q.machine = [m for m in q.machine if m in ("fr", "en")]
                if fr:
                    q.set_text("fr", fr)
                if en:
                    q.set_text("en", en)
                if own:
                    q.set_text(self.language, own)
                if q.origin == "seed":
                    q.origin = "reworded"
                ok("reword", q.id, e.reason)
            elif e.op == "remove":
                if q.status != "pending":
                    reject("remove", q.id, "only pending questions can be removed (it has a partial answer)")
                    continue
                q.status = "removed"
                ok("remove", q.id, e.reason)
            elif e.op == "reorder":
                if e.after_id is not None and e.after_id == q.id:
                    reject("reorder", q.id, "cannot place a question after itself")
                    continue
                if e.after_id is not None and self.get(e.after_id) is None:
                    reject("reorder", q.id, f"unknown after_id {e.after_id!r}")
                    continue
                if e.after_id is not None and self.get(e.after_id).phase != q.phase:
                    reject("phase", q.id, f"reorder: {e.after_id!r} belongs to another phase")
                    continue
                self.questions.remove(q)
                self._insert_after(q, e.after_id)
                ok("reorder", q.id, e.reason)

        self.sync_slot_coverage()  # a question added on a known use case starts with its filled slots

        # 2b. recipe matches (after the adds: a use case created in this sync can hold one)
        self.apply_recipe_matches(result.recipe_matches, ok, reject, by="sync", read=read)

        # 3. upcoming questions: only open ones, never the focus, topped up from the queue
        nxt: list[str] = []
        for qid in result.next_questions:
            if qid == self.focus_id:
                continue
            q = self.get(qid)
            if q is not None and q.open and q.phase != phase:
                reject("phase", qid, f"next: belongs to phase {q.phase}, the interview is in {phase}")
            elif q is not None and q.open and qid not in nxt:
                nxt.append(qid)
            elif q is None or not q.open:
                reject("next", qid, "not an open question")
        self.next_questions = nxt[:UPCOMING_COUNT]
        self._refill_next()

        # 4. coverage (model's view, clamped; computed fallback for missing themes)
        computed = self.computed_coverage()
        given = {c.theme: c for c in result.coverage if c.theme in computed}
        for tid in computed:
            if self.coverage_read.get(tid, 0) > read:  # a newer read raised it: never lower it from an older one
                self.coverage[tid] = max(self.coverage.get(tid, 0.0), computed[tid])
                continue
            if tid in given:
                self.coverage[tid] = round(min(1.0, max(0.0, given[tid].score)), 2)
                if given[tid].note:
                    self.coverage_notes[tid] = given[tid].note
            else:
                self.coverage[tid] = computed[tid]
            self.coverage_read[tid] = read

        # 5. plan delta (append, de-duplicated)
        for section in PLAN_SECTIONS:
            bucket = self.plan_notes.setdefault(section, [])
            for item in getattr(result.plan_delta, section):
                item = item.strip()
                if item and item.casefold() not in {b.casefold() for b in bucket}:
                    bucket.append(item)

        # 6. cleaned passages for the interview log
        for p in result.clean_passages:
            if p.text.strip():
                self.passages.append(
                    PassageLog(sync=sync_no, start=p.start, speaker=p.speaker, lang=p.lang, text=p.text.strip())
                )

        # 7. enough_data is sticky
        if result.enough_data and not self.enough_data:
            self.enough_data = True
            self.enough_data_reason = result.enough_data_reason
            if self.mode == "interview":
                self.mode = "finalize"

        self.sync_count = sync_no
        self.sync_read = max(self.sync_read, read)
        self.analysis_read = max(self.analysis_read, read)
        self.last_sync_at = time.time()
        self.last_sync_error = None
        self.edit_log.extend(report.applied + report.rejected)
        return report

    def _apply_updates(self, updates: list[QuestionUpdate], sync_no: int, ok, reject, by: str,
                       pieces: bool = True, read: int | None = None) -> None:
        """Status / answer / covered details / rung / thread, shared by the full sync and the fast
        pass. `pieces=False` ignores answer pieces (see apply_sync_result). `read`: the transcript
        version the analysis read; a question last changed by a newer read is left as it is."""
        kind = "status" if by == "sync" else "fast"
        read = self.analysis_read if read is None else read
        for u in updates:
            q = self.get(u.question_id)
            if q is None:
                reject(kind, u.question_id, "unknown question id")
                continue
            if q.locked:
                if u.status != "answered" or (u.answer and u.answer != q.answer):
                    reject(kind, q.id, "question is answered/locked; change ignored")
                continue
            if q.status == "removed":
                reject(kind, q.id, "question was removed")
                continue
            if q.read > read:  # optimistic lock: an analysis that read further already wrote it
                reject("stale", q.id, f"read at line {read}; kept the {q.updated_by or ''} result read at line {q.read}")
                continue
            if _RANK[u.status] < _RANK[q.status]:
                reject(kind, q.id, f"status cannot go back from {q.status} to {u.status}")
                continue
            before = (q.status, q.answer, list(q.covered), q.rung, len(q.thread), q.vague)
            rung_before = q.rung
            for item in u.covered:  # covered details only grow (slot items: see apply_slot_fills)
                e = q.match_expect(item)
                if e is not None and e not in q.covered:
                    q.covered.append(e)
            q.covered = [e for e in q.all_expect() if e in q.covered]  # keep the ladder order

            # rung: only moves down the ladder; climbs by itself when a rung is fully covered (a
            # use-case question also climbs over rungs that have no required slot)
            rank = max(_RUNG_RANK[q.rung], _RUNG_RANK[u.rung])
            while rank < 2 and (q.rung_items(RUNGS[rank]) or q.slot_map) and not q.missing(RUNGS[rank]):
                rank += 1
            wants_done = rank >= 3 or u.status == "answered" or (
                rank == 2 and q.rung_items("implementation") and not q.missing("implementation"))
            enough = q.sufficient()
            if wants_done and not enough:
                missing = ", ".join(q.missing_slots()) or f"{len(q.covered)} of {len(q.all_expect())} details"
                reject("gate", q.id, f"kept partial: {len(q.covered)} of {len(q.all_expect())} expected details "
                                     f"heard (needs {q.required_covered()}; missing: {missing})")
                rank = min(rank, 2)
            status = u.status
            if wants_done and enough:
                rank, status = 3, "answered"
            elif status == "answered":
                status = "partial"
            q.rung = ("business", "workflow", "implementation", "done")[rank]
            q.status = status
            if u.answer.strip():
                q.answer = u.answer.strip()
            elif u.answer_piece.strip() and u.answer_piece.strip() not in q.answer:
                q.answer = f"{q.answer} {u.answer_piece.strip()}".strip()  # fast pass: draft = pieces
            if u.evidence.strip():
                q.evidence = u.evidence.strip()

            # thread: append the new answer piece, then (one) follow-up
            piece = u.answer_piece.strip()
            if pieces and piece and not (q.thread and q.thread[-1].kind == "answer" and q.thread[-1].text == piece):
                q.thread.append(ThreadEntry(kind="answer", text=piece, rung=rung_before, at=time.time(), by=by))
            if q.status == "answered":
                q.answered_at_sync = sync_no
                q.vague = False
            else:
                q.vague = u.vague
                if u.follow_up.strip():
                    self._append_followup(q, u.follow_up.strip(), by=by, lang=self.language)
            q.thread = q.thread[-MAX_THREAD:]
            if q.id != self.focus_id and q.thread_at is None and q.answer:
                q.thread_at = time.time()  # answered spontaneously: it joins the thread now
            if (q.status, q.answer, q.covered, q.rung, len(q.thread), q.vague) != before:
                q.updated_at = time.time()
                q.updated_by = by
                q.read = max(q.read, read)
                ok(kind, q.id, f"-> {q.status}, rung {q.rung} ({len(q.covered)}/{len(q.all_expect())} details)")

    @staticmethod
    def _append_followup(q: QuestionState, text: str, by: str, lang: str = "") -> bool:
        """Append a follow-up only after an answer piece (or as the first entry). A follow-up that
        is already pending stays: the FDE may be asking it right now."""
        if q.thread and q.thread[-1].kind == "followup":
            return False
        if any(e.kind == "followup" and _fold(e.text) == _fold(text) for e in q.thread):
            return False  # never ask the same thing twice
        q.thread.append(ThreadEntry(kind="followup", text=text, rung=q.rung if q.rung != "done" else "implementation",
                                    at=time.time(), by=by, lang=lang))
        q.updated_at = time.time()
        return True

    def apply_fast_result(self, result: FastResult, read: int | None = None) -> ApplyReport:
        """Fast incremental pass: same locking rules as a full sync, but it only touches
        answers, covered details, rung, thread, statuses and coverage. Coverage can only go up here
        (the fast pass sees a few lines, not the whole interview); the full sync can correct
        it either way. The focus never changes here. `read`: the transcript version it read (None =
        the newest applied); records written by a newer read are kept."""
        report = ApplyReport()
        sync_no = self.sync_count
        read = self.analysis_read if read is None else read

        def ok(kind, target, detail):
            report.applied.append(EditLogEntry(sync=sync_no, kind=kind, target=target, applied=True, detail=detail))

        def reject(kind, target, detail):
            report.rejected.append(EditLogEntry(sync=sync_no, kind=kind, target=target, applied=False, detail=detail))

        self.apply_slot_fills(result.slots, ok, reject, by="fast", read=read)
        self.apply_map_updates(result.maps, ok, reject, by="fast", read=read)
        self._apply_updates(result.question_updates, sync_no, ok, reject, by="fast", read=read)
        computed = self.computed_coverage()
        for c in result.coverage:
            if c.theme in computed:
                score = round(min(1.0, max(0.0, c.score)), 2)
                new = max(self.coverage.get(c.theme, 0.0), score, computed[c.theme])
                if new > self.coverage.get(c.theme, 0.0):
                    self.coverage_read[c.theme] = max(self.coverage_read.get(c.theme, 0), read)
                self.coverage[c.theme] = new
        for tid, v in computed.items():  # answered questions always count
            self.coverage[tid] = max(self.coverage.get(tid, 0.0), v)
        self._refill_next()
        self.fast_count += 1
        self.fast_read = max(self.fast_read, read)
        self.analysis_read = max(self.analysis_read, read)
        self.last_fast_error = None
        self.edit_log.extend(report.applied + report.rejected)
        return report

    def history(self) -> list[QuestionState]:
        """The interview thread above the current question, oldest first: questions that left the
        focus (answered, moved on, skipped) and questions answered spontaneously. A question
        that comes back into focus leaves the history; when it leaves the focus again it goes
        to the bottom."""
        qs = [q for q in self.questions if q.thread_at is not None and q.id != self.focus_id
              and (q.status != "removed" or q.id in self.focus_history)]
        return sorted(qs, key=lambda q: q.thread_at)

    def recent_updates(self, n: int = 4, exclude: str | None = None) -> list[QuestionState]:
        qs = [q for q in self.questions if q.updated_at and q.answer and q.status != "removed" and q.id != exclude]
        return sorted(qs, key=lambda q: q.updated_at, reverse=True)[:n]

    # --- focus ------------------------------------------------------------------
    def advance(self, reason: str = "next") -> str | None:
        """Move the focus to the next upcoming open question. Returns the new focus id."""
        old = self.focus_id
        if (left := self.get(old)) is not None:
            left.thread_at = time.time()  # joins the thread, at the bottom
        if self.phase is None:
            self.phase = left.phase if left is not None else self.current_phase
        # phase by phase: the current phase while it is not done, then the next one (phases.py)
        candidates = self.upcoming_order(exclude=old)
        self.focus_id = candidates[0] if candidates else None
        self.focus_since = time.time()
        if self.focus_id:
            self.focus_history.append(self.focus_id)
            new_phase = self.get(self.focus_id).phase
            if new_phase != self.phase:
                why = ("skipped by the FDE" if self.phase in self.phases_skipped
                       else "time budget spent" if self.phase_budget_spent(self.phase)
                       else "maps filled" if self.maps_enough(self.phase or "") else "questions done")
                self._enter_phase(new_phase, why)
        self.next_questions = [i for i in self.next_questions if i != self.focus_id]
        self._refill_next()
        self.edit_log.append(EditLogEntry(sync=self.sync_count, kind="focus", target=self.focus_id,
                                          applied=True, detail=f"{old} -> {self.focus_id} ({reason})"))
        return self.focus_id

    # --- manual overrides from the FDE console ---------------------------------
    def mark_answered(self, qid: str, note: str = "") -> bool:
        q = self.get(qid)
        if q is None or not q.open:
            return False
        q.status = "answered"
        q.answered_at_sync = self.sync_count
        q.updated_at = time.time()
        q.updated_by = "manual"
        q.rung = "done"
        if note:
            q.answer = note
        if qid == self.focus_id:
            self.advance("marked answered by the FDE")
        else:
            q.thread_at = q.thread_at or time.time()
            self._drop_from_next(qid)
        self.coverage = {**self.coverage, **_raise_only(self.coverage, self.computed_coverage())}
        return True

    def skip(self, qid: str) -> bool:
        q = self.get(qid)
        if q is None or q.status != "pending":
            return False
        q.status = "removed"
        if qid == self.focus_id:
            self.advance("skipped by the FDE")
        else:
            self._drop_from_next(qid)
        return True

    def force_enough(self, reason: str = "FDE decision") -> None:
        self.enough_data = True
        self.enough_data_reason = self.enough_data_reason or reason
        if self.mode in ("consent", "interview"):
            self.mode = "finalize"

    # --- helpers -------------------------------------------------------------
    def _drop_from_next(self, qid: str) -> None:
        self.next_questions = [i for i in self.next_questions if i != qid]
        self._refill_next()

    def _refill_next(self) -> None:
        focus = self.get(self.focus_id)
        if focus is None or focus.status == "removed":
            order = self.upcoming_order() if self.phase else [q.id for q in self.queue()]
            self.focus_id = order[0] if order else None
            if self.focus_id and self.focus_since is None:
                self.focus_since = time.time()
            if self.focus_id and self.focus_id not in self.focus_history:
                self.focus_history.append(self.focus_id)
            if self.phase is not None and self.focus_id and self.get(self.focus_id).phase != self.phase:
                self._enter_phase(self.get(self.focus_id).phase, "questions done")
        if self.phase is None and self.focus_id:
            self.phase = self.get(self.focus_id).phase
        phase = self.current_phase
        done = self.phase_done(phase)  # e.g. its time budget is spent: the upcoming list shows the next phase
        self.next_questions = [
            i for i in self.next_questions
            if i != self.focus_id and (q := self.get(i)) is not None and q.open and q.phase == phase and not done
        ][:UPCOMING_COUNT]
        for qid in self.upcoming_order():
            if len(self.next_questions) >= UPCOMING_COUNT:
                break
            if qid not in self.next_questions:
                self.next_questions.append(qid)

    def refresh_upcoming(self) -> list[str]:
        """Re-derive the upcoming list (time-dependent: a spent phase budget shows the next phase)."""
        self._refill_next()
        return list(self.next_questions)

    def theme_phase(self, theme: str | None) -> str | None:
        """A theme's phase: the phase most of its questions are in (None if it has none)."""
        qs = [q.phase for q in self.questions if q.theme == theme]
        return max(set(qs), key=qs.count) if qs else None

    def _park(self, q: QuestionState) -> None:
        """A question for a later phase (a workflow heard early): at the end of its phase."""
        idx = P.PHASE_INDEX[q.phase]
        pos = next((i for i, x in enumerate(self.questions) if P.PHASE_INDEX[x.phase] > idx), len(self.questions))
        self.questions.insert(pos, q)

    def _use_case_for_add(self, uid: str, title: str) -> UseCaseState:
        uc = self.get_use_case(uid)
        if uc is None:
            base = re.sub(r"[^a-z0-9-]+", "-", uid.lower()).strip("-") or "use-case"
            uc = self.get_use_case(base)
            if uc is None:
                uc = UseCaseState(id=base, titles={self.language: title.strip() or base}, origin="added")
                self.use_cases.append(uc)
        return uc

    def _unique_id(self, base: str) -> str:
        base = re.sub(r"[^a-z0-9-]+", "-", base.lower()).strip("-") or "q"
        existing = {q.id for q in self.questions}
        if base not in existing:
            return base
        n = 2
        while f"{base}-{n}" in existing:
            n += 1
        return f"{base}-{n}"

    def _insert_after(self, q: QuestionState, after_id: str | None) -> None:
        """after_id=None puts the question at the head of its phase's open questions."""
        anchor = self.get(after_id)
        if anchor is not None and anchor is not q:
            self.questions.insert(self.questions.index(anchor) + 1, q)
            return
        first_open = next((i for i, x in enumerate(self.questions) if x.open and x.phase == q.phase), None)
        if first_open is None:
            self._park(q)
            return
        self.questions.insert(first_open, q)


def _raise_only(old: dict[str, float], new: dict[str, float]) -> dict[str, float]:
    return {k: max(old.get(k, 0.0), v) for k, v in new.items()}


def attach_slots(q: QuestionState, use_case: str, keys: list[str], source_lang: str, tr=None) -> None:
    """Attach a use case to a question: its required slots become `expect` items, labelled in the
    source language (their identity) with the locale-file label of every other language as a
    translation. `tr(lang)` may return a Translator with Claude's overlay."""
    q.use_case = use_case
    for k in keys:
        label = i18n.t(f"slots.{k}", source_lang) if tr is None else tr(source_lang)(f"slots.{k}")
        if label in q.slot_map:
            continue
        q.expect.setdefault(SLOT_RUNG[k], []).append(label)
        q.slot_map[label] = k
        for code in i18n.available():
            text = i18n.t(f"slots.{k}", code)
            if code != source_lang and text != label:
                q.expect_i18n.setdefault(code, {})[label] = text
    if not q.covered and not q.thread:  # start on the first rung that has something to dig
        q.rung = next((r for r in RUNGS if q.expect.get(r)), "business")


def _question_from_def(qd, theme: str, lang: str, phase: str = P.DEEP_DIVES) -> QuestionState:
    """Seed question: wording in every language given; expect items stored in the questionnaire
    language (their identity), other languages' variants kept as translations."""
    expect = _empty_ladder()
    expect_i18n: dict[str, dict[str, str]] = {}
    for r in RUNGS:
        for item in getattr(qd.expect, r):
            if isinstance(item, str):
                expect[r].append(item)
                continue
            canonical = next((item[c] for c in (*i18n.fallback_chain(lang), "fr") if item.get(c)),
                             next(iter(item.values())))
            expect[r].append(canonical)
            for code, text in item.items():
                if text and text != canonical:
                    expect_i18n.setdefault(code, {})[canonical] = text
    q = QuestionState(id=qd.id, theme=theme, text_fr=qd.fr, text_en=qd.en, texts=dict(qd.text),
                      priority=qd.priority, expect=expect, expect_i18n=expect_i18n, keywords=qd.keywords,
                      phase=phase)
    if qd.use_case:
        attach_slots(q, qd.use_case, qd.required_slots(), lang)
    else:
        q.gathers = qd.gathered_slots()
    return q
