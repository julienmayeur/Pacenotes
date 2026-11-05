"""Interviewee research: a small web agent builds a profile card for each person we meet.

For a contact of the client sheet ("Who we meet": name + role) and the company, one `claude -p`
run (Sonnet by default, like Auto-fill: a dozen searches and page reads, answered in about a
minute; `--people-model` / PACENOTE_PEOPLE_MODEL), web search and web fetch only, the answer held to a
JSON schema, no API key in its environment, stoppable. It returns either several `candidates`
when the name fits several people (the FDE picks one, then it runs again for that person), or a
list of `facts`: current role and tenure, career, education, public talks / posts / interviews /
articles, interests shared in a professional context, languages, location (city or region only),
mutual context with the engineer's own company (only when PACENOTE_ENGINEER_CONTEXT describes it), and
icebreakers grounded in those facts. Every fact carries the page that states it and a confidence;
nothing unsourced is kept.

PRIVACY: professional and publicly self-published information only. The prompt says so, and a
deterministic filter (privacy_filter) drops, whatever the model returned, every fact touching
family, health, religion, politics, sexual orientation, ethnicity, personal finances or legal
matters, birth dates, home addresses, contact details, anything finer than a city or region,
and facts taken from personal social networks or login walls.

The card is saved straight away (no accept step) in clients/<slug>/people/<contact>.json (not in
git, like the research output), per fact with its provenance: a re-run replaces the agent's facts
only; facts the FDE edited (manual) stay, and facts the FDE removed don't come back.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict

from . import fsutil, i18n
from .llm import _kill_group, find_claude
from .research import ResearchJob, run_claude_stream

log = logging.getLogger(__name__)

# Sonnet: the job is a dozen searches and careful reading of sources, not deep reasoning; it
# answers in about a minute where Opus takes several (same trade-off as Auto-fill).
DEFAULT_PEOPLE_MODEL = "claude-sonnet-5-5"
PEOPLE_EFFORT = "medium"
PEOPLE_TIMEOUT = 6 * 60.0
CACHE_SECONDS = 30 * 60.0
MAX_RUNNING = 2
MAX_JOBS = 30
MIN_CONFIDENCE = 0.3
LOW_CONFIDENCE = 0.6
MAX_CANDIDATES = 5
MAX_FACTS = 40
MAX_LEN = 400

CATEGORIES = ("role", "tenure", "career", "education", "publication", "interest", "language", "location",
              "mutual_context", "icebreaker")
SINGLE = ("role", "tenure", "location")  # one value each

# Who the engineer is (their company, what it does), for the mutual_context facts. Optional: set
# PACENOTE_ENGINEER_CONTEXT (or pass engineer_context=) to e.g. "Acme Labs (acme.example), a software
# company that sends forward-deployed engineers to its clients". Unset, no mutual context is sought.
ENGINEER_CONTEXT_ENV = "PACENOTE_ENGINEER_CONTEXT"


def mutual_context_rule(engineer_context: str) -> str:
    """The mutual_context line of the system prompt: what to look for, or to leave the category out."""
    ctx = " ".join((engineer_context or "").split())
    if not ctx:
        return "- mutual_context: leave this category out (the engineer's own context was not given)."
    return (f"- mutual_context: what they publicly share with the engineer's side ({ctx}) — industries, tools, "
            "communities, events, schools or public connections in common. Only from sources; nothing generic.")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Fact(_Strict):
    category: Literal["role", "tenure", "career", "education", "publication", "interest", "language", "location",
                      "mutual_context", "icebreaker"]
    value: str
    source_url: str
    confidence: float


class PersonCandidate(_Strict):
    name: str
    headline: str
    company: str
    location: str
    url: str


class PersonOutput(_Strict):
    status: Literal["profile", "candidates", "not_found"]
    person: str  # the person identified (name as published), "" when ambiguous or not found
    candidates: list[PersonCandidate]
    facts: list[Fact]


assert set(Fact.model_fields["category"].annotation.__args__) == set(CATEGORIES)


PEOPLE_SYSTEM = """\
You prepare a forward-deployed engineer for a meeting. From a person's name, role and company, \
find who this person is professionally, from public sources, and return a short profile card.

