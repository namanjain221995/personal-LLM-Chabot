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
import re
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

# ...but a request that did NOT fit is trimmed until the answer has real
# room: min(what was asked for, this, a quarter of the window). Stopping at
# 256 left a 5.2 MB paste 403 tokens to answer in (2026-09-18). A request
# that fits untouched is never trimmed to make this room.
OVERFLOW_ANSWER_ROOM = 32_768


def _overflow_room(ceiling: int, window: int) -> int:
    return max(MIN_OUTPUT_TOKENS, min(int(ceiling), OVERFLOW_ANSWER_ROOM, int(window) // 4))

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

#: The base URLs whose cached window a REAL /tokenize reported.
#:
#: `_window_cache` alone does not establish that. `model_window` ends with
#: `resolved = window or settings.model_max_context` and caches THAT, so one
#: failed /tokenize during an engine recovery (172-188 s, 2026-09-12) poisons
#: the cache with a configured constant (MAIN_MODEL_MAX_LEN, else
#: MODEL_MAX_CONTEXT) for the life of the process, and nothing invalidates it.
#: Anything that decides to send BEFORE it has counted is betting the window
#: is real, so it may only read a window this set vouches for: written in
#: `count_tokens`'s success branch beside the cache write, discarded the
#: moment a count fails.
_window_from_server: set = set()


def window_is_server_reported(base_url: str) -> bool:
    """Did a real /tokenize report the cached window for this endpoint?"""
    return base_url in _window_from_server


#: One lock per event loop. A module-level asyncio.Lock binds to the first loop
#: that ever WAITS on it, and a later loop that contends it raises "is bound to
#: a different event loop" - which is what failed PR #79's CI: two generations
#: resumed at start-up both asked for the window while an earlier test's loop
#: owned the lock. Production runs one loop, so there it is the same one lock.
_locks: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def _window_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _locks.get(loop)
    if lock is None:
        lock = _locks[loop] = asyncio.Lock()
    return lock

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


# --------------------------------------------------------------------------
# Sending while the exact count is still in flight
# --------------------------------------------------------------------------
#
# WHAT THIS BUYS. Every `fit_request` blocks on a /tokenize round trip before
# a single byte of the request reaches the engine: 9.5-11.6 ms per main-model
# call, 28.0 ms of critical path on the harness's plain shape and 41.5 ms on
# its docs shape, in front of an engine whose own first token costs 49.8 ms.
# The count is not needed to BUILD the request — it is needed to prove the
# request fits and to size `max_tokens`. When a bound the prompt cannot game
# already proves both, the count can run beside the send instead of in front
# of it.
#
# WHAT IT MUST NOT COST. The body on the wire, to the byte. So the fast path
# fires only where the trim loop provably breaks on its first iteration and
# `max_tokens` provably lands on the same number the slow path would compute;
# see `_may_send_first` for the five conditions and why each one is there.
# The count itself is NOT skipped: it still happens, once, per turn, and its
# result still reaches `_measured`, `_last_count_exact` and the meter through
# `settle_pending_count`.

#: The exact count `fit_request` started but did not wait for, handed to the
#: caller that will settle it. A ContextVar, never a module global: one
#: shared slot is clobbered by the next concurrent turn.
_pending_count: ContextVar[Optional["_PendingCount"]] = ContextVar("_pending_count", default=None)

#: May THIS caller ask for a send-first fit? Only a caller that settles the
#: count it leaves running may, and `llm._fit` is the one place that does
#: (`settles_pending_count`). A direct `fit_request` — a test, a future
#: caller — gets today's blocking behaviour and can leak nothing.
_settles_count: ContextVar[bool] = ContextVar("_settles_count", default=False)

#: Strong references to counts in flight, so nothing is collected between the
#: send and its settlement even after the ContextVar has been cleared.
_INFLIGHT_COUNTS: set = set()


class _PendingCount:
    """An exact count running beside the request it describes."""

    __slots__ = ("task", "messages", "base_url", "window")

    def __init__(self, task, messages: list, base_url: str, window: int) -> None:
        self.task = task
        self.messages = messages
        self.base_url = base_url
        self.window = int(window)


@contextlib.contextmanager
def settles_pending_count():
    """Mark this call site as one that settles (or cancels) a pending count.

    `fit_request` refuses to send first outside this mark, which is what
    makes "the task never outlives the request" structural rather than a
    convention every future caller has to remember.
    """
    token = _settles_count.set(True)
    try:
        yield
    finally:
        _settles_count.reset(token)


def _swallow_count_result(task) -> None:
    _INFLIGHT_COUNTS.discard(task)
    if task.cancelled():
        return
    # Retrieve any exception so a cancelled or abandoned count can never
    # surface as "Task exception was never retrieved" on the event loop.
    task.exception()


def _discard_count(task) -> None:
    if not task.done():
        task.cancel()


async def _count_beside_the_send(base_url: str, model: str, msgs: list):
    """The exact count, run as its own task.

    It runs in a COPY of the caller's context, so the `_last_count_exact` and
    `_measured` it writes are invisible to the caller — which is why the
    verdict is returned here and written back by `settle_pending_count` in
    the caller's own context instead.
    """
    _last_count_exact.set(False)
    count, served_window = await count_tokens(base_url, model, msgs)
    return int(count), served_window, bool(_last_count_exact.get())


def _may_send_first(
    msgs: Sequence[dict], *, base_url: str, window: int, margin: int, ceiling: int
) -> bool:
    """May this request go on the wire before its exact count comes back?

    Only when the answer is provably the same either way:

    1. A caller that settles the count (`settles_pending_count`).
    2. A window a real /tokenize reported (`_window_from_server`), never one
       `model_window` resolved from configuration after a failed count.
    3. `window - upper_bound_messages(msgs) - margin >= max(ceiling,
       MIN_OUTPUT_TOKENS)`. The BOUND, not `estimate_messages`: the estimate
       is len/3 for ASCII and a bytes-per-token average otherwise, and any
       non-Latin script defeats it (N013). `max(ceiling, MIN_OUTPUT_TOKENS)`,
       not `ceiling`: a bounded classifier call asks for `ceiling = 4`, and a
       budget of 4 does not prove the trim loop breaks at `budget >= 256`.
       With this, the loop provably breaks on its FIRST iteration — `sized`
       is the input list unchanged and `max_tokens` is `ceiling` exactly.
    4. `upper_bound_messages(msgs) < admission_long_threshold_tokens // 2`,
       and the origin is not the /v1 surface. This is the ordering with
       admission: `admission.prompt_tokens` runs INSIDE the send, i.e. after
       this returns, so it will not find `_measured` yet. Below half the
       threshold it takes its own sanctioned no-round-trip branch and
       `lane_for` returns NORMAL by prompt on the estimate exactly as it does
       on the exact count — and a chat-origin NORMAL ticket charges no KV and
       arms no closure timer, so the count is the ONLY thing the lane would
       have used it for. The /v1 surface is excluded because its NORMAL lane
       does charge KV from the count, and an estimate is not a count.
    5. Every message is a plain `{role, content}` text turn. Neither
       `estimate_messages` nor `upper_bound_messages` counts `tool_calls`
       arguments at all — an assistant message with `content=None` and
       120,000 characters of tool arguments bounds at 55 against a real
       prompt of ~30,000 tokens — and image parts, `role: "tool"` turns and
       any other key the chat template renders are outside what the bound
       covers. `compaction._certainly_no_compaction` refuses the same shapes
       for the same reason.

    A continuation never reaches here at all: `llm._fit` dispatches to
    `_fit_continuation` before `fit_request` is called.
    """
    if not _settles_count.get():
        return False
    if base_url not in _window_from_server:
        return False
    for m in msgs:
        if not isinstance(m, dict):
            return False
        if set(m.keys()) - {"role", "content"}:
            return False
        if m.get("role") not in ("system", "user", "assistant"):
            return False
        if not isinstance(m.get("content"), str):
            return False
    bound = upper_bound_messages(msgs)
    if int(window) - bound - int(margin) < max(int(ceiling), MIN_OUTPUT_TOKENS):
        return False
    threshold = max(1, int(settings.admission_long_threshold_tokens))
    if bound >= threshold // 2:
        return False
    from . import admission  # local: admission imports this module

    if admission.current_origin() == admission.ORIGIN_V1:
        return False
    return True


async def settle_pending_count(messages: Sequence[dict], base_url: str) -> None:
    """Wait for the count `fit_request` left running and write it back HERE.

    Called once the send has been dispatched. It writes `_measured` and
    `_last_count_exact` in the CALLER's context (the task that did the count
    wrote them only in its own copy), so the meter and the admission lanes
    read exactly what they read on the blocking path.

    A no-op when nothing is pending, which is every call that took the slow
    path and every caller whose `fit_request` was replaced by a test double.
    """
    pending = _pending_count.get()
    if pending is None:
        return
    _pending_count.set(None)
    if pending.messages is not messages or pending.base_url != base_url:
        # Not the request this count describes: nothing may be written back.
        _discard_count(pending.task)
        return
    try:
        count, served_window, exact = await pending.task
    except asyncio.CancelledError:
        _discard_count(pending.task)
        raise
    except Exception:  # pragma: no cover - count_tokens swallows its own
        return

    if served_window and int(served_window) != pending.window:
        # THE WINDOW SHRANK UNDER US. On the blocking path every fit re-reads
        # the served window from the very /tokenize it is about to use, so a
        # window that shrank self-heals on the call that would otherwise
        # overflow. Having sent first, this call cannot. So the mark is
        # withdrawn: every later call takes the slow path and re-reads the
        # window there, until a fresh successful count vouches for it again.
        _window_cache[base_url] = int(served_window)
        _window_from_server.discard(base_url)
        from . import metrics  # local: keeps this module import-light

        metrics.inc(
            "context_window_changed_under_send_total",
            "Times the served context window differed from the one a send-first "
            "request was sized against; send-first is withdrawn until a fresh count.",
        )
        import logging

        logging.getLogger(__name__).warning(
            "served context window is %s, not the %s this request was sized against; "
            "sizing will wait for its count again",
            served_window,
            pending.window,
        )

    _last_count_exact.set(bool(exact))
    _measured.set((pending.messages, base_url, int(count)) if exact else None)


def has_pending_count() -> bool:
    """Was the request in this context sized BEFORE its count came back?

    True only between a send-first `fit_request` and its settlement, which is
    exactly the window in which a refusal from the engine says something about
    the window the request was sized against (`forget_server_window`).
    """
    return _pending_count.get() is not None


def forget_server_window(base_url: str) -> None:
    """Stop vouching for this endpoint's cached window.

    `settle_pending_count` withdraws the mark when the served window turns out
    not to be the one a request was sized against — but it only runs when the
    send SUCCEEDED. When the engine REFUSES the oversized request instead, the
    count that would have reported the real window is cancelled with the
    request, nothing is written back, and the endpoint keeps its mark: every
    later turn is sized send-first against the same stale window and is
    refused again. Measured 2026-09-27 (QA): 4 of 4 `llm.chat_completion`
    turns refused, for the life of the process, whenever the refusal arrived
    before the /tokenize answer, where the blocking path served all four.

    So `llm` calls this on a size refusal of a send-first request. The cache
    itself is left alone — the next call takes the slow path and re-reads the
    served window from the very count it is about to use, exactly as the
    blocking path always did.
    """
    _window_from_server.discard(base_url)


def cancel_pending_count() -> None:
    """Drop a pending count without waiting for it.

    Every exit that is not a dispatched send — a refusal, a cancellation, a
    Stop — comes here. A Stop used to end 2.94 s after the click because a
    3 s count was still owed (main.py, 2026-09-18); nothing this request
    started may outlive it.
    """
    pending = _pending_count.get()
    if pending is None:
        return
    _pending_count.set(None)
    _discard_count(pending.task)


#: /tokenize answers with the whole token-id list beside the count, so its
#: body grows with the prompt: ~6 bytes a token, ~600 KB for a 100K-token
#: prompt, and `json.loads` of that ran on the event loop, stalling every
#: other stream in flight (plan item 4, 2026-09-13). A body past this size is
#: parsed in a worker thread instead; a small one is cheaper to parse inline
#: than to hand to a thread.
_TOKENIZE_JSON_INLINE_BYTES = 64 * 1024


async def _tokenize_json(resp) -> dict:
    body = getattr(resp, "content", b"") or b""
    if len(body) <= _TOKENIZE_JSON_INLINE_BYTES:
        return resp.json()
    return await asyncio.to_thread(resp.json)


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
        data = await _tokenize_json(resp)
        count = int(data["count"])
        window = data.get("max_model_len")
        window = int(window) if window else None
        if window:
            _window_cache[base_url] = window
            # The server itself named this window on this call: only now may
            # a caller trust it without counting first (`_window_from_server`).
            _window_from_server.add(base_url)
        _last_count_exact.set(True)
        return count, window
    except Exception:
        # Multimodal payloads and transient failures land here; estimate.
        # The endpoint did not answer, so whatever window is cached is no
        # longer vouched for — `model_window` may now resolve it from
        # configuration, and a send-first caller must not build on that.
        _window_from_server.discard(base_url)
        _last_count_exact.set(False)
        return estimate_messages(messages), _window_cache.get(base_url)


async def model_window(base_url: str, model: str) -> int:
    """The serving model's context window, cached per base URL."""
    cached = _window_cache.get(base_url)
    if cached:
        return cached
    async with _window_lock():
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


def _string_chars(messages: Sequence[dict]) -> int:
    return sum(len(m["content"]) for m in messages if isinstance(m.get("content"), str))


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

    Returns (messages, max_tokens). A request with at least MIN_OUTPUT_TOKENS
    of completion room is returned as it is. One without is trimmed — oldest
    turns first, then the longest message clipped — until `_overflow_room`
    exists; the pinned system block and the current user message are never
    dropped.

    A request that a bound the prompt cannot game already proves will fit
    does not wait for its count: it returns at once with the same messages
    and the same `max_tokens`, and the count runs beside the send (see
    `_may_send_first`). The caller settles it with `settle_pending_count` as
    soon as the request has been dispatched, or drops it with
    `cancel_pending_count` if it never was.
    """
    window = await model_window(base_url, model)
    margin = settings.context_safety_margin
    ceiling = requested_max_tokens or settings.model_max_output

    msgs = list(messages)

    if _may_send_first(msgs, base_url=base_url, window=window, margin=margin, ceiling=ceiling):
        # PROVEN IDENTICAL, NOT ASSUMED. The gate holds
        # `window - upper_bound - margin >= max(ceiling, MIN_OUTPUT_TOKENS)`,
        # and the real count can only be SMALLER than the upper bound, so the
        # loop below would break on its first iteration with `msgs` untouched
        # and `budget >= ceiling`. `floor` is that guaranteed lower bound on
        # the budget; the same arithmetic the slow path ends with therefore
        # lands on the same number, and the body on the wire is byte-for-byte
        # what it would have been.
        floor = int(window) - upper_bound_messages(msgs) - int(margin)
        # Nothing in llm.py awaits between a fit and its send, so this cannot
        # fire today; it makes "one pending count per context, and it never
        # outlives its request" true by construction rather than by reading
        # every call site again.
        cancel_pending_count()
        task = asyncio.get_running_loop().create_task(
            _count_beside_the_send(base_url, model, msgs)
        )
        _INFLIGHT_COUNTS.add(task)
        task.add_done_callback(_swallow_count_result)
        _pending_count.set(_PendingCount(task, msgs, base_url, int(window)))
        # Not measured YET. `settle_pending_count` writes both the moment the
        # request has been dispatched; until then the lanes read what they
        # read for any uncounted prompt.
        _last_count_exact.set(False)
        _measured.set(None)
        return msgs, max(1, min(int(ceiling), floor))

    _last_count_exact.set(False)
    prompt_tokens, served_window = await count_tokens(base_url, model, msgs)
    if served_window:
        window = served_window

    dropped = 0
    clipped = 0
    room = MIN_OUTPUT_TOKENS
    for _ in range(_MAX_FIT_ROUNDS):
        budget = window - prompt_tokens - margin
        if budget >= room:
            break
        # It did not fit: from here on, trim for an answer's room.
        room = _overflow_room(ceiling, window)
        deficit = room - budget
        # Each old turn's share of the MEASURED count (the estimate scaled to
        # it), so one round drops as many turns as the deficit needs. One per
        # round spent all 24 rounds on a conversation of 24+ messages and a
        # 1.77M-token paste went to a 1M window with max_tokens=1 (QA
        # 2026-09-18); CHAT_HISTORY_TURNS allows 400.
        _, rest = _split_pinned(msgs)
        scale = prompt_tokens / max(1, estimate_messages(msgs))
        costs = [estimate_messages([m]) * scale for m in rest[:-1]]

        # 1. Prefer dropping whole old turns — they cost nothing to lose —
        # when dropping them can make the room.
        if costs and sum(costs) >= deficit:
            shed, drop = 0.0, 0
            while drop < len(costs) and shed < deficit:
                shed += costs[drop]
                drop += 1
            msgs = trim_to_fit(msgs, drop)
            dropped += drop
        else:
            # 2. Dropping every old turn cannot make the room: a SINGLE
            # message is bigger than the window (a large paste, a whole
            # document). The old turns still go first, all of them in this
            # round, and the longest message is clipped by what is STILL
            # missing — otherwise this is the 400 that trimming alone cannot
            # prevent. Clipping it by the whole deficit and keeping the turns
            # cut an 8k-window paste from 15,689 to 2,000 characters to keep
            # three old turns (QA r1 repair, measured).
            # At the prompt's own measured characters per token (never fewer
            # than the pessimistic estimate's), so one clip is usually enough.
            chars_per_token = max(_CHARS_PER_TOKEN, _string_chars(msgs) / max(1, prompt_tokens))
            if costs:
                msgs = trim_to_fit(msgs, len(costs))
                dropped += len(costs)
                deficit -= sum(costs)
            idx = _longest_content_index(msgs)
            content = msgs[idx]["content"] if idx is not None else ""
            target = max(_MIN_CLIPPED_CHARS, len(content) - int(deficit * chars_per_token) - 1024)
            if target < len(content):
                msgs = list(msgs)
                msgs[idx] = {**msgs[idx], "content": clip_middle(content, target)}
                clipped += 1
            elif not costs:
                break  # nothing to drop and nothing left to shrink

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


# --------------------------------------------------------------------------
# What may enter the prompt from the person's saved memory
# --------------------------------------------------------------------------

#: A row that speaks about ONE occasion — "the first answer", "this reply",
#: "the next round" — rather than about answers as a rule. Such a row cannot
#: still be true on the next turn, so it is not durable however it is phrased.
#: It needs its own test because the write side's preference clause rescues
#: it: "The user wants the first answer to be a professional
#: self-introduction (~150-180 words)" reads as "answer … to be", the shape
#: of "answers to be short", and that one row is what produced the owner's
#: 164-word greeting (2026-09-21).
_ONE_OCCASION_RE = re.compile(
    r"\b(?:the\s+(?:first|next|last|final|second|third)|this|that)\s+"
    r"(?:answer|reply|response|message|round|question|introduction)\b",
    re.I,
)


def prompt_facts(rows: Optional[Sequence[Any]]) -> List[Any]:
    """The saved-memory rows that may be shown to the model, in order.

    A row is dropped when it is a one-off task request rather than something
    durable about the person. That judgement already runs when a fact is
    WRITTEN (facts.is_durable, release 1) — it is run again here because rows
    written before release 1 were never judged at all, and they are still
    read on every turn. The owner's account holds 13 written on 2026-09-16
    out of a pasted interview-simulation prompt ("The user is asking for the
    interview to begin with a self-introduction"), and with them in the block
    "hi ??" came back as a 164-word first-person candidate self-introduction,
    3 of 3 live runs at Fast (2026-09-21).

    Deliberately the SAME function as the write side, called through the
    module so the two can never drift apart, plus the one shape only a stored
    row shows (_ONE_OCCASION_RE). A genuine standing preference ("answers in
    Hindi", "layman terms") is durable and stays.
    """
    from . import facts

    kept: List[Any] = []
    for row in rows or ():
        text = row.get("fact") if isinstance(row, dict) else row
        if not isinstance(text, str) or not text.strip():
            continue
        if not facts.is_durable(text) or _ONE_OCCASION_RE.search(text):
            continue
        kept.append(row)
    return kept
