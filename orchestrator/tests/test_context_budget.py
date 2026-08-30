"""Phase 0.2/0.3: per-model context budgeting.

The bug this prevents: a fixed max_tokens=8000 sent to the "fast" model, whose
window is far smaller than the main model's, left ~192 tokens of prompt room
and returned `This model's maximum context length is 8192 tokens` on a bare
"hi". Budgets are now derived from the window of the model that will actually
serve the call.
"""
import asyncio

import pytest

from app import context
from app import llm as llm_module
from app.config import settings
from app.engines import recent_turns


@pytest.fixture(autouse=True)
def clean_window_cache():
    context._window_cache.clear()
    yield
    context._window_cache.clear()


def fake_counter(window: int, per_message: int = 10):
    """Stand-in for /tokenize: counts messages, reports a fixed window."""

    async def counter(base_url, model, messages):
        return per_message * len(messages), window

    return counter


def fit(messages, *, window, requested=None, per_message=10):
    async def run():
        return await context.fit_request(
            messages,
            base_url="http://x/v1",
            model="m",
            requested_max_tokens=requested,
        )

    return run, window, per_message


def run_fit(monkeypatch, messages, *, window, requested=None, per_message=10):
    monkeypatch.setattr(context, "count_tokens", fake_counter(window, per_message))
    return asyncio.run(
        context.fit_request(
            messages,
            base_url="http://x/v1",
            model="m",
            requested_max_tokens=requested,
        )
    )


# ---------------------------------------------------------------------------
# Budget maths
# ---------------------------------------------------------------------------


def test_small_window_clamps_the_requested_output(monkeypatch):
    """The exact reported failure: 8000 requested against an 8192 window."""
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]
    sized, budget = run_fit(monkeypatch, msgs, window=8192, requested=8000)
    prompt = 10 * len(sized)
    assert prompt + budget + settings.context_safety_margin <= 8192
    assert budget < 8000  # clamped down to fit
    assert len(sized) == 2  # nothing needed to be dropped


def test_large_window_keeps_the_requested_output(monkeypatch):
    msgs = [{"role": "user", "content": "hi"}]
    _, budget = run_fit(monkeypatch, msgs, window=131072, requested=8000)
    assert budget == 8000


def test_never_exceeds_the_caller_ceiling(monkeypatch):
    msgs = [{"role": "user", "content": "hi"}]
    _, budget = run_fit(monkeypatch, msgs, window=131072, requested=500)
    assert budget == 500


def test_falls_back_to_configured_output_when_unspecified(monkeypatch):
    msgs = [{"role": "user", "content": "hi"}]
    _, budget = run_fit(monkeypatch, msgs, window=131072)
    assert budget == settings.model_max_output


def test_budget_is_never_zero_or_negative(monkeypatch):
    """Even an over-full prompt yields a positive, sendable max_tokens."""
    msgs = [{"role": "user", "content": "x"}]
    _, budget = run_fit(monkeypatch, msgs, window=8, requested=4000, per_message=100)
    assert budget >= 1


# ---------------------------------------------------------------------------
# Trimming
# ---------------------------------------------------------------------------


def test_trims_oldest_turns_until_the_completion_fits(monkeypatch):
    msgs = [{"role": "system", "content": "sys"}] + [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"}
        for i in range(20)
    ]
    sized, budget = run_fit(
        monkeypatch, msgs, window=1000, requested=400, per_message=40
    )
    assert len(sized) < len(msgs)  # something was dropped
    assert budget >= context.MIN_OUTPUT_TOKENS
    # The pinned system block and the newest turn both survive.
    assert sized[0]["content"] == "sys"
    assert sized[-1]["content"] == "m19"


def test_trimming_keeps_every_pinned_system_block():
    msgs = [
        {"role": "system", "content": "engine instructions"},
        {"role": "system", "content": "recall from other chats"},
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "older answer"},
        {"role": "user", "content": "newest"},
    ]
    trimmed = context.trim_to_fit(msgs, 2)
    assert [m["content"] for m in trimmed] == [
        "engine instructions",
        "recall from other chats",
        "newest",
    ]


def test_trimming_never_drops_the_final_message():
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "only turn"},
    ]
    assert context.trim_to_fit(msgs, 99) == msgs


