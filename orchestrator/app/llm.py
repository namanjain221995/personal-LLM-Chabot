"""LLM clients — every model is served by vLLM behind OpenAI-compatible
endpoints (owner override of SPEC §4).

Since 2026-07-28 ONE set of weights serves every chat path:

- Qwen3.6-35B-A3B (NVFP4, multimodal, reasoning) → OPENAI_BASE_URL, and
  ROUTER_BASE_URL / VISION_BASE_URL now point at the same endpoint;
- Qwen3-Embedding-0.6B → EMBED_BASE_URL (embeddings).

"Smart" vs "Fast" is therefore NOT two models — it is one model with the
reasoning pass on or off (`enable_thinking`), which is where the latency
actually lives. See `wants_thinking`.

Context windows are enforced server-side by each vLLM instance
(--max-model-len); §8's DEFAULT/REPORT context split is applied by the
callers through prompt sizing and max_tokens.

Nothing here performs network I/O at import time.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections import OrderedDict
from collections.abc import Mapping
from contextvars import ContextVar
from typing import Any, AsyncIterator, List, Optional, Sequence, Tuple

from . import admission as _admission
from . import breaker as _breaker
from . import context, metrics
from .config import settings
from .context import clip_message_contents
from .model_capabilities import ModelCapabilities, ReasoningField
from .resilience import ModelUnavailable, resilient, sidecar_recovery_s  # noqa: F401 — re-exported for callers

# ---------------------------------------------------------------------------
# OUTAGE TOLERANCE. Every model call below opens through
# `resilience.resilient`, which waits for /health and retries through the
# recoverable failures a restarting engine produces (connection refused, a
# 5xx from a dying engine) and re-raises everything else untouched — a 4xx,
# a read timeout, a cancellation. For streams only the OPEN is wrapped: once
# a token has been forwarded a re-open would duplicate text. The window is
# short for a person watching a chat and long for a background job; see
# app/resilience.py for the two windows and the measured outage that sized
# them.
#
# STRICT ONE-MODEL MODE (2026-09-12, docs/availability/CONTRACT.md v2 §1,
# §6.7, §8.2–8.3). Only the main model answers a person; nothing stands in
# for it. Every call to it goes through ONE choke point — `_primary_send`
# / `_open_stream` — which is where the circuit breaker is consulted
# before the first attempt (an OPEN breaker means the engine is not
# touched, the caller is queued on the READY event and the chat worker's
# row says so), where the admission lanes size the prompt and hold a slot
# (app/admission.py), and where a stream's first chunk — not its headers —
# settles the breaker. The router (`router_chat_completion`) is an internal
# classifier and never receives a user-answer request: `stream_chat_events`
# asserts the engine it streams from.
# ---------------------------------------------------------------------------

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Token accounting for one chat turn (V18 telemetry).
#
# A turn is rarely one model call: a route classification, the answer, a
# vision pass and a thinking-budget fallback are all calls, and an operator
# asks "what did this turn cost", not "what did the third call cost". So
# these ACCUMULATE and the caller resets once per turn — the same ContextVar
# idiom, and the same per-asyncio-task scope, as context._trim_notice.
#
# Exactness matters more than coverage: only what the SERVER reports is
# counted. Where a runtime cannot report usage the fields stay None and the
# analytics console says "not measured" rather than showing a guess.
# ---------------------------------------------------------------------------

_usage: ContextVar[Optional[dict]] = ContextVar("_usage", default=None)

#: Turned off for the process the first time a server rejects the option, so
#: an unsupporting runtime costs one failed stream, not every stream.
_ASK_FOR_USAGE = {"enabled": True}


def reset_usage() -> None:
    """Start a fresh turn's accounting. Call once per chat request."""
    _usage.set(None)


def get_usage() -> Optional[dict]:
    """Totals for this turn, or None when no call reported any.

    None means NOT MEASURED. It must never be rendered as zero.
    """
    return _usage.get()


def _record_usage(prompt: int, completion: int) -> None:
    prev = _usage.get() or {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}
    _usage.set(
        {
            "prompt_tokens": prev["prompt_tokens"] + max(0, int(prompt or 0)),
            "completion_tokens": prev["completion_tokens"] + max(0, int(completion or 0)),
            "calls": prev["calls"] + 1,
        }
    )


def _capture_usage(chunk) -> None:
    """Read the usage chunk vLLM sends last. It carries no choices, so every
    streaming loop here already skips it for text purposes."""
    usage = getattr(chunk, "usage", None)
    if usage is None:
        return
    _record_usage(
        getattr(usage, "prompt_tokens", 0) or 0,
        getattr(usage, "completion_tokens", 0) or 0,
    )


# ---------------------------------------------------------------------------
# WHY THE LAST CALL STOPPED.
#
# `finish_reason` is the only honest signal that a generation hit its ceiling
# rather than finishing what it had to say. Until now this file had no
# handling for one at all: the streaming loop read `choices[0].delta` and
# never looked at `choices[0].finish_reason`, so every engine downstream had
# to guess. OCR guessed with a non-streaming read (engines/ocr.py), Deep
# Research guessed by reconstructing the ceiling and comparing token counts,
# and its own docstring named the fix — "the honest fix is a finish_reason
# from the streaming layer". This is that.
#
# A ContextVar, deliberately, for the same reason `_usage` is one: the
# yielded tuple is (kind, delta) and fourteen call sites branch on `kind ==
# "reasoning"` with `else` meaning "text". A third kind would be appended to
# answers as prose by most of them.
#
# UNLIKE `_usage`, this does NOT accumulate — it is the reason the MOST RECENT
# call ended, which is what a continuation loop must read after each segment.
# Every entry point clears it before opening a stream, so a call that dies
# before reporting one cannot leave the previous call's reason standing; a
# stale "length" there would loop a continuation forever.
# ---------------------------------------------------------------------------

_finish_reason: ContextVar[Optional[str]] = ContextVar("_finish_reason", default=None)

#: Set instead of a server reason when OUR guard stopped the stream, so a
#: caller can tell "the model ran out of room" from "we pulled the plug".
WALL_CLOCK_FINISH = "wall_clock"
THINKING_OVERRUN_FINISH = "thinking_overrun"


def reset_finish_reason() -> None:
    _finish_reason.set(None)


