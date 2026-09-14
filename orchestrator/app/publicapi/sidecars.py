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
capacity rule of the 2026-09-13 design).

NO CLOCK ENDS A REQUEST HERE (no-timeout design, sidecars_and_audio,
2026-09-14). Until this change a request waited PUBLIC_API_GATE_WAIT_S (30 s)
in total for its gate and then answered 503, and one engine call was cut at a
60 s read timeout (504). Both were clocks on a healthy engine. Now:

* the gate waits with NO LIMIT (`capacity.hold(wait_s=None)`); a client that
  leaves cancels the wait (the committed response cancels its work);
* lengths are checked BEFORE any gate (`check_embed_lengths`,
  `check_rerank_lengths`): an input whose UTF-8 bytes + 2 fit the window
  cannot overflow (a byte-level BPE spends at most one token per byte), and a
  longer one is counted by the engine's own `/tokenize` — API-server CPU, no
  KV, no gate — so an over-length input is a real 400 before anything waits;
* one engine call may be silent for PUBLIC_API_POOLING_SILENCE_S (600 s); what
  happens then is decided by the engine's /metrics witness
  (`liveness.SidecarWitness`), never by the clock: `progressing` re-sends,
  `unknown` re-sends up to 3 times, `lost` once, `stalled` is a retryable 503,
  and a second engine restart during the same call is a 503 with
  `x-should-retry: false` (the input is probably what crashes it);
* an engine that refuses connections (or answers 502/503/504) is waited out
  with backoff while it is continuously unavailable for less than the
  engine-down grace, PUBLIC_API_ENGINE_DOWN_GRACE_S (1,800 s).

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
import shutil
import sys
import time
from dataclasses import dataclass, field
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Dict,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    TypeVar,
)

import httpx

