"""Auto-fill of the client sheet: a small web agent that looks a company up from its name.

One `claude -p` run (Sonnet by default: a few searches and page reads, answered in well under a
minute; `--autofill-model` / PACENOTE_AUTOFILL_MODEL), web search and web fetch only, the answer held
to a JSON schema. It returns either several `candidates` when the name is ambiguous (the FDE picks
one, then it runs again for that company), or `fields`: each found field with its value, the URL
of the page that states it and a confidence from 0 to 1, plus the `linked_entities` (brands it
distributes, parent, subsidiaries, the brand's entity in this country…).

The primary fields describe the REGISTERED company (the legal entity: legal name, registration
number, registered address, its own website and LinkedIn). A brand site returned as the company's
website is not an error: it is moved to the linked entity it belongs to (separate_brand_sites),
and the company's own site is looked for among the pages the agent read.

The answer lands in the sheet directly (merge_into_sheet), field by field, with its provenance:
only fields that are empty or were filled by the agent are written; a value the FDE typed
(provenance "manual", or any value without provenance) is never overwritten; a field the FDE cleared
is empty again, so it is nobody's and the next run fills it.

Never invent: a field without a source page, or below MIN_CONFIDENCE, is dropped; URLs that are
not http(s), LinkedIn pages that are not on linkedin.com, are dropped too. Every source page is then
opened once (dead_links): a source that answers 404/410 or does not resolve was probably made up,
so its suggestion is kept only as low confidence, marked "source not found".
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import secrets
import socket
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict

from . import i18n
from .llm import _kill_group, find_claude
from .research import ResearchJob, run_claude_stream

log = logging.getLogger(__name__)

DEFAULT_AUTOFILL_MODEL = "claude-sonnet-5-5"
AUTOFILL_EFFORT = "medium"
AUTOFILL_TIMEOUT = 5 * 60.0
CACHE_SECONDS = 30 * 60.0  # the same name asked again within this window reuses the answer
MAX_RUNNING = 2  # older runs are stopped beyond this (the FDE kept typing)
MAX_JOBS = 30
MIN_CONFIDENCE = 0.3  # below: dropped
LOW_CONFIDENCE = 0.6  # below: shown as "low confidence", left out of "accept all"
MAX_CANDIDATES = 5

# client sheet fields the agent may suggest (ClientInfo attributes, all plain strings)
FIELDS = ("legal_name", "registration_number", "registered_address", "website", "country", "hq_city",
          "sector", "headcount", "description", "products", "leadership", "linkedin_url", "working_languages")
URL_FIELDS = ("website", "linkedin_url")
MAX_LEN = {"description": 600, "products": 400, "leadership": 400}
# how a linked brand / entity relates to the registered company (catalog admin.linked.relation.<x>)
RELATIONS = ("distributed_brand", "brand_entity", "parent", "subsidiary", "sister", "other")
MAX_ENTITIES = 8


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FieldValue(_Strict):
    value: str
    source_url: str
    confidence: float


class Candidate(_Strict):
    name: str
    website: str
    country: str
    description: str


class LinkedEntityOut(_Strict):
    name: str
    relation: str  # one of RELATIONS
    website: str
    linkedin_url: str
    source_url: str
    confidence: float


class FoundFields(_Strict):
    legal_name: FieldValue | None = None
    registration_number: FieldValue | None = None
    registered_address: FieldValue | None = None
    website: FieldValue | None = None
    country: FieldValue | None = None
    hq_city: FieldValue | None = None
    sector: FieldValue | None = None
    headcount: FieldValue | None = None
    description: FieldValue | None = None
    products: FieldValue | None = None
    leadership: FieldValue | None = None
    linkedin_url: FieldValue | None = None
    working_languages: FieldValue | None = None


class AutofillOutput(_Strict):
    status: Literal["fields", "candidates", "not_found"]
    company: str  # the company identified (its usual name), "" when not found or ambiguous
    candidates: list[Candidate]
    fields: FoundFields
    linked_entities: list[LinkedEntityOut]


assert set(FoundFields.model_fields) == set(FIELDS)


AUTOFILL_SYSTEM = """\
You fill in a client sheet for a forward-deployed engineer who is about to meet a company. From \
the company name (and what the engineer already knows), identify the company on the web and \
return public facts about it.

