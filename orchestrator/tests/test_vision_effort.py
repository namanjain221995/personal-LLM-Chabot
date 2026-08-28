"""The vision route honours the composer's Fast/Think/Max (2026-08-28).

Before this, `run_vision_engine` hard-coded `effort="medium"` — an alias for
"think" — so EVERY image upload ran the main 27B with thinking on regardless
of the picker. Measured on three 1280x800 screenshots: thinking on took
26-28s to the first visible token and produced ZERO answer chunks inside the
budget; thinking off answered in 2.3-2.9s. The fix is not a new mechanism —
`llm.stream_chat_events` already turns an effort into the chat template's
`enable_thinking` — it is the engine no longer overriding the caller.

Offline, like the rest of the suite: `llm.stream_chat_events` is faked where
only the forwarding matters, and `llm._client` is faked (the established
`tests/test_llm_clients.py` pattern) where the real effort -> thinking
mapping has to be proved end to end.
"""
import asyncio
import dataclasses
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import context, llm
from app.config import settings
from app.engines import router as router_engine
from app.engines import vision as vision_engine
from app.main import app

IMG = "aGVsbG8="


@pytest.fixture(autouse=True)
def _offline_vision(monkeypatch):
    """The OCR side-car is a separate service and irrelevant here; switching
    it off keeps these tests about effort and nothing else. The context
    window is pinned too, so the max_tokens assertions below describe the
    sizing logic rather than whatever a probe happened to cache."""
    monkeypatch.setattr(settings, "ocr_enabled", False)
    monkeypatch.setitem(
        context._window_cache, settings.openai_base_url, settings.model_max_context
    )


async def _collect(_event, _data):
    return None


def _fake_stream(recorder, pairs=(("token", "ok"),)):
    """Stand-in for llm.stream_chat_events that records how it was called."""

    async def fake(messages, *, model_choice="smart", effort="medium", **kwargs):
        recorder["model_choice"] = model_choice
        recorder["effort"] = effort
        recorder["max_tokens"] = kwargs.get("max_tokens")
        recorder["messages"] = list(messages)
        for pair in pairs:
            yield pair

    return fake


# ---------------------------------------------------------------------------
# The engine forwards the effort it is given
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("effort", ["fast", "think", "max"])
def test_engine_forwards_the_effort_it_is_given(monkeypatch, effort):
    rec = {}
    monkeypatch.setattr(llm, "stream_chat_events", _fake_stream(rec))

    asyncio.run(
        vision_engine.run_vision_engine("read this", IMG, [], _collect, effort=effort)
    )

    assert rec["effort"] == effort


@pytest.mark.parametrize(
    "wire,canonical",
    [("low", "fast"), ("medium", "think"), ("high", "think"), ("extra_high", "max")],
)
def test_engine_normalizes_legacy_wire_efforts(monkeypatch, wire, canonical):
    """Stored prefs and old clients still send the pre-collapse ladder; the
    engine canonicalizes through llm.normalize_effort like everything else."""
    rec = {}
    monkeypatch.setattr(llm, "stream_chat_events", _fake_stream(rec))

    asyncio.run(
        vision_engine.run_vision_engine("read this", IMG, [], _collect, effort=wire)
    )

    assert rec["effort"] == canonical


def test_engine_default_is_unchanged_when_no_effort_is_given(monkeypatch):
    """Callers that were not updated (graph.py's _vision_node, bare API
    calls) must keep pre-2026-08-28 behaviour exactly: thinking ON."""
    rec = {}
    monkeypatch.setattr(llm, "stream_chat_events", _fake_stream(rec))

    asyncio.run(vision_engine.run_vision_engine("read this", IMG, [], _collect))

    assert vision_engine.DEFAULT_EFFORT == "think"
    assert rec["effort"] == "think"
    assert llm.wants_thinking("smart", rec["effort"]) is True


def test_engine_still_pins_the_smart_model(monkeypatch):
    """No model fallback: the 27B IS the vision model, and the dedicated 8B
    VL model refused this work when measured."""
    rec = {}
    monkeypatch.setattr(llm, "stream_chat_events", _fake_stream(rec))

    asyncio.run(
        vision_engine.run_vision_engine("read this", IMG, [], _collect, effort="fast")
    )

    assert rec["model_choice"] == "smart"


# ---------------------------------------------------------------------------
# Token budget: generous, and never above what the endpoint declares
# ---------------------------------------------------------------------------


def test_budget_is_generous_and_within_the_declared_output_limit(monkeypatch):
    rec = {}
    monkeypatch.setattr(llm, "stream_chat_events", _fake_stream(rec))

    asyncio.run(
        vision_engine.run_vision_engine("read this", IMG, [], _collect, effort="fast")
    )

    limit = settings.vision_capabilities.output_limit
    assert rec["max_tokens"] == vision_engine.VISION_ANSWER_TOKENS
    assert rec["max_tokens"] <= limit
    # A thin budget is what starved the answer in the first place (measured:
    # 700 tokens with thinking on -> zero content chunks), so guard the floor.
    assert rec["max_tokens"] >= 4096


def test_budget_clamps_to_a_lowered_vision_output_limit(monkeypatch):
    """A deployment that lowers VISION_OUTPUT_LIMIT is never asked for more
    than it serves."""
    lowered = dataclasses.replace(settings.vision_capabilities, output_limit=2048)
    monkeypatch.setattr(settings, "vision_capabilities", lowered)
    assert vision_engine.vision_max_tokens() == 2048