def get_finish_reason() -> Optional[str]:
    """Why the last completion stopped, or None when the server said nothing.

    None means NOT REPORTED. It must never be read as "finished cleanly" —
    a continuation loop treats it as "stop", which is the safe direction.
    """
    return _finish_reason.get()


def _set_finish_reason(reason: Optional[str]) -> None:
    if reason:
        _finish_reason.set(str(reason))


def _capture_finish(chunk) -> None:
    """Read the finish_reason off a streamed chunk.

    It arrives on the last chunk that carries a choice; the usage chunk after
    it has none, which is why this checks rather than indexing blindly.
    """
    choices = getattr(chunk, "choices", None)
    if not choices:
        return
    _set_finish_reason(getattr(choices[0], "finish_reason", None))


def _is_primary(base_url: str) -> bool:
    """Is this endpoint the main model's? The breaker registry is the one
    place that maps URLs to engines, so the two can never disagree."""
    return _breaker.engine_for_base_url(base_url) == _breaker.MAIN


async def _primary_send(client, request: dict, *, what: str, base_url: str, stream: bool = False):
    """THE choke point for a call to the main model (module header).

    Per attempt, in order: the breaker (inside `resilient`, before anything
    is sent), the admission lane (inside the attempt, so a request queued
    for a recovering engine holds no lane slot for the whole reload), the
    engine. A sidecar endpoint that happens to come through here (the
    vision URL on a profile that points it elsewhere) has no breaker and
    no lane and gets the plain wrapper.
    """

    def create():
        return client.chat.completions.create(**request)

    if _is_primary(base_url):
        op = lambda: _admission.run(  # noqa: E731 — a named closure reads worse here
            create, messages=request.get("messages") or [], base_url=base_url,
            model=str(request.get("model") or settings.llm_model), stream=stream,
        )
    else:
        op = create
    return await resilient(op, what=what, base_url=base_url, stream=stream)


async def _open_stream(client, request: dict):
    """Open a streamed completion, asking for usage when the server allows it.

    `stream_options` is an OpenAI-API extension. A server that does not know
    it answers 400, which would otherwise turn a telemetry nicety into a total
    outage — so the first refusal drops the option for the whole process and
    the request is retried exactly as it would have been sent before.

    The stream comes back wrapped (resilience.GuardedStream): its first
    chunk is what tells the breaker the engine served, and a body that
    dies is reported as the failure it is. Consume it with `_consume` so
    an early exit closes it and releases its lane.
    """
    base_url = str(getattr(client, "base_url", "") or settings.openai_base_url)
    if not _ASK_FOR_USAGE["enabled"]:
        request.pop("stream_options", None)
        return await _primary_send(client, request, what="stream", base_url=base_url, stream=True)
    ask = dict(request)
    ask["stream_options"] = {"include_usage": True}
    try:
        return await _primary_send(client, ask, what="stream", base_url=base_url, stream=True)
    except _bad_request_error() as exc:
        # ONLY a 400 is "the server does not know this option". Until
        # 2026-09-11 this caught every exception, so the first streamed call
        # during an engine outage (a connection error) permanently switched
        # token telemetry off for the process and mis-reported the outage as
        # a refused option. A transport error now propagates as itself, and
        # the resilient wrapper above has already waited on it.
        if not _ASK_FOR_USAGE["enabled"]:
            raise
        _ASK_FOR_USAGE["enabled"] = False
        log.warning(
            "this runtime refused stream_options.include_usage (%s: %s); "
            "token telemetry will read 'not measured' until restart",
            type(exc).__name__, exc,
        )
        request.pop("stream_options", None)
        return await _primary_send(client, request, what="stream", base_url=base_url, stream=True)


@contextlib.asynccontextmanager
async def _consume(stream):
    """Iterate a stream and, on ANY early exit — the wall-clock guard, a
    thinking overrun, the consumer being cancelled or closed — close it, so
    the breaker permit and the admission lane it holds are released. A
    stream read to its end is left alone (its close is a no-op)."""
    try:
        yield stream
    finally:
        closer = getattr(stream, "close", None)
        if closer is not None:
            with contextlib.suppress(Exception):
                await closer()


def _bad_request_error():
    """openai.BadRequestError, imported lazily like the client itself."""
    from openai import BadRequestError

    return BadRequestError

# Local inference servers: the key is a placeholder, never a real secret.
LOCAL_API_KEY = "local-no-key"

# V2-DESIGN §1: chat request model/effort choices.
MODEL_CHOICES = ("smart", "fast")
# Four levels on ONE model. "fast" and "low" skip the reasoning pass; the
# difference between them is how much work the orchestrator may do (low may
# search the web, fast may not). See engines/orchestrate.py.
REASONING_EFFORTS = ("fast", "think", "max")
#: Pre-collapse wire values (2026-08-19 ladder collapse) normalize to the
#: three honest levels; accepted forever for stored prefs and old clients.
EFFORT_ALIASES = {"low": "fast", "medium": "think", "high": "think", "extra_high": "max"}


def normalize_effort(effort: str) -> str:
    """Canonical effort for any accepted wire value; unknown -> think."""
    value = (effort or "").strip().lower()
    if value in REASONING_EFFORTS:
        return value
    return EFFORT_ALIASES.get(value, "think")


def normalize_system(messages: Sequence[dict]) -> List[dict]:
    """Fold every system block into ONE system message at index 0.

    Qwen3.6's chat template rejects the request outright ("System message must
    be at the beginning") if it sees a second system turn or one that is not
    first. That is exactly the shape this app produces: the engines start with
    a system prompt, then compaction prepends the rolling summary and appends
    the semantic-recall block just before the latest question, and search adds
    its sources the same way. Every one of those turns is a 400 on this model.

    Order is preserved when joining, so the engine prompt still leads and the
    retrieved material still reads as later context. Blocks keep their own
    delimiters, so untrusted retrieved text stays as fenced as it was.
    """
    system_blocks: List[str] = []
    rest: List[dict] = []
    for m in messages:
        if m.get("role") != "system":
            rest.append(m)
            continue
        content = m.get("content")
        # A system turn is always plain text here; anything else (multimodal
        # parts) is left where it is rather than silently flattened.
        if not isinstance(content, str):
            rest.append(m)
            continue
        if content.strip():
            system_blocks.append(content.strip())
    if not system_blocks:
        return rest
    return [{"role": "system", "content": "\n\n".join(system_blocks)}, *rest]