from ..config import settings
from . import capacity, errors, liveness
from .endpoint_models import (
    embed_context_tokens,
    pooling_silence_s,
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
#: rather than queued behind one 2,048-input batch.
EMBED_CALL_MAX_INPUTS = 16
RERANK_CALL_MAX_PAIRS = 16

#: The token budget each engine's public calls may hold at once, and why.
#: Engine log 2026-09-11: 18,720 KV tokens on each of embed and rerank (4.57
#: windows of 4,096). The chat application's query embeddings and reranks sit
#: on the TTFT path; public use is capped at 8,192 (44%) so they keep >= 10,528.
_DEFAULT_BUDGET = {EMBED: 8192, RERANK: 8192}
#: An engine that is down: the chat application's own sidecar recovery window
#: is of this order, and a tighter retry loop only adds load to a restart.
UNAVAILABLE_RETRY_AFTER_S = 30

#: `/tokenize` for the length check: connect and read bounds of ONE attempt
#: (the design's 30 s), and how many long inputs are counted at once. A failed
#: count is never skipped — it is counted again inside the committed response
#: (`resolve_lengths`), waiting out an unavailable engine like any call.
TOKENIZE_CONNECT_S = 5.0
TOKENIZE_READ_S = 30.0
TOKENIZE_CONCURRENCY = 8

#: What a silent engine call may be re-sent, by the witness's verdict (design,
#: liveness_guard "Actions for embed/rerank").
UNKNOWN_RESENDS = 3
LOST_RESENDS = 1
#: Engine restarts coinciding with ONE call before it fails for good.
RESTARTS_BEFORE_REFUSAL = 2
#: A 5xx other than 502/503/504: the engine answered with an error. Once more,
#: then a retryable 503 (it is deterministic work; a loop helps nobody).
ENGINE_ERROR_RESENDS = 1
#: A call outstanding this long gets the engine's /metrics witness. A pooling
#: call is well under a second, so a healthy request never scrapes /metrics;
#: by the 600 s silence bound the witness has forty samples.
WITNESS_START_S = 15.0
#: Backoff while the engine refuses connections.
CONNECT_BACKOFF_MIN_S = 2.0
CONNECT_BACKOFF_MAX_S = 60.0

#: Tests install an `httpx.MockTransport` here; production leaves it None.
_transport: Optional[httpx.AsyncBaseTransport] = None

#: Seams for the tests: the sleep between re-dispatches, the clock, and the
#: witness sampler (one reference-counted /metrics scrape loop per engine).
_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
_clock: Callable[[], float] = time.monotonic


def witness_sampler() -> liveness.WitnessSampler:
    return liveness.sampler()


class _BoundedDefault:
    """The `wait_s` of a caller that named none: the retired synchronous
    budget, `capacity.sync_wait_s()` (PUBLIC_API_GATE_WAIT_S).

    WHY A BOUNDED DEFAULT SURVIVES (2026-09-14). The three public routes pass
    `wait_s=None` and have no clock. One library caller still runs BEFORE its
    status line with a budget of its own: the synchronous file preparation's
    reranker (`apifiles/retrieval.make_engine_reranker`, an uncommitted path
    owned by the Files API), which falls back to lexical order when the
    reranker is busy. Waiting forever there would hold a request silent past
    every edge, so a caller that says nothing keeps the old bound — and a
    refused connection fails at once for it instead of being waited out."""

    def __repr__(self) -> str:
        return "BOUNDED_DEFAULT"


BOUNDED_DEFAULT: Any = _BoundedDefault()


def _resolve_wait(wait_s: Any) -> Optional[float]:
    if wait_s is BOUNDED_DEFAULT:
        return float(capacity.sync_wait_s())
    return None if wait_s is None else max(0.0, float(wait_s))


T = TypeVar("T")


def __getattr__(name: str) -> Any:
    """The retired read-timeout constants, for readers outside this file
    (`file_inputs._patient_reranker` builds its client from one): each is now
    the silence bound PUBLIC_API_POOLING_SILENCE_S, read at call time."""
    if name in ("EMBED_READ_TIMEOUT_S", "RERANK_READ_TIMEOUT_S"):
        return pooling_silence_s()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ------------------------------------------------------------ settings --


def kv_budget_tokens(engine: str) -> int:
    """The token budget one public engine call is PACKED to — the same
    PUBLIC_API_<ENGINE>_KV_BUDGET_TOKENS setting `capacity.hold` admits
    against, so a packed call always fits the gate on its own."""
    return max(1, setting_int(f"PUBLIC_API_{engine.upper()}_KV_BUDGET_TOKENS", _DEFAULT_BUDGET[engine]))


def _root_of(base_url: str) -> str:
    """A vLLM server root (where /tokenize, /score and /metrics live) for a
    base URL that may end in /v1."""
    base = (base_url or "").rstrip("/")
    return base[: -len("/v1")] if base.endswith("/v1") else base


# ------------------------------------------------------------ the gate --


@contextlib.asynccontextmanager
async def hold(
    engine: str, *, weight_tokens: int = 0, wait_s: Optional[float] = None, yield_to_chat: bool = False
) -> AsyncIterator[None]:
    """`capacity.hold` — one unit of the engine's PUBLIC capacity for one
    engine call. `wait_s=None` (every caller here): no limit. A seam of one
    line, so the tests can see which gate each call takes."""
    async with capacity.hold(
        engine,
        weight_tokens=int(weight_tokens),
        wait_s=None if wait_s is None else float(wait_s),
        yield_to_chat=yield_to_chat,
    ):
        yield


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


class SidecarError(Exception):
    """A public engine call that did not produce an answer, with what the
    ledger needs to settle it honestly.

    `engine_calls` is how many engine requests were SENT (0: nothing ran — a
    refusal before the first call — which the route settles as `cancelled`;
    more: the engine did work, which is `failed`). `measured_tokens` is what
    the calls that completed reported, so a request that failed half way is
    charged for the half that ran and not for its estimate.
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


def engine_error(exc: BaseException, engine: str) -> errors.ApiError:
    """Anything an engine call raised → the §9 error a caller can act on.

    The exception's own text is NEVER used: an httpx error names the URL it
    failed on, which is an internal host and port.
    """
    if isinstance(exc, errors.ApiError):
        return exc
    log.warning("public %s engine call failed: %s", engine, type(exc).__name__, exc_info=True)
    return errors.model_unavailable(UNAVAILABLE_RETRY_AFTER_S)


def _refused_for_good() -> errors.ApiError:
    """Two engine restarts while this input was being processed: most likely
    the input is what takes the engine down, and an SDK retry would do it
    again. Retryable by a person, never by a client loop."""
    error = errors.model_unavailable(UNAVAILABLE_RETRY_AFTER_S)
    error.should_retry = False
    return error


def _over_length_error(param: str, limit: int, noun: str) -> errors.ApiError:
    return errors.ApiError(
        "context_length_exceeded",
        f"{noun} is longer than the model's {int(limit)} token limit.",
        param=param,
    )


# ------------------------------------------------------- the length check --


@dataclass
class LengthCheck:
    """What the pre-gate length check learned.

    `weights[i]` is item i's gate weight: its exact token count when the
    engine counted it, else its byte bound, capped at the window. `unresolved`
    are the long items `/tokenize` could not count yet (the engine was not
    reachable); they are counted again, patiently, before any engine call —
    never skipped."""

    weights: List[int]
    unresolved: List[int] = field(default_factory=list)
    counted: int = 0


class _TokenizeUnavailable(Exception):
    """`/tokenize` did not give a count (unreachable, 5xx, a malformed reply)."""


async def _http_client(timeout_s: float, *, connect_s: Optional[float] = None) -> httpx.AsyncClient:
    """One client per public request, built OFF the event loop.

    Constructing an `httpx.AsyncClient` loads the CA bundle into an SSL
    context — blocking file and CPU work (app/rerank.py measured it on the
    answer path, 2026-09-13) — and the event loop is shared with every chat
    stream in this process."""
    connect = float(connect_s if connect_s is not None else (getattr(settings, "llm_connect_timeout", 10.0) or 10.0))
    timeout = httpx.Timeout(
        connect=connect,
        read=float(timeout_s),
        write=float(getattr(settings, "llm_write_timeout", 30.0) or 30.0),
        pool=float(getattr(settings, "llm_write_timeout", 30.0) or 30.0),
    )
    transport = _transport
    return await asyncio.to_thread(
        lambda: httpx.AsyncClient(timeout=timeout, transport=transport, follow_redirects=False)
    )


async def _tokenize_count(client: httpx.AsyncClient, root: str, model: str, text: str) -> int:
    """The engine's own token count of `text` (vLLM `POST /tokenize`)."""
    try:
        response = await client.post(f"{root}/tokenize", json={"model": model, "prompt": text})
    except httpx.HTTPError as exc:
        raise _TokenizeUnavailable(type(exc).__name__) from None
    if response.status_code != 200:
        raise _TokenizeUnavailable(f"status {response.status_code}")
    try:
        count = response.json().get("count")
    except (ValueError, AttributeError):
        raise _TokenizeUnavailable("malformed reply") from None
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise _TokenizeUnavailable("malformed reply")
    return count


async def _count_long_items(
    texts: Sequence[str], indices: Sequence[int], *, root: str, model: str
) -> Tuple[Dict[int, int], List[int]]:
    """({index: count}, [indices not counted]) for `indices`, in parallel."""
    counts: Dict[int, int] = {}
    missing: List[int] = []
    if not indices:
        return counts, missing
    client = await _http_client(TOKENIZE_READ_S, connect_s=TOKENIZE_CONNECT_S)
    gate = asyncio.Semaphore(TOKENIZE_CONCURRENCY)

    async def one(index: int) -> None:
        async with gate:
            try:
                counts[index] = await _tokenize_count(client, root, model, texts[index])
            except _TokenizeUnavailable:
                missing.append(index)

    try:
        await asyncio.gather(*(one(index) for index in indices))
    finally:
        with contextlib.suppress(Exception):
            await client.aclose()
    return counts, sorted(missing)


def _param_for(field_name: str, index: int, single: bool) -> str:
    return field_name if single else f"{field_name}.{index}"


def _noun_for(field_name: str, index: int, single: bool) -> str:
    if field_name == "documents":
        return f"Document {index} with the query"
    return "The input" if single else f"Input {index}"


async def _check_lengths(
    texts: Sequence[str],
    bounds: Sequence[int],
    *,
    window: int,
    root: str,
    model: str,
    field_name: str,
    single: bool,
) -> LengthCheck:
    weights = [max(1, min(window, int(bound))) for bound in bounds]
    long = [index for index, bound in enumerate(bounds) if int(bound) > window]
    counts, missing = await _count_long_items(texts, long, root=root, model=model)
    over = sorted(index for index, count in counts.items() if count > window)
    if over:
        first = over[0]
        raise _over_length_error(_param_for(field_name, first, single), window, _noun_for(field_name, first, single))
    for index, count in counts.items():
        weights[index] = max(1, min(window, count))
    return LengthCheck(weights=weights, unresolved=missing, counted=len(counts))


def embed_bounds(inputs: Sequence[str]) -> List[int]:
    """Each input's token upper bound: UTF-8 bytes + 2."""
    return [_utf8_bytes(text) + 2 for text in inputs]


async def check_embed_lengths(inputs: Sequence[str], *, single_string: bool = False) -> LengthCheck:
    """The pre-gate length check of an embeddings request (module docstring).
    Raises the 400 `context_length_exceeded` naming `input.<i>`."""
    return await _check_lengths(
        inputs,
        embed_bounds(inputs),
        window=embed_context_tokens(),
        root=_root_of(str(getattr(settings, "embed_base_url", "") or "")),
        model=str(getattr(settings, "embed_model", "") or ""),
        field_name="input",
        single=single_string,
    )


def _rerank_pairs(query_text: str, doc_texts: Sequence[str]) -> List[str]:
    return [query_text + text for text in doc_texts]


async def check_rerank_lengths(query_text: str, doc_texts: Sequence[str]) -> LengthCheck:
    """The pre-gate length check of a rerank request: each TEMPLATED pair.
    Raises the 400 naming `documents.<i>`."""
    pairs = _rerank_pairs(query_text, doc_texts)
    return await _check_lengths(
        pairs,
        [_utf8_bytes(pair) for pair in pairs],
        window=rerank_context_tokens(),
        root=_root_of(str(getattr(settings, "rerank_base_url", "") or "")),
        model=str(getattr(settings, "rerank_model", "") or ""),
        field_name="documents",
        single=False,
    )


class _Unavailable:
    """One request's patience with an engine that refuses connections: backoff
    2 → 60 s, and a 503 only after the engine-down grace of CONTINUOUS
    unavailability. Any answer from the engine resets it."""

    def __init__(self, *, patient: bool = True) -> None:
        self.since: Optional[float] = None
        self.backoff = CONNECT_BACKOFF_MIN_S
        self.waits = 0
        self.patient = patient

    def reachable(self) -> None:
        self.since = None
        self.backoff = CONNECT_BACKOFF_MIN_S

    async def wait(self) -> None:
        if not self.patient:
            raise errors.model_unavailable(UNAVAILABLE_RETRY_AFTER_S)
        now = _clock()
        if self.since is None:
            self.since = now
        grace = liveness.engine_down_grace_s()
        spent = now - self.since
        if spent >= grace:
            raise errors.model_unavailable(UNAVAILABLE_RETRY_AFTER_S)
        self.waits += 1
        await _sleep(max(0.0, min(self.backoff, grace - spent)))
        self.backoff = min(CONNECT_BACKOFF_MAX_S, self.backoff * 2)


async def resolve_lengths(
    check: LengthCheck,
    texts: Sequence[str],
    *,
    window: int,
    root: str,
    model: str,
    field_name: str,
    single: bool,
    patient: bool = True,
) -> None:
    """Count the items the pre-gate check could not, waiting out an
    unavailable engine (a bounded caller, `patient=False`, is refused at
    once instead); raise the 400 for one that is too long."""
    pending = list(check.unresolved)
    patience = _Unavailable(patient=patient)
    while pending:
        counts, missing = await _count_long_items(texts, pending, root=root, model=model)
        over = sorted(index for index, count in counts.items() if count > window)
        if over:
            first = over[0]
            raise _over_length_error(_param_for(field_name, first, single), window, _noun_for(field_name, first, single))
        for index, count in counts.items():
            check.weights[index] = max(1, min(window, count))
        check.counted += len(counts)
        if missing and counts:
            patience.reachable()
        pending = missing
        if pending:
            await patience.wait()
    check.unresolved = []


# ------------------------------------------------------ the engine call --


class _Watch:
    """The /metrics witness of one outstanding engine call, acquired only if
    the call is still outstanding after WITNESS_START_S."""

    def __init__(self, engine: str, root: str) -> None:
        self.engine = engine
        self.root = root
        self.witness: Optional[liveness.SidecarWitness] = None
        self._task: Optional[asyncio.Task] = None

    def _acquire(self) -> None:
        if not self.root or self.witness is not None:
            return
        try:
            self.witness = witness_sampler().acquire(f"public-{self.engine}", self.root)
        except Exception:  # noqa: BLE001 - no witness is "unknown", never a failure
            log.debug("no /metrics witness for public %s", self.engine, exc_info=True)

    async def _start(self) -> None:
        await asyncio.sleep(WITNESS_START_S)
        self._acquire()

    def __enter__(self) -> "_Watch":
        if WITNESS_START_S <= 0:
            self._acquire()
        else:
            self._task = asyncio.ensure_future(self._start())
        return self

    def __exit__(self, *_exc: Any) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        if self.witness is not None:
            with contextlib.suppress(Exception):
                witness_sampler().release(f"public-{self.engine}")

    def verdict(self, outstanding_since: float) -> str:
        if self.witness is None:
            return "unknown"
        try:
            return self.witness.verdict(outstanding_since, self.witness.clock())
        except Exception:  # noqa: BLE001 - a witness never fails a call
            return "unknown"

    def restarts_since(self, moment: float) -> int:
        if self.witness is None:
            return 0
        try:
            return int(self.witness.restarts_since(moment))
        except Exception:  # noqa: BLE001
            return 0


@dataclass
class _Calls:
    """Engine requests SENT for one public request (the ledger's
    `engine_calls`), and the re-sends by reason (the usage row's meta)."""

    sent: int = 0
    resends: Dict[str, int] = field(default_factory=dict)

    def resent(self, reason: str) -> None:
        self.resends[reason] = self.resends.get(reason, 0) + 1


async def _attempt(
    engine: str, root: str, call: Callable[[], Awaitable[T]], *, weight: int, calls: _Calls, wait_s: Optional[float]
) -> Tuple[Optional[T], Optional[BaseException], str, int]:
    """(result, failure, witness verdict, restarts seen) of ONE send under the
    gate. The verdict is read only for a failure that is not a refused
    connection, over the whole time this call was outstanding."""
    with _Watch(engine, root) as watch:
        async with hold(engine, weight_tokens=weight, wait_s=wait_s):
            started = time.monotonic()
            calls.sent += 1
            try:
                return await call(), None, "", 0
            except (EngineRefusedInput, errors.ApiError):
                raise
            except Exception as exc:  # noqa: BLE001 - classified by the caller
                failure: BaseException = exc
        if liveness.sidecar_error_kind(failure) == "connect":
            calls.sent -= 1  # the request never reached the engine
            return None, failure, "", 0
        return None, failure, watch.verdict(started), watch.restarts_since(started)


async def _dispatch(
    engine: str,
    root: str,
    call: Callable[[], Awaitable[T]],
    *,
    weight: int,
    calls: _Calls,
    patience: _Unavailable,
    wait_s: Optional[float] = None,
) -> T:
    """One engine call under the gate, re-sent as the evidence says (module
    docstring). The gate is released between attempts: a call waiting for an
    engine to come back holds no capacity anyone else could use meanwhile."""
    unknown = lost = engine_errors = restarts = 0
    while True:
        result, failure, verdict, restarted = await _attempt(
            engine, root, call, weight=weight, calls=calls, wait_s=wait_s
        )
        if failure is None:
            patience.reachable()
            return result  # type: ignore[return-value]
        kind = liveness.sidecar_error_kind(failure)
        if kind == "connect":
            log.info("public %s engine unreachable (%s); waiting for it", engine, type(failure).__name__)
            await patience.wait()
            continue
        patience.reachable()
        if kind == "status":
            engine_errors += 1
            calls.resent("engine_error")
            if engine_errors > ENGINE_ERROR_RESENDS:
                raise engine_error(failure, engine)
            continue
        # The silence bound, or a connection that broke with the call out.
        if verdict == liveness.REASON_RESTARTED:
            restarts += 1  # per call cut by a restart, however many the witness saw
            calls.resent("restarted")
            if restarts >= RESTARTS_BEFORE_REFUSAL:
                log.warning("public %s call outlived %d engine restarts; refusing it", engine, restarts)
                raise _refused_for_good()
            continue
        if verdict == liveness.REASON_STALLED:
            log.warning("public %s engine is stalled with this call outstanding", engine)
            raise errors.model_unavailable(UNAVAILABLE_RETRY_AFTER_S)
        if verdict == "progressing":
            calls.resent("progressing")
            continue
        if verdict == liveness.REASON_LOST or kind == "broken":
            lost += 1
            calls.resent("lost")
            if lost > LOST_RESENDS:
                raise errors.model_unavailable(UNAVAILABLE_RETRY_AFTER_S)
            continue
        unknown += 1
        calls.resent("unknown")
        if unknown > UNKNOWN_RESENDS:
            raise errors.model_unavailable(UNAVAILABLE_RETRY_AFTER_S)


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
    #: Re-sends by reason (witness verdicts), for the usage row.
    resends: Dict[str, int] = field(default_factory=dict)


def embed_weights(inputs: Sequence[str]) -> List[int]:
    """The gate weight of each input before any count: its UTF-8 byte length
    + 2 (a byte-level BPE spends at most a token per byte), capped at the
    engine window."""
    window = embed_context_tokens()
    return [min(window, bound) for bound in embed_bounds(inputs)]


async def _embed_call(texts: Sequence[str]) -> Tuple[List[List[float]], Optional[int]]:
    """ONE engine call. Its read bound is the silence bound
    PUBLIC_API_POOLING_SILENCE_S, a setting — never a per-request value,
    because `llm._client`'s cache key carries the timeout (AUDIT F048)."""
    from .. import llm  # lazy: publicapi must import without the engine stack

    client = llm._client(settings.embed_base_url, read_timeout=pooling_silence_s())
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


async def embed(
    inputs: Sequence[str],
    *,
    single_string: bool = False,
    lengths: Optional[LengthCheck] = None,
    wait_s: Any = BOUNDED_DEFAULT,
) -> EmbedOutcome:
    """Embed every input, in order, under the `embed` gate, with no clock.

    `lengths` is the route's pre-gate `check_embed_lengths` (so an
    over-length input was a 400 before the response committed); without it
    the check runs here. Items it could not count are counted first.

    A batch the engine still refuses with a 400 is retried one input at a time
    (still under the gate) so the refusal names the input at fault —
    `input.3`, not "one of these sixteen".

    `wait_s=None` (the route): no clock. The default is `BOUNDED_DEFAULT`.
    """
    wait = _resolve_wait(wait_s)
    calls = _Calls()
    patience = _Unavailable(patient=wait is None)
    root = _root_of(str(getattr(settings, "embed_base_url", "") or ""))
    vectors: List[Optional[List[float]]] = [None] * len(inputs)
    total = 0
    reported = True
    outcome = EmbedOutcome(vectors=[], prompt_tokens=None)

    async def run(indices: List[int], weights: List[int]) -> None:
        nonlocal total, reported
        got, tokens = await _dispatch(
            EMBED,
            root,
            lambda: _embed_call([inputs[i] for i in indices]),
            weight=sum(weights[i] for i in indices),
            calls=calls,
            patience=patience,
            wait_s=wait,
        )
        for i, vector in zip(indices, got):
            vectors[i] = vector
        if tokens is None:
            reported = False
        else:
            total += tokens
            outcome.partial_prompt_tokens = total

    try:
        check = lengths if lengths is not None else await check_embed_lengths(inputs, single_string=single_string)
        if check.unresolved:
            await resolve_lengths(
                check, inputs, window=embed_context_tokens(), root=root,
                model=str(getattr(settings, "embed_model", "") or ""), field_name="input", single=single_string,
                patient=wait is None,
            )
        weights = check.weights
        for batch in pack(weights, max_items=EMBED_CALL_MAX_INPUTS, budget=kv_budget_tokens(EMBED)):
            try:
                await run(batch, weights)
            except EngineRefusedInput as refused:
                if len(batch) == 1:
                    raise _refusal_for(batch[0], refused, single_string, "input", embed_context_tokens())
                for index in batch:
                    try:
                        await run([index], weights)
                    except EngineRefusedInput as alone:
                        raise _refusal_for(index, alone, single_string, "input", embed_context_tokens())
    except errors.ApiError as refusal:
        raise SidecarError(refusal, engine_calls=calls.sent, measured_tokens=outcome.partial_prompt_tokens) from None
    except Exception as exc:  # noqa: BLE001
        raise SidecarError(
            engine_error(exc, EMBED), engine_calls=calls.sent, measured_tokens=outcome.partial_prompt_tokens
        ) from None
    outcome.vectors = [vector or [] for vector in vectors]
    outcome.prompt_tokens = total if reported else None
    outcome.engine_calls = calls.sent
    outcome.resends = dict(calls.resends)
    return outcome


def _refusal_for(
    index: int, refused: EngineRefusedInput, single: bool, field_name: str, limit: int
) -> errors.ApiError:
    param = field_name if single else f"{field_name}.{index}"
    if refused.over_length:
        return _over_length_error(param, limit, _noun_for(field_name, index, single))
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
    resends: Dict[str, int] = field(default_factory=dict)


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
    query: str,
    documents: Sequence[str],
    *,
    instruction: Optional[str],
    lengths: Optional[LengthCheck] = None,
    wait_s: Any = BOUNDED_DEFAULT,
) -> RerankOutcome:
    """Score every (query, document) pair, in document order, under the
    `rerank` gate. Refusals name `documents.<i>` the way `embed` names
    inputs; `lengths` and `wait_s` as in `embed` (the route passes None)."""
    wait = _resolve_wait(wait_s)
    query_text = rerank_query_text(query, instruction)
    doc_texts = [rerank_document_text(text) for text in documents]
    window = rerank_context_tokens()
    root = _root_of(str(getattr(settings, "rerank_base_url", "") or ""))
    scores: List[Optional[float]] = [None] * len(documents)
    total = 0
    reported = True
    calls = _Calls()
    patience = _Unavailable(patient=wait is None)
    outcome = RerankOutcome(scores=[], input_tokens=None)
    client = await _http_client(pooling_silence_s())

    async def run(indices: List[int], weights: List[int]) -> None:
        nonlocal total, reported
        got, tokens = await _dispatch(
            RERANK,
            root,
            lambda: _score_call(client, query_text, [doc_texts[i] for i in indices]),
            weight=sum(weights[i] for i in indices),
            calls=calls,
            patience=patience,
            wait_s=wait,
        )
        for i, value in zip(indices, got):
            scores[i] = value
        if tokens is None:
            reported = False
        else:
            total += tokens
            outcome.partial_input_tokens = total

    try:
        check = lengths if lengths is not None else await check_rerank_lengths(query_text, doc_texts)
        if check.unresolved:
            await resolve_lengths(
                check, _rerank_pairs(query_text, doc_texts), window=window, root=root,
                model=str(getattr(settings, "rerank_model", "") or ""), field_name="documents", single=False,
                patient=wait is None,
            )
        weights = check.weights
        for batch in pack(weights, max_items=RERANK_CALL_MAX_PAIRS, budget=kv_budget_tokens(RERANK)):
            try:
                await run(batch, weights)
            except EngineRefusedInput as refused:
                if len(batch) == 1:
                    raise _refusal_for(batch[0], refused, False, "documents", window)
                for index in batch:
                    try:
                        await run([index], weights)
                    except EngineRefusedInput as alone:
                        raise _refusal_for(index, alone, False, "documents", window)
    except errors.ApiError as refusal:
        raise SidecarError(refusal, engine_calls=calls.sent, measured_tokens=outcome.partial_input_tokens) from None
    except Exception as exc:  # noqa: BLE001
        raise SidecarError(
            engine_error(exc, RERANK), engine_calls=calls.sent, measured_tokens=outcome.partial_input_tokens
        ) from None
    finally:
        with contextlib.suppress(Exception):
            await client.aclose()
    outcome.scores = [float(value) for value in scores]  # type: ignore[arg-type]
    outcome.input_tokens = total if reported else None
    outcome.engine_calls = calls.sent
    outcome.resends = dict(calls.resends)
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