def test_budget_honours_an_explicit_smaller_request(monkeypatch):
    rec = {}
    monkeypatch.setattr(llm, "stream_chat_events", _fake_stream(rec))

    asyncio.run(
        vision_engine.run_vision_engine(
            "read this", IMG, [], _collect, effort="fast", max_tokens=1500
        )
    )

    assert rec["max_tokens"] == 1500


# ---------------------------------------------------------------------------
# fast -> thinking OFF, end to end through the real llm layer
# ---------------------------------------------------------------------------


class _FakeClient:
    """Records create() kwargs and streams canned content deltas."""

    def __init__(self, recorder):
        self._recorder = recorder
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._chat_create)
        )

    async def _chat_create(self, **kwargs):
        self._recorder["chat_kwargs"] = kwargs

        async def gen():
            yield SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content="## All Data"))]
            )

        return gen()


def _patch_client(monkeypatch, recorder):
    fake = _FakeClient(recorder)

    def factory(base_url, api_key=None):
        recorder["base_url"] = base_url
        return fake

    monkeypatch.setattr(llm, "_client", factory)


@pytest.mark.parametrize(
    "effort,thinking",
    [("fast", False), ("think", True), ("max", True)],
)
def test_effort_reaches_the_chat_template_thinking_switch(monkeypatch, effort, thinking):
    """The whole fix in one assertion: the picker's level becomes
    chat_template_kwargs.enable_thinking on the real request body, via the
    helper (`llm.thinking_body`) the text route already uses."""
    rec = {}
    _patch_client(monkeypatch, rec)

    answer = asyncio.run(
        vision_engine.run_vision_engine("read this", IMG, [], _collect, effort=effort)
    )

    assert answer == "## All Data"
    extra_body = rec["chat_kwargs"]["extra_body"]
    assert extra_body["chat_template_kwargs"]["enable_thinking"] is thinking
    assert extra_body == llm.thinking_body(thinking)
    assert llm.wants_thinking("smart", effort) is thinking


def test_fast_does_not_get_the_thinking_floor(monkeypatch):
    """With thinking off the engine's own ceiling survives, so the answer has
    the whole budget to itself; with thinking on stream_chat_events floors the
    request at MAX_OUTPUT_TOKENS so the thought cannot eat the answer."""
    fast_rec, think_rec = {}, {}
    _patch_client(monkeypatch, fast_rec)
    asyncio.run(
        vision_engine.run_vision_engine("read this", IMG, [], _collect, effort="fast")
    )
    _patch_client(monkeypatch, think_rec)
    asyncio.run(
        vision_engine.run_vision_engine("read this", IMG, [], _collect, effort="think")
    )

    assert fast_rec["chat_kwargs"]["max_tokens"] == vision_engine.VISION_ANSWER_TOKENS
    assert think_rec["chat_kwargs"]["max_tokens"] > vision_engine.VISION_ANSWER_TOKENS


# ---------------------------------------------------------------------------
# main.py passes the request's effort through for an image request
# ---------------------------------------------------------------------------


def _parse_sse(body: str):
    events = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        lines = block.split("\n")
        events.append((lines[0][len("event: "):], json.loads(lines[1][len("data: "):])))
    return events


def _fake_engine(recorder):
    async def fake(message, images, history, emit, *, effort="think", max_tokens=None):
        recorder["effort"] = effort
        await emit("token", {"text": "seen"})
        await emit("meta", {"route": "vision"})
        return "seen"

    return fake


@pytest.mark.parametrize(
    "wire,applied",
    [("fast", "fast"), ("think", "think"), ("max", "max"), ("low", "fast"), ("high", "think")],
)
def test_chat_passes_the_request_effort_to_the_vision_engine(monkeypatch, wire, applied):
    rec = {}
    monkeypatch.setattr(vision_engine, "run_vision_engine", _fake_engine(rec))

    async def route_vision(message, has_image=False, history=()):
        return "vision"

    monkeypatch.setattr(router_engine, "route_request", route_vision)

    with TestClient(app) as client:
        resp = client.post(
            "/chat",
            json={"message": "what is in these", "images": [IMG, IMG], "effort": wire},
        )

    assert resp.status_code == 200
    # ChatRequest normalizes the wire value; the engine sees the canonical one.
    assert rec["effort"] == applied
    meta = dict(_parse_sse(resp.text))["meta"]
    # meta is trust metadata: it now reports the level that actually ran,
    # instead of the stale "no effort key: N/A".
    assert meta["route"] == "vision"
    assert meta["effort"] == applied
    assert meta["model"] == settings.vision_model


def test_image_request_without_an_effort_still_thinks(monkeypatch):
    """No effort in the payload → ChatRequest's default → today's behaviour."""
    rec = {}
    monkeypatch.setattr(vision_engine, "run_vision_engine", _fake_engine(rec))

    async def route_vision(message, has_image=False, history=()):
        return "vision"

    monkeypatch.setattr(router_engine, "route_request", route_vision)

    with TestClient(app) as client:
        resp = client.post("/chat", json={"message": "read this", "image": IMG})

    assert rec["effort"] == "think"
    assert dict(_parse_sse(resp.text))["meta"]["effort"] == "think"
