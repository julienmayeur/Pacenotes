"""HTTP pieces both web apps share (`pacenote run` and `pacenote admin`): the static pages, the language
catalogs for `static/i18n.js`, and the Plan view's responses."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from pacenote.plan import view as plan_view
from pacenote.shared import i18n

STATIC_DIR = Path(__file__).parent / "static"


def i18n_router(locales_dir: Path | None = None) -> APIRouter:
    """The catalogs for `static/i18n.js`:

    GET /api/i18n              -> {languages: [{code, name, native, label}], fallback, speech}
    GET /api/i18n/{lang}       -> the flat catalog for `lang`, fallbacks filled in
    """
    router = APIRouter()

    @router.get("/api/i18n")
    async def languages(ui: str | None = None) -> dict:
        return i18n.languages_payload(ui, locales_dir)

    @router.get("/api/i18n/{lang}")
    async def catalog(lang: str) -> dict:
        if not i18n.is_language(lang):
            raise HTTPException(status_code=404, detail=f"unknown language {lang!r}")
        return i18n.catalog_payload(lang, locales_dir)

    return router


# --- the Plan view -----------------------------------------------------------------------------------


def plan_page() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "plan.html").read_text(encoding="utf-8"))


def plan_payload(session_dir: Path) -> dict:
    try:
        return plan_view.plan_payload(session_dir)
    except plan_view.PlanNotFound as exc:
        raise HTTPException(status_code=404, detail="plan not generated yet") from exc


def plan_markdown(session_dir: Path) -> FileResponse:
    path = Path(session_dir) / "plan.md"
    if not path.exists():
        raise HTTPException(status_code=404, detail="plan not generated yet")
    return FileResponse(path, media_type="text/markdown; charset=utf-8")


async def plan_pdf(session_dir: Path) -> FileResponse:
    """Export PDF: 501 when no browser can print (the page then opens the print dialog)."""
    try:
        path = await asyncio.to_thread(plan_view.export_pdf, Path(session_dir))
    except plan_view.PlanNotFound as exc:
        raise HTTPException(status_code=404, detail="plan not generated yet") from exc
    except plan_view.PdfUnavailable as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except plan_view.PdfFailed as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return FileResponse(path, media_type="application/pdf", filename=plan_view.pdf_filename(Path(session_dir)))
