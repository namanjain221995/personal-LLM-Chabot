"""How `/v1` reaches an engine that is NOT the main model (2026-09-13).

WHY THIS FILE EXISTS. The owner asked for every model TechSara runs to be
offered on `/v1` (techsara-8b-vision on the router, techsara-ocr on
Unlimited-OCR, …). The main model is still reached ONLY through
`llm.stream_chat_events` — the breaker, the admission lanes and `_fit` live
there, and a second path to it would be a second set of those guarantees. The
router and the OCR engine have none of that machinery (they are sidecars the
chat app calls with one attempt and a fallback), so their public path is this
module: resolve the engine from `settings` at call time, stream from it, and
map what goes wrong into the CONTRACT §9 vocabulary.

THREE RULES.

1. **The address never comes from a request.** `target(engine)` reads the base
   URL and served model name from `settings` for a key out of the registry's
   closed set. Nothing a caller sends can name a host, and `stream_chat`
   refuses a target that is the main engine's URL, so this file can never be
   the way around the breaker and the lanes.
2. **The transport timeout is fixed per engine, never per request.** Every
   `/v1` generation is streamed, so the read timeout bounds the longest
   SILENCE between chunks, not the generation. There is NO wall clock
   (no-timeout design, 2026-09-13): the router/OCR read timeout is
   PUBLIC_API_SIDECAR_SILENCE_S (1,800 s), the fallback for when the engine's
   `/metrics` witnesses are unknown; `liveness.SidecarWitness` decides a
   stalled, lost or restarted engine long before it. And `llm._client` caches
   a client per (loop, URL, key, read timeout): a caller-derived value in that
   key turns the bounded cache into a new connection pool per request (F048).
3. **Lazy imports.** `publicapi` must stay importable by the OpenAPI lint job
   without the engine stack, so `llm`, `resilience` and `httpx` are imported
   inside the functions that use them.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence, Tuple

from ..config import settings
from . import errors, registry

log = logging.getLogger(__name__)

#: Read timeouts per engine for the POOLING engines (embeddings, rerank; T4
#: owns their re-send rules). The chat sidecars read `sidecar_read_timeout_s`.
READ_TIMEOUT_S: Dict[str, float] = {
    registry.ENGINE_EMBED: 60.0,
    registry.ENGINE_RERANK: 60.0,
}


def sidecar_read_timeout_s() -> float:
    """PUBLIC_API_SIDECAR_SILENCE_S (1,800 s): the router/OCR transport read
    timeout — the silence fallback when the witnesses are unknown, never a
    budget for the generation. Read per call (a monkeypatch or an operator's
    restart takes effect), and a SETTING, never a request value (F048)."""
    return max(1.0, registry.setting_float("PUBLIC_API_SIDECAR_SILENCE_S", 1800.0))


def metrics_root(resolved: "EngineTarget") -> str:
    """The engine's server root for `/metrics` (the base URL minus `/v1`)."""
    base = resolved.base_url.rstrip("/")
    return base[: -len("/v1")] if base.endswith("/v1") else base

#: How long a served-window probe waits. It is a nicety that can only NARROW a
#: ceiling; a slow engine must not add seconds to a request for it.
PROBE_TIMEOUT_S = 3.0
#: A failed probe is not retried for this long, so a dead engine costs one
#: probe per half minute rather than one per request.
PROBE_FAILURE_BACKOFF_S = 30.0

#: The engine keys `stream_chat` serves. The main model is deliberately absent.
SIDECAR_CHAT_ENGINES = (registry.ENGINE_ROUTER, registry.ENGINE_OCR)


@dataclass(frozen=True)
class EngineTarget:
    """Where one engine is, resolved from settings NOW. Never rendered."""

    key: str
    base_url: str
    model: str
    api_key: Optional[str] = field(default=None, repr=False)
    #: Speech runs on several replicas; `base_url` is the one public work
    #: goes to (the last), and these are all of them.
    base_urls: Tuple[str, ...] = ()


def target(engine: str) -> Optional[EngineTarget]:
    """The engine behind a registry key, or None when this deployment does
    not run it (the registry withdraws the model in the same cases)."""
    if engine == registry.ENGINE_MAIN:
        return EngineTarget(
            key=engine,
            base_url=str(settings.openai_base_url).rstrip("/"),
            model=str(settings.llm_model),
            api_key=getattr(settings, "openai_api_key", None),
        )
    configured = {
        registry.ENGINE_ROUTER: registry._router_configured,
        registry.ENGINE_OCR: registry._ocr_configured,
        registry.ENGINE_EMBED: registry._embed_configured,
        registry.ENGINE_RERANK: registry._rerank_configured,
        registry.ENGINE_ASR: registry._asr_configured,
    }.get(engine)
    if configured is None or not configured():
        return None
    model = {
        registry.ENGINE_ROUTER: getattr(settings, "router_model", ""),
        registry.ENGINE_OCR: getattr(settings, "ocr_model", ""),
        registry.ENGINE_EMBED: getattr(settings, "embed_model", ""),
        registry.ENGINE_RERANK: getattr(settings, "rerank_model", ""),
        registry.ENGINE_ASR: getattr(settings, "asr_model", ""),
    }[engine]
    urls: Tuple[str, ...] = ()
    if engine == registry.ENGINE_ASR:
        urls = tuple(str(u).rstrip("/") for u in (getattr(settings, "asr_base_urls", ()) or ()))
    return EngineTarget(
        key=engine,
        base_url=registry.engine_base_url(engine),
        model=str(model),
        api_key=None,
        base_urls=urls,
    )


