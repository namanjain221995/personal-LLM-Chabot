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
  and a second engine restart implicated in the same input is a 503 with
  `x-should-retry: false` (the input is probably what crashes it) — see
  "AN INPUT THAT CRASHES ITS ENGINE" below;
* an engine that refuses connections (or answers 502/503/504) is waited out
  with backoff while it is continuously unavailable for less than the
  engine-down grace, PUBLIC_API_ENGINE_DOWN_GRACE_S (1,800 s).

AN INPUT THAT CRASHES ITS ENGINE (review 2026-09-14, high). A pooling input
that takes vLLM down breaks its connection at once, long before a 15 s
/metrics scrape can see the new process, and the refusal that follows lands
far past the commit window, where it can only drop the connection — so the
SDK retried and each retry restarted the shared engine twice more. Now:

* a restart is IMPLICATED by evidence gathered per call, across its re-sends:
  the witness (held for the whole call, never released between re-sends)
  saw `process_start_time_seconds` change, or the connection broke with the
  call out and the very next send found the engine refusing connections;
* the first implicated restart under a call of several inputs splits it into
  single-input calls, so the second names the one input at fault;
* that input's sha256 is QUARANTINED for PUBLIC_API_POISON_QUARANTINE_S
  (3,600 s): the failure after the commit still drops the connection, and
  the SDK's retry is refused BEFORE its status line — a real 503 with
  `x-should-retry: false` that both SDKs obey. Two engine restarts per
  input, then none.

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
import hashlib
import logging
import weakref
from collections import OrderedDict
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
    setting_float,
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
#: (the design's 30 s). A failed count is never skipped — it is counted again
#: inside the committed response (`resolve_lengths`), waiting out an
#: unavailable engine like any call.
TOKENIZE_CONNECT_S = 5.0
TOKENIZE_READ_S = 30.0
#: Public `/tokenize` calls in flight at once for ONE engine, across EVERY
#: request in this process (PUBLIC_API_TOKENIZE_CONCURRENCY). WHY PROCESS-WIDE
#: (review 2026-09-14, medium): it was eight per request, through no gate, so
#: M concurrent requests put 8×M calls on the API server the chat
#: application's recall embeds through on a 4 s budget. Chat never passes
#: through this gate: a public burst queues here, not in front of chat.
DEFAULT_TOKENIZE_CONCURRENCY = 8
#: How long the check BEFORE the status line may count (PUBLIC_API_LENGTH_
#: CHECK_BUDGET_S, 8 s, never above the 15 s byte invariant). What is still
#: uncounted then is counted inside the committed response, and the route
#: subtracts what the check spent from its commit window, so the silence
#: before the first byte stays inside PUBLIC_API_SYNC_COMMIT_S whatever
#: /tokenize does.
DEFAULT_LENGTH_CHECK_BUDGET_S = 8.0
MAX_LENGTH_CHECK_BUDGET_S = 15.0
#: Engine counts remembered, by engine, model and the text's sha256 (LRU). A
#: count that finished inside a committed response — whose 400 could only
#: drop the connection — is known before the status line of the SDK's retry,
#: which therefore gets the real 400.
TOKEN_COUNT_CACHE_MAX = 16384

#: The read bound of the private one-call helpers `_embed_call` and
#: `_score_call` for callers that use them WITHOUT `_dispatch` — the Files
#: API's `apifiles/vectors.py` and `publicapi/file_inputs.py`, which have no
#: witness logic and fall back to lexical order on a failure (review
#: 2026-09-14, medium). The public routes pass PUBLIC_API_POOLING_SILENCE_S
#: explicitly, because only there a witness decides what a silence means; a
#: wedged engine must not hold a Files caller's embed slot for ten minutes.
EMBED_READ_TIMEOUT_S = 60.0
RERANK_READ_TIMEOUT_S = 60.0

#: What a silent engine call may be re-sent, by the witness's verdict (design,
#: liveness_guard "Actions for embed/rerank").
UNKNOWN_RESENDS = 3
LOST_RESENDS = 1
#: Engine restarts implicated in ONE input before it fails for good.
RESTARTS_BEFORE_REFUSAL = 2
#: How long an input that took its engine down that often is refused before
#: any gate (PUBLIC_API_POISON_QUARANTINE_S), and how many are remembered.
DEFAULT_POISON_QUARANTINE_S = 3600.0
POISON_QUARANTINE_MAX = 4096
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


