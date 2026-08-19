"""Non-streaming reasoning parse: extension fields first, <think> fallback.

The non-streaming path used to DISCARD reasoning and, worse, a backend
without --reasoning-parser would leak the raw <think> block into the answer
text that downstream code re-parses. split_reasoning handles both shapes.
"""
import asyncio
from types import SimpleNamespace

from app import llm
from app.config import settings


def _message(content=None, reasoning=None, extra=None):
    return SimpleNamespace(
        content=content, reasoning=reasoning, tool_calls=None, model_extra=extra
    )


def test_parser_field_wins_and_content_stays_clean():
    message = _message(content="the answer", reasoning="the thought")
    reasoning, content = llm.split_reasoning(message, settings.main_capabilities)
    assert (reasoning, content) == ("the thought", "the answer")


def test_reasoning_content_via_model_extra_is_found():
    message = SimpleNamespace(
        content="answer", tool_calls=None,
        model_extra={"reasoning_content": "thought"},
    )
    reasoning, content = llm.split_reasoning(message, settings.main_capabilities)
    assert (reasoning, content) == ("thought", "answer")


def test_raw_think_block_falls_back_and_is_stripped():
    message = _message(content="<think>step by step…</think>\nthe answer")
    reasoning, content = llm.split_reasoning(message, settings.main_capabilities)
    assert reasoning == "step by step…"
    assert content == "the answer"


def test_think_block_mid_text_is_not_reasoning():
    """Only a LEADING block is a bypassed thinking pass; a model quoting
    '<think>' later in prose is content."""
    message = _message(content="the tag <think>looks</think> like this")
    reasoning, content = llm.split_reasoning(message, settings.main_capabilities)
    assert reasoning == ""
    assert content == "the tag <think>looks</think> like this"


def test_no_reasoning_anywhere_is_the_quiet_default():
    reasoning, content = llm.split_reasoning(
        _message(content="plain"), settings.main_capabilities
    )
    assert (reasoning, content) == ("", "plain")


def test_chat_completion_with_reasoning_returns_both(monkeypatch):
    async def passthrough(messages, *, base_url, model, requested_max_tokens=None):
        return list(messages), requested_max_tokens or 8192

    captured = {}

    class OneShot:
        async def create(self, **request):
            captured.update(request)
            return SimpleNamespace(choices=[SimpleNamespace(
                message=_message(content="answer", reasoning="thought")
            )])

    monkeypatch.setattr(llm.context, "fit_request", passthrough)
    monkeypatch.setattr(
        llm, "_openai_client",
        lambda: SimpleNamespace(chat=SimpleNamespace(completions=OneShot())),
    )
    reasoning, content = asyncio.run(llm.chat_completion_with_reasoning(
        [{"role": "user", "content": "q"}], effort="high", max_tokens=1000,
    ))
    assert (reasoning, content) == ("thought", "answer")
    # effort=high thinks; with budgets OFF (the default) the request is
    # floored at the unbounded-thinking ceiling.
    assert captured["max_tokens"] == settings.max_output_tokens


def test_chat_with_tools_strips_a_leaked_think_block(monkeypatch):
    async def passthrough(messages, *, base_url, model, requested_max_tokens=None):
        return list(messages), requested_max_tokens or 8192

    class OneShot:
        async def create(self, **request):
            return SimpleNamespace(choices=[SimpleNamespace(
                message=_message(content="<think>hm</think>calling nothing")
            )])

    monkeypatch.setattr(llm.context, "fit_request", passthrough)
    monkeypatch.setattr(
        llm, "_openai_client",
        lambda: SimpleNamespace(chat=SimpleNamespace(completions=OneShot())),
    )
    text, calls = asyncio.run(llm.chat_with_tools(
        [{"role": "user", "content": "q"}],
        tools=[{"type": "function", "function": {"name": "t", "parameters": {}}}],
    ))
    assert text == "calling nothing"
    assert calls == []