#: One client per (event loop, endpoint, read timeout). A fresh AsyncOpenAI
#: per call — the shape until 2026-09-03 — opened a new connection pool for
#: every embedding, router and generation request; under concurrency that
#: is connection churn against four vLLM sidecars. Keyed by loop because an
#: httpx pool is bound to the loop that created it (tests run many).
_CLIENTS: dict = {}


def _client(base_url: str, api_key: Optional[str] = None, *, read_timeout: Optional[float] = None):
    import httpx
    from openai import AsyncOpenAI  # cheap, but keep out of module import path

    read = float(read_timeout if read_timeout is not None else settings.llm_request_timeout)
    try:
        loop_key = id(asyncio.get_running_loop())
    except RuntimeError:
        loop_key = None
    key = (loop_key, base_url, api_key or LOCAL_API_KEY, read)
    if loop_key is not None:
        cached = _CLIENTS.get(key)
        if cached is not None:
            return cached
    client = AsyncOpenAI(
        base_url=base_url,
        api_key=api_key or LOCAL_API_KEY,
        # A bare float collapses all four httpx timeouts onto one number —
        # including `connect`, which then waits minutes on a dead service, and
        # `read`, which for a non-streaming completion IS the whole generation.
        # Splitting them lets a real outage fail fast while a legitimately long
        # generation runs to the app's own wall clock.
        timeout=httpx.Timeout(
            connect=settings.llm_connect_timeout,
            read=read,
            write=settings.llm_write_timeout,
            pool=settings.llm_write_timeout,
        ),
        max_retries=settings.llm_max_retries,
    )
    if loop_key is not None:
        if len(_CLIENTS) > 64:  # tests: many loops; production: a handful
            _CLIENTS.clear()
        _CLIENTS[key] = client
    return client


def _openai_client():
    """Client for the main model (gpt-oss-120b) on OPENAI_BASE_URL."""
    return _client(settings.openai_base_url, settings.openai_api_key)


# ---------------------------------------------------------------------------
# gpt-oss-120b (main model)
# ---------------------------------------------------------------------------

async def chat_completion(
    messages: Sequence[dict],
    *,
    model: Optional[str] = None,
    temperature: float = 0.2,
    max_tokens: Optional[int] = None,
    thinking: bool = True,
) -> str:
    """Non-streaming chat completion; returns the assistant text.

    `thinking=False` skips the reasoning pass. Reasoning is drawn from the
    SAME budget as the answer, so for a translation task over a large prompt
    — writing SQL from a schema — the model can spend the entire allowance
    thinking and return an EMPTY string. Measured on a 11,500-token SQL
    prompt: 121 seconds, zero characters of output. Empty SQL then read as
    "not in the warehouse" and the question was answered from live Salesforce
    off the wrong object. The streaming path already learned this lesson (see
    stream_chat_completion); this is the same fix for the non-streaming one.
    """
    model_id = model or settings.llm_model
    client = _openai_client()
    sized, budget = await context.fit_request(
        normalize_system(messages),
        base_url=settings.openai_base_url,
        model=model_id,
        requested_max_tokens=max_tokens,
    )
    request = dict(
        model=model_id,
        messages=sized,
        temperature=temperature,
        max_tokens=budget,
    )
    extra_body = reasoning_extra_body(settings.main_capabilities, thinking)
    if extra_body is not None:
        request["extra_body"] = extra_body
    reset_finish_reason()
    resp = await _primary_send(client, request, what="chat_completion", base_url=settings.openai_base_url)
    _capture_usage(resp)
    _capture_finish(resp)
    _, content = split_reasoning(resp.choices[0].message, settings.main_capabilities)
    return content


async def chat_completion_with_reasoning(
    messages: Sequence[dict],
    *,
    model: Optional[str] = None,
    temperature: float = 0.2,
    max_tokens: Optional[int] = None,
    effort: str = "high",
) -> Tuple[str, str]:
    """Non-streaming completion that KEEPS the reasoning. → (reasoning, text).

    The non-streaming path historically discarded reasoning entirely; this is
    the collector best-of-N and any future judge/offline path use. Thinking
    follows the effort (fast/low: off), with the same budget-grown max_tokens
    as the streaming path.
    """
    model_id = model or settings.llm_model
    thinking_on = wants_thinking("smart", effort)
    budget_tokens = thinking_budget(effort) if thinking_on else None
    requested = max_tokens
    if thinking_on:
        if budget_tokens and max_tokens is not None:
            requested = max_tokens + budget_tokens
        elif budget_tokens is None:
            # Unbounded thinking: floor at MAX_OUTPUT_TOKENS so thinking +
            # answer always fit (same policy as the streaming path).
            requested = max(max_tokens or 0, settings.max_output_tokens)

    client = _openai_client()
    sized, budget = await context.fit_request(
        normalize_system(messages),
        base_url=settings.openai_base_url,
        model=model_id,
        requested_max_tokens=requested,
    )
    request = dict(
        model=model_id, messages=sized, temperature=temperature, max_tokens=budget
    )
    extra_body = reasoning_extra_body(settings.main_capabilities, thinking_on)
    if extra_body is not None:
        request["extra_body"] = extra_body
    import asyncio as _asyncio

    try:
        # The hang guard for the non-streaming collector: a candidate stuck
        # in a repetition loop dies at the wall clock instead of holding the
        # whole best-of-N gather hostage.
        resp = await _asyncio.wait_for(
            _primary_send(client, request, what="chat_completion_with_reasoning",
                          base_url=settings.openai_base_url),
            timeout=settings.gen_wall_clock_s,
        )
    except _asyncio.TimeoutError:
        log.error(
            "GENERATION WALL CLOCK EXCEEDED (non-streaming collector): "
            ">%.0fs on %s (effort %r) — candidate abandoned",
            settings.gen_wall_clock_s, model_id, effort,
        )
        raise RuntimeError(
            f"generation exceeded the {int(settings.gen_wall_clock_s)}s wall clock"
        ) from None
    return split_reasoning(resp.choices[0].message, settings.main_capabilities)


