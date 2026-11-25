"""Only a Salesforce-mode request reaches a Salesforce engine (design_v2 G6).

Defence in depth: main.py already gates both entry points on the mode. These
first lines keep it so for any other caller, with sf_assistant switched on.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.engines import sf_intel, sfk_bridge
from app.main import ChatRequest


def test_the_bridge_hands_back_a_non_salesforce_request_even_when_switched_on(monkeypatch):
    monkeypatch.setattr(sfk_bridge, "ASSISTANT_ENABLED", True)

    async def must_not_run(*_a, **_k):
        raise AssertionError("the assistant pipeline must not be asked")

    monkeypatch.setattr(sfk_bridge, "_try_assistant", must_not_run)

    async def emit(event, data):
        raise AssertionError("nothing may be emitted")

    for request in (
        ChatRequest(message="how many placements?", mode="assistant", conversation_id="c1"),
        SimpleNamespace(text="how many placements?", conversation_id="c1"),
    ):
        outcome = asyncio.run(sfk_bridge.try_answer("how many placements?", emit=emit, request=request, mode_sent=True))
        assert outcome is None


def test_sf_intel_hands_back_a_non_salesforce_mode_before_reading_any_state(monkeypatch):
    async def no_state(*_a, **_k):
        raise AssertionError("no Salesforce state may be read")

    monkeypatch.setattr(sf_intel.sf_state, "load_state", no_state)

    async def emit(event, data):
        raise AssertionError("nothing may be emitted")

    outcome = asyncio.run(sf_intel.run("how many placements?", [], emit, conversation_id="c1", mode="assistant"))
    assert outcome.handled is False and outcome.resolved_text == ""


def test_main_passes_the_mode_to_sf_intel():
    import inspect

    from app import main

    source = inspect.getsource(main.chat)
    call = source[source.index("sf_outcome = await sf_intel.run("):]
    call = call[: call.index(")\n") + 1]
    assert "mode=request.mode" in call
