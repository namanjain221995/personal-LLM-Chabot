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

from collections.abc import Mapping
from typing import AsyncIterator, List, Optional, Sequence, Tuple

from . import context
from .config import settings
from .context import clip_message_contents
from .model_capabilities import ModelCapabilities, ReasoningField

# Local inference servers: the key is a placeholder, never a real secret.
LOCAL_API_KEY = "local-no-key"

# V2-DESIGN §1: chat request model/effort choices.
MODEL_CHOICES = ("smart", "fast")
# Four levels on ONE model. "fast" and "low" skip the reasoning pass; the
# difference between them is how much work the orchestrator may do (low may
# search the web, fast may not). See engines/orchestrate.py.
REASONING_EFFORTS = ("fast", "low", "medium", "high")


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
    return resp.choices[0].message.content or ""


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
    # Fast and Low answer directly; Medium and High think first.
    return effort in ("medium", "high")


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


def _reasoning_delta(delta: object, capabilities: ModelCapabilities) -> Optional[str]:
    """Extract a reasoning delta without assuming a vLLM response shape.

    Missing extension fields are normal on OpenAI-compatible backends.  They
    simply produce no reasoning event; answer content continues through the
    unchanged ``token`` SSE path.
    """
    if not capabilities.supports_reasoning:
        return None
    field = capabilities.reasoning_field
    if field is ReasoningField.NONE:
        return None
    if field is ReasoningField.AUTO:
        names = (ReasoningField.REASONING.value, ReasoningField.REASONING_CONTENT.value)
    else:
        names = (field.value,)

    model_extra = _delta_value(delta, "model_extra")
    for name in names:
        value = _delta_value(delta, name)
        if not value and isinstance(model_extra, Mapping):
            value = model_extra.get(name)
        if value:
            return str(value)
    return None


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
    # Size the call to the window of the model that will actually serve it.
    # "fast" resolves to a much smaller window than "smart", so a fixed
    # max_tokens that is fine on one is a 400 on the other.
    sized, budget = await context.fit_request(
        normalize_system(apply_reasoning_effort(messages, effort, model_choice)),
        base_url=base_url,
        model=model_id,
        requested_max_tokens=max_tokens,
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
    extra_body = reasoning_extra_body(
        capabilities, wants_thinking(model_choice, effort)
    )
    if extra_body is not None:
        request["extra_body"] = extra_body
    stream = await client.chat.completions.create(**request)
    async for chunk in stream:
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
            yield "reasoning", reasoning
        content = _delta_value(delta, "content")
        if content:
            yield "token", str(content)


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
) -> Tuple[str, List[ToolCall]]:
    """One completion that may call tools. → (text, tool_calls).

    Raises whatever the client raises: a backend without
    `--enable-auto-tool-choice` answers 400, and the caller downgrades to
    guided JSON rather than pretending tool calling worked.
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
        tools=list(tools),
        tool_choice=tool_choice,
    )
    extra_body = reasoning_extra_body(settings.main_capabilities, thinking)
    if extra_body is not None:
        request["extra_body"] = extra_body
    resp = await client.chat.completions.create(**request)
    message = resp.choices[0].message
    return (_delta_value(message, "content") or ""), _parse_tool_calls(message)


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