# ------------------------------------------------------------ settings --


def kv_budget_tokens(engine: str) -> int:
    """The token budget one public engine call is PACKED to — the same
    PUBLIC_API_<ENGINE>_KV_BUDGET_TOKENS setting `capacity.hold` admits
    against, so a packed call always fits the gate on its own."""
    return max(1, setting_int(f"PUBLIC_API_{engine.upper()}_KV_BUDGET_TOKENS", _DEFAULT_BUDGET[engine]))


def tokenize_concurrency() -> int:
    return max(1, setting_int("PUBLIC_API_TOKENIZE_CONCURRENCY", DEFAULT_TOKENIZE_CONCURRENCY))


def length_check_budget_s() -> float:
    """PUBLIC_API_LENGTH_CHECK_BUDGET_S (8 s), clamped to [0, 15]."""
    value = setting_float("PUBLIC_API_LENGTH_CHECK_BUDGET_S", DEFAULT_LENGTH_CHECK_BUDGET_S)
    return min(MAX_LENGTH_CHECK_BUDGET_S, max(0.0, float(value)))


def poison_quarantine_s() -> float:
    return max(0.0, setting_float("PUBLIC_API_POISON_QUARANTINE_S", DEFAULT_POISON_QUARANTINE_S))


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
    #: Long items the engine has no tokenizer endpoint for (a 4xx from
    #: `/tokenize`): sent at the window weight, and the engine's own 400
    #: names the one that is too long.
    uncountable: List[int] = field(default_factory=list)


class _TokenizeUnavailable(Exception):
    """`/tokenize` did not give a count (unreachable, 5xx, a malformed reply)."""


class _TokenizerAbsent(Exception):
    """`/tokenize` answered a 4xx: this engine will not count, however long
    we wait (an image without the route, a model name it does not serve).
    Waiting out the 1,800 s engine-down grace for it helps nobody (review
    2026-09-14): the input is sent, and the engine's 400 decides."""


#: 4xx answers that are about load, not about the route: counted again later.
_TOKENIZE_TRANSIENT_4XX = frozenset({408, 425, 429})


def _text_key(scope: str, text: str) -> str:
    """sha256 of an engine scope and one text: the key of the count cache
    and of the quarantine. Never logged, never rendered."""
    digest = hashlib.sha256(scope.encode("utf-8", "surrogatepass"))
    digest.update(b"\x00")
    digest.update(text.encode("utf-8", "surrogatepass"))
    return digest.hexdigest()


class _CountCache:
    """The engine's token counts, least recently used evicted first."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._counts: "OrderedDict[str, int]" = OrderedDict()

    def get(self, key: str) -> Optional[int]:
        count = self._counts.get(key)
        if count is not None:
            self._counts.move_to_end(key)
        return count

    def put(self, key: str, count: int) -> None:
        self._counts[key] = int(count)
        self._counts.move_to_end(key)
        while len(self._counts) > self.limit:
            self._counts.popitem(last=False)

    def clear(self) -> None:
        self._counts.clear()


_COUNTS = _CountCache(TOKEN_COUNT_CACHE_MAX)

#: One `/tokenize` gate per engine root, per event loop (a test client runs
#: its own loop; a semaphore must not outlive the loop it was used on).
_TOKENIZE_GATES: Dict[str, Tuple[Any, asyncio.Semaphore]] = {}


def _tokenize_gate(root: str) -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    entry = _TOKENIZE_GATES.get(root)
    if entry is None or entry[0]() is not loop:
        entry = (weakref.ref(loop), asyncio.Semaphore(tokenize_concurrency()))
        _TOKENIZE_GATES[root] = entry
    return entry[1]


class _Quarantine:
    """Inputs (by `_text_key`) that took their engine down
    RESTARTS_BEFORE_REFUSAL times, refused before any gate until their time
    is up. Process-local, bounded, oldest evicted first."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._until: "OrderedDict[str, float]" = OrderedDict()

    def add(self, key: str, now: float, ttl_s: float) -> None:
        if ttl_s <= 0:
            return
        self._until[key] = now + ttl_s
        self._until.move_to_end(key)
        while len(self._until) > self.limit:
            self._until.popitem(last=False)

    def active(self, now: float) -> bool:
        for key in [key for key, until in self._until.items() if until <= now]:
            del self._until[key]
        return bool(self._until)

    def holds(self, key: str, now: float) -> bool:
        until = self._until.get(key)
        return until is not None and until > now

    def clear(self) -> None:
        self._until.clear()


