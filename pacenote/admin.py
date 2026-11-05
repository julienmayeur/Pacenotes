"""Admin panel (/admin): set up the client we work with next, auto-fill its sheet from the web,
run the research, edit the pre-plan.

Mounted in the interview app (`pacenote run`, `pacenote replay`) and served alone by `pacenote admin`. Local
only (127.0.0.1 by default); everything it writes stays under clients/<slug>/ and in
~/.pacenote/config.json (the next client).
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .clients import (
    ClientError,
    ClientInfo,
    client_dir,
    create_client,
    get_next_client,
    list_slugs,
    load_info,
    load_preplan,
    save_info,
    set_next_client,
    slug_from_name,
)
from . import fsutil, i18n, planview
from . import metrics as metrics_lib
from .autofill import (DEFAULT_AUTOFILL_MODEL, FIELDS as AUTOFILL_FIELDS, RELATIONS, AutofillManager, dead_links,
                       mark_manual_edits, merge_into_sheet)
from .people import (DEFAULT_PEOPLE_MODEL, CATEGORIES as PERSON_CATEGORIES, PeopleManager, contact_slug,
                     load_profile, save_profile_edit)
from .finalize import md_to_html
from .llm import DEFAULT_MODEL
from .research import ResearchManager, list_history, restore_version

STATIC_DIR = Path(__file__).parent / "static"


class NewClient(BaseModel):
    name: str
    slug: str | None = None
    language: str = "fr"


class NextBody(BaseModel):
    slug: str | None = None


class MarkdownBody(BaseModel):
    markdown: str


class CandidateBody(BaseModel):
    name: str
    website: str = ""
    country: str = ""
    description: str = ""


class AutofillBody(BaseModel):
    name: str
    hints: dict[str, str] = {}  # the values the FDE typed (manual): trusted, never searched again
    candidate: CandidateBody | None = None  # picked among the candidates of a previous run
    lang: str = "fr"  # language of the descriptive values (the interview language)
    force: bool = False  # the button: run again even when a recent answer exists


class ApplyBody(BaseModel):
    info: ClientInfo | None = None  # the sheet as the page holds it now (default: as saved)


class PersonBody(BaseModel):
    name: str
    role: str = ""
    candidate: dict | None = None  # picked among the homonyms of a previous run
    lang: str = "fr"
    force: bool = False


class MeasurementBody(BaseModel):
    value: float
    date: str | None = None  # YYYY-MM-DD, default today
    note: str = ""
    version: int  # the metrics.yaml version the page rendered (optimistic lock: a stale page gets 409)


class ProfileEdit(BaseModel):
    facts: list[dict] = []  # the card as edited (each fact: id, category, value, source_url, confidence, by)
    removed: list[str] = []  # ids of facts the FDE removed


def _render(text: str) -> str:
    return md_to_html(text or "")


def admin_router(clients_dir: Path, manager: ResearchManager, sessions_dir: Path | None = None,
                 autofill: AutofillManager | None = None, people: PeopleManager | None = None) -> APIRouter:
    """sessions_dir: where the interviews are saved (sessions/<client>/<id>), for the sessions list
    and the Plan view of each session; None hides them. autofill: the client-sheet web agent
    (default: Sonnet, same Claude CLI as the research)."""
    r = APIRouter()
    clients_dir = Path(clients_dir)
    autofill = autofill or AutofillManager(manager.claude_bin, DEFAULT_AUTOFILL_MODEL, check_links=dead_links)
    people = people or PeopleManager(clients_dir, manager.claude_bin, DEFAULT_PEOPLE_MODEL, check_links=dead_links)

    def sdir(slug: str, session_id: str) -> Path:
        if sessions_dir is None:
            raise HTTPException(status_code=404, detail="no sessions folder")
        try:
            return planview.session_path(sessions_dir, slug, session_id)
        except planview.PlanNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    def cdir(slug: str) -> Path:
        try:
            d = client_dir(clients_dir, slug)
        except ClientError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not (d / "questionnaire.yaml").exists():
            raise HTTPException(status_code=404, detail=f"unknown client {slug!r}")
        return d

    def read(path: Path) -> str:
        return path.read_text(encoding="utf-8") if path.exists() else ""

    @r.get("/admin", response_class=HTMLResponse)
    async def page() -> HTMLResponse:
        return HTMLResponse((STATIC_DIR / "admin.html").read_text(encoding="utf-8"))

    @r.get("/admin/api/clients")
    async def clients() -> dict:
        nxt = get_next_client()
        out = []
        for slug in list_slugs(clients_dir):
            info = load_info(clients_dir, slug)
            d = clients_dir / slug
            job = manager.status(slug)
            out.append({"slug": slug, "name": info.name, "sector": info.sector, "next": slug == nxt,
                        "has_preplan": (d / "preplan.md").exists(),
                        "researching": bool(job and job["status"] == "running")})
        return {"clients": out, "next": nxt}

    @r.post("/admin/api/clients")
    async def create(body: NewClient) -> dict:
        slug = (body.slug or slug_from_name(body.name)).strip()
        try:
            create_client(clients_dir, slug, body.name, body.language)
        except ClientError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return await detail(slug)

    @r.get("/admin/api/clients/{slug}")
    async def detail(slug: str) -> dict:
        d = cdir(slug)
        preplan = load_preplan(d) or {}
        research_md, preplan_md = read(d / "research.md"), read(d / "preplan.md")
        return {
            "slug": slug,
            "info": load_info(clients_dir, slug).model_dump(),
            "next": get_next_client() == slug,
            "preplan_md": preplan_md, "preplan_html": _render(preplan_md),
            "research_md": research_md, "research_html": _render(research_md),
            "generated_at": preplan.get("generated_at"), "model": preplan.get("model"),
            "summary": preplan.get("summary", ""),
            "use_cases": preplan.get("use_cases", []),
            "seed_questions": preplan.get("seed_questions", []),
            "sources": preplan.get("sources", []),
            "history": list_history(d),
            "profiles": {c.name: load_profile(d, c.name) for c in load_info(clients_dir, slug).contacts if c.name.strip()},
            "research": manager.status(slug),
            "research_model": manager.model,
        }

    @r.get("/admin/api/clients/{slug}/sessions")
    async def sessions(slug: str) -> dict:
        cdir(slug)
        return {"sessions": planview.list_sessions(sessions_dir, slug) if sessions_dir is not None else []}

    @r.get("/admin/plan/{slug}/{session_id}", response_class=HTMLResponse)
    async def plan_page(slug: str, session_id: str) -> HTMLResponse:
        sdir(slug, session_id)
        return planview.page_response()

    @r.get("/admin/api/plan/{slug}/{session_id}")
    async def plan_data(slug: str, session_id: str) -> dict:
        return planview.payload_response(sdir(slug, session_id))

    @r.get("/admin/api/plan/{slug}/{session_id}/md")
    async def plan_markdown(slug: str, session_id: str):
        return planview.markdown_response(sdir(slug, session_id))

    @r.get("/admin/api/plan/{slug}/{session_id}/pdf")
    async def plan_pdf(slug: str, session_id: str):
        return await planview.pdf_response(sdir(slug, session_id))

    @r.put("/admin/api/clients/{slug}")
    async def update(slug: str, info: ClientInfo) -> dict:
        cdir(slug)
        try:
            save_info(clients_dir, slug, mark_manual_edits(load_info(clients_dir, slug), info))
        except ClientError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return await detail(slug)

    # --- auto-fill of the client sheet (pacenote/autofill.py) ------------------------------------
    @r.get("/admin/api/autofill")
    async def autofill_info() -> dict:
        """Lets the page know the agent is there (an older server answers 404: no button)."""
        return {"enabled": True, "model": autofill.model, "fields": list(AUTOFILL_FIELDS),
                "relations": list(RELATIONS), "people": {"model": people.model, "categories": list(PERSON_CATEGORIES)}}

    @r.post("/admin/api/autofill")
    async def autofill_start(body: AutofillBody) -> dict:
        name = " ".join(body.name.split())
        if len(name) < 2 or len(name) > 120:
            raise HTTPException(status_code=400, detail="company name: 2 to 120 characters")
        try:
            lang = i18n.normalize_interview(body.lang or "fr")
        except i18n.LanguageError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        unknown = sorted(set(body.hints) - set(AUTOFILL_FIELDS))
        if unknown:
            raise HTTPException(status_code=400, detail=f"unknown fields: {', '.join(unknown)}")
        candidate = body.candidate.model_dump() if body.candidate else None
        if candidate is not None and not candidate["name"].strip():
            raise HTTPException(status_code=400, detail="candidate without a name")
        return autofill.start(name, body.hints, candidate, lang, force=body.force).view()

    @r.get("/admin/api/autofill/{job_id}")
    async def autofill_status(job_id: str) -> dict:
        job = autofill.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown auto-fill run")
        return job.view()

    @r.post("/admin/api/autofill/{job_id}/stop")
    async def autofill_stop(job_id: str) -> dict:
        if autofill.get(job_id) is None:
            raise HTTPException(status_code=404, detail="unknown auto-fill run")
        return {"stopped": autofill.stop(job_id)}

    @r.post("/admin/api/clients/{slug}/autofill/{job_id}/apply")
    async def autofill_apply(slug: str, job_id: str, body: ApplyBody | None = None) -> dict:
        """The agent's answer straight into the sheet (saved): empty and agent-filled fields only,
        never a manual one."""
        cdir(slug)
        job = autofill.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown auto-fill run")
        if job.status != "done":
            raise HTTPException(status_code=409, detail="auto-fill still running")
        saved = load_info(clients_dir, slug)
        current = mark_manual_edits(saved, body.info) if body and body.info else saved
        info, written = merge_into_sheet(current, job.result)
        try:
            save_info(clients_dir, slug, info)
        except ClientError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return await detail(slug) | {"written": written}

    # --- interviewee research: a profile card per contact (pacenote/people.py) ----------------
    @r.post("/admin/api/clients/{slug}/people")
    async def person_start(slug: str, body: PersonBody) -> dict:
        cdir(slug)
        name = " ".join(body.name.split())
        if len(name) < 3 or len(name) > 120:
            raise HTTPException(status_code=400, detail="person name: 3 to 120 characters")
        try:
            lang = i18n.normalize_interview(body.lang or "fr")
        except i18n.LanguageError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if body.candidate is not None and not str(body.candidate.get("name") or "").strip():
            raise HTTPException(status_code=400, detail="candidate without a name")
        info = load_info(clients_dir, slug)
        return people.start(slug, name, " ".join(body.role.split())[:120], info, body.candidate, lang,
                            force=body.force).view()

    @r.get("/admin/api/people/{job_id}")
    async def person_status(job_id: str) -> dict:
        job = people.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown person research")
        return job.view()

    @r.post("/admin/api/people/{job_id}/stop")
    async def person_stop(job_id: str) -> dict:
        if people.get(job_id) is None:
            raise HTTPException(status_code=404, detail="unknown person research")
        return {"stopped": people.stop(job_id)}

    @r.get("/admin/api/clients/{slug}/people/{contact}")
    async def person_profile(slug: str, contact: str) -> dict:
        prof = load_profile(cdir(slug), contact)
        if prof is None:
            raise HTTPException(status_code=404, detail="no profile")
        return prof

    @r.put("/admin/api/clients/{slug}/people/{contact}")
    async def person_edit(slug: str, contact: str, body: ProfileEdit) -> dict:
        d = cdir(slug)
        if not contact_slug(contact):
            raise HTTPException(status_code=400, detail="bad contact")
        try:
            return save_profile_edit(d, contact, body.facts, body.removed)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="no profile") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # --- metric monitoring (pacenote/metrics.py): clients/<slug>/metrics.yaml ----------------------
    @r.get("/admin/api/clients/{slug}/metrics")
    async def metrics_view(slug: str) -> dict:
        cdir(slug)
        try:
            return metrics_lib.view(metrics_lib.load(clients_dir, slug))
        except metrics_lib.MetricsError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @r.post("/admin/api/clients/{slug}/metrics/{metric_id}/measurements")
    async def metrics_add(slug: str, metric_id: str, body: MeasurementBody) -> dict:
        cdir(slug)
        try:
            f = metrics_lib.add_measurement(clients_dir, slug, metric_id, body.value, when=body.date or None,
                                            source="manual", note=body.note, expected_version=body.version)
        except metrics_lib.StaleWrite as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except metrics_lib.UnknownMetric as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except metrics_lib.MetricsError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return metrics_lib.view(f)

    @r.post("/admin/api/next")
    async def next_client(body: NextBody) -> dict:
        if body.slug:
            cdir(body.slug)
        set_next_client(body.slug)
        return {"next": get_next_client()}

    @r.post("/admin/api/clients/{slug}/research")
    async def start_research(slug: str) -> dict:
        cdir(slug)
        return manager.start(slug).view()

    @r.get("/admin/api/clients/{slug}/research")
    async def research_status(slug: str) -> dict:
        cdir(slug)
        return manager.status(slug) or {"slug": slug, "status": "idle", "steps": []}

    @r.post("/admin/api/clients/{slug}/research/stop")
    async def stop_research(slug: str) -> dict:
        cdir(slug)
        return {"stopped": manager.stop(slug)}

    @r.put("/admin/api/clients/{slug}/preplan")
    async def save_preplan(slug: str, body: MarkdownBody) -> dict:
        fsutil.atomic_write_text(cdir(slug) / "preplan.md", body.markdown.rstrip() + "\n")
        return {"ok": True, "html": _render(body.markdown)}

    @r.put("/admin/api/clients/{slug}/research-md")
    async def save_research(slug: str, body: MarkdownBody) -> dict:
        fsutil.atomic_write_text(cdir(slug) / "research.md", body.markdown.rstrip() + "\n")
        return {"ok": True, "html": _render(body.markdown)}

    @r.get("/admin/api/clients/{slug}/history/{version}")
    async def history_version(slug: str, version: str) -> dict:
        h = cdir(slug) / "history" / version
        if not h.is_dir() or "/" in version or ".." in version:
            raise HTTPException(status_code=404, detail="unknown version")
        meta = json.loads(read(h / "preplan.json") or "{}")
        return {"version": version, "preplan_html": _render(read(h / "preplan.md")),
                "research_html": _render(read(h / "research.md")), "generated_at": meta.get("generated_at")}

    @r.post("/admin/api/clients/{slug}/history/{version}/restore")
    async def history_restore(slug: str, version: str) -> dict:
        try:
            restore_version(cdir(slug), version)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="unknown version") from exc
        return await detail(slug)

    return r


def create_admin_app(clients_dir: Path, claude_bin: str | None = None, model: str = DEFAULT_MODEL,
                     manager: ResearchManager | None = None, sessions_dir: Path | None = None,
                     autofill_model: str = DEFAULT_AUTOFILL_MODEL,
                     autofill: AutofillManager | None = None, people_model: str | None = None,
                     people: PeopleManager | None = None, bind_host: str | None = None) -> FastAPI:
    """The admin panel on its own (`pacenote admin`), without an interview session."""
    from .web_guard import SameOriginGuard

    app = FastAPI(title="FDE kickoff — admin", docs_url=None, redoc_url=None)
    app.add_middleware(SameOriginGuard, bind_host=bind_host)  # no other web page can drive it
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(admin_router(clients_dir, manager or ResearchManager(clients_dir, claude_bin, model),
                                    sessions_dir=sessions_dir,
                                    autofill=autofill or AutofillManager(claude_bin, autofill_model, check_links=dead_links),
                                    people=people or PeopleManager(clients_dir, claude_bin, people_model or DEFAULT_PEOPLE_MODEL,
                                                                   check_links=dead_links)))
    app.include_router(i18n.api_router())  # catalogs for static/i18n.js

    @app.get("/", response_class=HTMLResponse)
    async def root() -> HTMLResponse:
        return HTMLResponse('<meta http-equiv="refresh" content="0; url=/admin">')

    return app
