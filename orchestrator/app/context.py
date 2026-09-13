"""Per-request context budgeting (Phase 0.2/0.3).

Every model call is bounded by the window of the model that will actually
serve it — which is NOT one global number: each engine serves its own
`--max-model-len`, and the router's is much smaller than the main model's.
No number for either is written here; both are read from the engine. Sending a fixed
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
import base64
import binascii
import contextlib
import struct
import weakref
from collections import OrderedDict
from contextvars import ContextVar
from typing import Any, List, Optional, Sequence, Tuple

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

# ...but only for ASCII. Until 2026-09-13 every character was divided by three,
# which for Chinese, Devanagari or Gujarati under-counts by roughly 3x: a
# ~190,000-character Gujarati prompt estimated at ~63,000 tokens, under half
# the LONG-lane threshold, and was admitted to the NORMAL lane beside nine
# other prefills with a real size of ~190,000 tokens (N013) — the load shape
# of the 2026-09-11 GDN fault. A non-ASCII character is 2-4 UTF-8 bytes and a
# byte-level BPE spends at most one token per byte, so it is counted at half
# its byte length: one token for Cyrillic or accented Latin, 1.5 for CJK and
# the Indic scripts. That is above what the Qwen tokenizer spends on CJK and
# at or above it for Gujarati, and the lane decision does not rely on it
# alone (see `upper_bound_messages`).
_NON_ASCII_BYTES_PER_TOKEN = 2.0

# What one image costs the main model's prefill. Measured 2026-09-09 against
# Qwen3.6-35B-A3B on this box: 640px ≈ 297 tokens, 896px ≈ 525, 1280×720 ≈ 957
# — one token per ~32×32 pixels (patch 16, merge 2). The processor caps an
# image at 16,384 tokens (its default longest_edge of 16,777,216 pixels; the
# serving command sets no smaller max_pixels), which is what an image whose
# size cannot be read is charged: that is the direction that keeps a large
# image prefill out of the NORMAL lane (F046). Until 2026-09-13 an image part
# counted zero.
_IMAGE_PIXELS_PER_TOKEN_EDGE = 32
_IMAGE_MAX_TOKENS = 16384
_IMAGE_OVERHEAD_TOKENS = 4  # vision start/end markers around the patch tokens
# How much of a base64 data: URL is decoded to find the image header: enough
# for PNG/GIF/WebP (first bytes) and for a JPEG's SOF behind ordinary EXIF.
_IMAGE_HEADER_B64_CHARS = 96_000

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

#: One /tokenize client per (event loop, timeout), for the same reason
#: llm._CLIENTS exists — except this call site was missed by that 2026-09-03
#: change and kept building an AsyncClient per call.
#:
#: Constructing one costs a measured 11.7 ms of *synchronous* CPU on this
#: box (2026-09-06), effectively all of it building the default SSL context:
#: `AsyncClient(verify=False)` is 0.10 ms and `create_ssl_context(verify=True)`
#: alone is 11.7 ms. That cost is paid on the event loop, so it stalls every
#: other request in flight, and it buys nothing here — /tokenize is a plain
#: http call to a vLLM sidecar and never completes a TLS handshake.
#:
#: count_tokens runs 2-3 times per chat turn (compaction.measure, fit_messages,
#: model_window), so this is ~25-35 ms of avoidable event-loop block per turn,
#: multiplied by concurrency. Reusing the client also stops opening a fresh
#: connection pool per count. Keyed by loop because an httpx pool is bound to
#: the loop that created it (tests run many).
#:
#: Least recently used first, and an evicted client is closed on its own loop
#: (F048, 2026-09-13) — the whole dict used to be `.clear()`ed, leaving the
#: clients' pools to the garbage collector. The key must never carry a
#: caller-supplied value, or the bound becomes a new pool per request.
_TOKENIZE_CLIENTS: "OrderedDict[tuple, Any]" = OrderedDict()
_TOKENIZE_CACHE_MAX = 64
_CLOSING: set = set()


def _close_evicted(client, loop) -> None:
    """Schedule `client.aclose()` when its loop is the running one; a client
    of another (or a finished) loop is only dropped — its pool cannot be
    closed from here."""
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        return
    if loop is not running:
        return

    async def close() -> None:
        with contextlib.suppress(Exception):
            await client.aclose()

    task = running.create_task(close())
    _CLOSING.add(task)
    task.add_done_callback(_CLOSING.discard)


def _tokenize_client():
    """Shared httpx client for POST /tokenize; never closed mid-process."""
    import httpx

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    # Keyed by id() for the lookup, but the loop itself is held weakly and
    # re-checked: CPython recycles id()s once a loop is collected, so a bare
    # id key hands a later loop a client bound to a dead one. Weak, so a
    # finished loop is not kept alive by this cache.
    key = (id(loop), float(settings.tokenize_timeout))
    cached = _TOKENIZE_CLIENTS.get(key)
    if cached is not None:
        cached_loop_ref, client = cached
        same_loop = loop is None or (
            cached_loop_ref is not None and cached_loop_ref() is loop
        )
        if same_loop and not client.is_closed:
            _TOKENIZE_CLIENTS.move_to_end(key)
            return client
    client = httpx.AsyncClient(timeout=settings.tokenize_timeout)
    _TOKENIZE_CLIENTS[key] = (weakref.ref(loop) if loop is not None else None, client)
    _TOKENIZE_CLIENTS.move_to_end(key)
    # Tests: many loops; production: a handful.
    while len(_TOKENIZE_CLIENTS) > _TOKENIZE_CACHE_MAX:
        _, (old_loop_ref, old) = _TOKENIZE_CLIENTS.popitem(last=False)
        _close_evicted(old, old_loop_ref() if old_loop_ref is not None else None)
    return client


def service_root(base_url: str) -> str:
    """`http://vllm:30000/v1` → `http://vllm:30000` (tokenize is not under /v1)."""
    root = base_url.rstrip("/")
    return root[: -len("/v1")] if root.endswith("/v1") else root


def estimate_tokens(text: str) -> int:
    text = text or ""
    if text.isascii():  # the common case, and a C-speed check
        return int(len(text) / _CHARS_PER_TOKEN) + 1
    ascii_chars = len(text.encode("ascii", "ignore"))
    non_ascii_bytes = len(text.encode("utf-8", "surrogatepass")) - ascii_chars
    return int(ascii_chars / _CHARS_PER_TOKEN + non_ascii_bytes / _NON_ASCII_BYTES_PER_TOKEN) + 1


def _text_bytes(text: str) -> int:
    """UTF-8 length: a byte-level BPE never spends more tokens than this."""
    text = text or ""
    return len(text) if text.isascii() else len(text.encode("utf-8", "surrogatepass"))


def _image_dimensions(data: bytes) -> Optional[Tuple[int, int]]:
    """(width, height) from a PNG, GIF, WebP or JPEG header, or None."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        return struct.unpack("<HH", data[6:10])
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and len(data) >= 30:
        chunk = data[12:16]
        if chunk == b"VP8X":
            w = int.from_bytes(data[24:27], "little") + 1
            h = int.from_bytes(data[27:30], "little") + 1
            return w, h
        if chunk == b"VP8L" and len(data) >= 25:
            bits = int.from_bytes(data[21:25], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        if chunk == b"VP8 ":
            w, h = struct.unpack("<HH", data[26:30])
            return w & 0x3FFF, h & 0x3FFF
        return None
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7 or marker == 0xFF:
                i += 1
                continue
            length = struct.unpack(">H", data[i + 2:i + 4])[0]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return w, h
            i += 2 + length
    return None


def estimate_image_tokens(part: dict) -> int:
    """Prefill tokens for one `image_url` part (constants above).

    The size is read from the header of a base64 data: URL — which is how
    every image reaches the model from this process (engines/vision.py). An
    image whose size cannot be read (a remote URL, a truncated or unknown
    format) is charged the processor's ceiling.
    """
    image = part.get("image_url")
    url = image.get("url") if isinstance(image, dict) else image
    dims = None
    comma = url.find(",", 0, 256) if isinstance(url, str) and url.startswith("data:") else -1
    if comma > 0:
        # A slice, never a split: the URL is the whole image, often megabytes.
        payload = url[comma + 1: comma + 1 + _IMAGE_HEADER_B64_CHARS]
        payload = payload[: len(payload) - len(payload) % 4]
        with contextlib.suppress(binascii.Error, ValueError, struct.error, IndexError):
            dims = _image_dimensions(base64.b64decode(payload))
    if not dims or dims[0] <= 0 or dims[1] <= 0:
        return _IMAGE_MAX_TOKENS + _IMAGE_OVERHEAD_TOKENS
    edge = _IMAGE_PIXELS_PER_TOKEN_EDGE
    patches = (-(-dims[0] // edge)) * (-(-dims[1] // edge))
    return min(_IMAGE_MAX_TOKENS, patches) + _IMAGE_OVERHEAD_TOKENS


def _is_image_part(part: Any) -> bool:
    return isinstance(part, dict) and (part.get("type") == "image_url" or "image_url" in part)


def estimate_messages(messages: Sequence[dict]) -> int:
    """Rough token count for a message list, including per-message overhead.

    Image parts are charged by their pixel size (`estimate_image_tokens`);
    until 2026-09-13 they counted zero, so a prompt of images sized as its
    caption (F046).
    """
    total = 0
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    total += estimate_tokens(part["text"])
                elif _is_image_part(part):
                    total += estimate_image_tokens(part)
        total += 4  # role + delimiters
    return total + 3  # generation primer


def upper_bound_messages(messages: Sequence[dict]) -> int:
    """A count the real prompt cannot exceed, for a decision that must not be
    gamed DOWN: text at its UTF-8 byte length (a byte-level BPE spends at most
    a token per byte), images as `estimate_image_tokens`.

    The admission lanes trust a "certainly small" verdict only from this —
    never from the character estimate, which any non-Latin script defeats
    (N013, 2026-09-13).
    """
    total = 0
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            total += _text_bytes(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    total += _text_bytes(part["text"])
                elif _is_image_part(part):
                    total += estimate_image_tokens(part)
        total += 8  # role + delimiters, generously
    return total + 8  # generation primer


#: Was the last `count_tokens` in this task an exact /tokenize count, or the
#: estimate it falls back to? Read by `fit_request` so the count it hands the
#: admission lanes is only ever an exact one.
_last_count_exact: ContextVar[bool] = ContextVar("_last_count_exact", default=False)

#: The exact prompt count `fit_request` measured for the messages it returned:
#: (the returned list itself, base_url, count). The admission lanes used to
#: throw this away and re-estimate the same messages from their characters
#: (N013, 2026-09-13); `measured_prompt_tokens` hands it back instead, for
#: exactly that list object and nothing else.
_measured: ContextVar[Optional[Tuple[list, str, int]]] = ContextVar("_measured", default=None)


def measured_prompt_tokens(messages: Sequence[dict], base_url: str) -> Optional[int]:
    """The exact count `fit_request` took for THIS message list on THIS
    endpoint in the current task, or None. Identity, not equality: a list
    that was copied or changed after sizing is not the list that was counted."""
    found = _measured.get()
    if found is None or found[0] is not messages or found[1] != base_url:
        return None
    return found[2]


async def count_tokens(
    base_url: str, model: str, messages: Sequence[dict]
) -> Tuple[int, Optional[int]]:
    """Exact (token_count, max_model_len) from vLLM, or an estimate.

    Returns max_model_len=None when the server could not be asked, so callers
    can fall back to the configured window.
    """
    from .llm import normalize_system

    # Fold system blocks exactly as the completion path does before ITS call.
    # Engines routinely carry more than one system message (their own prompt
    # plus recall/memory blocks prepended to the history), and this model's
    # chat template raises "System message must be at the beginning" on any
    # extra one — so the raw list 400'd on /tokenize, every count silently
    # fell back to the pessimistic estimate, and the engine log filled with
    # tracebacks for requests that then succeeded (the completion had folded).
    # Counting the SAME shape the completion sends is also simply more exact
    # (2026-08-30).
    payload = {"model": model, "messages": normalize_system(list(messages))}
    try:
        client = _tokenize_client()
        resp = await client.post(f"{service_root(base_url)}/tokenize", json=payload)
        resp.raise_for_status()
        data = resp.json()
        count = int(data["count"])
        window = data.get("max_model_len")
        window = int(window) if window else None
        if window:
            _window_cache[base_url] = window
        _last_count_exact.set(True)
        return count, window
    except Exception:
        # Multimodal payloads and transient failures land here; estimate.
        _last_count_exact.set(False)
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
    _last_count_exact.set(False)
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

        _last_count_exact.set(False)
        prompt_tokens, _ = await count_tokens(base_url, model, msgs)

    # `prompt_tokens` always describes `msgs` as returned (every change above
    # is followed by a recount). Kept for the admission lanes only when exact.
    _measured.set((msgs, base_url, int(prompt_tokens)) if _last_count_exact.get() else None)

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
