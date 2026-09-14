"""llm.stream_chat_events — the `answer_plan` keyword (answer-quality C2/C6).

A fake main engine behind the real breaker, admission lanes and resilient
wrapper (the fixture of tests/test_llm_public_stream_kwargs.py); no network.

Pinned:
- with no plan, the request dict — extra_body, the 65,536 thinking floor, the
  temperature — is byte-identical to what the code sent before the keyword
  existed, for fast/low/think/max, the fast model choice and a continuation
  (the snapshot below was captured from the unmodified llm.py);
- the default LEGACY Fast plan sends exactly the same request as no plan;
- a routed thinking plan puts top_p and presence_penalty top-level, top_k and
  min_p inside extra_body next to chat_template_kwargs, turns thinking on,
  and is not floored at MAX_OUTPUT_TOKENS;
- a closure plan (thinking off, continue_final_message) keeps both the
  continuation keys and the sampling extensions in extra_body;
- a strict backend (no vLLM extensions) never receives top_k/min_p;
- a key this layer never sends (a seed) fails before anything is sent.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app import llm
from app.config import settings
from app.core import answer_sampling
from tests.test_llm_public_stream_kwargs import MODEL, MSGS, world  # noqa: F401 — fixture

_NO_THINK = {"chat_template_kwargs": {"enable_thinking": False}}
_THINK = {"chat_template_kwargs": {"enable_thinking": True}}
_BASE = {"model": MODEL, "stream": True, "stream_options": {"include_usage": True}}

#: Captured from llm.py BEFORE the answer_plan keyword (worktree base 1756614).
SNAPSHOT = {
    "fast_smart_8000_t06": {**_BASE, "extra_body": _NO_THINK, "max_tokens": 8000, "temperature": 0.6},
    "low_smart_8000_t06": {**_BASE, "extra_body": _NO_THINK, "max_tokens": 8000, "temperature": 0.6},
    "think_smart_16000_t03": {**_BASE, "extra_body": _THINK, "max_tokens": 65536, "temperature": 0.3},
    "max_smart_16000_t03": {**_BASE, "extra_body": _THINK, "max_tokens": 65536, "temperature": 0.3},
    "fast_fastmodel_6000_t06": {**_BASE, "extra_body": _NO_THINK, "max_tokens": 6000, "temperature": 0.6},
    "think_continue": {
        **_BASE,
        "extra_body": {**_THINK, "add_generation_prompt": False, "continue_final_message": True},
        "max_tokens": 860,
        "temperature": 0.3,
    },
}

CASES = {
    "fast_smart_8000_t06": dict(model_choice="smart", effort="fast", temperature=0.6, max_tokens=8000),
    "low_smart_8000_t06": dict(model_choice="smart", effort="low", temperature=0.6, max_tokens=8000),
    "think_smart_16000_t03": dict(model_choice="smart", effort="think", temperature=0.3, max_tokens=16000),
    "max_smart_16000_t03": dict(model_choice="smart", effort="max", temperature=0.3, max_tokens=16000),
    "fast_fastmodel_6000_t06": dict(model_choice="fast", effort="fast", temperature=0.6, max_tokens=6000),
    "think_continue": dict(model_choice="smart", effort="think", temperature=0.3, max_tokens=100,
                           continue_final_message=True),
}


def _sent(world, **kwargs) -> dict:
    async def run():
        world.engine.calls.clear()
        async for _ in llm.stream_chat_events(MSGS, **kwargs):
            pass
        call = dict(world.engine.calls[0])
        call.pop("messages")
        return call

    return asyncio.run(run())


@pytest.mark.parametrize("name", sorted(CASES))
def test_no_plan_is_byte_identical_to_before(world, name):
    assert _sent(world, **CASES[name]) == SNAPSHOT[name]
    assert _sent(world, **CASES[name], answer_plan=None) == SNAPSHOT[name]


@pytest.mark.parametrize("name", ["fast_smart_8000_t06", "low_smart_8000_t06", "fast_fastmodel_6000_t06"])
def test_the_default_legacy_fast_plan_sends_todays_request(world, name):
    kwargs = CASES[name]
    plan = answer_sampling.fast_sampling_for(
        "hello", [], mode="assistant", effort=llm.normalize_effort(kwargs["effort"]),
        model_choice=kwargs["model_choice"],
    )
    assert plan is not None and plan.profile == "legacy"
    assert _sent(world, **kwargs, answer_plan=plan) == SNAPSHOT[name]


def test_a_routed_thinking_plan_places_keys_and_skips_the_floor(world):
    plan = SimpleNamespace(sampling=answer_sampling.routed_thinking_sampling(), enable_thinking=True)
    sent = _sent(world, model_choice="smart", effort="fast", temperature=0.6, max_tokens=1024 + 8000,
                 answer_plan=plan)
    assert sent == {
        **_BASE,
        "max_tokens": 9024,
        "temperature": 1.0,
        "top_p": 0.95,
        "presence_penalty": 1.5,
        "extra_body": {"chat_template_kwargs": {"enable_thinking": True}, "top_k": 20, "min_p": 0.0},
    }
    assert sent["max_tokens"] < settings.max_output_tokens
    assert "seed" not in sent


def test_client_budget_mode_does_not_apply_to_a_plan(world, monkeypatch):
    monkeypatch.setattr(settings, "thinking_budget_mode", "client")
    plan = SimpleNamespace(sampling=answer_sampling.routed_thinking_sampling(), enable_thinking=True)
    sent = _sent(world, model_choice="smart", effort="think", temperature=0.3, max_tokens=9024, answer_plan=plan)
    # Not max_tokens + THINKING_BUDGET_HIGH: the plan sized the call itself.
    assert sent["max_tokens"] == 9024


def test_a_closure_plan_keeps_continuation_and_extensions_together(world):
    plan = SimpleNamespace(sampling=answer_sampling.closure_sampling("structured"), enable_thinking=False)
    messages = MSGS + [{"role": "assistant", "content": "<think>\nreasoning\n</think>\n\n"}]

    async def run():
        async for _ in llm.stream_chat_events(
            messages, model_choice="smart", effort="fast", temperature=0.6, max_tokens=100,
            continue_final_message=True, answer_plan=plan,
        ):
            pass
        return dict(world.engine.calls[0])

    sent = asyncio.run(run())
    assert sent["temperature"] == 1.0 and sent["top_p"] == 0.95
    assert "presence_penalty" not in sent
    assert sent["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": False},
        "top_k": 20,
        "min_p": 0.0,
        "continue_final_message": True,
        "add_generation_prompt": False,
    }


def test_enable_thinking_false_turns_off_think_effort(world):
    plan = SimpleNamespace(sampling={}, enable_thinking=False)
    sent = _sent(world, model_choice="smart", effort="think", temperature=0.3, max_tokens=500, answer_plan=plan)
    assert sent["extra_body"] == _NO_THINK
    assert sent["max_tokens"] == 500 and sent["temperature"] == 0.3


def test_enable_thinking_true_never_applies_to_the_fast_model_choice(world):
    plan = SimpleNamespace(sampling={}, enable_thinking=True)
    sent = _sent(world, model_choice="fast", effort="fast", temperature=0.6, max_tokens=500, answer_plan=plan)
    assert sent["extra_body"] == _NO_THINK


def test_a_strict_backend_gets_no_vllm_extensions(world, monkeypatch):
    strict = SimpleNamespace(
        supports_reasoning=False,
        reasoning_field=llm.ReasoningField.NONE,
        allows_extra_body=lambda name: False,
    )
    monkeypatch.setattr(llm, "capabilities_for_model_choice", lambda choice: strict)
    plan = SimpleNamespace(sampling=answer_sampling.routed_thinking_sampling(), enable_thinking=None)
    sent = _sent(world, model_choice="smart", effort="fast", temperature=0.6, max_tokens=500, answer_plan=plan)
    assert "extra_body" not in sent
    assert {k: sent[k] for k in ("temperature", "top_p", "presence_penalty")} == {
        "temperature": 1.0, "top_p": 0.95, "presence_penalty": 1.5,
    }
    assert "top_k" not in sent and "min_p" not in sent


def test_a_seed_is_refused_before_anything_is_sent(world):
    plan = SimpleNamespace(sampling={"temperature": 0.6, "seed": 7}, enable_thinking=None)

    async def run():
        async for _ in llm.stream_chat_events(MSGS, effort="fast", temperature=0.6, max_tokens=10, answer_plan=plan):
            pass

    with pytest.raises(ValueError):
        asyncio.run(run())
    assert world.engine.calls == []


def test_the_qwen_instruct_plan_on_the_wire(world):
    plan = answer_sampling.fast_sampling_for(
        "tell me about green tea", [], mode="assistant", effort="fast", model_choice="smart",
        profile="qwen_instruct", prose_presence=0.0,
    )
    sent = _sent(world, model_choice="smart", effort="fast", temperature=plan.sampling["temperature"],
                 max_tokens=plan.segment_max_tokens, answer_plan=plan)
    assert sent == {
        **_BASE,
        "max_tokens": 8000,
        "temperature": 0.7,
        "top_p": 0.8,
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}, "top_k": 20, "min_p": 0.0},
    }


@pytest.mark.parametrize("name", ["think_smart_16000_t03", "max_smart_16000_t03", "think_continue"])
def test_a_sampling_only_plan_keeps_thinking_sizing_untouched(world, name):
    # enable_thinking None: the plan changes sampling keys, never the thinking
    # switch, the client budget or the 65,536 floor, so a thinking decision
    # made elsewhere keeps its sizing when the default Fast plan rides along.
    plan = SimpleNamespace(sampling={"temperature": CASES[name]["temperature"]}, enable_thinking=None)
    assert _sent(world, **CASES[name], answer_plan=plan) == SNAPSHOT[name]


def test_a_sampling_only_plan_keeps_the_client_budget(world, monkeypatch):
    monkeypatch.setattr(settings, "thinking_budget_mode", "client")
    bare = _sent(world, model_choice="smart", effort="think", temperature=0.3, max_tokens=1000)
    assert bare["max_tokens"] > 1000  # the client budget was added
    plan = SimpleNamespace(sampling={"temperature": 0.3}, enable_thinking=None)
    assert _sent(world, model_choice="smart", effort="think", temperature=0.3, max_tokens=1000, answer_plan=plan) == bare


def test_the_forced_closure_retry_keeps_a_sampling_only_plans_extensions(monkeypatch):
    """Client budget mode rebuilds extra_body for the thinking-off retry; a
    sampling-only plan's top_k/min_p must survive that rebuild, and its
    top-level keys ride along with dict(request)."""
    from tests import test_reasoning_budgets as rb

    monkeypatch.setattr(settings, "thinking_budget_mode", "client")
    monkeypatch.setattr(settings, "thinking_budget_high", 10)
    monkeypatch.setattr(settings, "thinking_budget_grace", 1.2)
    async def passthrough(messages, *, base_url, model, requested_max_tokens=None):
        return list(messages), requested_max_tokens if requested_max_tokens else 8192

    monkeypatch.setattr(llm.context, "fit_request", passthrough)
    env = {}
    monkeypatch.setattr(llm, "_client", lambda *a, **k: env["client"])
    runaway = rb.FakeStream([rb._chunk(reasoning="t")] * 40)
    fallback = rb.FakeStream([rb._chunk(content="answer")])
    env["client"], completions = rb._fake_client([runaway, fallback])
    plan = SimpleNamespace(
        sampling={"temperature": 0.7, "top_p": 0.8, "top_k": 20, "min_p": 0.0}, enable_thinking=None
    )
    asyncio.run(rb._collect(llm.stream_chat_events(
        [{"role": "user", "content": "q"}], effort="think", max_tokens=500, answer_plan=plan,
    )))
    first, retry = completions.requests[0], completions.requests[1]
    assert first["extra_body"]["top_k"] == 20
    assert retry["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False
    assert (retry["extra_body"]["top_k"], retry["extra_body"]["min_p"]) == (20, 0.0)
    assert (retry["temperature"], retry["top_p"]) == (0.7, 0.8)
