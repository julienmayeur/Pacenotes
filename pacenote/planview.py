"""The plan inside the app: the payload of the Plan view, and its PDF export.

plan.md (+ plan.json) in the session folder is the source of truth; nothing else is written at
the end of an interview. The Plan view (static/plan.html) renders plan.md in the app, at the end
of the interview (/plan) and for any saved session from the admin panel
(/admin/plan/<client>/<session>). "Export PDF" prints the same document with a headless
Chrome / Chromium already on the Mac (Google Chrome, Chromium, Edge, Brave, or the Chromium that
Playwright keeps in ~/Library/Caches/ms-playwright): no Python dependency. Without one, the page
falls back to the browser's print dialog (its print stylesheet gives the same clean pages).
"""

from __future__ import annotations

import glob
import html
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from . import briefs, i18n
from .finalize import md_to_html

PDF_TIMEOUT = 60.0
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

# Chrome-family browsers that can `--print-to-pdf` headless, in order of preference.
MAC_BROWSERS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
)
PATH_BROWSERS = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome")
PLAYWRIGHT_CACHES = ("~/Library/Caches/ms-playwright", "~/.cache/ms-playwright")
PLAYWRIGHT_GLOBS = (  # newest build first within each pattern
    "chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell",
    "chromium-*/chrome-mac*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
    "chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium",
    "chromium-*/chrome-linux*/chrome",
)


class PlanNotFound(LookupError):
    pass


class PdfUnavailable(RuntimeError):
    """No Chrome-family browser to print with: the page uses the browser's print dialog instead."""


class PdfFailed(RuntimeError):
    pass


# --- the Plan view ------------------------------------------------------------------------------


def _build(path: str) -> int:
    m = re.search(r"-(\d+)/", path)
    return int(m.group(1)) if m else 0


def find_chrome() -> str | None:
    """PACENOTE_CHROME, then the usual macOS apps, then PATH, then Playwright's Chromium cache."""
    explicit = os.environ.get("PACENOTE_CHROME")
    if explicit is not None:  # set (even empty): only that one ("" = none, e.g. to test the fallback)
        p = os.path.expanduser(explicit)
        return p if explicit and os.path.isfile(p) and os.access(p, os.X_OK) else None
    for p in MAC_BROWSERS:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    for name in PATH_BROWSERS:
        if found := shutil.which(name):
            return found
    for cache in PLAYWRIGHT_CACHES:
        root = os.path.expanduser(cache)
        for pattern in PLAYWRIGHT_GLOBS:
            hits = sorted(glob.glob(os.path.join(root, pattern)), key=_build, reverse=True)
            for p in hits:
                if os.access(p, os.X_OK):
                    return p
    return None


