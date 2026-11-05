"""The removed Live Salesforce toggle, and a warehouse that is briefly busy.

Neither reaches the org any more (owner order 2026-11-04). An old browser may
still send sf_live: the server ignores it. The sync-worker briefly holding the
warehouse's write lock (WarehouseBusy) used to surface as a raw 'Could not set
lock' IO error on the user's screen.
"""
import asyncio

from fastapi.testclient import TestClient

from app.engines import sql as sql_engine
from app.engines.sql import WarehouseBusy, run_sql_engine
from app.main import app


class Rec:
    def __init__(self):
        self.events = []

    async def emit(self, e, d):
        self.events.append((e, d))

    def tokens(self):
        return "".join(d["text"] for e, d in self.events if e == "token")


def test_warehouse_busy_is_a_friendly_message(monkeypatch):
    """Never the raw 'Could not set lock' IO error again."""

    async def busy(*a, **k):
        raise WarehouseBusy("locked")

    monkeypatch.setattr(sql_engine, "generate_and_run_sql", busy)
    monkeypatch.setattr(sql_engine.os.path, "exists", lambda p: True)
    rec = Rec()
    asyncio.run(run_sql_engine("how many accounts?", [], rec.emit))
    text = rec.tokens()
    assert "being refreshed" in text
    assert "lock" not in text.lower()


def test_an_old_browser_sending_sf_live_takes_the_normal_route(monkeypatch):
    seen = {"graph": 0}

    class FakeGraph:
        async def ainvoke(self, state):
            seen["graph"] += 1
            await state["emit"]("meta", {"route": "sql"})
            return {"answer": "from the warehouse"}

    def boom(*a, **k):
        raise AssertionError("the toggle's own route ran")

    monkeypatch.setattr("app.engines.sql.run_sql_engine", boom)
    monkeypatch.setattr("app.main.get_graph", lambda: FakeGraph())

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
    assert seen["graph"] == 1