async def stream_chat_completion(
    messages: Sequence[dict],
    *,
    model: Optional[str] = None,
    temperature: float = 0.2,
    max_tokens: Optional[int] = None,
    thinking: bool = True,
) -> AsyncIterator[str]:
    """Streaming chat completion; yields text deltas."""
    model_id = model or settings.llm_model
    client = _openai_client()
    sized, budget = await context.fit_request(
        normalize_system(messages),
        base_url=settings.openai_base_url,
        model=model_id,
        requested_max_tokens=max_tokens,
    )
    request = dict(
        model=model_id,
        messages=sized,
        temperature=temperature,
        max_tokens=budget,
        stream=True,
        # Reasoning is drawn from the SAME budget as the answer. Summarising a
        # result set does not need it, and with hundreds of rows in the prompt
        # the model spent the whole allowance thinking and streamed NO answer
        # at all — the UI showed a data table with empty prose above it.
    )
    extra_body = reasoning_extra_body(settings.main_capabilities, thinking)
    if extra_body is not None:
        request["extra_body"] = extra_body
    reset_finish_reason()
    async with _consume(await _open_stream(client, request)) as stream:
        async for chunk in stream:
            _capture_usage(chunk)
            _capture_finish(chunk)
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            content = _delta_value(delta, "content") if delta is not None else None
            if content:
                yield str(content)


# ---------------------------------------------------------------------------
# V2 (V2-DESIGN §3a): model picker + reasoning effort + reasoning stream
# ---------------------------------------------------------------------------

def resolve_model_choice(choice: str) -> Tuple[str, str, str]:
    """Resolve a V2 model choice to (base_url, api_key, served model id).

    Every choice resolves to MAIN_MODEL on OPENAI_BASE_URL. "fast" used to
    name the router engine; in strict one-model mode (CONTRACT v2 §1) the
    router is a classifier and may not write a person's answer, and "fast"
    is what it always really meant — the same model with the reasoning
    pass off (`wants_thinking`). A client that still sends the old value
    (a stored preference) is served honestly rather than refused.
    """
    return settings.openai_base_url, settings.openai_api_key, settings.llm_model


def served_model_id(choice: str) -> str:
    """The served model id a V2 model choice resolves to (for meta.model)."""
    return resolve_model_choice(choice)[2]


def capabilities_for_model_choice(choice: str) -> ModelCapabilities:
    """Capabilities for the endpoint selected by a V2 model choice — the
    main model's, whatever the choice (see resolve_model_choice)."""
    return settings.main_capabilities


class AnswerEngineViolation(RuntimeError):
    """A user-facing answer was about to be streamed from an engine that is
    not the main model. Never expected to fire: `resolve_model_choice`
    returns the main model for every choice. It exists so the invariant of
    CONTRACT v2 §1 is checked where the answer is produced, not assumed."""


def assert_answer_engine(base_url: str) -> None:
    """The guard `stream_chat_events` runs before it opens the answer stream."""
    if _is_primary(base_url):
        return
    log.error(
        "llm.answer_engine REFUSED: a user answer would have come from %s, not the main model (CONTRACT v2 §1)",
        "the router" if base_url.rstrip("/") == settings.router_base_url.rstrip("/") else "another engine",
    )
    raise AnswerEngineViolation("user answers come from the main model only")


def wants_thinking(model_choice: str = "smart", effort: str = "medium") -> bool:
    """Should this call run the model's reasoning pass?

    One set of weights now serves both picker choices, so "Fast" is not a
    smaller model — it is the SAME model with thinking switched off. That is
    what actually makes it fast: the reasoning pass, not the parameter count,
    is where the seconds go. Effort "low" means the same thing.
    """
    if model_choice != "smart":
        return False
    # Fast answers directly; Think and Max reason first.
    return normalize_effort(effort) in ("think", "max")


def thinking_budget(effort: str) -> Optional[int]:
    """Thinking tokens this effort may spend, or None for unbounded/none.

    OFF by default (owner decision 2026-08-19: local deployment, no
    per-token cost — thinking runs until the model closes it naturally).
    Returns a budget ONLY when THINKING_BUDGET_MODE=client, which re-enables
    the Phase 1 enforcement exactly as built. Budget values were DERIVED
    from the measured decode rate (46.6 tok/s thinking-on — docs/CONFIG.md).
    """
    if settings.thinking_budget_mode != "client":
        return None
    # Post-collapse mapping: think carries the old High budget, max the
    # old Extra-High one. THINKING_BUDGET_MEDIUM is retired (kept as an
    # env for compatibility, no longer consulted).
    return {
        "think": settings.thinking_budget_high,
        "max": settings.thinking_budget_extra_high,
    }.get(normalize_effort(effort))


def thinking_body(enabled: bool) -> dict:
    """`extra_body` toggling the chat template's thinking block.

    Qwen3.6's template honours `enable_thinking`; passing it through
    chat_template_kwargs is how a single deployment serves both a reasoning
    and a quick-answer mode.
    """
    return {"chat_template_kwargs": {"enable_thinking": bool(enabled)}}


def reasoning_extra_body(
    capabilities: ModelCapabilities, enabled: bool
) -> Optional[dict]:
    """Return the Qwen/vLLM thinking switch only when the backend allows it.

    Native and third-party OpenAI-compatible runtimes frequently reject
    unknown ``extra_body`` fields with HTTP 400.  Capability profiles opt into
    this extension explicitly; an unsupported backend receives no key at all.
    """
    if not capabilities.supports_reasoning:
        return None
    if not capabilities.allows_extra_body("chat_template_kwargs"):
        return None
    return thinking_body(enabled)


def _delta_value(delta: object, name: str):
    if isinstance(delta, Mapping):
        return delta.get(name)
    return getattr(delta, name, None)


def _reasoning_field_names(capabilities: ModelCapabilities) -> Tuple[str, ...]:
    """The delta/message field names that may carry reasoning, or ()."""
    if not capabilities.supports_reasoning:
        return ()
    field = capabilities.reasoning_field
    if field is ReasoningField.NONE:
        return ()
    if field is ReasoningField.AUTO:
        return (ReasoningField.REASONING.value, ReasoningField.REASONING_CONTENT.value)
    return (field.value,)


def _reasoning_delta(delta: object, capabilities: ModelCapabilities) -> Optional[str]:
    """Extract a reasoning delta without assuming a vLLM response shape.

    Missing extension fields are normal on OpenAI-compatible backends.  They
    simply produce no reasoning event; answer content continues through the
    unchanged ``token`` SSE path.
    """
    model_extra = _delta_value(delta, "model_extra")
    for name in _reasoning_field_names(capabilities):
        value = _delta_value(delta, name)
        if not value and isinstance(model_extra, Mapping):
            value = model_extra.get(name)
        if value:
            return str(value)
    return None


