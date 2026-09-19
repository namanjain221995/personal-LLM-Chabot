"""A rewrite of pasted text at Think is one pass of the chat engine (hotfix 1.2, P1).

Live on production main 4e7cf8e, the reported turn at Think (a ~10,000-character
posting pasted as plain lines, "change it in the same way", a sample) went to
the multi-step AGENT 3 of 3 times: orchestrate.decide showed the classifier
only the first 2,000 characters - the posting - and never the ask. The agent
took 236-953 s and came back as plain lines. Through /chat here, with the
classifier stubbed to the answer it gave live.
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app import llm
from app.config import settings
from app.engines import agent as agent_engine

from tests.test_orchestrate import _REWRITE


def _events(body: str):
    out = []
    for block in body.split("\n\n"):
        lines = block.strip().split("\n")
        if len(lines) >= 2 and lines[0].startswith("event: "):
            out.append((lines[0][7:], json.loads(lines[1][6:])))
    return out


def test_the_reported_rewrite_at_think_is_answered_by_the_chat_engine(monkeypatch):
    from app.main import app

    async def classifier(messages, **kwargs):
        system = str(messages[0].get("content", ""))
        if "route a user's request" in system:
            return json.dumps({"agent": True, "search": False})
        return "no"

    async def no_agent(*args, **kwargs):  # pragma: no cover - reaching it is the failure
        raise AssertionError("a rewrite of pasted text went to the agent")

    async def stream(messages, **kwargs):
        yield ("token", "Rewritten.")

    monkeypatch.setattr(settings, "search_enabled", False)
    monkeypatch.setattr(llm, "router_chat_completion", classifier)
    monkeypatch.setattr(llm, "stream_chat_events", stream)
    monkeypatch.setattr(agent_engine, "run_agent_engine", no_agent)
    with TestClient(app) as client:
        resp = client.post(
            "/chat", json={"message": _REWRITE, "mode": "assistant", "effort": "think"}
        )
    assert resp.status_code == 200
    events = _events(resp.text)
    meta = [d for k, d in events if k == "meta"][-1]
    assert meta["route"] == "chat"
    assert not [k for k, _ in events if k == "error"]
