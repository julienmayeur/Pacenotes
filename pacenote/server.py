"""FastAPI app: the full-screen page, an SSE stream of state, and FDE controls."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from importlib import resources
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import i18n, planview
from .admin import admin_router
from .autofill import DEFAULT_AUTOFILL_MODEL, AutofillManager, dead_links
from .people import DEFAULT_PEOPLE_MODEL, PeopleManager
from .capture import CaptureError
from .research import DEFAULT_MODEL as RESEARCH_MODEL, ResearchManager
from .session import Session
from .web_guard import SameOriginGuard

log = logging.getLogger(__name__)

STATIC_DIR = Path(str(resources.files("pacenote") / "static"))


class WisprBody(BaseModel):
    text: str


class LanguageBody(BaseModel):
    ui: str | None = None  # FDE screen language (needs a locale file)
    interview: str | None = None  # questions, follow-ups, summaries, plan (any language)
    speech: list[str] | None = None  # speech locales, before the interview starts


class NextBody(BaseModel):
    from_: str | None = Field(default=None, alias="from")


def create_app(session: Session, auto_start: bool = False, research: ResearchManager | None = None,
               bind_host: str | None = None, claude_bin: str | None = None) -> FastAPI:
    """The interview app. Listening starts ONLY on POST /api/start (the Start button): never at
    server start, page load or consent (auto_start is `pacenote replay --auto-start`, saved SRTs)."""
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        session.prepare_languages()  # translate the questionnaire if the interview language needs it
        if auto_start:
            await session.start()
        try:
            yield
        finally:
            # Server shutting down (Ctrl-C): stop listening, flush the last cues, save.
            await session.shutdown()

    app = FastAPI(title="FDE kickoff", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(SameOriginGuard, bind_host=bind_host)  # no other web page can drive it (web_guard.py)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(i18n.api_router())  # catalogs for static/i18n.js (any screen)
    # the admin panel (clients, research, pre-plan) on its own page: /admin
    clients_dir = Path(session.client.dir).parent
    sessions_dir = Path(session.session_dir).resolve().parent.parent  # sessions/<client>/<id>
    app.include_router(admin_router(clients_dir, research or ResearchManager(
        clients_dir, claude_bin=claude_bin, model=os.environ.get("PACENOTE_RESEARCH_MODEL", RESEARCH_MODEL)),
        sessions_dir=sessions_dir,
        autofill=AutofillManager(claude_bin=claude_bin, model=os.environ.get("PACENOTE_AUTOFILL_MODEL", DEFAULT_AUTOFILL_MODEL),
                                 check_links=dead_links),
        people=PeopleManager(clients_dir, claude_bin=claude_bin,
                             model=os.environ.get("PACENOTE_PEOPLE_MODEL", DEFAULT_PEOPLE_MODEL), check_links=dead_links)))

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))

    @app.get("/api/state")
    async def get_state() -> dict:
        return session.view()

    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        async def stream():
            last = -1
            yield f"event: state\ndata: {json.dumps(session.view(), ensure_ascii=False)}\n\n"
            last = session._version
            while True:
                if await request.is_disconnected():
                    break
                v = await session.wait_for_change(last, timeout=15.0)
                if v != last:
                    last = v
                    yield f"event: state\ndata: {json.dumps(session.view(), ensure_ascii=False)}\n\n"
                else:
                    yield ": keep-alive\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/start")
    async def start() -> dict:
        try:
            await session.start()
        except (CaptureError, OSError) as exc:  # speech helper missing, failed or died at start
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return session.view()

    @app.post("/api/language")
    async def set_language(body: LanguageBody) -> dict:
        try:
            await session.set_languages(ui=body.ui, interview=body.interview, speech=body.speech)
        except i18n.LanguageError as exc:
            raise HTTPException(status_code=409 if body.speech is not None and session.state.mode != "consent"
                                else 400, detail=str(exc)) from exc
        return session.view()

    @app.post("/api/sync")
    async def sync_now() -> dict:
        if session.state.mode != "interview":
            raise HTTPException(status_code=409, detail="not interviewing")
        session.request_sync()
        return {"ok": True}

    @app.post("/api/next")
    async def next_question(body: NextBody | None = None) -> dict:
        """Move on to the next question (n / Enter on the page). `from` = the question the FDE was
        looking at; ignored if the focus already moved on."""
        if session.state.mode not in ("interview", "finalize"):
            raise HTTPException(status_code=409, detail="not interviewing")
        await session.next_question(body.from_ if body else None)
        return session.view()

    @app.post("/api/next-phase")
    async def next_phase(body: NextBody | None = None) -> dict:
        """Close the current phase and move to the next one (p on the page)."""
        if session.state.mode not in ("interview", "finalize"):
            raise HTTPException(status_code=409, detail="not interviewing")
        await session.next_phase(body.from_ if body else None)
        return session.view()

    @app.post("/api/questions/{qid}/answered")
    async def answered(qid: str) -> dict:
        if not await session.mark_answered(qid):
            raise HTTPException(status_code=409, detail="question is not open")
        return session.view()

    @app.post("/api/questions/{qid}/skip")
    async def skip(qid: str) -> dict:
        if not await session.skip(qid):
            raise HTTPException(status_code=409, detail="only pending questions can be skipped")
        return session.view()

    @app.post("/api/enough")
    async def enough() -> dict:
        await session.force_enough()
        return session.view()

    @app.post("/api/wispr")
    async def wispr(body: WisprBody) -> dict:
        await session.set_wispr(body.text)
        return session.view()

    finalize_task: list[asyncio.Task] = []  # a reference, so the running task is not garbage-collected

    @app.post("/api/finalize")
    async def finalize() -> dict:
        running = finalize_task and not finalize_task[0].done()
        if running or session.finalizing or session.state.mode == "generating":
            raise HTTPException(status_code=409, detail="already generating")
        finalize_task[:] = [asyncio.create_task(session.finalize(), name="finalize")]
        return {"ok": True}

    # the Plan view of this session (static/plan.html), its data and its PDF export
    @app.get("/plan", response_class=HTMLResponse)
    async def plan_page() -> HTMLResponse:
        return planview.page_response()

    @app.get("/api/plan")
    async def plan_data() -> dict:
        return planview.payload_response(session.session_dir)

    @app.get("/api/plan/md")
    async def plan_markdown() -> FileResponse:
        return planview.markdown_response(session.session_dir)

    @app.get("/api/plan/pdf")
    async def plan_pdf() -> FileResponse:
        return await planview.pdf_response(session.session_dir)

    @app.get("/plan.md")
    async def plan_md() -> FileResponse:
        return _plan_file(session, "md", "text/markdown")

    return app


def _plan_file(session: Session, key: str, media: str) -> FileResponse:
    path = session.state.plan_files.get(key)
    if not path or not Path(path).exists():
        raise HTTPException(status_code=404, detail="plan not generated yet")
    return FileResponse(path, media_type=f"{media}; charset=utf-8")
