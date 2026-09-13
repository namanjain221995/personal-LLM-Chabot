"""The three non-generating engines, as `/v1` reaches them (2026-09-13).

    embed   Qwen3-Embedding-0.6B     vLLM --runner pooling    POST /v1/embeddings
    rerank  Qwen3-Reranker-0.6B      vLLM --convert classify  POST /score
    asr     whisper-large-v3         compose/whisper/server   POST /v1/audio/transcriptions

WHY NOT THE CHAT APPLICATION'S CLIENTS, which already talk to all three.

* `llm.embed_texts` CLIPS every input at EMBED_INPUT_CHAR_CAP and throws the
  engine's `usage` away. Both are right for recall — a long question embedded
  on its first 8,000 characters is better than no recall — and both are wrong
  for a caller who asked for the embedding of the text they sent and is billed
  by the token.
* `rerank.score` TRUNCATES the query at 600 characters and each document at
  3,000, refuses a pool of equal scores as "degenerate", and trips a breaker
  its canary owns. Each protects a chat answer that has a fallback order to
  keep; a public caller asked for scores, and a silent truncation would score
  a document the caller never sent. The TEMPLATE is kept exactly (PREFIX,
  `<Instruct>`, `<Query>`, `<Document>`, SUFFIX from `app/rerank.py`), because
  without it the scores do not mean "probability the document answers the
  query" (the 2026-09-03 measurement in that file).
* `asr.transcribe` ignores `language` and drops `duration`, and it takes a slot
  in DICTATION's pool — a public burst would tell a person pressing the
  microphone "busy". `/v1` sends its own multipart request instead, to the
  replica dictation does not prefer.

WHAT IS SHARED WITH THE CHAT APPLICATION: the engines, and nothing else. Every
engine call here runs inside `capacity.hold(<engine>, …)` — the per-engine gate
the public side queues on — and chat-app code never passes through that gate,
so chat never waits on public queueing; only the public side yields (the
capacity rule of the 2026-09-13 design). A gate that does not admit in time is
`503 model_unavailable` with `Retry-After`: never a 429, never per caller.

WHAT NEVER LEAVES THIS FILE: base URLs, served model names and engine error
text. Every failure is mapped to a fixed sentence in the CONTRACT §9 table
(`engine_error`); the engine's own message is read only to CLASSIFY a 400 as
over-length, never rendered.
"""
from __future__ import annotations

