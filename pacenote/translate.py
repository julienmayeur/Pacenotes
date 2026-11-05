"""On-the-fly translation of the questionnaire into the interview language.

When the FDE picks an interview language that the questionnaire has no variant for (e.g. `de`
for a questionnaire written in fr/en), everything the FDE reads or Claude copies verbatim is
translated once, in a few parallel `claude -p` calls on the fast model (Claude Code CLI, subscription):
question wordings, `expect` items ("Ce qui manque"), theme titles, the consent line, and the
interview-language strings of the locale catalog (plan headings, end banner) when there is no
locale file for that language. Results are stored in the session state and cached on disk per
source text, so the next session in that language starts translated.

The same path serves the FDE's display language (the screen-language selector, top right): when
it differs from the interview language, the questionnaire texts AND the follow-ups generated
live are translated for display only (`f|` items, stored in `ThreadEntry.texts`). The follow-ups
are still generated, prompted and written to the plan in the interview language.

Variants written in questionnaire.yaml always win; translations only fill the gaps. Until the
translation lands (or if it fails, or the engine cannot translate) the screen shows the closest
fallback (English, then French).
"""

from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from . import fsutil, i18n
from .state import SessionState

CHUNK = 30  # texts per `claude -p` call
PARALLEL = 4  # calls at once (~116 texts for the example client: ~8 s instead of ~30 s in one call)

# Catalog namespaces shown in the interview language (CEO-facing or in the plan).
INTERVIEW_NAMESPACES = ("interview.", "plan.", "offline.")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TranslatedItem(_Strict):
    id: str
    text: str


class TranslationResult(_Strict):
    items: list[TranslatedItem]


TRANSLATE_SYSTEM = """\
You translate short interface and interview texts for a forward-deployed engineer who is about \
to interview the CEO of {client}. Translate every item into {lang_name}. Keep the meaning, the \
tone (spoken, plain, direct: these are questions read aloud to a CEO) and the length. Address \
the CEO formally (vous, Sie, usted, Lei, u), as in a first business meeting. Keep \
product names, company names, acronyms (ERP, CRM, EDI…) and placeholders like {{{{stamp}}}} or \
{{{{item}}}} exactly as they are. Use the client glossary for names:
{glossary}

Return one entry per input id, with the same id, and nothing else.
"""


def missing_items(state: SessionState, lang: str) -> list[dict]:
    """What has no variant in `lang` yet: [{id, from, text}]."""
    items: list[dict] = []
    src = state.source_language or "fr"
    for q in state.questions:
        if q.status == "removed":
            continue
        if not q.has_text(lang):
            items.append({"id": f"q|{q.id}", "from": src, "text": q.text(src)})
        done = q.expect_i18n.get(lang, {})
        for e in q.all_expect():
            if lang != src and e not in done:
                items.append({"id": f"e|{q.id}|{e}", "from": src, "text": e})
        for e in q.thread:  # follow-ups generated in another language (display only; keyed by
            gen = e.lang or state.language  # time: the thread is trimmed from the front)
            if e.kind == "followup" and gen != lang and not e.texts.get(lang):
                items.append({"id": f"f|{q.id}|{e.at!r}", "from": gen, "text": e.text})
    for t in state.themes:
        if not ({**t.titles, "fr": t.title_fr, "en": t.title_en}).get(lang):
            items.append({"id": f"t|{t.id}", "from": src, "text": t.title(src)})
    if not ({**state.consent, "fr": state.consent_fr, "en": state.consent_en}).get(lang):
        items.append({"id": "consent", "from": src, "text": state.consent_text(src)})
    overlay = state.i18n_overlay.get(lang, {})
    for key, text in i18n.missing_keys(lang, INTERVIEW_NAMESPACES).items():
        if key not in overlay:
            items.append({"id": f"k|{key}", "from": i18n.FALLBACK, "text": text})
    return items


