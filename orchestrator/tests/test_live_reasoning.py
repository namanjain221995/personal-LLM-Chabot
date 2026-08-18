"""LIVE reasoning separation — opt-in, against a real vLLM endpoint.

Skipped unless LIVE_VLLM_BASE_URL is set (the suite stays offline by
default, per conftest's contract). On the DGX box the main model is
published at http://127.0.0.1:8000/v1 by the published overlay:

    LIVE_VLLM_BASE_URL=http://127.0.0.1:8000/v1 .venv/bin/python -m pytest \
        tests/test_live_reasoning.py -q

Asserts the mission's Phase 1 contract end to end through OUR stack
(llm.stream_chat_events, not a raw client): reasoning deltas arrive on the
separate channel at high effort, and never at fast/low.
"""
import asyncio
import os

import pytest

from app import llm
from app.config import settings

LIVE = os.environ.get("LIVE_VLLM_BASE_URL", "").strip()

pytestmark = pytest.mark.skipif(
    not LIVE, reason="LIVE_VLLM_BASE_URL not set — live vLLM test is opt-in"
)


@pytest.fixture()
def live(monkeypatch):
    monkeypatch.setattr(settings, "openai_base_url", LIVE)
    # Keep the live run SHORT: the point is channel separation, not depth.
    monkeypatch.setattr(settings, "thinking_budget_high", 200)
    return LIVE


async def _run(effort: str):
    kinds = {"reasoning": 0, "token": 0}
    async for kind, _delta in llm.stream_chat_events(
        [{"role": "user", "content": "In one short sentence: why is the sky blue?"}],
        effort=effort,
        max_tokens=120,
    ):
        kinds[kind] += 1
    return kinds


def test_high_reasons_on_the_separate_channel(live):
    kinds = asyncio.run(_run("high"))
    assert kinds["reasoning"] > 0, "high effort produced no reasoning deltas"
    assert kinds["token"] > 0, "high effort produced no answer"


@pytest.mark.parametrize("effort", ["fast", "low"])
def test_fast_and_low_never_reason(live, effort):
    kinds = asyncio.run(_run(effort))
    assert kinds["reasoning"] == 0, f"{effort} leaked reasoning deltas"
    assert kinds["token"] > 0