import array
import asyncio
import base64
import contextlib
import logging
import secrets
import shutil
import sys
import time
from dataclasses import dataclass
from typing import (
    Any,
    AsyncIterator,
    Callable,
    Dict,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

import httpx

from ..config import settings
from . import capacity, errors
from .endpoint_models import (
    embed_context_tokens,
    max_audio_seconds,
    rerank_context_tokens,
    setting_int,
)

log = logging.getLogger(__name__)

EMBED = "embed"
RERANK = "rerank"
ASR = "asr"

#: Inputs per embedding call and pairs per `/score` call. Sixteen keeps one
#: public call to a sub-second pooling pass on a 0.6B model, so the chat
#: application's query embeddings (a 1 s slot wait, a 4 s timeout, on the TTFT
#: path) and its reranks (1-2 s waits) are interleaved between public calls
#: rather than queued behind one 256-input batch.
EMBED_CALL_MAX_INPUTS = 16
RERANK_CALL_MAX_PAIRS = 16

#: Fixed per-engine read timeouts — never a per-request value, because
#: `llm._client`'s cache key carries the timeout and a caller-derived one would
#: mint a client per distinct value (AUDIT F048). A 16-input pooling pass is
#: well under a second; 60 s is a stuck-engine guard, not a budget.
EMBED_READ_TIMEOUT_S = 60.0
RERANK_READ_TIMEOUT_S = 60.0

#: The token budget each engine's public calls may hold at once, and why.
#: Engine log 2026-09-11: 18,720 KV tokens on each of embed and rerank (4.57
#: windows of 4,096). The chat application's query embeddings and reranks sit
#: on the TTFT path; public use is capped at 8,192 (44%) so they keep >= 10,528.
_DEFAULT_BUDGET = {EMBED: 8192, RERANK: 8192}
#: An engine that is down: the chat application's own sidecar recovery window
#: is of this order, and a tighter retry loop only adds load to a restart.
UNAVAILABLE_RETRY_AFTER_S = 30

#: Tests install an `httpx.MockTransport` here; production leaves it None.
_transport: Optional[httpx.AsyncBaseTransport] = None


# ------------------------------------------------------------ settings --


def kv_budget_tokens(engine: str) -> int:
    """The token budget one public engine call is PACKED to — the same
    PUBLIC_API_<ENGINE>_KV_BUDGET_TOKENS setting `capacity.hold` admits
    against, so a packed call always fits the gate on its own."""
    return max(1, setting_int(f"PUBLIC_API_{engine.upper()}_KV_BUDGET_TOKENS", _DEFAULT_BUDGET[engine]))


def sync_wait_s() -> float:
    """How long a synchronous public request may queue for an engine in
    total: `capacity.sync_wait_s()`, PUBLIC_API_GATE_WAIT_S (30 s), which keeps
    the silent pre-header wait well inside Cloudflare's 100 s."""
    return float(capacity.sync_wait_s())


# ------------------------------------------------------------ the gate --


@contextlib.asynccontextmanager
async def hold(
    engine: str, *, weight_tokens: int = 0, wait_s: float, yield_to_chat: bool = False
) -> AsyncIterator[None]:
    """`capacity.hold` — one unit of the engine's PUBLIC capacity for one
    engine call, or the 503 `model_unavailable` "at capacity" with Retry-After.
    A seam of one line, so the tests can see which gate each call takes."""
    async with capacity.hold(
        engine, weight_tokens=int(weight_tokens), wait_s=float(wait_s), yield_to_chat=yield_to_chat
    ):
        yield


class _WaitBudget:
    """One request's total queueing time across all of its engine calls.

    A 256-input embedding request is sixteen engine calls, each taking the
    gate again so the chat application and other callers interleave. Without a
    shared budget each call could wait the full 30 s — eight minutes of silence
    before a synchronous answer. The TOTAL wait is bounded instead.
    """

    def __init__(self, total_s: float) -> None:
        self.remaining = max(0.0, float(total_s))

    @contextlib.asynccontextmanager
    async def hold(self, engine: str, *, weight_tokens: int = 0, yield_to_chat: bool = False):
        started = time.monotonic()
        entered = False
        try:
            async with hold(
                engine,
                weight_tokens=weight_tokens,
                wait_s=max(0.001, self.remaining),
                yield_to_chat=yield_to_chat,
            ):
                entered = True
                self.remaining = max(0.0, self.remaining - (time.monotonic() - started))
                yield
        finally:
            if not entered:
                self.remaining = 0.0


# ------------------------------------------------------------ packing --


def _utf8_bytes(text: str) -> int:
    return len(text) if text.isascii() else len(text.encode("utf-8", "surrogatepass"))


def pack(weights: Sequence[int], *, max_items: int, budget: int) -> List[List[int]]:
    """Consecutive indices grouped into engine calls of at most `max_items`
    and at most `budget` summed weight. A single item heavier than the budget
    goes alone — the capacity gate admits it only when nothing else public
    holds the engine. Order is preserved, so results map back by index."""
    batches: List[List[int]] = []
    current: List[int] = []
    used = 0
    for index, weight in enumerate(weights):
        weight = max(1, int(weight))
        if current and (len(current) >= max_items or used + weight > budget):
            batches.append(current)
            current, used = [], 0
        current.append(index)
        used += weight
    if current:
        batches.append(current)
    return batches


# ------------------------------------------------------------ failures --


def _looks_over_length(message: str) -> bool:
    lowered = (message or "").lower()
    return any(word in lowered for word in ("context length", "maximum", "too long", "tokens", "length"))


def _status_of(exc: BaseException) -> Optional[int]:
    status = getattr(exc, "status_code", None)
    if status is None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _is_timeout(exc: BaseException) -> bool:
    if isinstance(exc, (asyncio.TimeoutError, httpx.TimeoutException)):
        return True
    try:
        import openai

        return isinstance(exc, openai.APITimeoutError)
    except Exception:  # noqa: BLE001
        return False


class SidecarError(Exception):
    """A public engine call that did not produce an answer, with what the
    ledger needs to settle it honestly.

    `engine_calls` is how many engine requests were SENT (0: nothing ran — a
    capacity refusal before the first call — which the route settles as
    `cancelled`; more: the engine did work, which is `failed`). `measured_tokens`
    is what the calls that completed reported, so a request that failed half
    way is charged for the half that ran and not for its estimate.
    """

    def __init__(self, error: errors.ApiError, *, engine_calls: int, measured_tokens: int = 0) -> None:
        super().__init__(error.message)
        self.error = error
        self.engine_calls = int(engine_calls)
        self.measured_tokens = int(measured_tokens)


class EngineRefusedInput(Exception):
    """The engine answered 400 for one batch. Carries whether the refusal read
    as over-length, so a single-item retry can name the offending index."""

    def __init__(self, over_length: bool) -> None:
        super().__init__("the engine refused the input")
        self.over_length = over_length


def engine_error(exc: BaseException, engine: str, *, timeout_s: Optional[float] = None) -> errors.ApiError:
    """Anything an engine call raised → the §9 error a caller can act on.

    The exception's own text is NEVER used: an httpx error names the URL it
    failed on, which is an internal host and port.
    """
    if isinstance(exc, errors.ApiError):
        return exc
    if _is_timeout(exc):
        return errors.timeout(timeout_s)
    log.warning("public %s engine call failed: %s", engine, type(exc).__name__, exc_info=True)
    return errors.model_unavailable(UNAVAILABLE_RETRY_AFTER_S)


def _over_length_error(param: str, limit: int, noun: str) -> errors.ApiError:
    return errors.ApiError(
        "context_length_exceeded",
        f"{noun} is longer than the model's {int(limit)} token limit.",
        param=param,
    )


# ---------------------------------------------------------- embeddings --


@dataclass
class EmbedOutcome:
    vectors: List[List[float]]
    #: Summed over every engine call; None when any call did not report.
    prompt_tokens: Optional[int]
    engine_calls: int = 0
    #: Tokens measured on the calls that DID complete, for the ledger of a
    #: request that failed half way.
    partial_prompt_tokens: int = 0


def embed_weights(inputs: Sequence[str]) -> List[int]:
    """The gate weight of each input: its UTF-8 byte length (a byte-level BPE
    spends at most a token per byte, so a caller cannot game it down),
    capped at the engine window — the engine refuses anything longer."""
    window = embed_context_tokens()
    return [min(window, _utf8_bytes(text) + 2) for text in inputs]


async def _embed_call(texts: Sequence[str]) -> Tuple[List[List[float]], Optional[int]]:
    from .. import llm  # lazy: publicapi must import without the engine stack

    client = llm._client(settings.embed_base_url, read_timeout=EMBED_READ_TIMEOUT_S)
    try:
        response = await client.embeddings.create(
            model=settings.embed_model, input=list(texts), encoding_format="float"
        )
    except Exception as exc:  # noqa: BLE001
        if _status_of(exc) == 400:
            raise EngineRefusedInput(_looks_over_length(str(getattr(exc, "message", "") or exc))) from None
        raise
    rows = sorted(response.data, key=lambda item: item.index)
    if len(rows) != len(texts):
        raise RuntimeError("the embedding engine returned the wrong number of vectors")
    usage = getattr(response, "usage", None)
    tokens = getattr(usage, "prompt_tokens", None) if usage is not None else None
    return [list(row.embedding) for row in rows], (int(tokens) if tokens is not None else None)


async def embed(inputs: Sequence[str], *, single_string: bool = False) -> EmbedOutcome:
    """Embed every input, in order, under the `embed` gate.

    A batch the engine refuses with a 400 is retried one input at a time
    (still under the gate) so the refusal names the input at fault —
    `input.3`, not "one of these sixteen". That costs at most fifteen tiny
    extra calls, and only on a request that was going to fail anyway.
    """
    weights = embed_weights(inputs)
    budget = kv_budget_tokens(EMBED)
    waits = _WaitBudget(sync_wait_s())
    vectors: List[Optional[List[float]]] = [None] * len(inputs)
    total = 0
    reported = True
    calls = 0
    outcome = EmbedOutcome(vectors=[], prompt_tokens=None)

    async def run(indices: List[int]) -> None:
        nonlocal total, reported, calls
        async with waits.hold(EMBED, weight_tokens=sum(weights[i] for i in indices)):
            calls += 1
            got, tokens = await _embed_call([inputs[i] for i in indices])
        for i, vector in zip(indices, got):
            vectors[i] = vector
        if tokens is None:
            reported = False
        else:
            total += tokens
            outcome.partial_prompt_tokens = total

    try:
        for batch in pack(weights, max_items=EMBED_CALL_MAX_INPUTS, budget=budget):
            try:
                await run(batch)
            except EngineRefusedInput as refused:
                if len(batch) == 1:
                    raise _refusal_for(batch[0], refused, single_string, "input", embed_context_tokens())
                for index in batch:
                    try:
                        await run([index])
                    except EngineRefusedInput as alone:
                        raise _refusal_for(index, alone, single_string, "input", embed_context_tokens())
    except errors.ApiError as refusal:
        raise SidecarError(refusal, engine_calls=calls, measured_tokens=outcome.partial_prompt_tokens) from None
    except Exception as exc:  # noqa: BLE001
        raise SidecarError(
            engine_error(exc, EMBED, timeout_s=EMBED_READ_TIMEOUT_S),
            engine_calls=calls,
            measured_tokens=outcome.partial_prompt_tokens,
        ) from None
    outcome.vectors = [vector or [] for vector in vectors]
    outcome.prompt_tokens = total if reported else None
    outcome.engine_calls = calls
    return outcome


def _refusal_for(
    index: int, refused: EngineRefusedInput, single: bool, field_name: str, limit: int
) -> errors.ApiError:
    param = field_name if single else f"{field_name}.{index}"
    if refused.over_length:
        noun = "The input" if single else f"Input {index}"
        return _over_length_error(param, limit, noun)
    return errors.invalid_request("The model could not process this input.", param=param)


def encode_base64(vector: Sequence[float]) -> str:
    """OpenAI's `encoding_format: base64`: little-endian float32, base64.

    Built here from the engine's float list rather than asked of the engine,
    whose byte order is the host's and whose option names have moved between
    vLLM releases — the wire promise is little-endian, so it is made where it
    can be checked."""
    packed = array.array("f", (float(value) for value in vector))
    if sys.byteorder != "little":  # pragma: no cover - every host we run on is little-endian
        packed.byteswap()
    return base64.b64encode(packed.tobytes()).decode("ascii")


# -------------------------------------------------------------- rerank --


def rerank_query_text(query: str, instruction: Optional[str]) -> str:
    """The model-card template's query half, WITHOUT `rerank.format_query`'s
    600-character cut (module docstring). The whitespace collapse is kept:
    it is part of how the chat client feeds the template, so the same query
    scores the same way on both surfaces."""
    from .. import rerank

    collapsed = " ".join((query or "").split())
    return f"{rerank.PREFIX}<Instruct>: {instruction or rerank.DEFAULT_INSTRUCTION}\n<Query>: {collapsed}\n"


def rerank_document_text(text: str) -> str:
    from .. import rerank

    return f"<Document>: {text or ''}{rerank.SUFFIX}"


@dataclass
class RerankOutcome:
    scores: List[float]
    input_tokens: Optional[int]
    engine_calls: int = 0
    partial_input_tokens: int = 0


async def _http_client(timeout_s: float) -> httpx.AsyncClient:
    """One client per public request, built OFF the event loop.

    Constructing an `httpx.AsyncClient` loads the CA bundle into an SSL
    context — blocking file and CPU work (app/rerank.py measured it on the
    answer path, 2026-09-13) — and the event loop is shared with every chat
    stream in this process."""
    timeout = httpx.Timeout(
        connect=float(getattr(settings, "llm_connect_timeout", 10.0) or 10.0),
        read=float(timeout_s),
        write=float(getattr(settings, "llm_write_timeout", 30.0) or 30.0),
        pool=float(getattr(settings, "llm_write_timeout", 30.0) or 30.0),
    )
    transport = _transport
    return await asyncio.to_thread(
        lambda: httpx.AsyncClient(timeout=timeout, transport=transport, follow_redirects=False)
    )


async def _score_call(client: httpx.AsyncClient, query_text: str, documents: Sequence[str]) -> Tuple[List[float], Optional[int]]:
    from .. import rerank

    headers = {}
    if getattr(settings, "rerank_api_key", ""):
        headers["Authorization"] = f"Bearer {settings.rerank_api_key}"
    response = await client.post(
        rerank.score_url(settings.rerank_base_url),
        json={"model": settings.rerank_model, "text_1": query_text, "text_2": list(documents)},
        headers=headers,
    )
    if response.status_code == 400:
        try:
            detail = str(response.json())
        except Exception:  # noqa: BLE001
            detail = ""
        raise EngineRefusedInput(_looks_over_length(detail))
    response.raise_for_status()
    payload = response.json()
    scores = rerank.parse_scores(payload, len(documents))
    usage = payload.get("usage") if isinstance(payload, Mapping) else None
    tokens = usage.get("prompt_tokens") if isinstance(usage, Mapping) else None
    return scores, (int(tokens) if isinstance(tokens, (int, float)) else None)


async def rerank_scores(
    query: str, documents: Sequence[str], *, instruction: Optional[str]
) -> RerankOutcome:
    """Score every (query, document) pair, in document order, under the
    `rerank` gate. Refusals name `documents.<i>` the way `embed` names inputs."""
    query_text = rerank_query_text(query, instruction)
    doc_texts = [rerank_document_text(text) for text in documents]
    window = rerank_context_tokens()
    query_bytes = _utf8_bytes(query_text)
    weights = [min(window, query_bytes + _utf8_bytes(text)) for text in doc_texts]
    budget = kv_budget_tokens(RERANK)
    waits = _WaitBudget(sync_wait_s())
    scores: List[Optional[float]] = [None] * len(documents)
    total = 0
    reported = True
    calls = 0
    outcome = RerankOutcome(scores=[], input_tokens=None)
    client = await _http_client(RERANK_READ_TIMEOUT_S)

    async def run(indices: List[int]) -> None:
        nonlocal total, reported, calls
        async with waits.hold(RERANK, weight_tokens=sum(weights[i] for i in indices)):
            calls += 1
            got, tokens = await _score_call(client, query_text, [doc_texts[i] for i in indices])
        for i, value in zip(indices, got):
            scores[i] = value
        if tokens is None:
            reported = False
        else:
            total += tokens
            outcome.partial_input_tokens = total

    try:
        for batch in pack(weights, max_items=RERANK_CALL_MAX_PAIRS, budget=budget):
            try:
                await run(batch)
            except EngineRefusedInput as refused:
                if len(batch) == 1:
                    raise _refusal_for(batch[0], refused, False, "documents", window)
                for index in batch:
                    try:
                        await run([index])
                    except EngineRefusedInput as alone:
                        raise _refusal_for(index, alone, False, "documents", window)
    except errors.ApiError as refusal:
        raise SidecarError(refusal, engine_calls=calls, measured_tokens=outcome.partial_input_tokens) from None
    except Exception as exc:  # noqa: BLE001
        raise SidecarError(
            engine_error(exc, RERANK, timeout_s=RERANK_READ_TIMEOUT_S),
            engine_calls=calls,
            measured_tokens=outcome.partial_input_tokens,
        ) from None
    finally:
        with contextlib.suppress(Exception):
            await client.aclose()
    outcome.scores = [float(value) for value in scores]  # type: ignore[arg-type]
    outcome.input_tokens = total if reported else None
    outcome.engine_calls = calls
    return outcome


# ------------------------------------------------------- transcription --

#: A filename the engine's multipart parser wants, derived from the CONTENT
#: TYPE — never the caller's filename, which tells the engine nothing and would
#: give caller text somewhere to be logged (the rule of app/audio_api.py).
_EXTENSIONS = {
    "audio/webm": "webm", "video/webm": "webm", "audio/ogg": "ogg", "audio/opus": "opus",
    "audio/mp4": "mp4", "video/mp4": "mp4", "audio/m4a": "m4a", "audio/x-m4a": "m4a",
    "audio/mpeg": "mp3", "audio/mpga": "mp3", "audio/wav": "wav", "audio/x-wav": "wav",
    "audio/wave": "wav", "audio/flac": "flac", "audio/x-flac": "flac", "audio/aac": "aac",
    "audio/3gpp": "3gp",
}

#: Streamed to the engine in slices of this size, so the request body is never
#: assembled as a second whole copy of the audio.
_UPLOAD_SLICE_BYTES = 1024 * 1024


def allowed_audio_types() -> frozenset:
    """The content types the chat application's microphone route accepts
    (`audio_api.ALLOWED_TYPES`). Imported lazily: that module pulls in the
    browser authentication stack, which `/v1` does not otherwise need."""
    try:
        from .. import audio_api

        return frozenset(audio_api.ALLOWED_TYPES)
    except Exception:  # noqa: BLE001
        return frozenset(_EXTENSIONS)


def replica_order() -> List[str]:
    """The speech replicas to try, best first: the LAST healthy one.

    Dictation's `RoutedProvider` sends a clip to the least-active healthy
    replica and breaks ties toward index 0, so on a quiet fleet the first
    replica takes dictation. Public work goes to the other end, so a public
    clip and a person's dictation land on different GPUs whenever there are
    two. Replicas dictation has stood down go last, and are still tried —
    the same "never refuse a request nobody had to lose" rule as dictation's.
    """
    urls = [url for url in getattr(settings, "asr_base_urls", ()) if url and url.strip()]
    available: Dict[str, bool] = {}
    try:
        from .. import asr

        for row in asr.provider().stats():
            available[str(row.get("endpoint") or "")] = bool(row.get("available", True))
    except Exception:  # noqa: BLE001 - health is advisory
        available = {}
    healthy = [url for url in reversed(urls) if available.get(url, True)]
    cooling = [url for url in reversed(urls) if not available.get(url, True)]
    return healthy + cooling


@contextlib.contextmanager
def counted_in_dictation_routing(base_url: str) -> Iterator[None]:
    """Count a public clip on its replica in dictation's least-active routing
    for as long as it is being sent and decoded.

    WHY (adversarial review 2026-09-13). `asr.RoutedProvider` sends each
    dictation to the replica with the fewest clips IT started, and a public
    clip is not one of them: with dictation A on replica 0 and a public clip
    decoding on replica 1, dictation B saw replica 1 as free and waited behind
    the public clip inside whisper's one-clip-at-a-time lock (6.72 s for a
    1 s clip, scaled). Counted here, B goes to replica 0 behind A's short
    dictation instead.

    Uses `RoutedProvider.reserve(index)` when asr.py has it (needs
    integration, the clean seam); until then the provider's own per-replica
    counter, incremented and decremented synchronously (no await between
    them and the read in `_order`, so nothing can observe a torn value).
    Advisory: a provider of another shape is left alone.
    """
    provider = None
    index: Optional[int] = None
    try:
        from .. import asr

        provider = asr.provider()
        for position, row in enumerate(provider.stats()):
            if str(row.get("endpoint") or "").rstrip("/") == str(base_url).rstrip("/"):
                index = position
                break
    except Exception:  # noqa: BLE001 - routing hints must never fail a request
        provider, index = None, None
    reserve = getattr(provider, "reserve", None)
    if index is not None and callable(reserve):
        with reserve(index):
            yield
        return
    active = getattr(provider, "_active", None)
    if index is None or not isinstance(active, dict) or index not in active:
        yield
        return
    active[index] += 1
    try:
        yield
    finally:
        active[index] = max(0, active[index] - 1)


def _multipart_stream(
    boundary: str, fields: Sequence[Tuple[str, str]], audio: bytearray, content_type: str
) -> Tuple[Callable[[], AsyncIterator[bytes]], int]:
    """A multipart body for the engine as an async stream, and its length.

    Written out rather than handed to httpx's `files=`, which wants `bytes`:
    converting the `bytearray` would be the second whole copy of the audio
    this path promises not to make."""
    head = bytearray()
    for name, value in fields:
        head += (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n"
        ).encode("utf-8")
    extension = _EXTENSIONS.get(content_type, "bin")
    head += (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"audio.{extension}\"\r\n"
        f"Content-Type: {content_type or 'application/octet-stream'}\r\n\r\n"
    ).encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
    length = len(head) + len(audio) + len(tail)

    async def stream() -> AsyncIterator[bytes]:
        yield bytes(head)
        view = memoryview(audio)
        try:
            for start in range(0, len(view), _UPLOAD_SLICE_BYTES):
                yield bytes(view[start:start + _UPLOAD_SLICE_BYTES])
        finally:
            view.release()
        yield tail

    return stream, length


@dataclass
class TranscriptionOutcome:
    reply: Dict[str, Any]
    duration_s: Optional[float]
    processing_ms: Optional[int]
    replica_index: int = 0
    probed_seconds: Optional[float] = None


def audio_too_long(limit_s: int) -> errors.ApiError:
    return errors.ApiError(
        "request_too_large",
        f"The audio is longer than the {int(limit_s)} second limit.",
        param="file",
    )


async def probe_seconds(audio: bytearray, *, timeout_s: float = 15.0) -> Optional[float]:
    """The clip's duration from `ffprobe` reading stdin, or None when unknown.

    WHY BEFORE THE ENGINE. The public ceiling (300 s) is half the engine's own
    (600 s), and the engine only measures after decoding — so without this a
    450 s clip would take a GPU for a minute and then be refused. A browser
    WebM often carries no duration and an MP4 with its index at the end cannot
    be probed from a pipe; those return None, and the engine's measured
    duration decides afterwards (`transcribe`).

    Fed from a memoryview in slices: the audio is not copied whole to do it.
    """
    if shutil.which("ffprobe") is None:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", "-i", "pipe:0",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except Exception:  # noqa: BLE001
        return None

    async def feed() -> None:
        assert proc.stdin is not None
        view = memoryview(audio)
        try:
            for start in range(0, len(view), 256 * 1024):
                proc.stdin.write(view[start:start + 256 * 1024])
                await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass  # ffprobe read the header it needed and left
        finally:
            view.release()
            with contextlib.suppress(Exception):
                proc.stdin.close()

    async def collect() -> bytes:
        assert proc.stdout is not None
        out = await proc.stdout.read()
        await proc.wait()
        return out

    # NOT `proc.communicate()`: with no `input` it closes stdin at once, which
    # would cut `feed` off after the first slice and have ffprobe judge the
    # clip on 256 KiB of it.
    try:
        _, out = await asyncio.wait_for(asyncio.gather(feed(), collect()), timeout=timeout_s)
    except Exception:  # noqa: BLE001
        with contextlib.suppress(Exception):
            proc.kill()
        return None
    try:
        value = float((out or b"").decode("ascii", "replace").strip().splitlines()[0])
    except (ValueError, IndexError):
        return None
    return value if value > 0 else None


async def transcribe(
    audio: bytearray, *, content_type: str, language: Optional[str], verbose: bool
) -> TranscriptionOutcome:
    """One clip, one replica, under the fleet-wide `asr` gate.

    The gate is taken with `yield_to_chat`: `capacity.hold` waits while a chat
    generation is in flight and while dictation has anyone waiting or every
    replica busy, then proceeds — whisper decodes one clip per GPU and a
    saturated replica takes chat decode from ~71 to ~24 tok/s (2026-09-08).
    """
    sent = [0]
    try:
        return await _transcribe(audio, content_type=content_type, language=language, verbose=verbose, sent=sent)
    except errors.ApiError as refusal:
        raise SidecarError(refusal, engine_calls=sent[0]) from None
    except Exception as exc:  # noqa: BLE001
        raise SidecarError(engine_error(exc, ASR), engine_calls=sent[0]) from None


async def _transcribe(
    audio: bytearray, *, content_type: str, language: Optional[str], verbose: bool, sent: List[int]
) -> TranscriptionOutcome:
    limit = max_audio_seconds()
    probed = await probe_seconds(audio)
    if probed is not None and probed > limit + 0.5:
        raise audio_too_long(limit)
    timeout_s = float(getattr(settings, "asr_timeout_s", 240.0) or 240.0)
    order = replica_order()
    if not order:
        raise errors.model_unavailable(UNAVAILABLE_RETRY_AFTER_S)
    fields: List[Tuple[str, str]] = [
        ("model", str(getattr(settings, "asr_model", "") or "whisper")),
        ("response_format", "verbose_json" if verbose else "json"),
    ]
    if language:
        fields.append(("language", language))
    client = await _http_client(timeout_s)
    try:
        async with hold(ASR, wait_s=sync_wait_s(), yield_to_chat=True):
            last: Optional[BaseException] = None
            for position, base_url in enumerate(order):
                boundary = secrets.token_hex(16)
                stream, length = _multipart_stream(boundary, fields, audio, content_type)
                sent[0] += 1
                try:
                    with counted_in_dictation_routing(base_url):
                        response = await client.post(
                            f"{base_url.rstrip('/')}/audio/transcriptions",
                            content=stream(),
                            headers={
                                "Content-Type": f"multipart/form-data; boundary={boundary}",
                                "Content-Length": str(length),
                            },
                        )
                except httpx.TimeoutException as exc:
                    # The clip may be decoding: sending it to a second GPU
                    # would double the cost to the chat model, not halve it.
                    raise errors.timeout(timeout_s) from exc
                except httpx.TransportError as exc:
                    last = exc
                    log.warning("public transcription: replica %d unreachable", position)
                    continue
                if response.status_code == 413:
                    raise audio_too_long(limit)
                if response.status_code == 400:
                    raise errors.invalid_request(
                        "The audio could not be decoded. Send a supported audio file.", param="file"
                    )
                if response.status_code >= 500 and position + 1 < len(order):
                    last = RuntimeError(f"engine returned {response.status_code}")
                    continue
                if response.status_code != 200:
                    raise errors.model_unavailable(UNAVAILABLE_RETRY_AFTER_S)
                try:
                    reply = response.json()
                except ValueError:
                    raise errors.model_unavailable(UNAVAILABLE_RETRY_AFTER_S) from None
                if not isinstance(reply, dict):
                    raise errors.model_unavailable(UNAVAILABLE_RETRY_AFTER_S)
                duration = _float_or_none(reply.get("duration"))
                processing = _float_or_none(reply.get("processing_ms"))
                return TranscriptionOutcome(
                    reply=reply,
                    duration_s=duration,
                    processing_ms=int(processing) if processing is not None else None,
                    replica_index=position,
                    probed_seconds=probed,
                )
            raise engine_error(last or RuntimeError("no replica answered"), ASR, timeout_s=timeout_s)
    finally:
        with contextlib.suppress(Exception):
            await client.aclose()


def _float_or_none(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number >= 0 else None