#: A raw thinking block at the head of `content` — what a response looks like
#: when a path bypasses vLLM's --reasoning-parser (a backend without the
#: flag, or a template that emitted <think> anyway).
_THINK_FALLBACK_RE = re.compile(r"^\s*<think>(.*?)</think>\s*", re.S | re.I)


def split_reasoning(
    message: object, capabilities: ModelCapabilities
) -> Tuple[str, str]:
    """(reasoning, content) from a NON-streaming completion message.

    Reads the parser's extension field (`reasoning` / `reasoning_content`)
    first, then falls back to a literal <think>…</think> block at the head of
    the content — so a path that bypasses the reasoning parser still returns
    clean answer text instead of leaking the thought into it.
    """
    content = str(_delta_value(message, "content") or "")
    reasoning = ""
    model_extra = _delta_value(message, "model_extra")
    for name in _reasoning_field_names(capabilities):
        value = _delta_value(message, name)
        if not value and isinstance(model_extra, Mapping):
            value = model_extra.get(name)
        if value:
            reasoning = str(value)
            break
    if not reasoning:
        match = _THINK_FALLBACK_RE.match(content)
        if match:
            reasoning = match.group(1).strip()
            content = content[match.end():]
    return reasoning, content


def apply_reasoning_effort(
    messages: Sequence[dict], effort: str, model_choice: str = "smart"
) -> List[dict]:
    """Messages for a request at the given effort.

    Historically this prepended gpt-oss's "Reasoning: <effort>" system line.
    That model is long gone and Qwen3.6 ignores such a line, so effort is now
    expressed where it has a real effect — `enable_thinking` (see
    `wants_thinking`). Kept as the single place that shapes messages by
    effort, and still a no-op passthrough.
    """
    return list(messages)


async def stream_chat_events(
    messages: Sequence[dict],
    *,
    model_choice: str = "smart",
    effort: str = "medium",
    temperature: float = 0.2,
    max_tokens: Optional[int] = None,
) -> AsyncIterator[Tuple[str, str]]:
    """Streaming completion from the selected model, yielding (kind, delta)
    pairs: ("reasoning", <delta.reasoning_content>) for vLLM thinking deltas
    and ("token", <delta.content>) for answer text (V2-DESIGN §3a).
    """
    base_url, api_key, model_id = resolve_model_choice(model_choice)
    thinking_on = wants_thinking(model_choice, effort)
    budget_tokens = thinking_budget(effort) if thinking_on else None
    # Sizing: reasoning and answer draw from one max_tokens pool, and the
    # documented failure mode is the model spending the whole allowance
    # thinking and streaming nothing. Budgeted mode (THINKING_BUDGET_MODE=
    # client) adds the budget on top of the caller's answer ceiling.
    # UNBOUNDED mode (the default) floors the request at MAX_OUTPUT_TOKENS
    # (65,536) whenever thinking is on, so however long the model thinks the
    # answer always has room — the 262k window is the only wall above that.
    requested = max_tokens
    if thinking_on:
        if budget_tokens and max_tokens is not None:
            requested = max_tokens + budget_tokens
        elif budget_tokens is None:
            requested = max(max_tokens or 0, settings.max_output_tokens)
    shaped_messages = apply_reasoning_effort(messages, effort, model_choice)
    capabilities = capabilities_for_model_choice(model_choice)
    # The answer a person reads comes from the main model, whatever the
    # picker said (CONTRACT v2 §1): checked here, where it is produced.
    assert_answer_engine(base_url)
    client = _client(base_url, api_key)
    # Size the call to the window of the model that will actually serve it.
    sized, budget = await context.fit_request(
        normalize_system(shaped_messages),
        base_url=base_url,
        model=model_id,
        requested_max_tokens=requested,
    )
    request = dict(
        model=model_id,
        messages=sized,
        temperature=temperature,
        max_tokens=budget,
        stream=True,
    )
    # THE picker's real mechanism on the DGX runtime: Smart thinks, Fast does
    # not. Other runtimes omit this vLLM-specific extension entirely.
    extra_body = reasoning_extra_body(capabilities, thinking_on)
    if extra_body is not None:
        if budget_tokens and settings.server_thinking_budget:
            # OFF by default: tested 2026-08-19 against this vLLM/Qwen3.6
            # build and silently ignored (docs/CONFIG.md). Client-side
            # enforcement below runs regardless, and stays the ONLY
            # mechanism whenever tools are attached.
            extra_body["chat_template_kwargs"]["thinking_token_budget"] = budget_tokens
        request["extra_body"] = extra_body

    # Client-side budget enforcement — ACTIVE ONLY in budgeted mode. On this
    # deployment one streamed chunk is one token (verified:
    # usage.completion_tokens == chunk count), so counting reasoning deltas
    # IS counting reasoning tokens. The cap carries a grace factor so a
    # thought at the nominal budget finishes its clause instead of being
    # guillotined mid-sentence.
    cap = int(budget_tokens * settings.thinking_budget_grace) if budget_tokens else None
    reasoning_seen = 0
    token_seen = 0
    # Hang guard, NOT a budget: it exists to catch degenerate repetition
    # loops, and at the measured decode rate it only fires far past any real
    # answer. Applies in BOTH modes.
    import time as _time

    started = _time.monotonic()
    reset_finish_reason()
    async with _consume(await _open_stream(client, request)) as stream:
        async for chunk in stream:
            _capture_finish(chunk)
            elapsed = _time.monotonic() - started
            if elapsed > settings.gen_wall_clock_s:
                log.error(
                    "GENERATION WALL CLOCK EXCEEDED: %.0fs > %.0fs on %s "
                    "(effort %r, %d reasoning + %d answer chunks) — killing the "
                    "stream and returning what was produced",
                    elapsed, settings.gen_wall_clock_s, model_id, effort,
                    reasoning_seen, token_seen,
                )
                with contextlib.suppress(Exception):
                    await stream.close()
                # Not "length": the model had room left, we took it away. A
                # continuation loop must be able to tell those apart — one is
                # worth resuming, the other means something is wrong.
                _finish_reason.set(WALL_CLOCK_FINISH)
                yield (
                    "token",
                    f"\n\n[generation stopped after {int(elapsed)}s — wall-clock "
                    "guard; the text above is what was produced]",
                )
                return
            _capture_usage(chunk)
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta is None:
                continue
            # vLLM extension field; absent on models without a reasoning stream.
            # vLLM has shipped the thinking delta under both names: `reasoning`
            # (v0.20+, e.g. the 26.05 NGC image) and `reasoning_content` (older).
            reasoning = _reasoning_delta(delta, capabilities)
            if reasoning:
                reasoning_seen += 1
                if cap is not None and reasoning_seen > cap:
                    # Forced closure: the model is looping in its own head. Stop
                    # paying for it and answer the question directly — the
                    # reasoning shown so far stays on screen, the answer comes
                    # from a thinking-off pass over the identical prompt.
                    log.warning(
                        "thinking overran its budget (%d tokens, cap %d) at "
                        "effort %r on %s; forcing closure and answering without "
                        "thinking",
                        budget_tokens, cap, effort, model_id,
                    )
                    with contextlib.suppress(Exception):
                        await stream.close()
                    # The same engine asked again without thinking — never
                    # another engine.
                    retry = dict(request)
                    # The retry only writes the ANSWER, so the caller's original
                    # ceiling is the honest budget for it.
                    retry["max_tokens"] = (
                        min(budget, max_tokens) if max_tokens is not None else budget
                    )
                    fb_extra = reasoning_extra_body(capabilities, False)
                    if fb_extra is not None:
                        retry["extra_body"] = fb_extra
                    else:
                        retry.pop("extra_body", None)
                    reset_finish_reason()
                    async with _consume(await _open_stream(client, retry)) as fb_stream:
                        async for fb_chunk in fb_stream:
                            _capture_usage(fb_chunk)
                            _capture_finish(fb_chunk)
                            if not fb_chunk.choices:
                                continue
                            fb_delta = fb_chunk.choices[0].delta
                            if fb_delta is None:
                                continue
                            fb_content = _delta_value(fb_delta, "content")
                            if fb_content:
                                yield "token", str(fb_content)
                    return
                yield "reasoning", reasoning
            content = _delta_value(delta, "content")
            if content:
                token_seen += 1
                yield "token", str(content)
    # Usage telemetry (log-only): with budgets off this is the record of what
    # unbounded thinking actually cost, and the data a future budget decision
    # would be made from.
    if thinking_on:
        log.info(
            "generation usage: %d reasoning + %d answer chunks in %.1fs "
            "(effort %r, budget_mode %s)",
            reasoning_seen, token_seen, _time.monotonic() - started,
            effort, settings.thinking_budget_mode,
        )


