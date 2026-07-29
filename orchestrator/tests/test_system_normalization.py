"""Qwen3.6 accepts exactly ONE system message, and only at index 0.

Anything else comes back as `400 System message must be at the beginning` —
which is every request this app makes, because compaction prepends a rolling
summary and appends the recall block, and search appends its sources. The fold
in llm.normalize_system is what keeps the app working on this model, so these
tests describe the guarantee rather than the implementation.
"""
import pytest

from app import llm


def roles(messages):
    return [m["role"] for m in messages]


# ---------------------------------------------------------------------------
# The invariant itself
# ---------------------------------------------------------------------------


def test_the_real_shape_this_app_produces_becomes_one_leading_system():
    """Engine prompt + rolling summary + recall block + question."""
    out = llm.normalize_system([
        {"role": "system", "content": "You are a Salesforce analyst."},
        {"role": "system", "content": "SUMMARY OF EARLIER TURNS: ..."},
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": "earlier answer"},
        {"role": "system", "content": "RETRIEVED CONTEXT: ..."},
        {"role": "user", "content": "now what?"},
    ])
    assert roles(out) == ["system", "user", "assistant", "user"]
    assert out.count({"role": "system", "content": out[0]["content"]}) == 1


@pytest.mark.parametrize("messages", [
    [{"role": "user", "content": "hi"}, {"role": "system", "content": "late block"}],
    [{"role": "system", "content": "a"}, {"role": "system", "content": "b"}],
    [{"role": "system", "content": "a"}, {"role": "user", "content": "q"},
     {"role": "system", "content": "b"}, {"role": "user", "content": "q2"}],
])
def test_never_more_than_one_system_and_never_after_a_turn(messages):
    out = llm.normalize_system(messages)
    assert roles(out).count("system") <= 1
    if "system" in roles(out):
        assert roles(out)[0] == "system"


def test_no_system_message_stays_untouched():
    msgs = [{"role": "user", "content": "hi"}]
    assert llm.normalize_system(msgs) == msgs


def test_input_is_not_mutated():
    """Callers reuse their message lists (retries, history persistence)."""
    msgs = [{"role": "system", "content": "a"}, {"role": "user", "content": "q"},
            {"role": "system", "content": "b"}]
    before = [dict(m) for m in msgs]
    llm.normalize_system(msgs)
    assert msgs == before


# ---------------------------------------------------------------------------
# What must survive the fold
# ---------------------------------------------------------------------------


def test_every_block_survives_in_order():
    out = llm.normalize_system([
        {"role": "system", "content": "FIRST"},
        {"role": "user", "content": "q"},
        {"role": "system", "content": "SECOND"},
        {"role": "system", "content": "THIRD"},
    ])
    text = out[0]["content"]
    assert text.index("FIRST") < text.index("SECOND") < text.index("THIRD")


def test_blocks_are_separated_not_run_together():
    """A fence that ends one block must not merge into the next one's text."""
    out = llm.normalize_system([
        {"role": "system", "content": "Rules end here."},
        {"role": "system", "content": "<<<UNTRUSTED>>> retrieved text"},
    ])
    assert "here.\n\n<<<UNTRUSTED>>>" in out[0]["content"]


def test_conversation_turns_keep_their_order():
    out = llm.normalize_system([
        {"role": "system", "content": "s"},
        {"role": "user", "content": "1"},
        {"role": "assistant", "content": "2"},
        {"role": "user", "content": "3"},
    ])
    assert [m["content"] for m in out[1:]] == ["1", "2", "3"]


def test_empty_system_blocks_do_not_create_a_stray_message():
    out = llm.normalize_system([
        {"role": "system", "content": "   "},
        {"role": "user", "content": "q"},
    ])
    assert roles(out) == ["user"]


def test_multimodal_content_is_left_alone_not_flattened():
    """Image parts are lists, not strings — folding them would corrupt them."""
    parts = [{"type": "text", "text": "look"},
             {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}}]
    out = llm.normalize_system([
        {"role": "system", "content": "s"},
        {"role": "user", "content": parts},
    ])
    assert out[-1]["content"] is parts


# ---------------------------------------------------------------------------
# Applied on every path that talks to a model
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [
    "chat_completion", "stream_chat_completion", "stream_chat_events",
    "router_chat_completion", "vision_chat_stream",
])
def test_every_send_path_normalizes(path):
    import inspect
    src = inspect.getsource(getattr(llm, path))
    assert "normalize_system" in src, f"{path} would 400 on Qwen3.6"


# ---------------------------------------------------------------------------
# Every event an engine emits must be on the SSE allowlist
# ---------------------------------------------------------------------------


def test_every_emitted_event_type_is_allowed():
    """`sse_event` RAISES on an unknown type, and it is called from inside the
    streaming response — so one unlisted event does not degrade a feature, it
    kills the whole answer mid-stream with no error event. Adding an emit
    without adding the name here is a silent, total failure."""
    import pathlib
    import re

    from app import sse

    root = pathlib.Path(sse.__file__).parent
    emitted = set()
    for path in root.rglob("*.py"):
        emitted.update(re.findall(r'emit\(\s*"(\w+)"', path.read_text()))
    unlisted = emitted - set(sse.ALL_EVENTS)
    assert not unlisted, f"emitted but not in ALL_EVENTS: {sorted(unlisted)}"