# ---------------------------------------------------------------------------
# Estimation fallback + clipping
# ---------------------------------------------------------------------------


def test_estimate_is_pessimistic_enough():
    # ~4 chars/token in reality; the estimate must not UNDER-count.
    text = "the quick brown fox jumps over the lazy dog " * 20
    assert context.estimate_tokens(text) >= len(text) / 4


def test_service_root_strips_the_v1_suffix():
    assert context.service_root("http://vllm:30000/v1") == "http://vllm:30000"
    assert context.service_root("http://vllm:30000/v1/") == "http://vllm:30000"
    assert context.service_root("http://vllm:30000") == "http://vllm:30000"


def test_clip_message_contents_caps_long_inputs():
    msgs = [{"role": "user", "content": "x" * 10000}, {"role": "user", "content": "ok"}]
    clipped = context.clip_message_contents(msgs, 100)
    assert len(clipped[0]["content"]) < 200
    assert "truncated" in clipped[0]["content"]
    assert clipped[1]["content"] == "ok"  # short content untouched


def test_count_tokens_falls_back_to_an_estimate_when_tokenize_fails(monkeypatch):
    msgs = [{"role": "user", "content": "hello there"}]
    # No server is listening on this port in tests.
    count, window = asyncio.run(
        context.count_tokens("http://127.0.0.1:9/v1", "m", msgs)
    )
    assert count > 0
    assert window is None


# ---------------------------------------------------------------------------
# 0.6 — system blocks survive the per-engine turn slice
# ---------------------------------------------------------------------------


def test_recent_turns_keeps_system_blocks_beyond_the_window():
    history = [
        {"role": "system", "content": "pages the user shared earlier"},
        *[
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"t{i}"}
            for i in range(12)
        ],
    ]
    kept = recent_turns(history, 4)
    assert kept[0]["content"] == "pages the user shared earlier"
    assert [m["content"] for m in kept[1:]] == ["t8", "t9", "t10", "t11"]


def test_recent_turns_matches_a_plain_slice_when_there_is_no_system_block():
    history = [{"role": "user", "content": f"t{i}"} for i in range(10)]
    assert recent_turns(history, 4) == history[-4:]


def test_recent_turns_handles_short_history():
    history = [{"role": "user", "content": "only"}]
    assert recent_turns(history, 6) == history
    assert recent_turns([], 6) == []


# ---------------------------------------------------------------------------
# A SINGLE message larger than the whole window (turn-dropping cannot help)
# ---------------------------------------------------------------------------


def realistic_counter(window: int):
    """Counts by content length, so clipping actually reduces the count."""

    async def counter(base_url, model, messages):
        total = sum(
            len(m.get("content", "")) // 3 + 4
            for m in messages
            if isinstance(m.get("content"), str)
        )
        return total, window

    return counter