# ---------------------------------------------------------------------------
# Structured output + tool calling (Salesforce Intelligence Mode)
#
# Control flow must never be parsed out of prose. Two mechanisms, tried in
# order of how strictly the SERVER enforces the shape:
#
#   1. tool calling  — vLLM's own tool parser (--tool-call-parser qwen3_xml
#      --enable-auto-tool-choice) turns the model's call into structured
#      `tool_calls`, so no regex of ours ever touches the model's output;
#   2. guided JSON   — `response_format: json_schema`, which constrains
#      DECODING, so the reply cannot be malformed in the first place.
#
# Both are optional at runtime: a backend that rejects the extension returns a
# 400, which is caught and downgraded rather than failing the request. The
# caller validates with pydantic either way (see core/sf_intel/planner.py),
# because "the server said it matched the schema" and "this is a decision we
# can act on" are not the same claim.
# ---------------------------------------------------------------------------


class ToolCall:
    """One parsed tool call: the name and its already-decoded arguments."""

    __slots__ = ("id", "name", "arguments")

    def __init__(self, id: str, name: str, arguments: dict) -> None:
        self.id = id
        self.name = name
        self.arguments = arguments

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ToolCall(name={self.name!r}, arguments={self.arguments!r})"


def _parse_tool_calls(message: object) -> List[ToolCall]:
    """Read `tool_calls` off a completion message, tolerating shapes.

    vLLM has shipped these as objects and as plain dicts depending on the
    client version, and `arguments` is a JSON *string* per the OpenAI schema.
    A call whose arguments will not parse is dropped rather than guessed at.
    """
    import json

    raw = _delta_value(message, "tool_calls") or []
    calls: List[ToolCall] = []
    for item in raw:
        function = _delta_value(item, "function")
        if function is None:
            continue
        name = _delta_value(function, "name")
        arguments = _delta_value(function, "arguments")
        if not name:
            continue
        if isinstance(arguments, str):
            try:
                parsed = json.loads(arguments or "{}")
            except ValueError:
                continue
        elif isinstance(arguments, Mapping):
            parsed = dict(arguments)
        else:
            parsed = {}
        if not isinstance(parsed, dict):
            continue
        calls.append(
            ToolCall(str(_delta_value(item, "id") or ""), str(name), parsed)
        )
    return calls


async def chat_with_tools(
    messages: Sequence[dict],
    *,
    tools: Sequence[dict],
    tool_choice: object = "auto",
    model: Optional[str] = None,
    temperature: float = 0.0,
    max_tokens: Optional[int] = None,
    thinking: bool = False,
    effort: str = "medium",
) -> Tuple[str, List[ToolCall]]:
    """One completion that may call tools. → (text, tool_calls).

    Raises whatever the client raises: a backend without
    `--enable-auto-tool-choice` answers 400, and the caller downgrades to
    guided JSON rather than pretending tool calling worked.

    Budget policy for tools + thinking: NEVER a server-side thinking cut —
    a budget enforced inside the <think> block can truncate mid-tool-call
    and corrupt the arguments. Instead the effort's thinking budget is added
    to max_tokens (generous room), and overruns surface as a normal finish
    rather than a mangled call.
    """
    client = _openai_client()
    model_id = model or settings.llm_model
    requested = max_tokens
    if thinking:
        budget_tokens = thinking_budget(effort)
        if budget_tokens and max_tokens is not None:
            requested = max_tokens + budget_tokens
        elif budget_tokens is None:
            requested = max(max_tokens or 0, settings.max_output_tokens)
    sized, budget = await context.fit_request(
        normalize_system(messages),
        base_url=settings.openai_base_url,
        model=model_id,
        requested_max_tokens=requested,
    )
    request = dict(
        model=model_id,
        messages=sized,
        temperature=temperature,
        max_tokens=budget,
        tools=list(tools),
        tool_choice=tool_choice,
    )
    extra_body = reasoning_extra_body(settings.main_capabilities, thinking)
    if extra_body is not None:
        request["extra_body"] = extra_body
    reset_finish_reason()
    resp = await _primary_send(client, request, what="chat_with_tools", base_url=settings.openai_base_url)
    _capture_usage(resp)
    _capture_finish(resp)
    message = resp.choices[0].message
    # The <think> fallback matters here most: a raw thinking block leaking
    # into `text` would be re-parsed downstream as if the model SAID it.
    _, text = split_reasoning(message, settings.main_capabilities)
    return text, _parse_tool_calls(message)