_QUARANTINE = _Quarantine(POISON_QUARANTINE_MAX)


def reset_for_tests() -> None:
    """Forget counts, quarantined inputs, tokenize gates and the memory
    budget's holders (every suite's fixtures call this)."""
    _COUNTS.clear()
    _QUARANTINE.clear()
    _TOKENIZE_GATES.clear()
    POOLING_MEMORY.reset()


def _crashed_engine_error(param: Optional[str]) -> errors.ApiError:
    """An input that took its engine down twice: retryable by a person
    after the quarantine, never by a client loop."""
    error = errors.ApiError(
        "model_unavailable",
        "The model stopped while processing this input; it is not retried automatically.",
        param=param,
        retry_after=UNAVAILABLE_RETRY_AFTER_S,
    )
    error.should_retry = False
    return error


def _quarantine_scope(engine: str) -> str:
    model = settings.embed_model if engine == EMBED else settings.rerank_model
    return f"quarantine:{engine}:{model or ''}"


async def _keys(scope: str, texts: Sequence[str]) -> List[str]:
    """`_text_key` of every text, off the event loop when there is a lot of
    text to hash (8 MiB of sha256 is tens of milliseconds of CPU)."""
    if sum(len(text) for text in texts) > 256 * 1024:
        return await asyncio.to_thread(lambda: [_text_key(scope, text) for text in texts])
    return [_text_key(scope, text) for text in texts]


async def _refuse_quarantined(engine: str, texts: Sequence[str], *, field_name: str, single: bool) -> None:
    now = _clock()
    if not _QUARANTINE.active(now):
        return
    for index, key in enumerate(await _keys(_quarantine_scope(engine), texts)):
        if _QUARANTINE.holds(key, now):
            log.warning("public %s refused a quarantined input before its gate", engine)
            raise _crashed_engine_error(_param_for(field_name, index, single))


def _quarantine(engine: str, text: str) -> None:
    _QUARANTINE.add(_text_key(_quarantine_scope(engine), text), _clock(), poison_quarantine_s())


# ------------------------------------------------------ the memory budget --


#: What one embedding holds from its engine reply until its response is sent:
#: 1,024 doubles in an `array('d')` (8 B each), the rendered number (at most
#: 24 B of JSON) and the join of the rendered parts (as much again). base64:
#: 4 float32 bytes as 5⅓ characters, twice.
EMBEDDING_FLOAT_BYTES = 8 + 2 * 24
EMBEDDING_BASE64_BYTES = 8 + 2 * 6
#: A rerank document's score, index and result object.
RERANK_RESULT_BYTES = 512
DEFAULT_POOLING_MEMORY_BYTES = 512 * 1024 * 1024
MEMORY_RETRY_AFTER_S = 5


def pooling_memory_bytes() -> int:
    """PUBLIC_API_POOLING_MEMORY_BYTES (512 MiB)."""
    return max(1, setting_int("PUBLIC_API_POOLING_MEMORY_BYTES", DEFAULT_POOLING_MEMORY_BYTES))


def embed_memory_bytes(body_bytes: int, inputs: int, *, base64_wanted: bool = False) -> int:
    """The bytes one embeddings request holds while it is accepted but not
    finished: its body and the parsed strings (twice the body), and every
    vector until the response is rendered and sent."""
    from .endpoint_models import EMBEDDING_DIMENSIONS

    per_vector = EMBEDDING_DIMENSIONS * (EMBEDDING_BASE64_BYTES if base64_wanted else EMBEDDING_FLOAT_BYTES)
    return 2 * max(0, int(body_bytes)) + max(0, int(inputs)) * per_vector