Use web search and web fetch, and be quick: a few searches and three to five pages are usually \
enough. First find the REGISTERED company (the legal entity) in an official registry (e.g. \
Pappers, Societe.com, annuaire-entreprises.data.gouv.fr, Companies House, Handelsregister, \
OpenCorporates): its legal name, registration number and registered address. Then its own \
official website (search for it by name, and by name plus what it does) and its home or about \
page, its own LinkedIn company page, then reputable press.

The registered company vs the brands linked to it: a distributor, importer or holding often has \
its own small site while the brands it distributes have their own sites and LinkedIn pages \
(e.g. a distributor of an oven brand, and that brand's site and LinkedIn page for the country). \
Both matter and neither is a mistake:
- the fields describe the registered company itself: website and linkedin_url are ITS OWN site \
and page. When the site you find belongs to a brand, its parent or another entity, look for the \
company's own site (search its legal name, the registry's "site internet" entry, its LinkedIn page); \
if it has none, leave website out.
- every related brand or entity goes in linked_entities: name, relation (distributed_brand: a \
brand the company distributes or imports; brand_entity: that brand's entity or site for this \
country; parent; subsidiary; sister: same group; other), its website and LinkedIn page when \
found ("" otherwise), the page that shows the link (source_url) and a confidence. Up to 8, the \
most relevant first.

Rules:
- Never invent. Return a field only when a page you found states it, and give that page's URL as \
source_url: the value must be what that very page says, so the engineer can check it in one \
click. Copy that URL exactly as you fetched it or as a search result showed it; never build one \
yourself. Don't merge facts from different pages into one value; when two sources disagree, keep the \
more official one. When you don't find a field, leave it out (null). Never guess a URL.
- confidence (0 to 1): how sure you are that the value is right and about this very company. \
About 0.9 when the company's own site or an official registry states it; about 0.6 for one \
secondary source; lower when you are inferring. Leave out anything below 0.3.
- leadership: only people publicly named as executives (CEO, managing director, founders…) on \
official or reputable sources, as "Name — role; Name — role". No contact details.
- headcount: as published, with its source's wording or range and year when given (e.g. \
"20–49 employees (2023)"). No estimate of your own.
- working_languages: the languages the company works and sells in, from its site and markets.
- registration_number: as the registry gives it, with its kind (e.g. "SIREN 123 456 789", \
"Companies House 01234567", "HRB 12345 München").
- If the name fits several distinct companies and what the engineer knows doesn't settle it, \
return status "candidates" with 2 to 5 candidates (name, website, country, a one-line \
description), and no fields. If one company is clearly meant, don't ask: return its fields.
- If you find no such company, return status "not_found".
- Write the descriptive values (sector, description, products, leadership roles, country, \
hq_city, headcount, working_languages, candidate descriptions) in {lang_name}. Keep company \
names, brand names, people's names and URLs as they are.

Fields:
- legal_name: the registered legal name, with its legal form (e.g. "BRIOCHE & FOUR SAS")
- registration_number: see above
- registered_address: the registered office address, as the registry states it
- website: the registered company's own website URL (https://…)
- country: the country of the head office
- hq_city: the city of the head office
- sector: what they do, in a few words (e.g. "distribution of professional bakery equipment")
- headcount: number of employees
- description: one or two plain sentences on what the company does and for whom
- products: key products, brands or services, comma-separated
- leadership: see above
- linkedin_url: the registered company's own LinkedIn page URL
- working_languages: see above
"""


def build_autofill_prompt(name: str, hints: dict[str, str], candidate: dict | None, wanted: list[str]) -> str:
    known = "\n".join(f"- {k}: {v}" for k, v in hints.items() if v) or "- (nothing yet)"
    lines = [f"Company name: {name}", "", "Already known (typed by the engineer, trust it):", known, ""]
    if candidate:
        lines += ["The engineer picked this company among several matches; fill in the fields for this one "
                  "(do not return candidates):",
                  "\n".join(f"- {k}: {v}" for k, v in candidate.items() if v), ""]
    lines += [f"Fields to find: {', '.join(wanted)}", "", "Look the company up now and return the sheet."]
    return "\n".join(lines)


# --- reading the answer --------------------------------------------------------------------------


def _clean(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _is_url(url: str) -> bool:
    return bool(re.match(r"^https?://[^\s/$.?#][^\s]*$", url, re.I))


def _confidence(value) -> float | None:
    try:
        c = float(value)
    except (TypeError, ValueError):
        return None
    if c != c:  # NaN
        return None
    if 1 < c <= 100:  # a percentage
        c /= 100
    return max(0.0, min(1.0, c))


def parse_output(data: dict) -> dict:
    """The agent's answer, checked field by field (lenient: a bad field is dropped, not the whole
    answer). Returns {"status": "fields"|"candidates"|"not_found", "company", "candidates", "fields"}
    where fields = {name: {"value", "source_url", "confidence", "low"}}."""
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    fields: dict[str, dict] = {}
    raw = data.get("fields") if isinstance(data.get("fields"), dict) else {}
    for name in FIELDS:
        item = raw.get(name)
        if not isinstance(item, dict):
            continue
        value, source, conf = _clean(item.get("value")), _clean(item.get("source_url")), _confidence(item.get("confidence"))
        if not value or conf is None or conf < MIN_CONFIDENCE or not _is_url(source):
            continue  # unknown, unsourced or too unsure: never shown
        if name in URL_FIELDS and not _is_url(value):
            continue
        if name == "linkedin_url" and "linkedin.com/" not in value.lower():
            continue
        value = value[:MAX_LEN.get(name, 200)]
        fields[name] = {"value": value, "source_url": source, "confidence": round(conf, 2),
                        "low": conf < LOW_CONFIDENCE}
    candidates = []
    for c in data.get("candidates") or []:
        if not isinstance(c, dict) or not _clean(c.get("name")):
            continue
        site = _clean(c.get("website"))
        candidates.append({"name": _clean(c.get("name"))[:120], "website": site if _is_url(site) else "",
                           "country": _clean(c.get("country"))[:80], "description": _clean(c.get("description"))[:300]})
    candidates = candidates[:MAX_CANDIDATES]
    entities = []
    for e in data.get("linked_entities") or []:
        if not isinstance(e, dict) or not _clean(e.get("name")):
            continue
        source, conf = _clean(e.get("source_url")), _confidence(e.get("confidence"))
        if conf is None or conf < MIN_CONFIDENCE or not _is_url(source):
            continue  # an unsourced link between two companies is a guess: dropped
        site, li = _clean(e.get("website")), _clean(e.get("linkedin_url"))
        rel = _clean(e.get("relation")).lower().replace(" ", "_").replace("-", "_")
        entities.append({"name": _clean(e.get("name"))[:120], "relation": rel if rel in RELATIONS else "other",
                         "website": site if _is_url(site) else "",
                         "linkedin_url": li if _is_url(li) and "linkedin.com/" in li.lower() else "",
                         "source_url": source, "confidence": round(conf, 2), "low": conf < LOW_CONFIDENCE})
    status = data.get("status")
    company = _clean(data.get("company"))
    if status == "candidates" and candidates and not (len(candidates) == 1 and fields):
        return {"status": "candidates", "company": "", "candidates": candidates, "fields": {}, "linked_entities": []}
    if not fields and not entities:
        return {"status": "not_found", "company": company, "candidates": [], "fields": {}, "linked_entities": []}
    return separate_brand_sites({"status": "fields", "company": company, "candidates": [], "fields": fields,
                                 "linked_entities": _dedupe(entities)[:MAX_ENTITIES]})


# --- the registered company vs its linked brands -----------------------------------------------------

LEGAL_FORMS = {"sas", "sasu", "sarl", "sa", "eurl", "sci", "snc", "scs", "gmbh", "ag", "kg", "ltd", "limited", "inc",
               "llc", "plc", "bv", "nv", "srl", "spa", "sl", "slu", "co", "corp", "company", "cie", "et"}


def host(url: str) -> str:
    try:
        h = (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


def _site_label(h: str) -> str:
    """The name part of a host: for-naxa.fr -> fornaxa, www.brioche-four.co.uk -> briochefour."""
    parts = [p for p in h.split(".") if p]
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "gouv", "ac"):
        main = parts[-3]
    else:
        main = parts[-2] if len(parts) >= 2 else (parts[0] if parts else "")
    return re.sub(r"[^a-z0-9]", "", main)


def _linkedin_slug(url: str) -> str:
    m = re.search(r"linkedin\.com/(?:company|school|showcase)/([^/?#]+)", url, re.I)
    return re.sub(r"[^a-z0-9]", "", fold(urllib.parse.unquote(m.group(1)))) if m else ""


def _compact(name: str) -> str:
    words = re.findall(r"[a-z0-9]+", fold(name))
    return "".join(w for w in words if w not in LEGAL_FORMS)


def _label_of(kind: str, url: str) -> str:
    return _linkedin_slug(url) if kind == "linkedin_url" else _site_label(host(url))


def name_matches(name: str, label: str) -> bool:
    """Whether a site or LinkedIn label is this name's: briochefour ~ "Brioche & Four SAS", fornaxa ~ "Fornaxa"."""
    c = _compact(name)
    return len(label) >= 3 and len(c) >= 3 and (label in c or c in label)