async def json_completion(
    messages: Sequence[dict],
    *,
    json_schema: Optional[dict] = None,
    schema_name: str = "decision",
    model: Optional[str] = None,
    temperature: float = 0.0,
    max_tokens: Optional[int] = None,
    thinking: bool = False,
    effort: Optional[str] = None,
) -> str:
    """A completion constrained to one JSON schema, with an honest fallback.

    When the runtime supports guided decoding the reply CANNOT be malformed.
    When it does not (a 400 on `response_format`), the same request is retried
    unconstrained — the caller still validates, and a validation failure there
    is repaired once before anything falls back to a deterministic path.

    `max_tokens` is the ANSWER's ceiling. With `thinking` on, reasoning and
    answer draw from one pool, so the request is sized the way `stream_chat`
    sizes it: the answer ceiling plus the effort's thinking budget in
    budgeted mode, and floored at MAX_OUTPUT_TOKENS in the default unbounded
    mode. Until 2026-09-11 the caller's number went through untouched and
    every thinking-on JSON call here was the first of its kind — the
    Artifact Studio's outline (2,500) and review (2,000) both ended inside
    the reasoning block, finish_reason=length, no JSON, and Think effort
    silently became Fast plus two minutes of thinking.
    """
    model_id = model or settings.llm_model
    reset_finish_reason()
    requested = max_tokens
    if thinking:
        budget_tokens = thinking_budget(effort) if effort else None
        if budget_tokens and max_tokens is not None:
            requested = max_tokens + budget_tokens
        elif budget_tokens is None:
            requested = max(max_tokens or 0, settings.max_output_tokens)

    client = _openai_client()
    sized, budget = await context.fit_request(
        normalize_system(messages),
        base_url=settings.openai_base_url,
        model=model_id,
        requested_max_tokens=requested,
    )
    base = dict(
        model=model_id,
        messages=sized,
        temperature=temperature,
        max_tokens=budget,
    )
    extra_body = reasoning_extra_body(settings.main_capabilities, thinking)
    if extra_body is not None:
        base["extra_body"] = extra_body

    if json_schema is not None:
        guided = dict(base)
        guided["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "schema": json_schema,
                "strict": False,
            },
        }
        try:
            resp = await _primary_send(client, guided, what="json_completion", base_url=settings.openai_base_url)
            _note_truncation(resp, schema_name, budget)
            return resp.choices[0].message.content or ""
        except _bad_request_error() as exc:
            # A 400 is the documented "this backend has no guided decoding"
            # shape and the ONLY one that should downgrade. This used to
            # catch every exception, so during an engine outage each call
            # logged a misleading "guided JSON unavailable" and immediately
            # re-sent an unconstrained request into the same dead port.
            log.info(
                "guided JSON unavailable on this backend (%s: %s); retrying "
                "unconstrained",
                type(exc).__name__,
                str(exc)[:160],
            )

    resp = await _primary_send(client, base, what="json_completion", base_url=settings.openai_base_url)
    _note_truncation(resp, schema_name, budget)
    return resp.choices[0].message.content or ""


def _note_truncation(resp: Any, schema_name: str, budget: Optional[int]) -> None:
    """A JSON answer cut off at max_tokens is unparseable and the caller
    reports it as "not JSON"; the caller can tell the two apart through
    `get_finish_reason()` (set here, as the streaming loop sets it), and the
    operator sees the budget it hit — the schema and the numbers, never the
    content. (The 2026-09-11 e2e run lost a workbook to a 180 s repair pass
    that ran to 12,000 tokens; the log said only "not JSON".)"""
    try:
        choice = resp.choices[0]
        reason = getattr(choice, "finish_reason", None)
        usage = getattr(resp, "usage", None)
        produced = getattr(usage, "completion_tokens", None)
    except (AttributeError, IndexError, TypeError):
        return
    _set_finish_reason(reason)
    if reason == "length":
        log.warning("json_completion: %s answer truncated at max_tokens=%s (completion_tokens=%s)", schema_name, budget, produced)


# ---------------------------------------------------------------------------
# Sidecar vLLM services (router / vision / embeddings)
# ---------------------------------------------------------------------------

async def router_chat_completion(
    messages: Sequence[dict],
    *,
    temperature: float = 0.0,
    max_tokens: int = 200,
) -> str:
    """Router model (ROUTER_MODEL on ROUTER_BASE_URL); returns assistant text.

    INTERNAL ONLY (CONTRACT v2 §1): these are classification calls ("which
    engine?", "search: yes/no", a frame caption), never a person's answer —
    the router has no breaker, no queue and no lane because nothing a person
    reads comes from it; `stream_chat_events` refuses to stream from its
    URL. A long message is CLIPPED rather than sent whole — the opening of a
    message determines its class, and the router's window is far smaller
    than the main model's.
    """
    client = _client(settings.router_base_url)
    sized, budget = await context.fit_request(
        normalize_system(
            clip_message_contents(messages, settings.router_input_char_cap)
        ),
        base_url=settings.router_base_url,
        model=settings.router_model,
        requested_max_tokens=max_tokens,
    )
    request = dict(
        model=settings.router_model,
        messages=sized,
        temperature=temperature,
        max_tokens=budget,
    )
    # A classification call must never spend its budget reasoning when the
    # selected runtime exposes the Qwen chat-template switch.
    extra_body = reasoning_extra_body(settings.router_capabilities, False)
    if extra_body is not None:
        request["extra_body"] = extra_body
    # A sidecar: on an interactive turn it gets one attempt and the caller's
    # fallback (every caller has one); a job with a recovery window waits.
    resp = await resilient(
        lambda: client.chat.completions.create(**request),
        what="router_chat_completion", base_url=settings.router_base_url,
        recovery_s=sidecar_recovery_s(),
    )
    return resp.choices[0].message.content or ""


