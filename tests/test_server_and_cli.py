
from fastapi.testclient import TestClient

from pacenote.capture import ReplaySource
from pacenote.cli import main
from pacenote.llm import OfflineEngine
from pacenote.server import create_app
from pacenote.session import Session, Settings, purge

from .conftest import SAMPLES


def _session(tmp_path, client_cfg):
    return Session(
        client=client_cfg,
        session_dir=tmp_path / "web",
        engine=OfflineEngine(),
        source_factory=lambda d: ReplaySource({"fr": SAMPLES / "fr.srt", "en": SAMPLES / "en.srt"}, speed=0),
        settings=Settings(sync_interval=3600, poll_interval=0.05),
    )


def test_web_flow(tmp_path, client_cfg):
    session = _session(tmp_path, client_cfg)
    with TestClient(create_app(session)) as http:
        page = http.get("/")
        assert page.status_code == 200 and "/static/app.js" in page.text
        assert http.get("/static/app.js").status_code == 200
        st = http.get("/api/state").json()
        assert st["mode"] == "consent" and "transcrit" in st["consent"]["fr"]
        assert st["current"]["id"] == "strategy-success" and len(st["next"]) == 3
        assert http.post("/api/sync").status_code == 409  # not started yet

        st = http.post("/api/start").json()
        assert st["mode"] == "interview" and st["started_at"]
        cur = st["current"]["id"]
        st = http.post(f"/api/questions/{cur}/answered").json()
        assert st["current"]["id"] != cur
        assert http.post(f"/api/questions/{cur}/answered").status_code == 409
        assert http.post("/api/wispr", json={"text": "transcript"}).json()["wispr_merged"]
        assert http.get("/plan.html").status_code == 404
        st = http.post("/api/enough").json()
        assert st["enough_data"] and st["mode"] == "finalize"


def test_sse_stream_sends_state(tmp_path, client_cfg):
    import asyncio
    import json

    session = _session(tmp_path, client_cfg)
    app = create_app(session)
    route = next(r for r in app.routes if getattr(r, "path", "") == "/api/events")

    class Req:
        async def is_disconnected(self):
            return False

    async def first_event():
        resp = await route.endpoint(Req())
        chunk = await resp.body_iterator.__anext__()
        await resp.body_iterator.aclose()
        return chunk

    chunk = asyncio.run(first_event())
    assert chunk.startswith("event: state\ndata: ")
    assert json.loads(chunk.split("data: ", 1)[1])["client"] == "Brioche & Four"


def test_purge(tmp_path):
    sessions = tmp_path / "sessions"
    for sid in ("20260101-100000", "20260102-100000"):
        d = sessions / "brioche-four" / sid
        d.mkdir(parents=True)
        (d / "fr.srt").write_text("x")
    (sessions / "other").mkdir()
    assert main(["purge", "--client", "brioche-four", "--sessions-dir", str(sessions),
                 "--session", "20260101-100000", "--yes"]) == 0
    assert not (sessions / "brioche-four" / "20260101-100000").exists()
    assert (sessions / "brioche-four" / "20260102-100000").exists()
    assert main(["purge", "--client", "brioche-four", "--sessions-dir", str(sessions), "--yes"]) == 0
    assert not (sessions / "brioche-four").exists() and (sessions / "other").exists()
    import pytest
    with pytest.raises(ValueError):
        purge(sessions, "../outside")
    with pytest.raises(ValueError):
        purge(sessions, "other", "../../x")


def test_new_client_scaffold_is_loadable(tmp_path):
    from pacenote.questionnaire import load_client

    assert main(["new-client", "acme", "--name", "ACME", "--clients-dir", str(tmp_path)]) == 0
    cfg = load_client(tmp_path, "acme")
    assert cfg.questionnaire.client == "ACME" and "FDE" in cfg.glossary
    assert main(["new-client", "Bad Slug", "--name", "x", "--clients-dir", str(tmp_path)]) == 2