def _entity_key(e: dict) -> str:
    return fold(e.get("name") or "") or host(e.get("website") or "")


def _dedupe(entities: list[dict]) -> list[dict]:
    seen, out = set(), []
    for e in entities:
        k = _entity_key(e)
        if k and k not in seen:
            seen.add(k)
            out.append(e)
    return out


def separate_brand_sites(result: dict) -> dict:
    """A website / LinkedIn page that belongs to a linked brand or entity (the same site, or a site
    named after it and not after the company) is moved to that entity, never discarded; a brand
    named in `products` gets its own entry (distributed_brand). The company's own site or page is
    then looked for among the pages the agent read (a source whose host is named after the
    company); found, it becomes the website, otherwise the field stays empty."""
    fields, entities = result["fields"], result["linked_entities"]
    names = [n for n in (result.get("company"), (fields.get("legal_name") or {}).get("value")) if n]
    brands = [b.strip() for b in re.split(r"[,;/]", (fields.get("products") or {}).get("value", "")) if b.strip()]
    sources = [f["source_url"] for f in fields.values()] + [e["source_url"] for e in entities]
    moved = []
    for kind in ("website", "linkedin_url"):
        f = fields.get(kind)
        if not f or not names:
            continue
        label = _label_of(kind, f["value"])
        if any(name_matches(n, label) for n in names):
            continue  # the company's own
        owner = next((e for e in entities if (e[kind] and _label_of(kind, e[kind]) == label)
                      or name_matches(e["name"], label)), None)
        if owner is None:
            brand = next((b for b in brands if name_matches(b, label)), None)
            if brand is None:
                continue  # nothing says it is someone else's: kept as the company's
            owner = {"name": brand, "relation": "distributed_brand", "website": "", "linkedin_url": "",
                     "source_url": f["source_url"], "confidence": f["confidence"], "low": f["low"]}
            entities.append(owner)
        if not owner[kind]:
            owner[kind] = f["value"]
        del fields[kind]
        moved.append(kind)
        own = next((u for u in sources if _is_url(u) and (kind != "linkedin_url" or _linkedin_slug(u))
                    and any(name_matches(n, _label_of(kind, u)) for n in names)), None)
        if own:
            u = urllib.parse.urlparse(own)
            if kind == "linkedin_url":  # the page itself, not one of its tabs (/about/, /posts/…)
                m = re.search(r"/(company|school|showcase)/([^/?#]+)", u.path)
                value = f"https://www.linkedin.com/{m.group(1)}/{m.group(2)}"
            else:
                value = f"{u.scheme}://{u.hostname}"
            fields[kind] = {"value": value, "source_url": own, "confidence": 0.7, "low": False}
    result["moved"] = moved
    return result