async def vision_chat_stream(
    messages: Sequence[dict],
    *,
    temperature: float = 0.2,
    max_tokens: Optional[int] = None,
) -> AsyncIterator[str]:
    """Vision model (VISION_MODEL on VISION_BASE_URL); yields text deltas.

    User messages carry OpenAI multimodal content parts, e.g.
    [{"type": "text", ...}, {"type": "image_url", "image_url": {"url": "data:..."}}].
    """
    client = _client(settings.vision_base_url)
    request = dict(
        model=settings.vision_model,
        messages=normalize_system(messages),
        temperature=temperature,
        max_tokens=max_tokens,
        stream=True,
    )
    async with _consume(await _open_stream(client, request)) as stream:
        async for chunk in stream:
            _capture_usage(chunk)
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta and delta.content:
                yield delta.content


async def embed_texts(
    texts: Sequence[str],
    *,
    model: Optional[str] = None,
    timeout: Optional[float] = None,
    kind: str = "batch",
) -> List[List[float]]:
    """Embed texts via EMBED_BASE_URL ({model, input}); one vector per input,
    in input order.

    The embedding model has the smallest window in the stack, and a long user
    question was previously sent to it verbatim — a 400 that, unlike the
    router's, was not caught anywhere. Inputs are clipped first.
    """
    # Its own read timeout (ADR-0001 D13). Embeddings sit on the request
    # path of every assistant turn (recall, dense retrieval); with the
    # generation-sized read timeout a hung embedding service held every
    # chat for up to the whole wall clock before failing soft. Batches (the
    # indexer, the recall backfill) get the longer budget.
    read = float(timeout if timeout is not None else settings.embed_batch_timeout_s)
    client = _client(settings.embed_base_url, read_timeout=read)
    cap = settings.embed_input_char_cap
    started = time.perf_counter()
    # A query embedding is on the chat path with its own few-second budget
    # (embed_query fails soft to EmbedUnavailable), so it gets ONE attempt.
    # Everything else is a sidecar call: one attempt on an interactive turn
    # too (recall and dense retrieval fall back to lexical-only in under a
    # second, which is what they did before the wrapper existed), and the
    # full window only where nobody is watching — an index or backfill batch.
    recovery = 0.0 if kind == "query" else sidecar_recovery_s()
    try:
        resp = await resilient(
            lambda: client.embeddings.create(
                model=model or settings.embed_model,
                input=[t[:cap] for t in texts],
            ),
            what=f"embed_{kind}", base_url=settings.embed_base_url, recovery_s=recovery,
        )
    except Exception:
        metrics.inc("embed_requests_total", outcome="error", kind=kind)
        metrics.observe("embed_seconds", time.perf_counter() - started, kind=kind)
        raise
    metrics.inc("embed_requests_total", outcome="ok", kind=kind)
    metrics.observe("embed_seconds", time.perf_counter() - started, kind=kind)
    metrics.observe("embed_batch_size", float(len(texts)), kind=kind)
    return [item.embedding for item in sorted(resp.data, key=lambda d: d.index)]


class EmbedUnavailable(RuntimeError):
    """A query embedding did not happen (busy past the wait, or failed)."""


#: Qwen3-Embedding is asymmetric: queries carry an instruction, documents do
#: not. Documents stay as indexed (no reindex); only the query side changes.
#: Measured by tools/rag_eval.py — see docs/07-brain/04-eval-and-benchmarks.md.
QUERY_INSTRUCTION = (
    "Instruct: Given a web search query, retrieve relevant passages that "
    "answer the query\nQuery: "
)

_EMBED_LRU: "OrderedDict[tuple, List[float]]" = OrderedDict()
_EMBED_LRU_MAX = 1024
_embed_slots: Optional[asyncio.Semaphore] = None
_embed_slots_loop: Optional[asyncio.AbstractEventLoop] = None


def _embed_semaphore() -> asyncio.Semaphore:
    global _embed_slots, _embed_slots_loop
    loop = asyncio.get_running_loop()
    if _embed_slots is None or _embed_slots_loop is not loop:
        _embed_slots = asyncio.Semaphore(max(1, int(settings.embed_max_inflight)))
        _embed_slots_loop = loop
    return _embed_slots


async def embed_query(
    text: str,
    *,
    instruction: Optional[str] = None,
    wait: Optional[float] = None,
    timeout: Optional[float] = None,
) -> List[float]:
    """ONE embedding of a query, cached and bounded (ADR-0001 D10/D13).

    The same question was embedded up to three times per turn (recall, the
    dense half of retrieval, site Q&A). One LRU keyed on (model,
    instruction, text) collapses that; a semaphore with a wait deadline
    keeps a burst from piling onto the embedding sidecar (concurrency 4) —
    past the deadline the caller gets `EmbedUnavailable` and retrieves
    lexical-only, which it says in its metrics.
    """
    clean = " ".join((text or "").split())
    if not clean:
        raise EmbedUnavailable("empty query")
    key = (settings.embed_model, instruction or "", clean)
    hit = _EMBED_LRU.get(key)
    if hit is not None:
        _EMBED_LRU.move_to_end(key)
        metrics.inc("embed_requests_total", outcome="cache", kind="query")
        return list(hit)
    sem = _embed_semaphore()
    deadline = float(wait if wait is not None else settings.embed_wait_s)
    queued = time.perf_counter()
    try:
        async with asyncio.timeout(deadline):
            await sem.acquire()
    except TimeoutError:
        metrics.inc("embed_requests_total", outcome="busy", kind="query")
        raise EmbedUnavailable("embedding service busy") from None
    metrics.observe("embed_queue_seconds", time.perf_counter() - queued, kind="query")
    try:
        vectors = await embed_texts(
            [f"{instruction}{clean}" if instruction else clean],
            timeout=float(timeout if timeout is not None else settings.embed_timeout_s),
            kind="query",
        )
    except Exception as exc:  # noqa: BLE001 — one outcome for callers
        raise EmbedUnavailable(str(exc)) from exc
    finally:
        sem.release()
    if not vectors or not vectors[0]:
        raise EmbedUnavailable("empty embedding")
    _EMBED_LRU[key] = list(vectors[0])
    _EMBED_LRU.move_to_end(key)
    while len(_EMBED_LRU) > _EMBED_LRU_MAX:
        _EMBED_LRU.popitem(last=False)
    return list(vectors[0])


def embed_cache_clear() -> None:
    _EMBED_LRU.clear()
