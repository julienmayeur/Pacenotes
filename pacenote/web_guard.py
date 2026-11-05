"""Same-origin guard for the local web apps (`pacenote run` / `pacenote replay` / `pacenote admin`).

Why: the interview server listens on the Mac while the FDE browses the web. Without a check, any
page open in the browser could POST to http://127.0.0.1:8765/api/start (a "no-cors" fetch or an
auto-submitted form) and start listening, end the interview or launch admin agents; with DNS
rebinding it could also read the live transcript. Two cheap rules close that:

  * Host: every request must name this server by a loopback name (127.0.0.1, localhost, ::1),
    the host given with --host / PACENOTE_HOST, or one listed in PACENOTE_ALLOWED_HOSTS (comma-separated).
    A rebinding page reaches us with its own domain in Host: refused.
  * Origin: a state-changing request (POST, PUT, PATCH, DELETE) is refused when the browser says it
    comes from another origin. Browsers send `Origin` on every such request (same-origin included);
    when it is missing we read `Sec-Fetch-Site`. A request with neither (curl, the tests) is not a
    web page and goes through. A GET under /api or /admin/api (some export a PDF or poll agents)
    is refused when it is a cross-origin fetch; following a link (a navigation) is fine.
"""

from __future__ import annotations

import os

from starlette.responses import JSONResponse

LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
WILDCARD = {"", "0.0.0.0", "::", "[::]"}


def allowed_hosts(bind_host: str | None = None) -> set[str]:
    hosts = set(LOOPBACK)
    if bind_host and bind_host not in WILDCARD:
        hosts.add(bind_host.lower())
    hosts |= {h.strip().lower() for h in os.environ.get("PACENOTE_ALLOWED_HOSTS", "").split(",") if h.strip()}
    return hosts


def _hostname(host_header: str) -> str:
    h = host_header.strip().lower()
    if h.startswith("["):  # [::1]:8765
        return h.split("]", 1)[0] + "]"
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def check(method: str, path: str, headers: dict[str, str], hosts: set[str]) -> str | None:
    """None when the request may go through, else why it is refused."""
    host = headers.get("host", "")
    if _hostname(host) not in hosts:
        return f"unexpected Host {host!r}"
    origin = headers.get("origin")
    site = headers.get("sec-fetch-site", "")
    if method in UNSAFE:
        if origin is not None:
            if origin.lower() not in {f"http://{host.lower()}", f"https://{host.lower()}"}:
                return f"cross-origin request from {origin!r}"
        elif site and site not in ("same-origin", "none"):
            return f"cross-origin request (Sec-Fetch-Site: {site})"
    elif path.startswith(("/api/", "/admin/api/")) and site in ("cross-site", "same-site"):
        if headers.get("sec-fetch-mode", "") != "navigate":
            return f"cross-origin read (Sec-Fetch-Site: {site})"
    return None


class SameOriginGuard:
    """ASGI middleware applying `check` to every HTTP request; refused requests get a 403."""

    def __init__(self, app, bind_host: str | None = None):
        self.app = app
        self.hosts = allowed_hosts(bind_host if bind_host is not None else os.environ.get("PACENOTE_HOST"))

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
            why = check(scope.get("method", "GET").upper(), scope.get("path", ""), headers, self.hosts)
            if why:
                await JSONResponse({"detail": f"refused: {why}"}, status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)
