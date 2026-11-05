"""The one place that knows about languages.

Every screen, prompt and output file goes through this module (Python) or its browser twin
`static/i18n.js` (front end). Nothing else holds translated strings or language rules.

Three languages per session, persisted in the session state and resolved here:
  * ui        - the FDE's screen chrome (buttons, labels). Needs a locale file.
  * interview - what the FDE asks and reads: questions, follow-ups, "Ce qui manque" items,
                summaries, plan notes and the final plan. Any language: questionnaire variants
                are used when present, Claude translates the rest on the fly.
  * speech    - the yap locales listening to the room (e.g. ["de-DE", "en-US"]).

Catalogs
  `pacenote/locales/<lang>.json`, nested JSON in the i18next format (the de-facto web
  standard: `{{name}}` interpolation, dotted keys, `_meta.name`). The same files are served to
  the browser (`/api/i18n/<lang>`), so Python and JS read one source of truth. Adding a
  language = dropping in `es.json`; missing keys fall back (es-MX -> es -> en -> key).
  Babel (the standard Python i18n library) parses locale tags and names languages, so
  `de`, `de-DE` and `de_DE` all work and the switcher can label any language.

  Interview-language strings that have no locale file (e.g. plan headings in German) can be
  supplied at run time as an *overlay* (machine-translated by Claude, see translate.py).
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping

from babel import Locale, UnknownLocaleError
from pydantic import BaseModel, Field, field_validator

LOCALES_DIR = Path(__file__).resolve().parent / "locales"
FALLBACK = "en"  # last resort before the key itself
DEFAULT_SPEECH = ["fr-FR", "en-US"]  # the example client's default; see capture.DEFAULT_LOCALES

# A default yap locale for each language, used when only a language is given (`--locales de`).
DEFAULT_REGION = {
    "fr": "fr-FR", "en": "en-US", "de": "de-DE", "es": "es-ES", "it": "it-IT", "pt": "pt-PT",
    "nl": "nl-NL", "ja": "ja-JP", "zh": "zh-CN", "ko": "ko-KR", "sv": "sv-SE", "da": "da-DK",
    "nb": "nb-NO", "fi": "fi-FI", "pl": "pl-PL", "tr": "tr-TR", "ru": "ru-RU", "ar": "ar-SA",
}
# Offered in the speech/interview pickers (any other valid tag is accepted too).
COMMON_SPEECH = ["fr-FR", "en-US", "en-GB", "de-DE", "es-ES", "it-IT", "pt-PT", "pt-BR", "nl-NL",
                 "fr-CA", "fr-BE", "fr-CH", "de-CH", "de-AT", "es-MX", "ja-JP", "zh-CN"]

_TAG = re.compile(r"^[A-Za-z]{2,3}([-_][A-Za-z0-9]{2,8})*$")
_PARAM = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")


class LanguageError(ValueError):
    pass


# --- tags ---------------------------------------------------------------------------------
def parse(tag: str) -> Locale:
    """Babel Locale for 'de', 'de-DE', 'de_DE' (raises LanguageError)."""
    tag = (tag or "").strip()
    if not _TAG.match(tag):
        raise LanguageError(f"not a language tag: {tag!r}")
    try:
        return Locale.parse(tag.replace("-", "_"))
    except (UnknownLocaleError, ValueError) as exc:
        raise LanguageError(f"unknown language {tag!r}") from exc


def is_language(tag: str) -> bool:
    try:
        parse(tag)
        return True
    except LanguageError:
        return False


def language_of(tag: str) -> str:
    """'de-DE' -> 'de' (lower-case ISO 639 code)."""
    return parse(tag).language


def speech_locale(tag: str) -> str:
    """Canonical yap locale 'xx-YY' for 'de', 'de_de', 'de-DE'."""
    loc = parse(tag)
    if loc.territory:
        return f"{loc.language}-{loc.territory}"
    return DEFAULT_REGION.get(loc.language) or f"{loc.language}-{loc.language.upper()}"


def name(code: str, in_lang: str | None = None) -> str:
    """Display name of a language/locale: name('de', 'fr') == 'allemand',
    name('de') == 'Deutsch' (its own name), name('de-DE', 'en') == 'German (Germany)'."""
    try:
        loc = parse(code)
        target = parse(in_lang) if in_lang else loc
    except LanguageError:
        return code
    return loc.get_display_name(target) or code


def english_name(code: str) -> str:
    """For prompts: 'German', 'French' (no region)."""
    try:
        return parse(code).get_language_name("en") or code
    except LanguageError:
        return code


def fallback_chain(lang: str | None, default: str | None = None) -> list[str]:
    """'pt-BR' -> ['pt-BR', 'pt', <default>, 'en'] without duplicates."""
    out: list[str] = []
    for c in (lang, _base(lang), default, _base(default), FALLBACK):
        if c and c not in out:
            out.append(c)
    return out


def _base(tag: str | None) -> str | None:
    if not tag:
        return None
    return re.split(r"[-_]", tag)[0].lower()


# --- catalogs -----------------------------------------------------------------------------
def available(locales_dir: Path | None = None) -> list[str]:
    """Languages with a locale file, e.g. ['en', 'fr']."""
    d = Path(locales_dir or LOCALES_DIR)
    return sorted(p.stem for p in d.glob("*.json") if is_language(p.stem))


@lru_cache(maxsize=64)
def _load(path: str, mtime: float) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise LanguageError(f"{path}: a locale file must be a JSON object")
    return data


def catalog(lang: str, locales_dir: Path | None = None) -> dict:
    """The raw nested catalog of one locale file ({} when there is none)."""
    p = Path(locales_dir or LOCALES_DIR) / f"{lang}.json"
    if not p.exists():
        return {}
    return _load(str(p), p.stat().st_mtime)


def flatten(tree: Mapping, prefix: str = "") -> dict[str, Any]:
    """{'a': {'b': 'x'}} -> {'a.b': 'x'} (lists and other leaves are kept as values)."""
    out: dict[str, Any] = {}
    for k, v in tree.items():
        key = f"{prefix}{k}"
        if isinstance(v, Mapping):
            out.update(flatten(v, key + "."))
        else:
            out[key] = v
    return out


def unflatten(flat: Mapping[str, Any]) -> dict:
    out: dict = {}
    for key, v in flat.items():
        node = out
        *parents, leaf = key.split(".")
        for p in parents:
            node = node.setdefault(p, {})
        node[leaf] = v
    return out


def merged(lang: str, locales_dir: Path | None = None, overlay: Mapping[str, str] | None = None,
           default: str | None = None) -> dict[str, Any]:
    """Flat catalog for `lang` with every fallback filled in (overlay wins over files of the
    fallback languages, never over the language's own file)."""
    flat: dict[str, Any] = {}
    for code in reversed(fallback_chain(lang, default)):
        flat.update(flatten(catalog(code, locales_dir)))
        if code == lang and overlay:
            # machine-translated strings only fill gaps left by the language's own file
            own = flatten(catalog(lang, locales_dir))
            flat.update({k: v for k, v in overlay.items() if k not in own})
    return flat


def missing_keys(lang: str, prefixes: Iterable[str] = ("",), locales_dir: Path | None = None) -> dict[str, str]:
    """Keys (under `prefixes`) with no string in `lang` or its base language, with the English
    source text: what Claude has to translate for an interview language without a locale file."""
    have: dict[str, Any] = {}
    for code in (_base(lang), lang):
        if code:
            have.update(flatten(catalog(code, locales_dir)))
    src = flatten(catalog(FALLBACK, locales_dir))
    return {k: v for k, v in src.items()
            if isinstance(v, str) and any(k.startswith(p) for p in prefixes) and k not in have}


def interpolate(text: str, params: Mapping[str, Any] | None = None) -> str:
    if not params:
        return text
    return _PARAM.sub(lambda m: str(params.get(m.group(1), m.group(0))), text)


def t(key: str, lang: str | None = None, params: Mapping[str, Any] | None = None, *,
      overlay: Mapping[str, str] | None = None, locales_dir: Path | None = None, **kw: Any) -> Any:
    """Translate `key` in `lang` with fallback; `{{x}}` placeholders from params/kwargs.
    Returns the key itself when no catalog has it (visible, never a crash)."""
    lang = lang or FALLBACK
    if overlay and key in overlay and key not in flatten(catalog(lang, locales_dir)):
        value: Any = overlay[key]
    else:
        value = None
        for code in fallback_chain(lang):
            v = flatten(catalog(code, locales_dir)).get(key)
            if v is not None:
                value = v
                break
    if value is None:
        return key
    if isinstance(value, str):
        return interpolate(value, {**(params or {}), **kw})
    return value


class Translator:
    """t() bound to one language (and its overlay): `tr = Translator('de'); tr('plan.title')`."""

    def __init__(self, lang: str, overlay: Mapping[str, str] | None = None, locales_dir: Path | None = None):
        self.lang = lang
        self.overlay = dict(overlay or {})
        self.locales_dir = locales_dir

    def __call__(self, key: str, params: Mapping[str, Any] | None = None, **kw: Any) -> Any:
        return t(key, self.lang, params, overlay=self.overlay, locales_dir=self.locales_dir, **kw)


# --- the session's language state ------------------------------------------------------------
def resolve_ui(requested: str | None, *candidates: str | None, locales_dir: Path | None = None) -> str:
    """Best UI language that has a locale file: requested, then candidates (client language…),
    each with its base language, then English."""
    have = available(locales_dir)
    for c in (requested, *candidates):
        for code in (c, _base(c)):
            if code and code in have:
                return code
    return FALLBACK if FALLBACK in have else (have[0] if have else FALLBACK)


def normalize_interview(tag: str) -> str:
    """Interview language code: 'de-DE' -> 'de' (regional variants of the text are not kept apart)."""
    return language_of(tag)


def normalize_speech(locales: Iterable[str] | str) -> list[str]:
    """['de', 'en_us'] or 'de-DE,en-US' -> ['de-DE', 'en-US'] (order kept, duplicates dropped)."""
    if isinstance(locales, str):
        locales = [x for x in re.split(r"[,\s]+", locales) if x]
    out: list[str] = []
    for tag in locales:
        loc = speech_locale(tag)
        if loc not in out:
            out.append(loc)
    if not out:
        raise LanguageError("pick at least one spoken language")
    return out


def stream_ids(locales: Iterable[str]) -> dict[str, str]:
    """yap stream id per locale: the language code ('de-DE' -> 'de'); the full locale in lower
    case when two locales share a language ('en-us', 'en-gb')."""
    locales = list(locales)
    langs = [language_of(x) for x in locales]
    return {(lang if langs.count(lang) == 1 else loc.lower()): loc for lang, loc in zip(langs, locales)}


class LanguageState(BaseModel):
    """The session's current languages (persisted inside SessionState as `languages`)."""

    ui: str = "fr"
    interview: str = "fr"
    speech: list[str] = Field(default_factory=lambda: list(DEFAULT_SPEECH))

    @field_validator("interview")
    @classmethod
    def _interview(cls, v: str) -> str:
        return normalize_interview(v)

    @field_validator("speech")
    @classmethod
    def _speech(cls, v: list[str]) -> list[str]:
        return normalize_speech(v)

    @classmethod
    def for_client(cls, language: str, speech: Iterable[str] | None = None, ui: str | None = None) -> "LanguageState":
        """Defaults for a new session: interview and screen in the client's language."""
        return cls(ui=resolve_ui(ui, language), interview=language, speech=list(speech or DEFAULT_SPEECH))

    def stream_ids(self) -> dict[str, str]:
        return stream_ids(self.speech)


def secondary(primary: str, ui: str | None = None) -> str:
    """A second language next to `primary`: `ui` when it differs, else English (French for
    English). Used for the default spoken languages; the screens show ONE language at a time."""
    if ui and _base(ui) != _base(primary):
        return ui
    return "fr" if _base(primary) == "en" else "en"


def describe(lang: str, ui: str | None = None) -> dict[str, str]:
    """{code, name (in the UI language), native (its own name)} for pickers."""
    return {"code": lang, "name": name(lang, ui or lang), "native": name(lang)}


def languages_payload(ui: str | None = None, locales_dir: Path | None = None) -> dict:
    """What `/api/i18n` serves: UI languages with a locale file, the fallback, and speech options."""
    return {
        "languages": [describe(c, ui) | {"label": str(catalog(c, locales_dir).get("_meta", {}).get("name") or name(c))}
                      for c in available(locales_dir)],
        "fallback": FALLBACK,
        "speech": COMMON_SPEECH,
    }


# --- web: the catalogs for the browser helper (static/i18n.js) -------------------------------
def api_router(locales_dir: Path | None = None):
    """FastAPI router serving the catalogs to `static/i18n.js`. Any FastAPI app (the interview
    screen, an admin panel…) plugs in with `app.include_router(i18n.api_router())`.

    GET /api/i18n              -> {languages: [{code, name, native, label}], fallback, speech}
    GET /api/i18n/{lang}       -> the flat catalog for `lang`, fallbacks filled in
    """
    from fastapi import APIRouter, HTTPException

    router = APIRouter()

    @router.get("/api/i18n")
    async def languages(ui: str | None = None) -> dict:
        return languages_payload(ui, locales_dir)

    @router.get("/api/i18n/{lang}")
    async def catalog_for(lang: str) -> dict:
        if not is_language(lang):
            raise HTTPException(status_code=404, detail=f"unknown language {lang!r}")
        return {"lang": lang, "has_file": lang in available(locales_dir) or _base(lang) in available(locales_dir),
                "catalog": {k: v for k, v in merged(lang, locales_dir).items() if not k.startswith("_meta.")}}

    return router