def rerank_memory_bytes(body_bytes: int, documents: int) -> int:
    """A rerank request: the body, the parsed documents and the templated
    pairs the length check builds (three times the body), and the results."""
    return 3 * max(0, int(body_bytes)) + max(0, int(documents)) * RERANK_RESULT_BYTES


class PoolingMemory:
    """A process-wide byte budget of accepted-but-unfinished `/v1/embeddings`
    and `/v1/rerank` work — a PHYSICAL guard like the fd guard in main.py.

    WHY (review 2026-09-14, medium). The routes lost their clock: the gate
    waits with no limit, the body cap is 8 MiB and a request may carry 2,048
    inputs. A waiting request holds its parsed body, and a running one its
    finished vectors, until it answers — measured at ~150 MiB for one
    maximum-size request with Python float lists — and nothing refused the
    51st. Now a request is charged its estimate BEFORE its body is read, and
    again, exactly, once it is parsed; one that does not fit is a real
    `503 model_unavailable` with Retry-After before the status line. A single
    request larger than the whole budget is admitted when it is alone, so a
    small setting slows the route rather than closing it.

    Single event loop: no lock; every change is synchronous."""

    def __init__(self) -> None:
        self.used = 0
        self.holders = 0
        self.refused = 0
        #: Bumped by `reset`, so a reservation from before it cannot release
        #: into the new count.
        self.epoch = 0

    def _fits(self, extra: int, *, others: int) -> bool:
        return others == 0 or self.used + extra <= pooling_memory_bytes()

    def reserve(self, nbytes: int) -> "MemoryReservation":
        nbytes = max(0, int(nbytes))
        if not self._fits(nbytes, others=self.holders):
            self.refused += 1
            raise errors.model_unavailable(MEMORY_RETRY_AFTER_S)
        self.used += nbytes
        self.holders += 1
        return MemoryReservation(self, nbytes)

    def reset(self) -> None:
        self.used = 0
        self.holders = 0
        self.refused = 0
        self.epoch += 1


class MemoryReservation:
    """One request's share of `PoolingMemory`. `release` is idempotent, and
    `bind` releases it when the response object is collected, so a response
    that is never sent cannot keep the budget spent."""

    def __init__(self, budget: PoolingMemory, nbytes: int) -> None:
        self.budget = budget
        self.nbytes = nbytes
        self.released = False
        self.epoch = budget.epoch

    def resize(self, nbytes: int) -> None:
        nbytes = max(0, int(nbytes))
        if self.released or self.epoch != self.budget.epoch:
            return
        grow = nbytes - self.nbytes
        if grow > 0 and not self.budget._fits(grow, others=self.budget.holders - 1):
            self.budget.refused += 1
            raise errors.model_unavailable(MEMORY_RETRY_AFTER_S)
        self.budget.used += grow
        self.nbytes = nbytes

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        if self.epoch != self.budget.epoch:
            return
        self.budget.used = max(0, self.budget.used - self.nbytes)
        self.budget.holders = max(0, self.budget.holders - 1)

    def bind(self, owner: Any) -> None:
        weakref.finalize(owner, self.release)


POOLING_MEMORY = PoolingMemory()


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
    if 400 <= response.status_code < 500 and response.status_code not in _TOKENIZE_TRANSIENT_4XX:
        raise _TokenizerAbsent(f"status {response.status_code}")
    if response.status_code != 200:
        raise _TokenizeUnavailable(f"status {response.status_code}")
    try:
        count = response.json().get("count")
    except (ValueError, AttributeError):
        raise _TokenizeUnavailable("malformed reply") from None
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise _TokenizeUnavailable("malformed reply")
    return count


def _count_scope(root: str, model: str) -> str:
    return f"count:{root}:{model}"


@dataclass
class _Counted:
    counts: Dict[int, int] = field(default_factory=dict)
    #: Not counted: the engine was unreachable, or the budget ran out first.
    missing: List[int] = field(default_factory=list)
    #: The engine has no tokenizer route (`_TokenizerAbsent`).
    absent: List[int] = field(default_factory=list)


