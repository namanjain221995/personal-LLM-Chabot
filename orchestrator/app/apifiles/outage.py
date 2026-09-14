"""Is a failed engine call an OUTAGE (retry, never fail the file) or evidence
about this blob's work (spend one of its attempts)? (2026-09-14)

WHY THIS FILE EXISTS. Until 2026-09-14 the `index` stage turned only
`ApiError`, `ConnectionError`, `OSError` and `RuntimeError` into a deferral.
The embed call raises the openai SDK's own errors — `APIConnectionError`,
`APITimeoutError`, `InternalServerError` — whose MRO is `APIError →
OpenAIError → Exception`, so an embedding engine that was simply DOWN reached
the runner's generic handler and the file failed `internal_error` for good
(verifier, 2026-09-14: `status: error` 2.1 s after upload with the engine
stopped). And even a correct deferral spent one of five attempts per outage:
a 25-minute embed outage failed every file indexed during it.

THE RULE. A file is never failed because an engine is down.

* The request never reached the model — the connection was refused, the
  engine (or its proxy) answered 502/503/504 or 429, or the public capacity
  gate refused — the engine is PROVEN unavailable: defer without spending an
  attempt.
* The call timed out, connecting or waiting for a response: the engine's
  state is UNKNOWN (busy, wedged, unreachable, or gone): defer without
  spending an attempt.
* Anything else the engine did (another 5xx, a connection that broke with the
  call outstanding) is ambiguous, so the engine is PROBED with a one-input
  call. A probe that fails means the engine is down or unknown now: defer
  without spending an attempt. A probe that succeeds PROVES the engine is
  serving while this blob's call failed: that is about the blob's work, and it
  spends an attempt.
* Two exceptions spend an attempt, each with its reason:
  - the connection broke with this blob's call outstanding, the engine is gone
    right after, and this run had not written a single vector: the engine
    died UNDER this blob's batch. That is the only evidence an input crashes
    the engine, and the embed engine is shared with chat — an uncounted retry
    would crash it again every few minutes, forever. A real outage produces
    this at most once (the next run's calls are refused, not broken), and a
    run that made progress first is never counted;
  - the engine refused the input itself (HTTP 400): a verdict on this batch,
    not an outage.
* A run that wrote vectors before failing is never counted, whatever the
  failure: it made progress, so it is not a loop.

Counted deferrals keep the design §4.2 schedule (300 s apart, the fifth fails
`processing_unavailable`). Uncounted ones back off exponentially —
`OUTAGE_RETRY_BASE_S` doubling per consecutive outage deferral, capped at
PUBLIC_API_FILES_PROCESSING_RETRY_DELAY_S — so a short blip costs seconds and
a long outage costs one claim per blob per five minutes.

Exceptions that are not engine-shaped (a `TypeError`, a `KeyError`) are OUR
bug and are never classified here: retrying them for hours would hide them.
"""
from __future__ import annotations

from typing import Iterable, List, Optional

#: The first uncounted retry is due this many seconds after the outage.
OUTAGE_RETRY_BASE_S = 15.0

KIND_CONNECT = "connect"      # never reached the model: proven unavailable
KIND_TIMEOUT = "timeout"      # no answer in time: unknown
KIND_STATUS = "status"        # the engine answered this call with a 5xx
KIND_BROKEN = "broken"        # the connection broke with the call outstanding
KIND_REFUSED = "refused"      # the engine refused the input (400)
KIND_NOT_ENGINE = "not_engine"  # not an engine failure at all: our bug

#: A transport error that happened BEFORE the request reached the engine.
_CONNECT_NAMES = frozenset({"ConnectError", "ConnectTimeout", "PoolTimeout", "ConnectionRefusedError"})
_TIMEOUT_NAMES = frozenset({"ReadTimeout", "WriteTimeout", "TimeoutError", "APITimeoutError", "TimeoutException"})
_BROKEN_NAMES = frozenset({
    "RemoteProtocolError", "ReadError", "WriteError", "NetworkError", "ProtocolError",
    "ConnectionResetError", "BrokenPipeError", "ConnectionAbortedError", "IncompleteRead",
})
_UNAVAILABLE_STATUSES = frozenset({429, 502, 503, 504})
#: Class names anywhere in an exception's MRO that make it an engine failure
#: (openai's `APIError`, httpx/httpx2's `HTTPError`, the gate's `ApiError`,
#: sidecars' `EngineRefusedInput`) — matched by name so neither httpx package
#: has to be importable here.
_ENGINE_ROOTS = frozenset({"APIError", "HTTPError", "ApiError", "EngineRefusedInput"})