# --- into the sheet ---------------------------------------------------------------------------------


def is_manual(info, name: str) -> bool:
    """A field the FDE owns: marked manual, or holding a value Auto-fill did not put there. An empty
    (or whitespace-only) field is nobody's: a manual value the FDE cleared goes back to the agent."""
    if not _clean(getattr(info, name, "")):
        return False
    prov = (info.provenance or {}).get(name)
    if prov:
        return prov.get("by") == "manual"
    return bool(_clean(getattr(info, name, "")))


def merge_into_sheet(info, result: dict | None):
    """Write the agent's answer into the client sheet: every field that is empty or agent-filled
    takes the new value with its provenance (source, confidence); manual fields are never touched.
    Linked entities are merged by name: manual ones and the ones the FDE removed stay as they are.
    Returns (the new ClientInfo, the fields written)."""
    from .clients import LinkedEntity

    if not result or result.get("status") != "fields":
        return info, []
    update, prov, written = {}, dict(info.provenance or {}), []
    for name, f in result["fields"].items():
        if name not in FIELDS or is_manual(info, name):
            continue
        update[name] = f["value"]
        prov[name] = {"by": "agent", "source_url": f["source_url"], "confidence": f["confidence"]}
        if f.get("low"):
            prov[name]["low"] = True
        if f.get("dead_source"):
            prov[name]["dead_source"] = True
        written.append(name)
    for name in FIELDS:  # a field left empty keeps no provenance: the next run may fill it
        if name not in update and not _clean(getattr(info, name, "")):
            prov.pop(name, None)
    entities = list(info.linked_entities)
    keys = {_entity_key(e.model_dump()): i for i, e in enumerate(entities)}
    removed = set(info.removed_entities or [])
    for e in result.get("linked_entities") or []:
        k = _entity_key(e)
        if not k or k in removed:
            continue
        new = LinkedEntity(**{x: e[x] for x in ("name", "relation", "website", "linkedin_url", "source_url",
                                                "confidence")}, by="agent")
        if k in keys:
            if entities[keys[k]].by == "manual":
                continue
            entities[keys[k]] = new
        else:
            keys[k] = len(entities)
            entities.append(new)
        written.append(f"linked:{k}")
    update |= {"provenance": prov, "linked_entities": entities}
    return info.model_copy(update=update), written


