"""Detached generation lifecycle (LiveGeneration + /chat/active, /chat/stop,
/chat/attach). ChatGPT-style: a generation keeps running when the client
disconnects, can be re-attached to (with full replay), can be stopped
explicitly, and persists its answer server-side only when nobody was attached
at completion.
"""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app import llm
from app.main import LiveGeneration, _finalize_generation, _live_generations, app


def _parse_sse(text: str):
    events = []
    for block in text.strip().split("\n\n"):
        lines = block.strip().split("\n")
        if len(lines) >= 2 and lines[0].startswith("event: "):
            events.append((lines[0][7:], json.loads(lines[1][6:])))
    return events


def _fake_stream(deltas):
    async def fake(messages, **kwargs):
        for kind, text in deltas:
            yield kind, text

    return fake


# ---------------------------------------------------------------------------
# LiveGeneration unit behavior
# ---------------------------------------------------------------------------


def test_follow_replays_buffer_then_streams_live():
    async def scenario():
        gen = LiveGeneration("conv-1", None)
        await gen.publish("token", {"text": "a"})
        await gen.publish("token", {"text": "b"})

        got = []

        async def reader():
            async for frame in gen.follow():
                got.append(frame)

        task = asyncio.create_task(reader())
        await asyncio.sleep(0.01)  # reader drains the replayed buffer
        assert len(got) == 2  # replayed
        await gen.publish("done", {})
        await gen.finish()
        await asyncio.wait_for(task, 1)
        assert len(got) == 3
        assert gen.subscribers == 0

    asyncio.run(scenario())


def test_finalize_persists_only_when_detached(monkeypatch):
    saved = []
    from app import db as app_db

    monkeypatch.setattr(
        app_db, "add_message", lambda *a: saved.append(a) or {"id": 1}
    )

    async def scenario():
        # Nobody attached + signed in + has an answer → server persists.
        gen = LiveGeneration("conv-2", 7)
        gen.answer = "The answer"
        gen.final_meta = {"route": "chat"}
        _live_generations["conv-2"] = gen
        await _finalize_generation("conv-2", gen)
        assert "conv-2" not in _live_generations
        assert saved == [(7, "conv-2", "assistant", "The answer", {"route": "chat"})]

        # A subscriber is attached → the client persists; server must not.
        gen2 = LiveGeneration("conv-3", 7)
        gen2.answer = "x"
        gen2.subscribers = 1
        await _finalize_generation("conv-3", gen2)
        assert len(saved) == 1

        # Cancelled generations persist nothing.
        gen3 = LiveGeneration("conv-4", 7)
        gen3.answer = "partial"
        gen3.cancelled = True
        await _finalize_generation("conv-4", gen3)
        assert len(saved) == 1

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@pytest.fixture()
def assistant_stream(monkeypatch):
    monkeypatch.setattr(
        llm, "stream_chat_events", _fake_stream([("token", "Hello!")])
    )


def test_active_empty_and_attach_404_after_completion(assistant_stream):
    with TestClient(app) as client:
        resp = client.post(
            "/chat",
            json={
                "message": "hi",
                "mode": "assistant",
                "conversation_id": "det-1",
            },
        )
        assert resp.status_code == 200
        kinds = [e for e, _ in _parse_sse(resp.text)]
        assert kinds[-1] == "done"

        # Finished → no longer active, attach 404s (answer is in history).
        assert client.get("/chat/active").json() == {"active": []}
        assert client.get("/chat/attach/det-1").status_code == 404


def test_stop_without_generation_is_a_noop():
    with TestClient(app) as client:
        resp = client.post("/chat/stop", json={"conversation_id": "nope"})
        assert resp.status_code == 200
        assert resp.json() == {"stopped": False}


def test_new_send_replaces_running_generation_for_same_conversation():
    async def scenario():
        gen = LiveGeneration("conv-r", None)

        async def hang():
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                gen.cancelled = True
                raise

        gen.task = asyncio.create_task(hang())
        _live_generations["conv-r"] = gen
        await asyncio.sleep(0)

        # Simulate what POST /chat does on a same-conversation resend.
        previous = _live_generations.get("conv-r")
        assert previous is not None and not previous.done
        previous.task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await previous.task
        assert gen.cancelled
        _live_generations.pop("conv-r", None)

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Ownership: a generation is only reachable by the identity that started it
# ---------------------------------------------------------------------------


def test_generations_are_owner_scoped(monkeypatch):
    """attach/stop/active must not expose another account's generation."""
    from app import main as app_main

    gen = LiveGeneration("owned", user_id=42)
    _live_generations["owned"] = gen
    try:
        # Anonymous caller (no cookie) is NOT user 42.
        with TestClient(app) as client:
            assert client.get("/chat/active").json() == {"active": []}
            assert client.get("/chat/attach/owned").status_code == 404
            assert client.post(
                "/chat/stop", json={"conversation_id": "owned"}
            ).json() == {"stopped": False}

        # The owner sees it.
        monkeypatch.setattr(app_main, "_viewer_id", lambda _req: 42)
        with TestClient(app) as client:
            assert client.get("/chat/active").json() == {"active": ["owned"]}
    finally:
        _live_generations.pop("owned", None)


def test_owns_matches_identity_exactly():
    from app.main import _owns

    assert _owns(LiveGeneration("c", 5), 5)
    assert not _owns(LiveGeneration("c", 5), 6)
    assert not _owns(LiveGeneration("c", 5), None)  # signed-out cannot reach
    assert not _owns(LiveGeneration("c", None), 5)  # signed-in != anonymous
    assert _owns(LiveGeneration("c", None), None)