# ------------------------------------------------------- served window --

#: engine → monotonic time of the last probe attempt (success or not).
_probed_at: Dict[str, float] = {}


async def served_window(engine: str) -> Optional[int]:
    """The engine's own `max_model_len`, from `GET <base>/models`, cached.

    Reported to `registry.note_served_window`, which only ever NARROWS a
    public ceiling with it. Never raises and never blocks a request for more
    than PROBE_TIMEOUT_S: a probe that fails leaves the public number as it
    was (the engine's window was verified by `docker inspect` when the number
    was chosen) and is not retried for PROBE_FAILURE_BACKOFF_S.
    """
    hint = registry.served_window_hint(engine)
    if hint is not None:
        return hint
    resolved = target(engine)
    if resolved is None or not resolved.base_url:
        return None
    now = time.monotonic()
    last = _probed_at.get(engine)
    if last is not None and now - last < PROBE_FAILURE_BACKOFF_S:
        return None
    _probed_at[engine] = now
    try:
        import httpx

        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_S) as client:
            response = await client.get(f"{resolved.base_url}/models")
            response.raise_for_status()
            payload = response.json()
        tokens = None
        for entry in payload.get("data") or []:
            if not isinstance(entry, dict):
                continue
            if tokens is None or str(entry.get("id") or "") == resolved.model:
                length = entry.get("max_model_len")
                if isinstance(length, int) and length > 0:
                    tokens = length
        if tokens:
            registry.note_served_window(engine, resolved.base_url, tokens)
        return tokens
    except Exception:  # noqa: BLE001 - a probe is advisory, never a failure
        log.debug("served-window probe for %s failed", engine, exc_info=True)
        return None


def reset_probe_state() -> None:
    """For tests."""
    _probed_at.clear()
    registry.clear_served_windows()


# ------------------------------------------------------------- errors --

_CONNECTION_NAMES = frozenset(
    {
        "APIConnectionError",
        "ConnectError",
        "ConnectTimeout",
        "RemoteProtocolError",
        "ReadError",
        "WriteError",
        "NetworkError",
        "ModelUnavailable",
        "BreakerOpen",
        "QueuedForRecovery",
    }
)
_TIMEOUT_NAMES = frozenset(
    {"APITimeoutError", "ReadTimeout", "WriteTimeout", "PoolTimeout", "TimeoutException"}
)

UNAVAILABLE_RETRY_AFTER = 30.0


def _status_of(exc: BaseException) -> Optional[int]:
    status = getattr(exc, "status_code", None)
    if status is None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _body_text(exc: BaseException) -> str:
    """The engine's own words, lowercased, for CLASSIFYING only — never for
    a message (CONTRACT §9: an engine's text can name a path or a host)."""
    parts = [str(getattr(exc, "message", "") or ""), str(getattr(exc, "body", "") or "")]
    with contextlib.suppress(Exception):
        parts.append(str(exc))
    return " ".join(parts).lower()


def map_engine_exception(exc: BaseException, engine: str) -> Optional[errors.ApiError]:
    """The CONTRACT §9 error for an engine-side exception, or None when it is
    not one this function recognises. Every message is a FIXED sentence."""
    if isinstance(exc, errors.ApiError):
        return exc
    name = type(exc).__name__
    if isinstance(exc, asyncio.TimeoutError) or name in _TIMEOUT_NAMES:
        return errors.timeout()
    if name in _CONNECTION_NAMES:
        return errors.model_unavailable(retry_after=UNAVAILABLE_RETRY_AFTER)
    status = _status_of(exc)
    if status is None:
        return None
    if status in (400, 422):
        text = _body_text(exc)
        if "image" in text or "multimodal" in text or "media" in text:
            return errors.invalid_request(
                "An image in this request could not be read by the model.", param="input"
            )
        if any(
            marker in text
            for marker in ("context length", "maximum context", "max_model_len", "too long", "prompt is longer")
        ):
            return errors.context_length_exceeded()
        return errors.invalid_request("The model could not accept this request as sent.")
    if status == 413:
        return errors.context_length_exceeded()
    if status == 408:
        return errors.timeout()
    # 404 (the model is not loaded), 429 (engine queue), 5xx: the engine
    # cannot serve right now. Retry-safe.
    return errors.model_unavailable(retry_after=UNAVAILABLE_RETRY_AFTER)


def engine_error(exc: BaseException, engine: str) -> errors.ApiError:
    """`map_engine_exception`, or `errors.from_unexpected` — which DISCARDS
    the text of anything this code did not construct."""
    mapped = map_engine_exception(exc, engine)
    if mapped is not None:
        return mapped
    return errors.from_unexpected(exc)