def mark_manual_edits(old, new):
    """A sheet saved from the form: a company field whose value changed while its provenance was
    left as it was is the FDE's edit, so it becomes manual; same for a linked entity. A field the FDE
    cleared (empty or whitespace only) loses its provenance: it is no longer his, the agent refills it.
    (Removing a linked entity with × is different: it stays in removed_entities, never brought back.)"""
    prov = dict(new.provenance or {})
    for name in FIELDS:
        before, after = _clean(getattr(old, name, "")), _clean(getattr(new, name, ""))
        if not after:
            prov.pop(name, None)
        elif before != after and prov.get(name) == (old.provenance or {}).get(name):
            prov[name] = {"by": "manual"}
    olds = {_entity_key(e.model_dump()): e for e in old.linked_entities}
    entities = []
    for e in new.linked_entities:
        o = olds.get(_entity_key(e.model_dump()))
        if o is not None and e.by == o.by == "agent" and e.model_dump() != o.model_dump():
            e = e.model_copy(update={"by": "manual"})
        entities.append(e)
    return new.model_copy(update={"provenance": prov, "linked_entities": entities})


def _private_url(url: str) -> bool:
    """True for a URL the source check must not fetch: not http(s), or a host that resolves to a
    private, loopback, link-local or other non-public address (the URLs come from the model)."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return True
    try:
        infos = socket.getaddrinfo(parts.hostname, None)
    except (OSError, UnicodeError):
        return False  # does not resolve: the fetch reports it as not there, as before
    return any(not ipaddress.ip_address(info[4][0].split("%")[0]).is_global for info in infos)


class _PublicRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to a public address (otherwise the 3xx stands: counted as alive)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _private_url(newurl):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def dead_links(urls: list[str], timeout: float = 6.0, allow_private: bool = False) -> set[str]:
    """The URLs that answer 404/410 or don't resolve at all. Anything else counts as alive: 403, 429,
    999… are anti-bot walls (registries, LinkedIn) in front of real pages. Private, loopback and
    link-local addresses are never fetched (counted as alive) unless `allow_private` (tests)."""
    opener = urllib.request.build_opener() if allow_private else urllib.request.build_opener(_PublicRedirects)

    def dead(url: str) -> bool:
        if not allow_private and _private_url(url):
            return False
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh) pacenote source check"})
        try:
            with opener.open(req, timeout=timeout):
                return False
        except urllib.error.HTTPError as exc:
            return exc.code in (404, 410)
        except urllib.error.URLError as exc:  # DNS failure, refused: a page that isn't there
            return not isinstance(getattr(exc, "reason", None), TimeoutError)
        except Exception:  # timeouts, TLS oddities: not proof of anything
            return False

    urls = sorted(set(urls))
    if not urls:
        return set()
    with ThreadPoolExecutor(max_workers=min(8, len(urls))) as pool:
        return {u for u, d in zip(urls, pool.map(dead, urls)) if d}


def mark_dead_sources(result: dict, dead: set[str]) -> dict:
    """A value whose source page is not there stays, but low confidence, flagged."""
    for f in [*result["fields"].values(), *result.get("linked_entities", [])]:
        if f["source_url"] in dead:
            f |= {"dead_source": True, "low": True, "confidence": min(f["confidence"], round(LOW_CONFIDENCE - 0.01, 2))}
    return result


def fold(name: str) -> str:
    s = unicodedata.normalize("NFKD", name.lower())
    return re.sub(r"\s+", " ", "".join(c for c in s if not unicodedata.combining(c))).strip()


# --- the job ---------------------------------------------------------------------------------------


@dataclass
class AutofillJob(ResearchJob):
    name: str = ""
    key: str = ""
    hints: dict = field(default_factory=dict)
    candidate: dict | None = None
    result: dict | None = None

    def view(self) -> dict:
        v = super().view()
        v.pop("archived", None)
        return v | {"id": self.slug, "name": self.name, "candidate": self.candidate, "result": self.result}


class AutofillManager:
    """Auto-fill runs in background threads, keyed by (name, picked candidate, language): asking
    again for the same company while it runs, or within CACHE_SECONDS after, reuses that run."""

    def __init__(self, claude_bin: str | None = None, model: str = DEFAULT_AUTOFILL_MODEL,
                 effort: str = AUTOFILL_EFFORT, timeout: float = AUTOFILL_TIMEOUT,
                 check_links: Callable[[list[str]], set[str]] | None = None):
        """check_links: returns the dead source URLs (dead_links in the app; None = no check)."""
        self.claude_bin = claude_bin
        self.check_links = check_links
        self.model = model
        self.effort = effort
        self.timeout = timeout
        self.jobs: dict[str, AutofillJob] = {}
        self._lock = threading.Lock()

    @staticmethod
    def job_key(name: str, candidate: dict | None, lang: str) -> str:
        pick = fold((candidate or {}).get("website") or (candidate or {}).get("name") or "")
        return f"{fold(name)}|{pick}|{lang}"

    def get(self, job_id: str) -> AutofillJob | None:
        return self.jobs.get(job_id)

    def start(self, name: str, hints: dict[str, str] | None = None, candidate: dict | None = None,
              lang: str = "fr", force: bool = False) -> AutofillJob:
        name = _clean(name)
        hints = {k: _clean(v) for k, v in (hints or {}).items() if k in FIELDS and _clean(v)}
        key = self.job_key(name, candidate, lang)
        with self._lock:
            for job in self.jobs.values():
                if job.key != key:
                    continue
                fresh = job.status == "done" and time.time() - (job.finished_at or 0) < CACHE_SECONDS
                if job.status == "running" or (fresh and not force):
                    return job
            job = AutofillJob(slug=secrets.token_hex(6), lang=lang, name=name, key=key, hints=hints,
                              candidate=candidate)
            self.jobs[job.slug] = job
            running = [j for j in self.jobs.values() if j.status == "running" and j is not job]
            for old in running[:max(0, len(running) - (MAX_RUNNING - 1))]:
                self._stop(old)
            for old in [j for j in self.jobs.values() if j.status != "running"][:max(0, len(self.jobs) - MAX_JOBS)]:
                self.jobs.pop(old.slug, None)
        threading.Thread(target=self._run, args=(job,), daemon=True, name=f"autofill-{job.slug}").start()
        return job

    def stop(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if job is None or job.status != "running":
            return False
        self._stop(job)
        return True

    @staticmethod
    def _stop(job: AutofillJob) -> None:
        job._stop.set()
        if job._proc is not None:
            _kill_group(job._proc)

    def wait(self, job_id: str, timeout: float = 60.0) -> AutofillJob | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self.jobs.get(job_id)
            if job is None or job.status != "running":
                return job
            time.sleep(0.05)
        return self.jobs.get(job_id)

    def _run(self, job: AutofillJob) -> None:
        try:
            wanted = [f for f in FIELDS if f not in job.hints] or list(FIELDS)
            cmd = [
                find_claude(self.claude_bin), "-p",
                "--output-format", "stream-json", "--verbose", "--include-partial-messages",
                "--model", self.model, "--effort", self.effort,
                "--system-prompt", AUTOFILL_SYSTEM.format(lang_name=i18n.english_name(job.lang)),
                "--tools", "WebSearch,WebFetch",  # web lookup only: no shell, no files
                "--allowedTools", "WebSearch,WebFetch",
                "--strict-mcp-config", "--no-session-persistence",
                "--json-schema", json.dumps(AutofillOutput.model_json_schema(), separators=(",", ":")),
            ]
            job.say("info", "autofillStart", name=job.name, model=self.model)
            data = run_claude_stream(job, cmd, build_autofill_prompt(job.name, job.hints, job.candidate, wanted),
                                     self.timeout, "pacenote-autofill-", writing="autofillWriting")
            result = parse_output(data)
            if self.check_links and result["fields"] and not job._stop.is_set():
                job.say("info", "autofillChecking")
                urls = [f["source_url"] for f in result["fields"].values()] + [e["source_url"] for e in result["linked_entities"]]
                result = mark_dead_sources(result, self.check_links(urls))
            job.result = result
            job.status = "done"
            r = job.result
            if r["status"] == "candidates":
                job.say("done", "autofillCandidates", n=len(r["candidates"]))
            elif r["status"] == "fields":
                job.say("done", "autofillReady", n=len(r["fields"]), linked=len(r["linked_entities"]))
            else:
                job.say("done", "autofillNotFound")
        except Exception as exc:  # the sheet is never touched
            stopped = job._stop.is_set()
            if not stopped:
                log.warning("autofill failed for %r: %s", job.name, exc)
            job.status = "stopped" if stopped else "error"
            job.error = None if stopped else str(exc)[:300]
            if job.error:
                job.step("error", job.error)
            else:
                job.say("info", "stopped")
        finally:
            job.finished_at = time.time()
            job._proc = None
