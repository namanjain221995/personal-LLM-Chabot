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

import contextlib
import logging
import re
from collections.abc import Mapping
from typing import AsyncIterator, List, Optional, Sequence, Tuple

from . import context
from .config import settings
from .context import clip_message_contents
from .model_capabilities import ModelCapabilities, ReasoningField

log = logging.getLogger(__name__)

# Local inference servers: the key is a placeholder, never a real secret.
LOCAL_API_KEY = "local-no-key"

# V2-DESIGN §1: chat request model/effort choices.
MODEL_CHOICES = ("smart", "fast")
# Four levels on ONE model. "fast" and "low" skip the reasoning pass; the
# difference between them is how much work the orchestrator may do (low may
# search the web, fast may not). See engines/orchestrate.py.
REASONING_EFFORTS = ("fast", "low", "medium", "high", "extra_high")


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


def _client(base_url: str, api_key: Optional[str] = None):
    from openai import AsyncOpenAI  # cheap, but keep out of module import path

    return AsyncOpenAI(
        base_url=base_url,
        api_key=api_key or LOCAL_API_KEY,
        timeout=settings.llm_request_timeout,
    )


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
    client = _openai_client()
    model_id = model or settings.llm_model
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
    resp = await client.chat.completions.create(**request)
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
    client = _openai_client()
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
            client.chat.completions.create(**request),
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
    client = _openai_client()
    model_id = model or settings.llm_model
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
    stream = await client.chat.completions.create(**request)
    async for chunk in stream:
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

    "smart" (default) → MAIN_MODEL on OPENAI_BASE_URL; "fast" → ROUTER_MODEL
    on ROUTER_BASE_URL.
    """
    if choice == "fast":
        return settings.router_base_url, LOCAL_API_KEY, settings.router_model
    return settings.openai_base_url, settings.openai_api_key, settings.llm_model


def served_model_id(choice: str) -> str:
    """The served model id a V2 model choice resolves to (for meta.model)."""
    return resolve_model_choice(choice)[2]


def capabilities_for_model_choice(choice: str) -> ModelCapabilities:
    """Capabilities for the endpoint selected by a V2 model choice."""
    if choice == "fast":
        return settings.router_capabilities
    return settings.main_capabilities


def wants_thinking(model_choice: str = "smart", effort: str = "medium") -> bool:
    """Should this call run the model's reasoning pass?

    One set of weights now serves both picker choices, so "Fast" is not a
    smaller model — it is the SAME model with thinking switched off. That is
    what actually makes it fast: the reasoning pass, not the parameter count,
    is where the seconds go. Effort "low" means the same thing.
    """
    if model_choice != "smart":
        return False
    # Fast and Low answer directly; Medium and above think first.
    return effort in ("medium", "high", "extra_high")


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
    return {
        "medium": settings.thinking_budget_medium,
        "high": settings.thinking_budget_high,
        "extra_high": settings.thinking_budget_extra_high,
    }.get(effort)


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
    client = _client(base_url, api_key)
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
    # Size the call to the window of the model that will actually serve it.
    # "fast" resolves to a much smaller window than "smart", so a fixed
    # max_tokens that is fine on one is a 400 on the other.
    sized, budget = await context.fit_request(
        normalize_system(apply_reasoning_effort(messages, effort, model_choice)),
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
    capabilities = capabilities_for_model_choice(model_choice)
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
    stream = await client.chat.completions.create(**request)
    async for chunk in stream:
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
            yield (
                "token",
                f"\n\n[generation stopped after {int(elapsed)}s — wall-clock "
                "guard; the text above is what was produced]",
            )
            return
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
                fallback = dict(request)
                # The retry only writes the ANSWER, so the caller's original
                # ceiling is the honest budget for it.
                fallback["max_tokens"] = (
                    min(budget, max_tokens) if max_tokens is not None else budget
                )
                fb_extra = reasoning_extra_body(capabilities, False)
                if fb_extra is not None:
                    fallback["extra_body"] = fb_extra
                else:
                    fallback.pop("extra_body", None)
                fb_stream = await client.chat.completions.create(**fallback)
                async for fb_chunk in fb_stream:
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
    resp = await client.chat.completions.create(**request)
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
) -> str:
    """A completion constrained to one JSON schema, with an honest fallback.

    When the runtime supports guided decoding the reply CANNOT be malformed.
    When it does not (a 400 on `response_format`), the same request is retried
    unconstrained — the caller still validates, and a validation failure there
    is repaired once before anything falls back to a deterministic path.
    """
    client = _openai_client()
    model_id = model or settings.llm_model
    sized, budget = await context.fit_request(
        normalize_system(messages),
        base_url=settings.openai_base_url,
        model=model_id,
        requested_max_tokens=max_tokens,
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
            resp = await client.chat.completions.create(**guided)
            return resp.choices[0].message.content or ""
        except Exception as exc:  # noqa: BLE001 — downgrade, never fail here
            import logging

            logging.getLogger(__name__).info(
                "guided JSON unavailable on this backend (%s: %s); retrying "
                "unconstrained",
                type(exc).__name__,
                str(exc)[:160],
            )

    resp = await client.chat.completions.create(**base)
    return resp.choices[0].message.content or ""


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

    These are classification calls ("which engine?", "search: yes/no"), so a
    long message is CLIPPED rather than sent whole — the opening of a message
    determines its class, and the router's window is far smaller than the main
    model's.
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
    resp = await client.chat.completions.create(**request)
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
    stream = await client.chat.completions.create(
        model=settings.vision_model,
        messages=normalize_system(messages),
        temperature=temperature,
        max_tokens=max_tokens,
        stream=True,
    )
    async for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta and delta.content:
            yield delta.content


async def embed_texts(
    texts: Sequence[str],
    *,
    model: Optional[str] = None,
) -> List[List[float]]:
    """Embed texts via EMBED_BASE_URL ({model, input}); one vector per input,
    in input order.

    The embedding model has the smallest window in the stack, and a long user
    question was previously sent to it verbatim — a 400 that, unlike the
    router's, was not caught anywhere. Inputs are clipped first.
    """
    client = _client(settings.embed_base_url)
    cap = settings.embed_input_char_cap
    resp = await client.embeddings.create(
        model=model or settings.embed_model,
        input=[t[:cap] for t in texts],
    )
    return [item.embedding for item in sorted(resp.data, key=lambda d: d.index)]