Use web search and web fetch, and be quick: about five to ten searches and a few pages. Look for: \
the company's own site (team, about, press pages), official registries listing company officers, \
the person's public LinkedIn profile as shown in search results, interviews, talks, podcasts, \
articles, press releases, trade press, public posts they published themselves.

PRIVACY — mandatory, no exception:
- Professional and publicly self-published information only.
- Never collect, even if you see it: home address, family members, marital status, children, \
health, religion, political views or affiliation, sexual orientation, ethnicity or origin, \
personal finances (salary, wealth, debts), personal legal matters (lawsuits, convictions), \
date of birth, personal phone numbers or emails.
- Location: city or region only, never an address or a neighbourhood.
- Never log in, never try to get past a login wall (LinkedIn included): use only what is publicly \
visible or shown in search results. No personal social networks (Facebook, Instagram, TikTok).

Rules:
- Never invent. Return a fact only when a page you found states it, with that page's URL as \
source_url, copied exactly as fetched or as a search result showed it. Unknown: leave it out.
- confidence (0 to 1): how sure you are that the fact is right AND about this very person. About \
0.9 for the company's own site, a registry or the person's own profile; about 0.6 for one \
secondary source; lower when inferring. Leave out anything below 0.3.
- If the name fits several people and the company and role don't settle it, return status \
"candidates" with 2 to 5 candidates (name, headline, company, location at city or country level, \
the public page URL) and no facts. If one person is clearly meant, don't ask: return the facts.
- If you find nothing about this person, return status "not_found".
- Write the values in {lang_name}. Keep names, titles of talks or articles and URLs as they are.