def apply(state: SessionState, lang: str, translated: dict[str, str]) -> int:
    """Store translations (by item id) in the state. Returns how many were applied."""
    n = 0
    for iid, text in translated.items():
        text = (text or "").strip()
        if not text:
            continue
        kind, _, rest = iid.partition("|")
        if kind == "q":
            q = state.get(rest)
            if q is not None and not q.has_text(lang):
                q.set_text(lang, text, machine=True)
                n += 1
        elif kind == "e":
            qid, _, item = rest.partition("|")
            q = state.get(qid)
            if q is not None and item in q.all_expect():
                q.expect_i18n.setdefault(lang, {})[item] = text
                n += 1
        elif kind == "f":
            qid, _, at = rest.partition("|")
            q = state.get(qid)
            entry = next((e for e in (q.thread if q is not None else []) if e.kind == "followup" and repr(e.at) == at), None)
            if entry is not None:
                entry.texts[lang] = text
                n += 1
        elif kind == "t":
            th = next((t for t in state.themes if t.id == rest), None)
            if th is not None:
                th.titles[lang] = text
                n += 1
        elif kind == "consent":
            state.consent[lang] = text
            n += 1
        elif kind == "k":
            state.i18n_overlay.setdefault(lang, {})[rest] = text
            n += 1
    return n


def build_prompt(state: SessionState, lang: str, items: list[dict], glossary: str) -> tuple[str, str]:
    system = TRANSLATE_SYSTEM.format(client=state.client, lang_name=i18n.english_name(lang),
                                     glossary=glossary.strip() or "(none)")
    payload = [{"id": i["id"], "from": i18n.english_name(i["from"]), "text": i["text"]} for i in items]
    user = (f"Translate into {i18n.english_name(lang)} ({lang}).\n\n<items>\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=1)}\n</items>")
    return system, user


# --- cache -------------------------------------------------------------------------------
def _key(lang: str, item: dict) -> str:
    return hashlib.sha1(f"{item['from']}>{lang}\0{item['text']}".encode()).hexdigest()


def load_cache(path: Path | None) -> dict[str, str]:
    if path is None or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


_CACHE_LOCK = threading.Lock()  # the interview and display translations share one cache file


def merge_cache(path: Path | None, new: dict[str, str]) -> None:
    """Add `new` to the cache on disk: re-read, merge and save under one lock, so two translations
    finishing at once never drop each other's entries."""
    if path is None or not new:
        return
    with _CACHE_LOCK:
        save_cache(path, load_cache(path) | new)


def save_cache(path: Path | None, cache: dict[str, str]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fsutil.atomic_write_text(path, json.dumps(cache, ensure_ascii=False, indent=1))


def fetch(state: SessionState, lang: str, engine, glossary: str = "", cache_path: Path | None = None) -> dict[str, str]:
    """Translations {item id: text} for everything missing in `lang`: from the cache, then one
    engine.translate call for the rest. Only reads `state` (safe on a copy in a worker thread).
    Raises SyncFailed / ContractError from the engine, NotImplementedError when the engine
    cannot translate (offline) and the cache does not cover everything."""
    items = missing_items(state, lang)
    if not items:
        return {}
    cache = load_cache(cache_path)
    found = {i["id"]: cache[_key(lang, i)] for i in items if _key(lang, i) in cache}
    todo = [i for i in items if i["id"] not in found]
    if not todo:
        return found
    fn = getattr(engine, "translate", None)
    if not callable(fn):
        err = NotImplementedError(f"the {getattr(engine, 'name', '?')} engine cannot translate")
        err.partial = found  # type: ignore[attr-defined]
        raise err
    schema = TranslationResult.model_json_schema()
    by_id = {i["id"]: i for i in todo}

    def one(chunk: list[dict]) -> TranslationResult:
        system, user = build_prompt(state, lang, chunk, glossary)
        return TranslationResult.model_validate(fn(system, user, schema=schema))

    # current question first, then the rest; chunks run in parallel (`claude -p` each)
    focus = state.focus_id or ""
    todo.sort(key=lambda i: 0 if i["id"].split("|")[1:2] == [focus] else 1)
    chunks = [todo[i:i + CHUNK] for i in range(0, len(todo), CHUNK)]
    with ThreadPoolExecutor(max_workers=min(PARALLEL, len(chunks))) as pool:
        results = list(pool.map(one, chunks))
    new: dict[str, str] = {}
    for parsed in results:
        for it in parsed.items:
            if it.id in by_id and it.text.strip():
                found[it.id] = it.text.strip()
                new[_key(lang, by_id[it.id])] = it.text.strip()
    merge_cache(cache_path, new)
    return found


def translate_state(state: SessionState, lang: str, engine, glossary: str = "",
                    cache_path: Path | None = None) -> int:
    """fetch() + apply() in one go (blocking). Returns the number of texts applied."""
    return apply(state, lang, fetch(state, lang, engine, glossary, cache_path))