async def _count_long_items(
    texts: Sequence[str],
    indices: Sequence[int],
    *,
    root: str,
    model: str,
    budget_s: Optional[float] = None,
) -> _Counted:
    """The engine's counts for `indices`: remembered ones at once, the rest
    through the engine's process-wide `/tokenize` gate. `budget_s` bounds the
    whole call (the check before a status line); what is not counted by then
    is `missing`, never skipped."""
    result = _Counted()
    if not indices:
        return result
    scope = _count_scope(root, model)
    keys: Dict[int, str] = {}
    todo: List[int] = []
    for index in indices:
        key = _text_key(scope, texts[index])
        cached = _COUNTS.get(key)
        if cached is None:
            keys[index] = key
            todo.append(index)
        else:
            result.counts[index] = cached
    if not todo:
        return result
    gate = _tokenize_gate(root)
    client = await _http_client(TOKENIZE_READ_S, connect_s=TOKENIZE_CONNECT_S)

    async def one(index: int) -> None:
        async with gate:
            try:
                count = await _tokenize_count(client, root, model, texts[index])
            except _TokenizerAbsent:
                result.absent.append(index)
                return
            except _TokenizeUnavailable:
                result.missing.append(index)
                return
        result.counts[index] = count
        _COUNTS.put(keys[index], count)

    tasks = [asyncio.ensure_future(one(index)) for index in todo]
    try:
        if budget_s is None:
            await asyncio.gather(*tasks)
        else:
            # `asyncio.wait`, not `wait_for`: nothing is cancelled by the
            # budget except the counts still outstanding, and those are
            # handed on as `missing`.
            await asyncio.wait(tasks, timeout=max(0.0, float(budget_s)))
    finally:
        pending = [task for task in tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending)
        with contextlib.suppress(Exception):
            await client.aclose()
    settled = set(result.counts) | set(result.absent) | set(result.missing)
    result.missing.extend(index for index in todo if index not in settled)
    result.missing.sort()
    result.absent.sort()
    return result


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
    engine: str,
    window: int,
    root: str,
    model: str,
    field_name: str,
    single: bool,
    budget_s: Optional[float],
) -> LengthCheck:
    await _refuse_quarantined(engine, texts, field_name=field_name, single=single)
    weights = [max(1, min(window, int(bound))) for bound in bounds]
    long = [index for index, bound in enumerate(bounds) if int(bound) > window]
    counted = await _count_long_items(texts, long, root=root, model=model, budget_s=budget_s)
    over = sorted(index for index, count in counted.counts.items() if count > window)
    if over:
        first = over[0]
        raise _over_length_error(_param_for(field_name, first, single), window, _noun_for(field_name, first, single))
    for index, count in counted.counts.items():
        weights[index] = max(1, min(window, count))
    return LengthCheck(
        weights=weights, unresolved=counted.missing, counted=len(counted.counts), uncountable=counted.absent
    )


def embed_bounds(inputs: Sequence[str]) -> List[int]:
    """Each input's token upper bound: UTF-8 bytes + 2."""
    return [_utf8_bytes(text) + 2 for text in inputs]


async def check_embed_lengths(
    inputs: Sequence[str], *, single_string: bool = False, budget_s: Any = BOUNDED_DEFAULT
) -> LengthCheck:
    """The pre-gate length check of an embeddings request (module docstring).
    Raises the 400 `context_length_exceeded` naming `input.<i>`, or the 503
    `x-should-retry: false` for a quarantined input. Counts for at most
    `budget_s` (default PUBLIC_API_LENGTH_CHECK_BUDGET_S; None: no bound)."""
    return await _check_lengths(
        inputs,
        embed_bounds(inputs),
        engine=EMBED,
        budget_s=length_check_budget_s() if budget_s is BOUNDED_DEFAULT else budget_s,
        window=embed_context_tokens(),
        root=_root_of(str(getattr(settings, "embed_base_url", "") or "")),
        model=str(getattr(settings, "embed_model", "") or ""),
        field_name="input",
        single=single_string,
    )


def _rerank_pairs(query_text: str, doc_texts: Sequence[str]) -> List[str]:
    return [query_text + text for text in doc_texts]