Facts (category: what goes in value):
- role: the current job title and company. One.
- tenure: since when in this role or company (e.g. "since 2014", "about 10 years"). One.
- career: one previous company and role each, with years when given.
- education: one school or degree each, with years when given.
- publication: one public talk, post, interview, podcast or article each: its title or topic, \
and the link to it as source_url.
- interest: a professional interest they share publicly (topics they post or speak about).
- language: a language they speak, when stated publicly.
- location: the city or region where they work. One.
{mutual_context}
- icebreaker: a short opening line for the meeting, grounded in one of the facts above; its \
source_url is that fact's source. Two or three at most.
"""


def build_people_prompt(name: str, role: str, company: dict[str, str], candidate: dict | None,
                        known: list[dict]) -> str:
    lines = [f"Person: {name}", f"Role (as the engineer noted it): {role or '(not given)'}", "", "Company:"]
    lines += [f"- {k}: {v}" for k, v in company.items() if v] or ["- (not given)"]
    if candidate:
        lines += ["", "The engineer picked this person among several matches; return the facts for this one "
                  "(do not return candidates):", "\n".join(f"- {k}: {v}" for k, v in candidate.items() if v)]
    if known:
        lines += ["", "Already on the card (checked by the engineer, trust it, no need to search again):",
                  "\n".join(f"- {f['category']}: {f['value']}" for f in known)]
    lines += ["", "Look this person up now and return the profile card."]
    return "\n".join(lines)


# --- privacy: a deterministic filter on whatever the model returned -------------------------------


def fold(text: str) -> str:
    s = unicodedata.normalize("NFKD", str(text or "").lower())
    return re.sub(r"\s+", " ", "".join(c for c in s if not unicodedata.combining(c))).strip()


# accent-folded, lower-case patterns of sensitive personal data (EN + FR), matched on the value
SENSITIVE = {
    "family": r"\b(epouse|epoux|(son|un|her) mari|(est|etait|s'est) mariee?|mariee? (a|avec)|married|marriage|mariage|wife|"
              r"husband|spouse|conjoint|conjointe|compagne|compagnon|fiance|fiancee|divorc\w*|veuf|veuve|widow\w*|enfants?|fils|filles?|"
              r"daughters?|kids?|children|childless|petits?-enfants|grandchildren|bebe|baby|enceinte|pregnan\w*|"
              r"maternite|paternite|maternity|paternity|pere de|mere de|father of|mother of|parent of|"
              r"(his|her|their|my|sa|son|leur) (famille|family|sons?|soeurs?|freres?|sisters?|brothers?|parents))\b",
    "health": r"\b(maladie|malade|illness|ill with|disease|cancer|diagnosed|atteinte? d'une?|handicap\w*|disabilit\w*|"
              r"disabled|burn-?out|depression|anxiety|anxiete|hospitalise\w*|hospitalized|chirurgie|surgery|therapy|therapie|"
              r"sante mentale|mental health|(his|her|sa) (health|sante)|addiction|rehab|sobriety|sobre depuis)\b",
    "religion": r"\b(religio\w*|catholi\w*|protestant\w*|musulman\w*|muslim|islam\w*|juif|juive|jewish|judai\w*|"
                r"chretien\w*|christian\w*|evangeli\w*|bouddhis\w*|buddhis\w*|hindou\w*|hindu\w*|athee|atheist|"
                r"eglise|church|mosquee|mosque|synagogue|paroisse|parish|croyant|believer|faith)\b",
    "politics": r"\b(parti politique|political party|politician|homme politique|femme politique|"
                r"engagement politique|political (views?|opinions?|affiliation|activis\w*)|opinions? politiques?|"
                r"militant\w*|activist\w*|syndical\w*|syndicaliste|trade union|union member|vot(e|es|ed|ing) for|"
                r"a vote pour|electeur|conseill(er|ere) municipal\w*|maire de|mayor of|"
                r"candidat\w* aux (elections|municipales|legislatives)|left-wing|right-wing|de gauche|de droite|"
                r"democrats?|democratic party|republicans?|republican party|conservative party|labour party|en marche|rassemblement national|"
                r"france insoumise|les republicains|parti socialiste)\b",
    "sexuality": r"\b(sexual\w*|sexuel\w*|gay|lesbi\w*|homosexu\w*|bisexu\w*|lgbt\w*|transgen\w*|queer|"
                 r"coming out)\b",
    "ethnicity": r"\b(ethni\w*|racial|d'origine|of \w+ (origin|descent)|descent|immigr\w*|"
                 r"nationalite d'origine)\b",
    "finance": r"\b(salaire|salary|remuneration|compensation package|patrimoine|net worth|fortune personnelle|"
               r"personal wealth|dettes? personnelles?|personal debts?|faillite personnelle|personal bankruptcy|surendett\w*)\b",
    "legal": r"\b(condamn\w*|convicted|criminal|casier judiciaire|proces|lawsuit|sued|arrest\w*|"
             r"garde a vue|mis en examen|indicted|jail|prison)\b",
    "birth": r"\b(nee? le \d|born on|date de naissance|date of birth|birthday|"
             r"nee? en \d{4}|born in \d{4})\b",
    "home": r"\b(domicile|home address|adresse personnelle|lives at|habite au|vit au|residence principale)\b",
}
SENSITIVE_RE = {k: re.compile(v) for k, v in SENSITIVE.items()}
STREET = re.compile(r"\b\d{1,4}\s*(bis|ter)?,?\s+(rue|avenue|av\.?|bd|boulevard|chemin|allee|impasse|place|quai|route|"
                    r"cours|street|st\.?|road|rd\.?|lane|drive|way|strasse|via|calle)\b")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE = re.compile(r"(\+\d[\d .-]{7,}\d|\b0\d([ .-]?\d{2}){4}\b)")
POSTCODE = re.compile(r"\b\d{4,5}\b")
PERSONAL_NETWORKS = ("facebook.com", "instagram.com", "tiktok.com", "snapchat.com", "copainsdavant", "fb.com")
LOGIN_WALL = re.compile(r"authwall|/login|/signin|/sign-in|/uas/|/checkpoint/")


def sensitive_reason(fact: dict) -> str | None:
    """Why a fact must not be kept (None: it may be kept)."""
    value = fold(fact.get("value", ""))
    for kind, rx in SENSITIVE_RE.items():
        if rx.search(value):
            return kind
    if EMAIL.search(value) or PHONE.search(value):
        return "contact"
    if STREET.search(value):
        return "address"
    if fact.get("category") == "location" and (POSTCODE.search(value) or re.search(r"\d", value)):
        return "address"  # city or region only
    source = str(fact.get("source_url") or "").lower()
    if any(n in source for n in PERSONAL_NETWORKS):
        return "personal_network"
    if LOGIN_WALL.search(source):
        return "login_wall"
    return None


def privacy_filter(facts: list[dict]) -> tuple[list[dict], dict[str, int]]:
    """The facts that may be kept, and how many were dropped per reason."""
    kept, dropped = [], {}
    for f in facts:
        why = sensitive_reason(f)
        if why:
            dropped[why] = dropped.get(why, 0) + 1
        else:
            kept.append(f)
    return kept, dropped


# --- reading the answer ----------------------------------------------------------------------------


def _clean(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _is_url(url: str) -> bool:
    return bool(re.match(r"^https?://[^\s/$.?#][^\s]*$", url, re.I))


def _confidence(value) -> float | None:
    try:
        c = float(value)
    except (TypeError, ValueError):
        return None
    if c != c:
        return None
    if 1 < c <= 100:
        c /= 100
    return max(0.0, min(1.0, c))


def fact_id(category: str, value: str) -> str:
    return hashlib.sha1(f"{category}|{fold(value)}".encode()).hexdigest()[:10]


def parse_output(data: dict) -> dict:
    """The agent's answer checked fact by fact (a bad fact is dropped, not the answer), then the
    privacy filter. Returns {"status", "person", "candidates", "facts", "dropped"}."""
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    checked = []
    for f in data.get("facts") or []:
        if not isinstance(f, dict):
            continue
        cat, value = _clean(f.get("category")).lower(), _clean(f.get("value"))[:MAX_LEN]
        source, conf = _clean(f.get("source_url")), _confidence(f.get("confidence"))
        if cat not in CATEGORIES or not value or conf is None or conf < MIN_CONFIDENCE or not _is_url(source):
            continue  # unknown category, unsourced or too unsure: never shown
        checked.append({"id": fact_id(cat, value), "category": cat, "value": value, "source_url": source,
                        "confidence": round(conf, 2), "low": conf < LOW_CONFIDENCE, "by": "agent"})
    checked, dropped = privacy_filter(checked)  # first: a private value never takes a slot
    facts, seen, single = [], set(), set()
    for f in checked:
        if f["id"] in seen or (f["category"] in SINGLE and f["category"] in single):
            continue
        seen.add(f["id"])
        if f["category"] in SINGLE:
            single.add(f["category"])
        facts.append(f)
    facts = facts[:MAX_FACTS]
    candidates = []
    for c in data.get("candidates") or []:
        if not isinstance(c, dict) or not _clean(c.get("name")):
            continue
        url = _clean(c.get("url"))
        cand = {"name": _clean(c.get("name"))[:120], "headline": _clean(c.get("headline"))[:200],
                "company": _clean(c.get("company"))[:120], "location": _clean(c.get("location"))[:80],
                "url": url if _is_url(url) and not any(n in url.lower() for n in PERSONAL_NETWORKS) else ""}
        if sensitive_reason({"category": "location", "value": cand["location"]}):
            cand["location"] = ""
        candidates.append(cand)
    candidates = candidates[:MAX_CANDIDATES]
    person = _clean(data.get("person"))[:120]
    if data.get("status") == "candidates" and len(candidates) >= 2:
        return {"status": "candidates", "person": "", "candidates": candidates, "facts": [], "dropped": dropped}
    if not facts:
        return {"status": "not_found", "person": person, "candidates": [], "facts": [], "dropped": dropped}
    return {"status": "profile", "person": person, "candidates": [], "facts": facts, "dropped": dropped}


def mark_dead_sources(result: dict, dead: set[str]) -> dict:
    for f in result["facts"]:
        if f["source_url"] in dead:
            f |= {"dead_source": True, "low": True, "confidence": min(f["confidence"], round(LOW_CONFIDENCE - 0.01, 2))}
    return result


# --- the cards on disk ------------------------------------------------------------------------------


def contact_slug(name: str) -> str:
    s = fold(name)
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:60]


def profile_path(client_path: Path, name: str) -> Path:
    slug = contact_slug(name)
    if not slug:
        raise ValueError("empty contact name")
    return Path(client_path) / "people" / f"{slug}.json"


def load_profile(client_path: Path, name: str) -> dict | None:
    try:
        p = profile_path(client_path, name)
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def load_profiles(client_path: Path, info) -> dict[str, dict]:
    """The cards of the sheet's contacts that have one (for the interview context)."""
    out = {}
    for c in getattr(info, "contacts", []) or []:
        if c.name.strip():
            p = load_profile(client_path, c.name)
            if p and p.get("facts"):
                out[c.name] = p
    return out