def _state(session_dir: Path) -> dict:
    try:
        return json.loads((session_dir / "state.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def plan_title(plan_md: str, fallback: str) -> str:
    m = re.search(r"(?m)^#\s+(.+?)\s*$", plan_md)
    return m.group(1) if m else fallback


def plan_payload(session_dir: Path) -> dict:
    """What the Plan view shows for one session. Raises PlanNotFound before the plan exists."""
    session_dir = Path(session_dir)
    md_path = session_dir / "plan.md"
    if not md_path.exists():
        raise PlanNotFound(f"no plan in {session_dir}")
    plan_md = md_path.read_text(encoding="utf-8")
    st = _state(session_dir)
    lang = st.get("language") or "en"
    pdf = session_dir / "plan.pdf"
    cap = briefs.load_brief("capabilities")
    return {
        "client": st.get("client", ""),
        "slug": st.get("slug", session_dir.parent.name),
        "session": session_dir.name,
        "language": lang,  # the plan's language (the interview language), not the screen language
        "title": plan_title(plan_md, st.get("client", "")),
        "html": md_to_html(plan_md),
        "generated_at": datetime.fromtimestamp(md_path.stat().st_mtime).isoformat(timespec="minutes"),
        "files": {"md": str(md_path.resolve()),
                  **({"json": str((session_dir / "plan.json").resolve())} if (session_dir / "plan.json").exists() else {}),
                  **({"pdf": str(pdf.resolve())} if pdf.exists() else {})},
        "pdf_available": find_chrome() is not None,
        # the dated capability brief the plans are written with (briefs.py): warn when it is old
        "capabilities": {"as_of": cap.as_of_text if cap else "unknown", "stale": cap is None or cap.stale()},
    }


def list_sessions(sessions_dir: Path, slug: str) -> list[dict]:
    """Saved sessions of a client, newest first, with whether each has a plan."""
    base = Path(sessions_dir) / slug
    if not base.is_dir():
        return []
    out = []
    for d in sorted((p for p in base.iterdir() if p.is_dir() and _SESSION_ID.match(p.name)), reverse=True):
        st = _state(d)
        if not st and not (d / "plan.md").exists():
            continue
        out.append({"id": d.name, "mode": st.get("mode", ""), "language": st.get("language", ""),
                    "started_at": st.get("started_at"), "has_plan": (d / "plan.md").exists(),
                    "answered": sum(1 for q in st.get("questions", []) if q.get("status") == "answered")})
    return out


def session_path(sessions_dir: Path, slug: str, session_id: str) -> Path:
    """sessions/<slug>/<id>, refusing anything that would leave the sessions folder."""
    if not _SESSION_ID.match(slug) or not _SESSION_ID.match(session_id):
        raise PlanNotFound("bad session id")
    root = Path(sessions_dir).resolve()
    d = (root / slug / session_id).resolve()
    if root not in d.parents or not d.is_dir():
        raise PlanNotFound(f"no session {slug}/{session_id}")
    return d


# --- PDF --------------------------------------------------------------------------------------------

PRINT_CSS = """
@page { size: A4; margin: 16mm 15mm; }
:root { --ink:#1b1f24; --muted:#5b6470; --rule:#d9dde3; --accent:#2a5bd7; }
* { box-sizing: border-box; }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body { margin:0; background:#fff; color:var(--ink);
  font: 10.5pt/1.5 -apple-system, BlinkMacSystemFont, "Helvetica Neue", Helvetica, Arial, sans-serif; }
main { max-width: 820px; margin: 0 auto; }
h1 { font-size: 20pt; line-height:1.2; margin: 0 0 14px; }
h2 { font-size: 13.5pt; margin: 24px 0 8px; padding-bottom: 4px; border-bottom: 2px solid var(--accent);
  break-after: avoid; }
h3 { font-size: 11.5pt; margin: 18px 0 6px; break-after: avoid; }
p { margin: 6px 0; }
table { border-collapse: collapse; width: 100%; margin: 8px 0 14px; font-size: 9.5pt; }
th, td { border: 1px solid var(--rule); padding: 5px 7px; text-align: left; vertical-align: top; }
th { background: #f3f5f8; }
tr, li { break-inside: avoid; }
code { font-size: 9pt; color: var(--muted); }
blockquote { margin: 8px 0; padding: 6px 12px; border-left: 3px solid var(--rule); color: var(--muted); }
hr { border: 0; border-top: 1px solid var(--rule); margin: 24px 0 10px; }
figure.comm-map { margin: 8px 0 14px; break-inside: avoid; }
figure.comm-map details { display: none; }
"""


def print_document(plan_md: str, title: str, lang: str) -> str:
    """The plan as one self-contained page for printing (A4, no app chrome)."""
    return (f'<!doctype html>\n<html lang="{html.escape(lang)}">\n<head>\n<meta charset="utf-8">\n'
            f"<title>{html.escape(title)}</title>\n<style>{PRINT_CSS}</style>\n</head>\n<body>\n"
            f"<main>\n{md_to_html(plan_md)}\n</main>\n</body>\n</html>\n")


def export_pdf(session_dir: Path, chrome: str | None = None, timeout: float = PDF_TIMEOUT) -> Path:
    """Print plan.md to session_dir/plan.pdf with a headless Chrome-family browser."""
    session_dir = Path(session_dir)
    md_path = session_dir / "plan.md"
    if not md_path.exists():
        raise PlanNotFound(f"no plan in {session_dir}")
    chrome = chrome or find_chrome()
    if not chrome:
        raise PdfUnavailable("no Chrome, Chromium, Edge or Brave found (set PACENOTE_CHROME to one)")
    plan_md = md_path.read_text(encoding="utf-8")
    st = _state(session_dir)
    lang = st.get("language") or "en"
    title = plan_title(plan_md, st.get("client", "")) or i18n.t("plan.title", lang)
    target = session_dir / "plan.pdf"
    with tempfile.TemporaryDirectory(prefix="pacenote-pdf-") as tmp:
        page = Path(tmp) / "plan.html"  # a temporary print page, never kept in the session folder
        page.write_text(print_document(plan_md, title, lang), encoding="utf-8")
        out = Path(tmp) / "plan.pdf"
        cmd = [chrome, "--headless", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
               "--disable-extensions", f"--user-data-dir={Path(tmp) / 'profile'}", "--no-pdf-header-footer",
               "--print-to-pdf-no-header", f"--print-to-pdf={out}", page.as_uri()]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise PdfFailed(f"{Path(chrome).name} timed out after {timeout:.0f}s") from exc
        except OSError as exc:
            raise PdfFailed(f"could not start {chrome}: {exc}") from exc
        if not out.exists() or out.read_bytes()[:5] != b"%PDF-":
            detail = (proc.stderr or proc.stdout or "").strip()[-300:]
            raise PdfFailed(f"{Path(chrome).name} did not write a PDF (exit {proc.returncode}): {detail}")
        shutil.move(str(out), target)
    return target


def pdf_filename(session_dir: Path) -> str:
    st = _state(Path(session_dir))
    return f"{st.get('slug') or Path(session_dir).parent.name}-{Path(session_dir).name}-plan.pdf"


# --- HTTP (shared by the interview server and the admin panel) ------------------------------------

def page_response():
    from fastapi.responses import HTMLResponse

    return HTMLResponse((Path(__file__).parent / "static" / "plan.html").read_text(encoding="utf-8"))


def payload_response(session_dir: Path) -> dict:
    from fastapi import HTTPException

    try:
        return plan_payload(session_dir)
    except PlanNotFound as exc:
        raise HTTPException(status_code=404, detail="plan not generated yet") from exc


def markdown_response(session_dir: Path):
    from fastapi import HTTPException
    from fastapi.responses import FileResponse

    path = Path(session_dir) / "plan.md"
    if not path.exists():
        raise HTTPException(status_code=404, detail="plan not generated yet")
    return FileResponse(path, media_type="text/markdown; charset=utf-8")


async def pdf_response(session_dir: Path):
    """Export PDF: 501 when no browser can print (the page then opens the print dialog)."""
    import asyncio

    from fastapi import HTTPException
    from fastapi.responses import FileResponse

    try:
        path = await asyncio.to_thread(export_pdf, Path(session_dir))
    except PlanNotFound as exc:
        raise HTTPException(status_code=404, detail="plan not generated yet") from exc
    except PdfUnavailable as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except PdfFailed as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return FileResponse(path, media_type="application/pdf", filename=pdf_filename(Path(session_dir)))