def test_oversized_single_message_is_clipped_not_rejected(monkeypatch):
    monkeypatch.setattr(context, "count_tokens", realistic_counter(8192))
    huge = "".join(f"filler sentence {i}. " for i in range(20000))  # ~400k chars
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": huge + "\n\nReply with ACKNOWLEDGED."},
    ]
    sized, budget = asyncio.run(
        context.fit_request(
            msgs, base_url="http://x/v1", model="m", requested_max_tokens=8000
        )
    )
    prompt = sum(len(m["content"]) // 3 + 4 for m in sized)
    assert prompt + budget + settings.context_safety_margin <= 8192
    assert budget >= context.MIN_OUTPUT_TOKENS
    assert len(sized[-1]["content"]) < len(huge)  # it was shrunk


def test_clip_middle_keeps_the_head_and_the_trailing_instruction():
    text = "HEAD-MARKER" + ("x" * 5000) + "TAIL-INSTRUCTION"
    out = context.clip_middle(text, 400)
    assert out.startswith("HEAD-MARKER")
    assert out.endswith("TAIL-INSTRUCTION")
    assert "omitted to fit the context window" in out
    assert len(out) < len(text)


def test_clip_middle_leaves_short_text_alone():
    assert context.clip_middle("short", 100) == "short"


def test_fit_loop_terminates_on_pathological_input(monkeypatch):
    """A message that cannot shrink below the floor must still return."""
    monkeypatch.setattr(context, "count_tokens", realistic_counter(64))
    msgs = [{"role": "user", "content": "y" * 100000}]
    sized, budget = asyncio.run(
        context.fit_request(msgs, base_url="http://x/v1", model="m")
    )
    assert budget >= 1  # always sendable
    assert len(sized) == 1


# ---------------------------------------------------------------------------
# 0.9-5: trimming is REPORTED, not silent
# ---------------------------------------------------------------------------


def _fit_and_read_notice(messages, **kwargs):
    """Run fit_request and read the notice INSIDE the same context.

    asyncio.run() executes the coroutine in a fresh context, so a ContextVar
    set in there is invisible to the caller afterwards. Production reads it
    from within the same request task (main.py's worker), which is what
    test_trim_notice_reaches_the_chat_meta exercises end to end.
    """

    async def run():
        context.reset_trim_notice()
        await context.fit_request(messages, **kwargs)
        return context.get_trim_notice()

    return asyncio.run(run())


def test_trim_notice_records_what_was_removed(monkeypatch):
    monkeypatch.setattr(context, "count_tokens", realistic_counter(8192))
    huge = "".join(f"filler {i}. " for i in range(20000))
    notice = _fit_and_read_notice(
        [{"role": "user", "content": huge}],
        base_url="http://x/v1",
        model="m",
        requested_max_tokens=4000,
    )
    assert notice is not None
    assert notice["clipped_messages"] >= 1


def test_no_trim_notice_when_everything_fits(monkeypatch):
    monkeypatch.setattr(context, "count_tokens", fake_counter(131072))
    notice = _fit_and_read_notice(
        [{"role": "user", "content": "hi"}], base_url="http://x/v1", model="m"
    )
    assert notice is None


def test_trim_notice_reports_dropped_turns(monkeypatch):
    msgs = [{"role": "system", "content": "sys"}] + [
        {"role": "user", "content": f"m{i}"} for i in range(20)
    ]
    monkeypatch.setattr(context, "count_tokens", fake_counter(1000, 40))
    notice = _fit_and_read_notice(
        msgs, base_url="http://x/v1", model="m", requested_max_tokens=400
    )
    assert notice and notice["dropped_turns"] > 0


def test_trim_notice_reaches_the_chat_meta(monkeypatch, tmp_path):
    """The UI can only show the notice if /chat puts it in meta."""
    import json as _json

    from fastapi.testclient import TestClient

    from app.config import settings as _settings
    from app.main import app

    async def fake_events(messages, **kwargs):
        # Pretend the budget module had to clip something on this request.
        context._record_trim(0, 1)
        yield "token", "ok"

    monkeypatch.setattr(llm_module, "stream_chat_events", fake_events)
    with TestClient(app) as client:
        resp = client.post("/chat", json={"message": "hi", "mode": "assistant"})
    metas = [
        _json.loads(line[6:])
        for block in resp.text.strip().split("\n\n")
        for line in block.split("\n")
        if block.startswith("event: meta") and line.startswith("data: ")
    ]
    assert metas and metas[0]["input_trimmed"] == {
        "dropped_turns": 0,
        "clipped_messages": 1,
    }


def test_count_tokens_folds_system_messages_before_tokenize(monkeypatch):
    """Engines carry several system messages (their own prompt + recall
    blocks prepended to history). The model's chat template raises "System
    message must be at the beginning" on any extra one, so the raw list made
    every /tokenize call 400, every count fell back to the pessimistic
    estimate, and the engine log filled with tracebacks (2026-08-30). The
    tokenize payload must be the SAME folded shape the completion sends.
    """
    import asyncio

    from app import context

    sent = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"count": 42, "max_model_len": 1000}

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            sent.update(json or {})
            return _Resp()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    messages = [
        {"role": "system", "content": "engine prompt"},
        {"role": "system", "content": "recall block"},
        {"role": "user", "content": "hi"},
    ]
    count, window = asyncio.run(context.count_tokens("http://x/v1", "m", messages))
    assert count == 42
    roles = [m["role"] for m in sent["messages"]]
    assert roles == ["system", "user"]  # folded to ONE leading system
    assert "engine prompt" in sent["messages"][0]["content"]
    assert "recall block" in sent["messages"][0]["content"]
