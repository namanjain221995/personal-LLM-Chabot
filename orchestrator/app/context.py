"""Per-request context budgeting (Phase 0.2/0.3).

Every model call is bounded by the window of the model that will actually
serve it — which is NOT one global number: the main model runs at 131072 and
the "fast"/router model at a much smaller window. Sending a fixed
`max_tokens=8000` to the small model left ~192 tokens of prompt room and
returned a 400 on a bare "hi".

The window is read from the serving vLLM itself (`POST /tokenize` returns both
the exact chat-template token count and `max_model_len`), so it can never
drift from what the server is actually running. Counts are exact; the
character estimate is only a fallback for when the endpoint is unreachable.

Nothing here performs network I/O at import time.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from typing import List, Optional, Sequence, Tuple

from .config import settings

# What (if anything) had to be removed to make the last request fit, so the
# answer can say so instead of silently dropping part of a user's paste. A
# ContextVar keeps this per-request without threading a return value through
# every engine; each chat runs in its own asyncio task.
_trim_notice: ContextVar[Optional[dict]] = ContextVar("_trim_notice", default=None)


def reset_trim_notice() -> None:
    _trim_notice.set(None)


def get_trim_notice() -> Optional[dict]:
    return _trim_notice.get()


def _record_trim(dropped_turns: int, clipped_messages: int) -> None:
    if not dropped_turns and not clipped_messages:
        return
    prev = _trim_notice.get() or {"dropped_turns": 0, "clipped_messages": 0}
    _trim_notice.set(
        {
            "dropped_turns": prev["dropped_turns"] + dropped_turns,
            "clipped_messages": prev["clipped_messages"] + clipped_messages,
        }
    )

# Never emit a completion shorter than this; below it, trim history instead.
MIN_OUTPUT_TOKENS = 256

# Fallback when /tokenize is unavailable. Deliberately pessimistic (real text
# averages ~4 chars/token) so an estimate errs toward a smaller prompt.
_CHARS_PER_TOKEN = 3.0

# Bound on the fit loop: each round drops a turn or shrinks the largest
# message, so this cannot spin, but a hard cap keeps a pathological input
# from making dozens of tokenize round-trips.
_MAX_FIT_ROUNDS = 24
# Never clip a message below this — an unreadably short prompt is worse than
# a slightly over-budget one (which the final clamp still makes sendable).
_MIN_CLIPPED_CHARS = 2000

# base_url -> max_model_len, learned once per process.
_window_cache: dict = {}
_lock = asyncio.Lock()


def service_root(base_url: str) -> str:
    """`http://vllm:30000/v1` → `http://vllm:30000` (tokenize is not under /v1)."""
    root = base_url.rstrip("/")
    return root[: -len("/v1")] if root.endswith("/v1") else root


def estimate_tokens(text: str) -> int:
    return int(len(text or "") / _CHARS_PER_TOKEN) + 1


def estimate_messages(messages: Sequence[dict]) -> int:
    """Rough token count for a message list, including per-message overhead."""
    total = 0
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            # Multimodal parts: only the text parts are countable here; image
            # parts are handled by the server's own limits.
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    total += estimate_tokens(part["text"])
        total += 4  # role + delimiters
    return total + 3  # generation primer


async def count_tokens(
    base_url: str, model: str, messages: Sequence[dict]
) -> Tuple[int, Optional[int]]:
    """Exact (token_count, max_model_len) from vLLM, or an estimate.

    Returns max_model_len=None when the server could not be asked, so callers
    can fall back to the configured window.
    """
    import httpx

    payload = {"model": model, "messages": list(messages)}
    try:
        async with httpx.AsyncClient(timeout=settings.tokenize_timeout) as client:
            resp = await client.post(
                f"{service_root(base_url)}/tokenize", json=payload
            )
            resp.raise_for_status()
            data = resp.json()
        count = int(data["count"])
        window = data.get("max_model_len")
        window = int(window) if window else None
        if window:
            _window_cache[base_url] = window
        return count, window
    except Exception:
        # Multimodal payloads and transient failures land here; estimate.
        return estimate_messages(messages), _window_cache.get(base_url)


async def model_window(base_url: str, model: str) -> int:
    """The serving model's context window, cached per base URL."""
    cached = _window_cache.get(base_url)
    if cached:
        return cached
    async with _lock:
        cached = _window_cache.get(base_url)
        if cached:
            return cached
        _, window = await count_tokens(base_url, model, [{"role": "user", "content": "x"}])
        resolved = window or settings.model_max_context
        _window_cache[base_url] = resolved
        return resolved


