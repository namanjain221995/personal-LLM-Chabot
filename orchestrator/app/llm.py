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

from typing import AsyncIterator, List, Optional, Sequence, Tuple

from . import context
from .config import settings
from .context import clip_message_contents

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
) -> str:
    """Non-streaming chat completion; returns the assistant text."""
    client = _openai_client()
    model_id = model or settings.llm_model
    sized, budget = await context.fit_request(
        normalize_system(messages),
        base_url=settings.openai_base_url,
        model=model_id,
        requested_max_tokens=max_tokens,
    )
    resp = await client.chat.completions.create(
        model=model_id,
        messages=sized,
        temperature=temperature,
        max_tokens=budget,
        extra_body=thinking_body(True),
    )
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
    stream = await client.chat.completions.create(
        model=model_id,
        messages=sized,
        temperature=temperature,
        max_tokens=budget,
        stream=True,
        # Reasoning is drawn from the SAME budget as the answer. Summarising a
        # result set does not need it, and with hundreds of rows in the prompt
        # the model spent the whole allowance thinking and streamed NO answer
        # at all — the UI showed a data table with empty prose above it.
        extra_body=thinking_body(thinking),
    )
    async for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta and delta.content:
            yield delta.content


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
    stream = await client.chat.completions.create(
        model=model_id,
        messages=sized,
        temperature=temperature,
        max_tokens=budget,
        stream=True,
        # THE picker's real mechanism: Smart thinks, Fast does not.
        extra_body=thinking_body(wants_thinking(model_choice, effort)),
    )
    async for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta is None:
            continue
        # vLLM extension field; absent on models without a reasoning stream.
        # vLLM has shipped the thinking delta under both names: `reasoning`
        # (v0.20+, e.g. the 26.05 NGC image) and `reasoning_content` (older).
        reasoning = getattr(delta, "reasoning", None) or getattr(
            delta, "reasoning_content", None
        )
        if not reasoning and getattr(delta, "model_extra", None):
            reasoning = delta.model_extra.get("reasoning") or delta.model_extra.get(
                "reasoning_content"
            )
        if reasoning:
            yield "reasoning", reasoning
        if delta.content:
            yield "token", delta.content


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
    resp = await client.chat.completions.create(
        model=settings.router_model,
        messages=sized,
        temperature=temperature,
        max_tokens=budget,
        # A classification call must never spend its budget reasoning.
        extra_body=thinking_body(False),
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