# ------------------------------------------------------------ streaming --


def _capabilities(engine: str) -> Any:
    return getattr(
        settings,
        "router_capabilities" if engine == registry.ENGINE_ROUTER else "ocr_capabilities",
        None,
    )


async def stream_chat(
    resolved: EngineTarget,
    messages: Sequence[Dict[str, Any]],
    *,
    max_tokens: int,
    temperature: float,
    continue_final_message: bool = False,
    on_dispatch: Optional[Any] = None,
) -> AsyncIterator[Tuple[str, str]]:
    """Stream a chat completion from the router or the OCR engine, yielding
    `(kind, delta)` exactly like `llm.stream_chat_events`, so
    `streaming.Generation` pumps both the same way.

    Usage and finish reason are captured with llm's own helpers, INTO THE
    CONTEXTVARS OF THE TASK ITERATING THIS GENERATOR — `Generation`'s producer
    task, which reads them in its `finally` (the reason usage is not None).

    `max_tokens` is sent as given: the planner already clamped it to the
    public window with a bound that cannot under-count (OCR) or that the
    engine's larger real window absorbs (router), so there is no `/tokenize`
    round-trip here and no trimming of a caller's prompt, ever.

    No wall clock (2026-09-13). A silent engine is judged by the attempt's
    `liveness.SidecarGuard` (witness verdicts) and, when the witnesses are
    unknown, by the 1,800 s read timeout.

    `continue_final_message` resumes an interrupted answer: the last message
    is the assistant's partial text and the engine extends it
    (`add_generation_prompt: false`), so a durable router run is never
    regenerated from scratch (operator check in the design: verify on the
    router before relying on it; PUBLIC_API_RESUME_ENABLED is the switch).
    `on_dispatch` fires as the request is sent (before the first chunk).
    """
    from .. import llm
    from ..resilience import resilient

    if resolved.key not in SIDECAR_CHAT_ENGINES:
        raise ValueError(f"stream_chat serves {SIDECAR_CHAT_ENGINES}, not {resolved.key!r}")
    if llm._is_primary(resolved.base_url) or registry._same_address(
        resolved.base_url, settings.openai_base_url
    ):
        # Rule 1: this module is never the way around the breaker and lanes.
        raise errors.model_unavailable(retry_after=UNAVAILABLE_RETRY_AFTER)

    client = llm._client(
        resolved.base_url, resolved.api_key, read_timeout=sidecar_read_timeout_s()
    )
    request: Dict[str, Any] = dict(
        model=resolved.model,
        messages=llm.normalize_system(list(messages)),
        temperature=float(temperature),
        max_tokens=max(1, int(max_tokens)),
        stream=True,
        # vLLM reports usage on the last chunk only when asked. Both sidecars
        # are vLLM; llm._open_stream's refusal fallback exists for runtimes
        # that are not, and is not needed on this path.
        stream_options={"include_usage": True},
    )
    capabilities = _capabilities(resolved.key)
    extra_body: Dict[str, Any] = {}
    if capabilities is not None:
        extra_body = dict(llm.reasoning_extra_body(capabilities, False) or {})
    if continue_final_message:
        extra_body["continue_final_message"] = True
        extra_body["add_generation_prompt"] = False
    if extra_body:
        request["extra_body"] = extra_body

    llm.reset_finish_reason()

    async def open_stream() -> Any:
        # `on_dispatch` fires as the request is SENT, not after `resilient`
        # returns (2026-09-14 review, high): `resilient(stream=True)` primes
        # the first chunk as part of the open, so the old placement meant a
        # router call was "dispatched" only once it had produced a token —
        # the witness verdicts never applied to a prefill, and a router
        # crash mid-prefill looked like a refused connection. A call that
        # never reached the engine is told apart by its error instead
        # (`liveness.sidecar_error_kind`: connect vs status vs broken).
        if on_dispatch is not None:
            with contextlib.suppress(Exception):
                on_dispatch()
        return await client.chat.completions.create(**request)

    # ONE attempt (recovery_s=0): a public request is not queued behind an
    # engine restart; it gets the retry-safe 503 and its Retry-After.
    opened = await resilient(
        open_stream,
        what=f"public_{resolved.key}_stream",
        base_url=resolved.base_url,
        recovery_s=0,
        stream=True,
    )
    async with llm._consume(opened) as stream:
        async for chunk in stream:
            llm._capture_finish(chunk)
            llm._capture_usage(chunk)
            choices = getattr(chunk, "choices", None)
            if not choices:
                continue
            delta = getattr(choices[0], "delta", None)
            if delta is None:
                continue
            if capabilities is not None:
                reasoning = llm._reasoning_delta(delta, capabilities)
                if reasoning:
                    yield "reasoning", reasoning
            content = llm._delta_value(delta, "content")
            if content:
                yield "token", str(content)


__all__ = [
    "EngineTarget",
    "READ_TIMEOUT_S",
    "SIDECAR_CHAT_ENGINES",
    "metrics_root",
    "sidecar_read_timeout_s",
    "engine_error",
    "map_engine_exception",
    "served_window",
    "stream_chat",
    "target",
]