def _split_pinned(messages: Sequence[dict]) -> Tuple[List[dict], List[dict]]:
    """Leading system messages are pinned; the rest is trimmable history.

    The leading block carries the engine's instructions plus the cross-chat
    recall and shared-page context that main.py prepends — dropping those
    changes the answer, so they survive trimming.
    """
    msgs = list(messages)
    i = 0
    while i < len(msgs) and msgs[i].get("role") == "system":
        i += 1
    return msgs[:i], msgs[i:]


def trim_to_fit(messages: Sequence[dict], drop: int) -> List[dict]:
    """Drop `drop` OLDEST trimmable turns, always keeping the final message."""
    pinned, rest = _split_pinned(messages)
    if len(rest) <= 1:
        return list(messages)
    keep_from = min(drop, len(rest) - 1)
    return pinned + rest[keep_from:]


def clip_middle(text: str, max_chars: int) -> str:
    """Shrink `text` to ~max_chars by removing its MIDDLE.

    Head-only truncation loses the instruction that usually follows a long
    paste ("…<40k of log>… now summarize this"); tail-only truncation loses
    what the document is. Keeping both ends preserves the question and the
    opening, and says plainly what was dropped.
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    head = int(max_chars * 0.6)
    tail = max_chars - head
    removed = len(text) - head - tail
    return (
        text[:head]
        + f"\n\n…[{removed:,} characters omitted to fit the context window]…\n\n"
        + (text[-tail:] if tail > 0 else "")
    )


def _longest_content_index(messages: Sequence[dict]) -> Optional[int]:
    best, best_len = None, 0
    for i, m in enumerate(messages):
        c = m.get("content")
        if isinstance(c, str) and len(c) > best_len:
            best, best_len = i, len(c)
    return best


def clip_message_contents(messages: Sequence[dict], cap: int) -> List[dict]:
    """Clip every text content to `cap` characters (classification calls)."""
    out: List[dict] = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, str) and len(content) > cap:
            out.append({**m, "content": content[:cap] + "\n…[truncated]"})
        else:
            out.append(dict(m))
    return out


async def fit_request(
    messages: Sequence[dict],
    *,
    base_url: str,
    model: str,
    requested_max_tokens: Optional[int] = None,
) -> Tuple[List[dict], int]:
    """Size one model call so prompt + completion always fit the window.

    Returns (messages, max_tokens). Oldest turns are dropped until at least
    MIN_OUTPUT_TOKENS of completion room exists; the pinned system block and
    the current user message are never dropped.
    """
    window = await model_window(base_url, model)
    margin = settings.context_safety_margin
    ceiling = requested_max_tokens or settings.model_max_output

    msgs = list(messages)
    prompt_tokens, served_window = await count_tokens(base_url, model, msgs)
    if served_window:
        window = served_window

    dropped = 0
    clipped = 0
    for _ in range(_MAX_FIT_ROUNDS):
        budget = window - prompt_tokens - margin
        if budget >= MIN_OUTPUT_TOKENS:
            break

        # 1. Prefer dropping whole old turns — they cost nothing to lose.
        trimmed = trim_to_fit(msgs, 1)
        if len(trimmed) != len(msgs):
            msgs = trimmed
            dropped += 1
        else:
            # 2. Nothing left to drop: a SINGLE message is bigger than the
            # window (a large paste, a whole document). Shrink it in place —
            # otherwise this is the 400 that trimming alone cannot prevent.
            idx = _longest_content_index(msgs)
            if idx is None:
                break
            content = msgs[idx]["content"]
            shed_chars = int((MIN_OUTPUT_TOKENS - budget) * _CHARS_PER_TOKEN) + 1024
            target = len(content) - shed_chars
            if target < _MIN_CLIPPED_CHARS:
                target = _MIN_CLIPPED_CHARS
            if target >= len(content):
                break  # cannot shrink any further
            msgs = list(msgs)
            msgs[idx] = {**msgs[idx], "content": clip_middle(content, target)}
            clipped += 1

        prompt_tokens, _ = await count_tokens(base_url, model, msgs)

    if dropped or clipped:
        _record_trim(dropped, clipped)
        import logging

        logging.getLogger(__name__).warning(
            "context budget for %s (%d-token window): dropped %d old turn(s), "
            "clipped %d oversized message(s)",
            model,
            window,
            dropped,
            clipped,
        )

    budget = window - prompt_tokens - margin
    # Never negative, never above what the caller asked for.
    max_tokens = max(1, min(ceiling, budget))
    return msgs, max_tokens