async def check_rerank_lengths(
    query_text: str, doc_texts: Sequence[str], *, budget_s: Any = BOUNDED_DEFAULT
) -> LengthCheck:
    """The pre-gate length check of a rerank request: each TEMPLATED pair.
    Raises the 400 naming `documents.<i>`; `budget_s` as in
    `check_embed_lengths`."""
    pairs = _rerank_pairs(query_text, doc_texts)
    return await _check_lengths(
        pairs,
        [_utf8_bytes(pair) for pair in pairs],
        engine=RERANK,
        budget_s=length_check_budget_s() if budget_s is BOUNDED_DEFAULT else budget_s,
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
        counted = await _count_long_items(texts, pending, root=root, model=model)
        over = sorted(index for index, count in counted.counts.items() if count > window)
        if over:
            first = over[0]
            raise _over_length_error(_param_for(field_name, first, single), window, _noun_for(field_name, first, single))
        for index, count in counted.counts.items():
            check.weights[index] = max(1, min(window, count))
        check.counted += len(counted.counts)
        check.uncountable.extend(counted.absent)
        missing = counted.missing
        if missing and (counted.counts or counted.absent):
            patience.reachable()
        pending = missing
        if pending:
            await patience.wait()
    check.unresolved = []


# ------------------------------------------------------ the engine call --


class _Watch:
    """The /metrics witness of ONE engine call, for the whole call — every
    re-send included — acquired only if the call is still unanswered after
    WITNESS_START_S.

    WHY ACROSS RE-SENDS (review 2026-09-14, high). It was acquired and
    released per send, and `WitnessSampler.release` drops a key's witness
    when its last reference goes, so a lone request's restart history was
    thrown away between its re-sends and two restarts were never seen."""

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

    def clock(self) -> float:
        if self.witness is not None:
            with contextlib.suppress(Exception):
                return float(self.witness.clock())
        return time.monotonic()


@dataclass
class _Calls:
    """Engine requests SENT for one public request (the ledger's
    `engine_calls`), and the re-sends by reason (the usage row's meta)."""

    sent: int = 0
    resends: Dict[str, int] = field(default_factory=dict)

    def resent(self, reason: str) -> None:
        self.resends[reason] = self.resends.get(reason, 0) + 1

    def unsent(self, reason: str) -> None:
        left = self.resends.get(reason, 0) - 1
        if left > 0:
            self.resends[reason] = left
        else:
            self.resends.pop(reason, None)


class _RestartUnderSeveral(Exception):
    """The first implicated engine restart under a call of several inputs:
    the caller re-sends them one at a time, so a second restart names the
    one input at fault instead of quarantining its neighbours."""


class _EngineCrashedOnCall(Exception):
    """RESTARTS_BEFORE_REFUSAL implicated restarts under one call."""


async def _attempt(
    engine: str, call: Callable[[], Awaitable[T]], *, weight: int, calls: _Calls, wait_s: Optional[float],
    clock: Callable[[], float],
) -> Tuple[Optional[T], Optional[BaseException], float]:
    """(result, failure, when the send went out on the witness clock) of ONE
    send under the gate."""
    async with hold(engine, weight_tokens=weight, wait_s=wait_s):
        started = clock()
        calls.sent += 1
        try:
            return await call(), None, started
        except (EngineRefusedInput, errors.ApiError):
            raise
        except Exception as exc:  # noqa: BLE001 - classified by the caller
            failure: BaseException = exc
    if liveness.sidecar_error_kind(failure) == "connect":
        calls.sent -= 1  # the request never reached the engine
    return None, failure, started


async def _dispatch(
    engine: str,
    root: str,
    call: Callable[[], Awaitable[T]],
    *,
    weight: int,
    calls: _Calls,
    patience: _Unavailable,
    wait_s: Optional[float] = None,
    items: int = 1,
    restarts: int = 0,
) -> T:
    """One engine call under the gate, re-sent as the evidence says (module
    docstring). The gate is released between sends: a call waiting for an
    engine to come back holds no capacity anyone else could use meanwhile.

    `items` is how many inputs the call carries and `restarts` how many
    engine restarts were already implicated in them. A restart is implicated
    when the witness saw the process start again since the send went out, or
    when the send's connection BROKE and the next send found the engine
    refusing connections (it went down right after this call). The first
    under several items raises `_RestartUnderSeveral`; the
    RESTARTS_BEFORE_REFUSAL-th raises `_EngineCrashedOnCall`."""
    unknown = lost = engine_errors = 0
    broke = False  # the previous send broke with the call outstanding

    def implicated() -> None:
        nonlocal restarts
        restarts += 1
        calls.resent("restarted")
        if restarts >= RESTARTS_BEFORE_REFUSAL:
            log.warning("public %s call was implicated in %d engine restarts; refusing it", engine, restarts)
            raise _EngineCrashedOnCall()
        if items > 1:
            raise _RestartUnderSeveral()

    with _Watch(engine, root) as watch:
        while True:
            result, failure, started = await _attempt(
                engine, call, weight=weight, calls=calls, wait_s=wait_s, clock=watch.clock
            )
            if failure is None:
                patience.reachable()
                return result  # type: ignore[return-value]
            kind = liveness.sidecar_error_kind(failure)
            if kind == "connect":
                if broke:
                    # The engine went down right after the call that broke:
                    # that break was a restart, not a lost connection.
                    broke = False
                    lost -= 1
                    calls.unsent("lost")
                    implicated()
                log.info("public %s engine unreachable (%s); waiting for it", engine, type(failure).__name__)
                await patience.wait()
                continue
            broke = False
            patience.reachable()
            if kind == "status":
                engine_errors += 1
                calls.resent("engine_error")
                if engine_errors > ENGINE_ERROR_RESENDS:
                    raise engine_error(failure, engine)
                continue
            # The silence bound, or a connection that broke with the call out.
            verdict = watch.verdict(started)
            if verdict == liveness.REASON_RESTARTED or watch.restarts_since(started) > 0:
                implicated()
                continue
            if kind == "broken":
                # The call is not outstanding any more, so what the engine is
                # doing for OTHER callers ("progressing", "stalled") says
                # nothing about it. Re-sent once; the next send tells a
                # restart (connections refused) from a dropped connection.
                lost += 1
                calls.resent("lost")
                if lost > LOST_RESENDS:
                    raise errors.model_unavailable(UNAVAILABLE_RETRY_AFTER_S)
                broke = True
                continue
            if verdict == liveness.REASON_STALLED:
                log.warning("public %s engine is stalled with this call outstanding", engine)
                raise errors.model_unavailable(UNAVAILABLE_RETRY_AFTER_S)
            if verdict == "progressing":
                calls.resent("progressing")
                continue
            if verdict == liveness.REASON_LOST:
                lost += 1
                calls.resent("lost")
                if lost > LOST_RESENDS:
                    raise errors.model_unavailable(UNAVAILABLE_RETRY_AFTER_S)
                continue
            unknown += 1
            calls.resent("unknown")
            if unknown > UNKNOWN_RESENDS:
                raise errors.model_unavailable(UNAVAILABLE_RETRY_AFTER_S)


async def _run_batches(
    engine: str,
    batches: Sequence[List[int]],
    run: Callable[..., Awaitable[None]],
    *,
    text_of: Callable[[int], str],
    field_name: str,
    single: bool,
    window: int,
    count_scope: str,
) -> None:
    """Every packed call in order, with the two ways a call is re-sent one
    input at a time: the engine refused the batch with a 400 (the refusal
    then names the input), or an engine restart was implicated in it (a
    second one then names — and quarantines — the input that crashes it).

    An input the ENGINE refused as over-length is remembered as over the
    window in the count cache, so the retry of a request whose refusal came
    after its commit — an engine without `/tokenize` — is a real 400 before
    its status line instead of the same dropped connection again."""

    def refusal(index: int, refused: EngineRefusedInput) -> errors.ApiError:
        if refused.over_length:
            _COUNTS.put(_text_key(count_scope, text_of(index)), window + 1)
        return _refusal_for(index, refused, single, field_name, window)

    async def alone(index: int, *, restarts: int) -> None:
        try:
            await run([index], restarts=restarts)
        except EngineRefusedInput as refused:
            raise refusal(index, refused) from None
        except _EngineCrashedOnCall:
            _quarantine(engine, text_of(index))
            raise _crashed_engine_error(_param_for(field_name, index, single)) from None

    for batch in batches:
        try:
            await run(batch)
        except EngineRefusedInput as refused:
            if len(batch) == 1:
                raise refusal(batch[0], refused) from None
            for index in batch:
                await alone(index, restarts=0)
        except _RestartUnderSeveral:
            log.info("public %s restart under a call of %d inputs; sending them one at a time", engine, len(batch))
            for index in batch:
                await alone(index, restarts=1)
        except _EngineCrashedOnCall:
            # Only a call of ONE input gets here (several split first).
            _quarantine(engine, text_of(batch[0]))
            raise _crashed_engine_error(_param_for(field_name, batch[0], single)) from None


# ---------------------------------------------------------- embeddings --


@dataclass
class EmbedOutcome:
    #: One `array('d')` per input: the engine's doubles exactly (so the JSON
    #: rendering is byte-identical to a float list's) at 8 bytes a number
    #: instead of a Python float object's 32 (review 2026-09-14, memory).
    vectors: List[Sequence[float]]
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


async def _embed_call(
    texts: Sequence[str], *, read_timeout_s: Optional[float] = None
) -> Tuple[List[List[float]], Optional[int]]:
    """ONE engine call, ONE send. Its read bound is a setting or a constant
    — never a per-request value, because `llm._client`'s cache key carries
    the timeout (AUDIT F048): the public route passes the silence bound
    PUBLIC_API_POOLING_SILENCE_S; every other caller gets EMBED_READ_TIMEOUT_S.

    `max_retries=0` whatever LLM_MAX_RETRIES says: an SDK retry inside the
    call would turn "the connection broke, then the engine refused" — the
    evidence `_dispatch` reads as an implicated restart — into one opaque
    connection error."""
    from .. import llm  # lazy: publicapi must import without the engine stack

    read = EMBED_READ_TIMEOUT_S if read_timeout_s is None else float(read_timeout_s)
    client = llm._client(settings.embed_base_url, read_timeout=read)
    try:
        response = await client.with_options(max_retries=0).embeddings.create(
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
    vectors: List[Optional[Sequence[float]]] = [None] * len(inputs)
    total = 0
    reported = True
    outcome = EmbedOutcome(vectors=[], prompt_tokens=None)

    silence = pooling_silence_s()
    weights: List[int] = []

    async def run(indices: List[int], *, restarts: int = 0) -> None:
        nonlocal total, reported
        got, tokens = await _dispatch(
            EMBED,
            root,
            lambda: _embed_call([inputs[i] for i in indices], read_timeout_s=silence),
            weight=sum(weights[i] for i in indices),
            calls=calls,
            patience=patience,
            wait_s=wait,
            items=len(indices),
            restarts=restarts,
        )
        for i, vector in zip(indices, got):
            vectors[i] = array.array("d", vector)
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
        weights[:] = check.weights
        await _run_batches(
            EMBED,
            pack(weights, max_items=EMBED_CALL_MAX_INPUTS, budget=kv_budget_tokens(EMBED)),
            run,
            text_of=lambda index: inputs[index],
            field_name="input",
            single=single_string,
            window=embed_context_tokens(),
            count_scope=_count_scope(root, str(getattr(settings, "embed_model", "") or "")),
        )
    except errors.ApiError as refusal:
        raise SidecarError(refusal, engine_calls=calls.sent, measured_tokens=outcome.partial_prompt_tokens) from None
    except Exception as exc:  # noqa: BLE001
        raise SidecarError(
            engine_error(exc, EMBED), engine_calls=calls.sent, measured_tokens=outcome.partial_prompt_tokens
        ) from None
    outcome.vectors = [array.array("d") if vector is None else vector for vector in vectors]
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
    weights: List[int] = []

    async def run(indices: List[int], *, restarts: int = 0) -> None:
        nonlocal total, reported
        got, tokens = await _dispatch(
            RERANK,
            root,
            lambda: _score_call(client, query_text, [doc_texts[i] for i in indices]),
            weight=sum(weights[i] for i in indices),
            calls=calls,
            patience=patience,
            wait_s=wait,
            items=len(indices),
            restarts=restarts,
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
        weights[:] = check.weights
        await _run_batches(
            RERANK,
            pack(weights, max_items=RERANK_CALL_MAX_PAIRS, budget=kv_budget_tokens(RERANK)),
            run,
            text_of=lambda index: query_text + doc_texts[index],
            field_name="documents",
            single=False,
            window=window,
            count_scope=_count_scope(root, str(getattr(settings, "rerank_model", "") or "")),
        )
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