def _chain(exc: Optional[BaseException]) -> List[BaseException]:
    chain: List[BaseException] = []
    seen: set = set()
    current = exc
    while current is not None and id(current) not in seen and len(chain) < 8:
        seen.add(id(current))
        chain.append(current)
        last = getattr(current, "last", None)  # resilience.ModelUnavailable keeps the engine error here
        current = last if isinstance(last, BaseException) else (current.__cause__ or current.__context__)
    return chain


def _mro_names(items: Iterable[BaseException]) -> set:
    return {cls.__name__ for item in items for cls in type(item).__mro__}


def _status(exc: BaseException) -> Optional[int]:
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if status is None:
        status = getattr(exc, "status", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def classify(exc: BaseException) -> str:
    """One of the KIND_* values for an exception an engine call raised."""
    chain = _chain(exc)
    names = _mro_names(chain)
    type_names = {type(item).__name__ for item in chain}
    if not (names & _ENGINE_ROOTS) and not any(isinstance(item, (ConnectionError, TimeoutError)) for item in chain):
        return KIND_NOT_ENGINE
    if "EngineRefusedInput" in names:
        return KIND_REFUSED
    for item in chain:
        # The gate's `ApiError` carries `status` 503 (at capacity): unavailable.
        status = _status(item)
        if status is None:
            continue
        if status in _UNAVAILABLE_STATUSES:
            return KIND_CONNECT
        if status >= 500:
            return KIND_STATUS
        if status in (400, 413, 422):
            return KIND_REFUSED
    # APITimeoutError subclasses APIConnectionError: test the timeout first.
    if type_names & _TIMEOUT_NAMES:
        return KIND_TIMEOUT
    if type_names & _CONNECT_NAMES or any(isinstance(item, ConnectionRefusedError) for item in chain):
        return KIND_CONNECT
    # openai wraps both a refused connect and a mid-response break in
    # `APIConnectionError`; the httpx cause says which one it was.
    if type_names & _BROKEN_NAMES or any(isinstance(item, ConnectionError) for item in chain):
        return KIND_BROKEN
    if "APIConnectionError" in type_names:
        return KIND_CONNECT
    if any(isinstance(item, TimeoutError) for item in chain):
        return KIND_TIMEOUT
    return KIND_BROKEN


PROBE_SERVING = "serving"
PROBE_DOWN = "down"
PROBE_UNKNOWN = "unknown"


def probe_verdict(exc: Optional[BaseException]) -> str:
    """A probe call's outcome: it answered (`serving`), it could not connect
    (`down`), or anything else (`unknown`)."""
    if exc is None:
        return PROBE_SERVING
    return PROBE_DOWN if classify(exc) == KIND_CONNECT else PROBE_UNKNOWN


def needs_probe(kind: str) -> bool:
    """Only the ambiguous kinds are worth a probe call."""
    return kind in (KIND_STATUS, KIND_BROKEN)


def counts_attempt(kind: str, *, probe: Optional[str], progressed: bool) -> bool:
    """Whether this failure spends one of the blob's attempts (module docstring).

    `probe`: None when no probe ran, else a PROBE_* verdict.
    `progressed`: this run wrote engine output before failing."""
    if progressed:
        return False
    if kind == KIND_REFUSED:
        return True
    if kind in (KIND_CONNECT, KIND_TIMEOUT):
        return False
    if probe == PROBE_SERVING:
        return True
    return kind == KIND_BROKEN and probe == PROBE_DOWN


def backoff_s(consecutive: int, cap_s: float) -> float:
    """Seconds before the next uncounted retry: base · 2^n, capped."""
    cap = max(0.0, float(cap_s))
    n = max(0, int(consecutive))
    return min(cap, OUTAGE_RETRY_BASE_S * (2.0 ** min(n, 20)))
