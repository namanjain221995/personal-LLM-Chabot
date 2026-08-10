"""The Live Salesforce toggle and the WarehouseBusy fallback (2026-08-06).

Two ways a turn reaches the org directly: the composer's Live Salesforce
toggle (sf_live → force_live), and the sync-worker briefly holding the
warehouse's write lock (WarehouseBusy) — which used to surface as a raw
'Could not set lock' IO error on the user's screen.
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.engines import sql as sql_engine
from app.engines.sql import WarehouseBusy, run_sql_engine
from app.main import app


class Rec:
    def __init__(self):
        self.events = []

    async def emit(self, e, d):
        self.events.append((e, d))

    def statuses(self):
        return [d["text"] for e, d in self.events if e == "status"]

    def tokens(self):
        return "".join(d["text"] for e, d in self.events if e == "token")


@pytest.fixture()
def live_org(monkeypatch):
    """A fake live Salesforce + streaming model behind the sql engine."""
    from app import llm
    from app.core import salesforce as sf
    import app.engines.live_sf as live_sf

    monkeypatch.setattr(settings, "sf_live_enabled", True)
    monkeypatch.setattr(sf, "configured", lambda: True)

    async def fake_live(question, history=()):
        return "SELECT COUNT() FROM Account", [{"count": 382}]

    monkeypatch.setattr(live_sf, "fetch_live", fake_live)

    async def fake_stream(msgs, **kw):
        yield "token", "382 accounts."

    monkeypatch.setattr(llm, "stream_chat_events", fake_stream)


def test_force_live_skips_the_warehouse_entirely(live_org, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("warehouse consulted despite force_live")

    monkeypatch.setattr(sql_engine, "generate_and_run_sql", boom)
    rec = Rec()
    asyncio.run(run_sql_engine("how many accounts?", [], rec.emit, force_live=True))
    assert any("live" in s.lower() for s in rec.statuses())
    metas = [d for e, d in rec.events if e == "meta"]
    assert metas and metas[-1]["sql"] == "SELECT COUNT() FROM Account"


def test_warehouse_busy_falls_back_to_live(live_org, monkeypatch):
    async def busy(*a, **k):
        raise WarehouseBusy("locked by the sync worker")

    monkeypatch.setattr(sql_engine, "generate_and_run_sql", busy)
    monkeypatch.setattr(sql_engine.os.path, "exists", lambda p: True)
    rec = Rec()
    asyncio.run(run_sql_engine("how many accounts?", [], rec.emit))
    assert any("refreshed" in s.lower() for s in rec.statuses())
    assert "382" in rec.tokens()


def test_warehouse_busy_without_live_is_a_friendly_message(monkeypatch):
    """Never the raw 'Could not set lock' IO error again."""
    from app.core import salesforce as sf

    async def busy(*a, **k):
        raise WarehouseBusy("locked")

    monkeypatch.setattr(sql_engine, "generate_and_run_sql", busy)
    monkeypatch.setattr(sql_engine.os.path, "exists", lambda p: True)
    monkeypatch.setattr(sf, "configured", lambda: False)
    rec = Rec()
    asyncio.run(run_sql_engine("how many accounts?", [], rec.emit))
    text = rec.tokens()
    assert "being refreshed" in text
    assert "lock" not in text.lower()


def test_sf_live_request_reaches_the_engine_with_force_live(monkeypatch):
    seen = {}

    async def fake_engine(message, history, emit, *, force_live=False):
        seen["force_live"] = force_live
        await emit("meta", {"route": "sql"})
        return "ok"

    monkeypatch.setattr("app.engines.sql.run_sql_engine", fake_engine)

    async def no_auto(*a, **k):
        from app.engines.orchestrate import Plan

        return Plan(agent=False, search=False)

    monkeypatch.setattr("app.engines.orchestrate.decide", no_auto)

    with TestClient(app) as c:
        resp = c.post("/chat", json={
            "message": "how many accounts right now?",
            "mode": "salesforce", "sf_live": True, "effort": "medium",
        })
    assert resp.status_code == 200
    assert seen.get("force_live") is True