@dataclass
class TranscriptionOutcome:
    reply: Dict[str, Any]
    duration_s: Optional[float]
    processing_ms: Optional[int]
    replica_index: int = 0
    probed_seconds: Optional[float] = None


async def probe_seconds(audio: bytearray, *, timeout_s: float = 15.0) -> Optional[float]:
    """The clip's duration from `ffprobe` reading stdin, or None when unknown.

    Used by the inline `input_audio` path (`file_inputs.patient_transcriber`)
    to refuse a clip over ITS ceiling before a GPU decodes it. The bound is
    on a local helper process reading a buffer we already hold, not on an
    engine: a probe that does not answer is "duration unknown", never a
    failure. A browser WebM often carries no duration and an MP4 with its
    index at the end cannot be probed from a pipe; those return None.

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
    # clip on 256 KiB of it. `asyncio.timeout`, not `wait_for` (Python 3.11
    # cancellation, 2026-09-14).
    try:
        async with asyncio.timeout(timeout_s):
            _, out = await asyncio.gather(feed(), collect())
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
    """One short clip, sent whole, for a caller that holds the bytes in
    memory (the inline `input_audio` of a generation). `/v1/audio/transcriptions`
    does NOT come here: it runs `audio_jobs` (windows of any length).

    No clock (2026-09-14): the fleet-wide `asr` gate is waited on patiently
    and the reply is watched through the replica's /health with fail-over and
    PUBLIC_API_ASR_WINDOW_SILENCE_S of silence on a READY replica — the same
    `audio_jobs.WhisperDispatcher` every public window uses. It replaced a
    PUBLIC_API_GATE_WAIT_S gate wait and a 240 s total read timeout.
    `verbose` is accepted for the callers' shape; the engine is always asked
    for `verbose_json`, which carries the measured duration."""
    from . import audio_jobs

    dispatcher = audio_jobs.WhisperDispatcher(
        content_type=content_type or "application/octet-stream",
        extension=_EXTENSIONS.get(content_type, "bin"),
        no_speech_check=None,
    )

    async def queued(_data: Dict[str, Any]) -> None:
        return None

    try:
        reply = await dispatcher.transcribe(
            bytes(audio),
            language=language,
            index=0,
            on_wait=queued,
        )
    except audio_jobs.EngineFailure as failure:
        raise SidecarError(failure.error, engine_calls=int(dispatcher.stats.get("engine_calls", 0))) from None
    except errors.ApiError as refusal:
        raise SidecarError(refusal, engine_calls=int(dispatcher.stats.get("engine_calls", 0))) from None
    except Exception as exc:  # noqa: BLE001
        raise SidecarError(engine_error(exc, ASR), engine_calls=int(dispatcher.stats.get("engine_calls", 0))) from None
    finally:
        await dispatcher.aclose()
    duration = _float_or_none(reply.get("duration"))
    processing = _float_or_none(reply.get("processing_ms"))
    return TranscriptionOutcome(
        reply=reply,
        duration_s=duration,
        processing_ms=int(processing) if processing is not None else None,
    )


def _float_or_none(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number >= 0 else None