_LOCKS: dict[Path, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _profile_lock(path: Path) -> threading.Lock:
    """One lock per card file: an FDE edit and a finishing research run never overwrite each other."""
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(Path(path).resolve(), threading.Lock())


def _write(path: Path, profile: dict) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    fsutil.atomic_write_text(path, json.dumps(profile, ensure_ascii=False, indent=2) + "\n")
    return profile


def merge_profile(old: dict | None, name: str, role: str, result: dict, model: str,
                  candidate: dict | None = None) -> dict:
    """The new card: the agent's facts replace the previous agent facts; facts the FDE edited stay,
    facts the FDE removed are not brought back. Candidates (homonyms) keep the previous facts."""
    old = old or {}
    manual = [f for f in old.get("facts", []) if f.get("by") == "manual" and _clean(f.get("value"))]
    removed = list(old.get("removed", []))
    card = {"name": name, "role": role, "contact": contact_slug(name), "status": result["status"],
            "person": result.get("person") or old.get("person", ""), "candidates": result.get("candidates", []),
            "picked": candidate or old.get("picked"), "researched_at": datetime.now().isoformat(timespec="seconds"),
            "model": model, "dropped": result.get("dropped", {}), "removed": removed}
    if result["status"] != "profile":
        card["facts"] = old.get("facts", [])
        return card
    taken = {f["id"] for f in manual} | set(removed)
    single = {f["category"] for f in manual if f["category"] in SINGLE}
    agent = [f for f in result["facts"] if f["id"] not in taken and f["category"] not in single]
    card["facts"] = sorted(manual + agent, key=lambda f: CATEGORIES.index(f["category"]))
    return card


def save_profile_edit(client_path: Path, name: str, facts: list[dict], removed: list[str]) -> dict:
    """The FDE edited the card (double-click on a fact, or removed one): changed or new facts
    become manual, removed ones (×) are remembered so a re-run doesn't bring them back. A fact the
    FDE cleared (empty or whitespace only) is dropped but not remembered: the next run may fill it."""
    path = profile_path(client_path, name)
    with _profile_lock(path):
        return _save_profile_edit(path, facts, removed)


def _save_profile_edit(path: Path, facts: list[dict], removed: list[str]) -> dict:
    if not path.exists():
        raise FileNotFoundError(path)
    card = json.loads(path.read_text(encoding="utf-8"))
    before = {f["id"]: f for f in card.get("facts", [])}
    out, cleared = [], set()
    for f in facts:
        if not isinstance(f, dict):
            raise ValueError("a fact must be an object")
        cat, value = _clean(f.get("category")), _clean(f.get("value"))[:MAX_LEN]
        if cat not in CATEGORIES:
            raise ValueError(f"unknown category {cat!r}")
        if not value:
            cleared.add(str(f.get("id") or ""))
            continue
        old = before.get(str(f.get("id") or ""))
        if old is not None and old["value"] == value and old["category"] == cat:
            out.append(old)
            continue
        fid = str(f.get("id") or "") if old is not None else fact_id(cat, value)
        source = _clean(f.get("source_url")) if old is not None else ""
        entry = {"id": fid, "category": cat, "value": value, "source_url": source if _is_url(source) else "",
                 "confidence": None, "low": False, "by": "manual"}
        out.append(entry)
    gone = ({r for r in removed if r} | ({fid for fid in before} - {f["id"] for f in out})) - (cleared - set(removed))
    card["facts"] = out
    card["removed"] = sorted(set(card.get("removed", [])) | {g for g in gone if before.get(g, {}).get("by") == "agent"})
    return _write(path, card)


def compact_profile(card: dict) -> str:
    """One or two lines per person for the interview context."""
    facts = card.get("facts") or []
    if not facts:
        return ""

    def vals(cat: str, n: int) -> list[str]:
        return [f["value"] for f in facts if f["category"] == cat][:n]

    bits = [f"{label}: {'; '.join(v)}" for label, v in (
        ("role", vals("role", 1)), ("since", vals("tenure", 1)), ("career", vals("career", 3)),
        ("education", vals("education", 2)), ("public talks/articles", vals("publication", 3)),
        ("interests", vals("interest", 3)), ("languages", vals("language", 3)), ("based in", vals("location", 1)),
        ("in common with us", vals("mutual_context", 2)), ("icebreakers", vals("icebreaker", 2))) if v]
    return f"- {card.get('name')}" + (f" ({card['role']})" if card.get("role") else "") + ": " + ". ".join(bits) + "."


# --- the job ------------------------------------------------------------------------------------------


@dataclass
class PersonJob(ResearchJob):
    client: str = ""
    name: str = ""
    role: str = ""
    key: str = ""
    company: dict = field(default_factory=dict)
    candidate: dict | None = None
    result: dict | None = None
    profile: dict | None = None  # the card as saved

    def view(self) -> dict:
        v = super().view()
        v.pop("archived", None)
        return v | {"id": self.slug, "client": self.client, "name": self.name, "role": self.role,
                    "contact": contact_slug(self.name), "candidate": self.candidate, "result": self.result,
                    "profile": self.profile}


class PeopleManager:
    """Person research runs in background threads, keyed by (client, person, picked candidate,
    language); the same ask while it runs or within CACHE_SECONDS reuses that run."""

    def __init__(self, clients_dir: Path, claude_bin: str | None = None, model: str = DEFAULT_PEOPLE_MODEL,
                 effort: str = PEOPLE_EFFORT, timeout: float = PEOPLE_TIMEOUT,
                 check_links: Callable[[list[str]], set[str]] | None = None,
                 engineer_context: str | None = None):
        self.clients_dir = Path(clients_dir)
        self.engineer_context = (os.environ.get(ENGINEER_CONTEXT_ENV, "") if engineer_context is None
                                 else engineer_context)
        self.claude_bin = claude_bin
        self.model = model
        self.effort = effort
        self.timeout = timeout
        self.check_links = check_links
        self.jobs: dict[str, PersonJob] = {}
        self._lock = threading.Lock()

    @staticmethod
    def job_key(client: str, name: str, candidate: dict | None, lang: str) -> str:
        pick = fold((candidate or {}).get("url") or (candidate or {}).get("name") or "")
        return f"{client}|{fold(name)}|{pick}|{lang}"

    def get(self, job_id: str) -> PersonJob | None:
        return self.jobs.get(job_id)

    def start(self, client: str, name: str, role: str, info, candidate: dict | None = None, lang: str = "fr",
              force: bool = False) -> PersonJob:
        name = _clean(name)
        key = self.job_key(client, name, candidate, lang)
        company = {k: _clean(getattr(info, k, "")) for k in ("name", "legal_name", "website", "country", "hq_city",
                                                                "sector", "linkedin_url")}
        with self._lock:
            for job in self.jobs.values():
                if job.key != key:
                    continue
                fresh = job.status == "done" and time.time() - (job.finished_at or 0) < CACHE_SECONDS
                if job.status == "running" or (fresh and not force):
                    return job
            job = PersonJob(slug=secrets.token_hex(6), lang=lang, client=client, name=name, role=_clean(role),
                            key=key, company=company, candidate=candidate)
            self.jobs[job.slug] = job
            running = [j for j in self.jobs.values() if j.status == "running" and j is not job]
            for old in running[:max(0, len(running) - (MAX_RUNNING - 1))]:
                self._stop(old)
            for old in [j for j in self.jobs.values() if j.status != "running"][:max(0, len(self.jobs) - MAX_JOBS)]:
                self.jobs.pop(old.slug, None)
        threading.Thread(target=self._run, args=(job,), daemon=True, name=f"person-{job.slug}").start()
        return job

    def stop(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if job is None or job.status != "running":
            return False
        self._stop(job)
        return True

    @staticmethod
    def _stop(job: PersonJob) -> None:
        job._stop.set()
        if job._proc is not None:
            _kill_group(job._proc)

    def wait(self, job_id: str, timeout: float = 60.0) -> PersonJob | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self.jobs.get(job_id)
            if job is None or job.status != "running":
                return job
            time.sleep(0.05)
        return self.jobs.get(job_id)

    def _run(self, job: PersonJob) -> None:
        try:
            cdir = self.clients_dir / job.client
            old = load_profile(cdir, job.name)
            known = [f for f in (old or {}).get("facts", []) if f.get("by") == "manual" and _clean(f.get("value"))]
            cmd = [
                find_claude(self.claude_bin), "-p",
                "--output-format", "stream-json", "--verbose", "--include-partial-messages",
                "--model", self.model, "--effort", self.effort,
                "--system-prompt", PEOPLE_SYSTEM.format(lang_name=i18n.english_name(job.lang),
                                                       mutual_context=mutual_context_rule(self.engineer_context)),
                "--tools", "WebSearch,WebFetch",  # web lookup only: no shell, no files, no login
                "--allowedTools", "WebSearch,WebFetch",
                "--strict-mcp-config", "--no-session-persistence",
                "--json-schema", json.dumps(PersonOutput.model_json_schema(), separators=(",", ":")),
            ]
            job.say("info", "personStart", name=job.name, model=self.model)
            data = run_claude_stream(job, cmd, build_people_prompt(job.name, job.role, job.company, job.candidate, known),
                                     self.timeout, "pacenote-person-", writing="personWriting")
            result = parse_output(data)
            if self.check_links and result["facts"] and not job._stop.is_set():
                job.say("info", "autofillChecking")
                result = mark_dead_sources(result, self.check_links([f["source_url"] for f in result["facts"]]))
            if job._stop.is_set():
                raise RuntimeError("stopped")
            job.result = result
            path = profile_path(cdir, job.name)
            with _profile_lock(path):  # re-read: the FDE may have edited the card while the agent ran
                current = load_profile(cdir, job.name)
                if result["status"] != "not_found" or current is None:
                    job.profile = _write(path, merge_profile(current, job.name, job.role, result, self.model,
                                                             job.candidate))
                else:
                    job.profile = current
            job.status = "done"
            n_dropped = sum(result["dropped"].values())
            if n_dropped:
                job.say("info", "personFiltered", n=n_dropped)
            if result["status"] == "candidates":
                job.say("done", "personCandidates", n=len(result["candidates"]))
            elif result["status"] == "profile":
                job.say("done", "personReady", n=len(result["facts"]))
            else:
                job.say("done", "personNotFound")
        except Exception as exc:  # the card on disk is never touched
            stopped = job._stop.is_set()
            if not stopped:
                log.warning("person research failed for %r: %s", job.name, exc)
            job.status = "stopped" if stopped else "error"
            job.error = None if stopped else str(exc)[:300]
            if job.error:
                job.step("error", job.error)
            else:
                job.say("info", "stopped")
        finally:
            job.finished_at = time.time()
            job._proc = None
