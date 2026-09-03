"""The ONE cross-encoder client (ADR-0001 D4): templated, bounded, verified.

Qwen3-Reranker is a causal LM turned yes/no judge. It was trained on a
specific prompt — a system line, ``<Instruct>``, ``<Query>``, ``<Document>``
and an empty think block — and the vLLM ``/score`` endpoint applies none of
it: it scores whatever text pairs it is given. Measured on this deployment
(2026-09-03, six controlled passages for one office-holder question):

    passage                       raw /score   templated
    org chart naming the person      0.890       0.9997
    register listing the board       0.345       0.9947
    careers page (no answer)         0.702       0.088
    encyclopaedia "CEO" article      0.269       0.004

Raw scoring ranked a careers page above the board listing; templated scoring
separates answering from non-answering passages by three orders of
magnitude. Every reranked path (local knowledge, search candidate selection,
Salesforce RAG) goes through here so the fix is shared and the scores mean
the same thing everywhere: the probability that the document answers the
query.

Three guards, each from the design critique (2026-09-03):

BOUNDED (D13). The reranker container serves ``concurrency=4``; an unbounded
fan-out under load would queue there and stall every request behind it.
Two slots are RESERVED for the knowledge pipeline's stage 1 (a Fast answer
must not wait behind a Think search's 48-candidate pool); callers wait at
most their deadline for a slot and otherwise get ``RerankUnavailable`` —
their fallback is the hybrid order they already have, never a hang.

VERIFIED. A reranker that is up but wrong (a mis-served model, a broken
template) would be amplified, not degraded, because the answer probability
now decides relevance, sufficiency and supersession. A fixed canary triple
is scored at first use and by every worker cycle; a failure trips a breaker
and ``enabled()`` says no until it passes again. A single call whose scores
are all the same is likewise refused ("degenerate").

EXPLAINED. Every outcome is a metric: ok / busy / error / degenerate /
canary_failed / disabled, plus queue time, latency by batch size and an
in-flight gauge.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import List, Optional, Sequence

import httpx

from . import metrics
from .config import settings

log = logging.getLogger(__name__)

#: Verbatim from the model card (Qwen/Qwen3-Reranker-0.6B, "usage").
PREFIX = (
    "<|im_start|>system\nJudge whether the Document meets the requirements "
    "based on the Query and the Instruct provided. Note that the answer can "
    'only be "yes" or "no".<|im_end|>\n<|im_start|>user\n'
)
SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
#: The instruction the model card uses for web retrieval. A caller may pass
#: its own (e.g. "retrieve CRM records that match the question").
DEFAULT_INSTRUCTION = "Given a web search query, retrieve relevant passages that answer the query"

#: A passage longer than this contributes nothing the model can use inside
#: its 4K window once the template and the query are counted.
MAX_DOC_CHARS = 3000
MAX_QUERY_CHARS = 600

#: Callers name their pool. "local" = the knowledge pipeline's stage 1 (the
#: Fast path); "bulk" = search candidate selection and Salesforce RAG.
LOCAL = "local"
BULK = "bulk"

#: The canary: a query, a passage that answers it, one that does not. Plain
#: general knowledge — nothing about this workspace, nothing that dates.
CANARY_QUERY = "At what temperature does water boil at sea level?"
CANARY_POSITIVE = "At sea level, pure water boils at 100 degrees Celsius (212 degrees Fahrenheit)."
CANARY_NEGATIVE = "The museum opens at nine in the morning and closes at five on weekdays."
CANARY_MIN_POSITIVE = 0.9
CANARY_MAX_NEGATIVE = 0.1
CANARY_MIN_MARGIN = 0.7
#: n ≥ this and every score within this band → the model is not judging.
DEGENERATE_MIN_N = 6
DEGENERATE_BAND = 0.02


class RerankUnavailable(RuntimeError):
    """Scoring did not happen. The caller keeps its own order."""


def format_query(query: str, instruction: Optional[str] = None) -> str:
    q = " ".join((query or "").split())[:MAX_QUERY_CHARS]
    return f"{PREFIX}<Instruct>: {instruction or DEFAULT_INSTRUCTION}\n<Query>: {q}\n"


def format_document(text: str) -> str:
    return f"<Document>: {(text or '')[:MAX_DOC_CHARS]}{SUFFIX}"


def score_url(base_url: str) -> str:
    """The vLLM root ``/score`` endpoint for a base URL that may end in /v1."""
    root = (base_url or "").rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    return f"{root}/score"


# ---------------------------------------------------------------------------
# Breaker + canary
# ---------------------------------------------------------------------------

_breaker_until: float = 0.0
_breaker_reason: str = ""
_canary_checked: bool = False
_canary_lock: Optional[asyncio.Lock] = None


def configured() -> bool:
    caps = settings.reranker_capabilities
    return bool(
        settings.rerank_enabled
        and settings.rerank_base_url
        and caps.enabled
        and caps.supports_reranking
    )


def enabled() -> bool:
    """Configured AND not tripped by the canary."""
    if not configured():
        return False
    return time.monotonic() >= _breaker_until


def breaker_reason() -> str:
    return _breaker_reason if time.monotonic() < _breaker_until else ""


def _trip(reason: str) -> None:
    global _breaker_until, _breaker_reason
    _breaker_until = time.monotonic() + float(settings.rerank_breaker_s)
    _breaker_reason = reason
    metrics.set_gauge("rerank_canary_ok", 0.0)
    metrics.inc("knowledge_degraded_total", reason="rerank_canary")
    log.error("reranker disabled for %.0fs: %s", settings.rerank_breaker_s, reason)


def _reset_breaker() -> None:
    global _breaker_until, _breaker_reason
    _breaker_until = 0.0
    _breaker_reason = ""


async def canary(*, force: bool = False) -> bool:
    """Score the fixed triple; trip or clear the breaker accordingly.

    Called lazily before the first real scoring and from every worker cycle.
    Returns True when the reranker is judging correctly (or the canary is
    disabled). Never raises.
    """
    global _canary_checked, _canary_lock
    if not settings.rerank_canary_enabled or not configured():
        _canary_checked = True
        return configured()
    if _canary_lock is None:
        _canary_lock = asyncio.Lock()
    async with _canary_lock:
        if _canary_checked and not force and enabled():
            return True
        try:
            scores = await _post(
                CANARY_QUERY, [CANARY_POSITIVE, CANARY_NEGATIVE], None, float(settings.rerank_stage_timeout_s) * 3
            )
        except Exception as exc:  # noqa: BLE001
            _canary_checked = True
            # Unreachable is not "wrong": ordinary calls will fail and
            # degrade on their own. Only a WRONG answer trips the breaker.
            log.debug("reranker canary could not run: %s", exc)
            return enabled()
        pos, neg = scores
        margin = pos - neg
        metrics.set_gauge("rerank_canary_margin", margin)
        _canary_checked = True
        if pos >= CANARY_MIN_POSITIVE and neg <= CANARY_MAX_NEGATIVE and margin >= CANARY_MIN_MARGIN:
            if _breaker_until:
                log.info("reranker canary passed again (margin %.3f); breaker cleared", margin)
            _reset_breaker()
            metrics.set_gauge("rerank_canary_ok", 1.0)
            return True
        _trip(f"canary failed: positive={pos:.3f} negative={neg:.3f}")
        return False


# ---------------------------------------------------------------------------
# Slots
# ---------------------------------------------------------------------------

_pools: dict = {}
_pools_loop: Optional[asyncio.AbstractEventLoop] = None
_inflight = 0


def _pool(name: str) -> asyncio.Semaphore:
    """Two semaphores per event loop: `reserved` for stage 1, `shared` for
    everything (bulk callers may only use `shared`)."""
    global _pools, _pools_loop
    loop = asyncio.get_running_loop()
    if _pools_loop is not loop or not _pools:
        total = max(1, int(settings.rerank_max_inflight))
        reserved = max(0, min(int(settings.rerank_reserved_slots), total - 1))
        _pools = {
            "reserved": asyncio.Semaphore(max(reserved, 0) or 0) if reserved else None,
            "shared": asyncio.Semaphore(max(1, total - reserved)),
        }
        _pools_loop = loop
    return _pools[name]


async def _acquire(kind: str, wait: float) -> asyncio.Semaphore:
    """A slot from the caller's pool within `wait` seconds, or busy."""
    reserved = _pool("reserved")
    shared = _pool("shared")
    if kind == LOCAL and reserved is not None and not reserved.locked():
        # Reserved slot free right now: take it without waiting.
        await reserved.acquire()
        return reserved
    queued = time.perf_counter()
    try:
        async with asyncio.timeout(max(0.0, float(wait))):
            await shared.acquire()
    except TimeoutError:
        metrics.inc("rerank_requests_total", outcome="busy", kind=kind)
        raise RerankUnavailable("reranker busy") from None
    metrics.observe("rerank_queue_seconds", time.perf_counter() - queued, kind=kind)
    return shared


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def parse_scores(payload: object, n: int) -> List[float]:
    """``{"data": [{"index", "score"}, ...]}`` → scores in document order.

    Strict: the wrong count, a duplicate or missing index, or a non-finite
    score raises, because associating one document's score with another is
    worse than not scoring at all.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("reranker /score response has no data list")
    data = payload["data"]
    if len(data) != n:
        raise ValueError("reranker /score returned the wrong number of scores")
    out: List[Optional[float]] = [None] * n
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("reranker /score data item is not an object")
        try:
            index = int(item["index"])
            value = float(item["score"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("reranker /score item needs numeric index and score") from exc
        if index < 0 or index >= n or out[index] is not None or not math.isfinite(value):
            raise ValueError("reranker /score returned invalid indices or scores")
        out[index] = value
    if any(v is None for v in out):
        raise ValueError("reranker /score response indices are incomplete")
    return [float(v) for v in out]  # type: ignore[arg-type]


def degenerate(scores: Sequence[float]) -> bool:
    """All the same (within a hair) across a real pool: not a judgement."""
    if len(scores) < DEGENERATE_MIN_N:
        return False
    return (max(scores) - min(scores)) < DEGENERATE_BAND


async def _post(query: str, documents: Sequence[str], instruction: Optional[str], timeout: float) -> List[float]:
    headers = {}
    if settings.rerank_api_key:
        headers["Authorization"] = f"Bearer {settings.rerank_api_key}"
    body = {
        "model": settings.rerank_model,
        "text_1": format_query(query, instruction),
        "text_2": [format_document(d) for d in documents],
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(score_url(settings.rerank_base_url), json=body, headers=headers)
        resp.raise_for_status()
        return parse_scores(resp.json(), len(documents))


async def score(
    query: str,
    documents: Sequence[str],
    *,
    instruction: Optional[str] = None,
    kind: str = BULK,
    wait: Optional[float] = None,
    timeout: Optional[float] = None,
) -> List[float]:
    """Answer-probability of each document for the query, in input order.

    Raises ``RerankUnavailable`` when the reranker is disabled or tripped,
    busy past the wait deadline, unreachable, malformed, or degenerate.
    Never returns a partial list.
    """
    global _inflight
    if not documents:
        return []
    if not configured():
        metrics.inc("rerank_requests_total", outcome="disabled", kind=kind)
        raise RerankUnavailable("reranker disabled")
    if not _canary_checked:
        await canary()
    if not enabled():
        metrics.inc("rerank_requests_total", outcome="canary_failed", kind=kind)
        raise RerankUnavailable(f"reranker tripped: {breaker_reason()}")
    if wait is None:
        wait = float(settings.rerank_wait_s)
    slot = await _acquire(kind, wait)
    started = time.perf_counter()
    _inflight += 1
    metrics.set_gauge("rerank_inflight", float(_inflight))
    try:
        scores = await _post(query, documents, instruction, float(timeout or settings.rerank_timeout))
    except Exception as exc:  # noqa: BLE001 — every failure is one outcome for callers
        metrics.inc("rerank_requests_total", outcome="error", kind=kind)
        log.debug("rerank failed", exc_info=True)
        raise RerankUnavailable(str(exc)) from exc
    finally:
        _inflight -= 1
        metrics.set_gauge("rerank_inflight", float(_inflight))
        slot.release()
        metrics.observe("rerank_seconds", time.perf_counter() - started, kind=kind, n=str(_bucket(len(documents))))
    if degenerate(scores):
        metrics.inc("rerank_requests_total", outcome="degenerate", kind=kind)
        raise RerankUnavailable("reranker returned degenerate scores")
    metrics.inc("rerank_requests_total", outcome="ok", kind=kind)
    return scores


def _bucket(n: int) -> int:
    """Coarse label for the latency histogram: 8, 16, 32, 64…"""
    b = 8
    while b < n:
        b *= 2
    return b


async def order(
    query: str,
    items: Sequence[dict],
    *,
    text_key: str = "text",
    top_n: Optional[int] = None,
    instruction: Optional[str] = None,
    score_key: str = "rerank_score",
    kind: str = BULK,
    wait: Optional[float] = None,
    timeout: Optional[float] = None,
) -> List[dict]:
    """Items best-first by answer probability, each annotated with its score.

    Returns the input order (unscored) when the reranker is unavailable —
    reranking is an upgrade, never a gate.
    """
    if len(items) <= 1:
        return list(items[: top_n or len(items)])
    try:
        scores = await score(
            query,
            [str(i.get(text_key, "")) for i in items],
            instruction=instruction,
            kind=kind,
            wait=wait,
            timeout=timeout,
        )
    except RerankUnavailable:
        return list(items[: top_n or len(items)])
    ranked = sorted(range(len(items)), key=lambda i: scores[i], reverse=True)
    out = []
    for i in ranked[: top_n or len(items)]:
        row = dict(items[i])
        row[score_key] = scores[i]
        out.append(row)
    return out


def reset_for_tests() -> None:
    """Clear breaker, canary and pools (tests create many event loops)."""
    global _canary_checked, _pools, _pools_loop, _inflight
    _reset_breaker()
    _canary_checked = False
    _pools = {}
    _pools_loop = None
    _inflight = 0
