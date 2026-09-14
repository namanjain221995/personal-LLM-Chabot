"""Stay inside a deployment's ADVERTISED request budget — unless the target
claims to have no limits, in which case every sign of one is a finding.

WHY (2026-09-13): the owner's decision is no usage limits (CONTRACT-3 §12.1),
but a deployment started with PUBLIC_API_ENFORCE_LIMITS=true — the isolated
e2e stack is one, at 60 requests a minute — still answers 429 once the minute
is spent, and a conformance run that trips it reports a rate limit instead of
the behaviour it meant to test. The server says how much budget is left on
every authenticated answer (`RateLimit: "requests";r=<remaining>;t=<reset>`),
so the suite reads it and waits before sending the request that would be
refused. With the limits off the header is absent and this is a no-op.

The wait happens in a REQUEST hook, before the request is sent. It is NOT
invisible to a timer started before `client.responses.create(...)`: the first
long-stream test did exactly that and counted a 22 s pacing sleep as stream
silence (adversarial review, 2026-09-13, paced mock). Stream silence is now
measured inside the transport from the moment the response headers arrive
(`sse.WireClock`), so neither this sleep nor a limit wait below enters it.

LIMITS OFF (2026-09-13, review finding): when the target's own schema says
limits are off (`limits_off` built, or forced), `expect_limits_off()` turns
BOTH the pacing and the limit-429 waits off, and every RateLimit header or
limit-coded 429 seen anyway is counted. The plugin turns a non-zero count into
a FAIL row and a non-zero exit status: waiting quietly would ship a deployment
that breaks §12.1 with a green run.
"""
from __future__ import annotations

import json as _json
import re
import threading
import time
from typing import Any, List, Optional

_FIELD = re.compile(r'"requests"\s*;\s*r=(\d+)\s*;\s*t=(\d+)')
_lock = threading.Lock()
_state = {"remaining": None, "reset_at": 0.0, "waited_s": 0.0, "enabled": True}
#: Keep this many requests in hand: a test may send two back to back.
RESERVE = 2

#: What the target did that a limits-off deployment never does.
_observed = {"ratelimit_headers": 0, "limit_429s": 0, "examples": []}
_MAX_EXAMPLES = 3


def _example(text: str) -> None:
    if len(_observed["examples"]) < _MAX_EXAMPLES:
        _observed["examples"].append(text)


def on_response(response: Any) -> None:
    header = response.headers.get("ratelimit", "") or ""
    if header or response.headers.get("ratelimit-policy"):
        with _lock:
            _observed["ratelimit_headers"] += 1
            _example(f"{_describe(response)} carried RateLimit {header!r}")
    match = _FIELD.search(header)
    if not match:
        return
    with _lock:
        _state["remaining"] = int(match.group(1))
        _state["reset_at"] = time.monotonic() + int(match.group(2)) + 1


def on_request(_request: Any) -> None:
    with _lock:
        if not _state["enabled"]:
            return
        remaining, reset_at = _state["remaining"], _state["reset_at"]
    if remaining is None or remaining > RESERVE:
        if remaining is not None:
            with _lock:
                _state["remaining"] = remaining - 1
        return
    delay = reset_at - time.monotonic()
    if delay > 0:
        time.sleep(delay)
        with _lock:
            _state["waited_s"] += delay
    with _lock:
        _state["remaining"] = None


def waited_seconds() -> float:
    with _lock:
        return float(_state["waited_s"])


def _describe(response: Any) -> str:
    """Never raises: an observation must not break the hook that records it."""
    status = getattr(response, "status_code", "?")
    try:
        request = response.request
        return f"{request.method} {request.url.path} -> {status}"
    except Exception:  # noqa: BLE001 — a response built without a request
        return f"HTTP {status}"


# ------------------------------------------------------ enforced-limit waits --
#
# WHY A TRANSPORT (2026-09-13, first e2e run): the RateLimit header counts
# REQUESTS only, but a stack with limits enforced also reserves output tokens
# per minute — capped at output_tpm, so one request asking for 1,000,001
# tokens reserves 60,000 and needs an EMPTY minute — and the run above saw a
# 429 `rate_limit_error` (Retry-After 92) on a request that, alone, is a 400.
# The e2e console account may not raise a project's ceilings
# (api.limits.manage is super-admin only), so the suite waits out a LIMIT 429
# the way a patient client would and reports every wait in the summary. It
# never waits on any other 429 or status, never on a request that carries an
# Idempotency-Key (its 429 may be the behaviour under test), never when the
# target claims limits are off, and `--no-limit-wait` turns it off. Event
# hooks see only the final answer, so a test that counts attempts must not be
# run against a limit-enforcing target without reading the "limits:" line of
# the summary.

LIMIT_CODES = frozenset({"rate_limit_error", "quota_exceeded", "concurrency_limit_exceeded"})
MAX_LIMIT_WAITS = 4
_limit_waits = {"count": 0, "seconds": 0.0, "enabled": True}


def disable_limit_waits() -> None:
    _limit_waits["enabled"] = False


def expect_limits_off() -> None:
    """The target claims no usage limits: behave like a client that has never
    heard of them, so any limit reaches the test that hit it."""
    disable_limit_waits()
    with _lock:
        _state["enabled"] = False


def limit_waits() -> tuple:
    return int(_limit_waits["count"]), float(_limit_waits["seconds"])


def limits_off_violations() -> List[str]:
    """Every sign of a usage limit seen this run, as sentences (empty = none)."""
    with _lock:
        headers, codes, examples = _observed["ratelimit_headers"], _observed["limit_429s"], list(_observed["examples"])
    out: List[str] = []
    if codes:
        out.append(f"{codes} HTTP 429 with a usage-limit code (rate_limit_error/quota_exceeded/concurrency_limit_exceeded)")
    if headers:
        out.append(f"{headers} response(s) carried RateLimit / RateLimit-Policy headers")
    if out and examples:
        out.append("e.g. " + "; ".join(examples))
    return out


def reset_for_tests() -> None:
    """Selftest only: back to the state a fresh run starts in."""
    with _lock:
        _state.update(remaining=None, reset_at=0.0, waited_s=0.0, enabled=True)
        _observed.update(ratelimit_headers=0, limit_429s=0, examples=[])
    _limit_waits.update(count=0, seconds=0.0, enabled=True)


def _replayable(request: Any) -> bool:
    try:
        request.content  # raises when the body is an unread stream (a generator)
    except Exception:  # noqa: BLE001
        return False
    return True


def _limit_code(response: Any) -> str:
    try:
        response.read()
        return str(((_json.loads(response.content) or {}).get("error") or {}).get("code") or "")
    except Exception:  # noqa: BLE001
        return ""


def wrap(transport: Any, base: type, *, stream_base: Optional[type] = None, wire: Any = None) -> Any:
    """Wrap an httpx / httpx2 transport so a LIMIT 429 is waited out (unless
    limits are expected off) and, with `wire` (an `sse.WireClock`) and the
    matching `stream_base` (`httpx.SyncByteStream` / `httpx2.SyncByteStream`),
    every byte of an event-stream response is timed where it arrives."""

    class LimitPatientTransport(base):  # type: ignore[misc, valid-type]
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def handle_request(self, request: Any) -> Any:
            attempt = 0
            while True:
                sent_at = time.monotonic()
                response = self._inner.handle_request(request)
                if response.status_code != 429:
                    return self._maybe_timed(response, sent_at)
                code = _limit_code(response)
                if code in LIMIT_CODES:
                    with _lock:
                        _observed["limit_429s"] += 1
                        _example(f"{request.method} {request.url.path} -> 429 {code}")
                if (
                    not _limit_waits["enabled"]
                    or attempt >= MAX_LIMIT_WAITS
                    or not _replayable(request)
                    # A request carrying an Idempotency-Key is testing retry
                    # semantics itself: a 429 there may BE the answer under
                    # test (the pre-2026-09-13 "still running" was a 429).
                    or "idempotency-key" in request.headers
                    or code not in LIMIT_CODES
                ):
                    return response
                try:
                    delay = min(max(float(response.headers.get("retry-after") or 1), 1.0), 120.0)
                except ValueError:
                    delay = 5.0
                response.close()
                with _lock:
                    _limit_waits["count"] += 1
                    _limit_waits["seconds"] += delay
                time.sleep(delay)
                attempt += 1

        def _maybe_timed(self, response: Any, sent_at: float) -> Any:
            if wire is None or stream_base is None:
                return response
            if not str(response.headers.get("content-type", "")).startswith("text/event-stream"):
                return response
            wire.start(sent_at)
            inner_stream = response.stream

            class TimedStream(stream_base):  # type: ignore[misc, valid-type]
                def __iter__(self):
                    for chunk in inner_stream:
                        if chunk:
                            wire.chunk(chunk)
                        yield chunk
                    wire.end()

                def close(self) -> None:
                    inner_stream.close()

            response.stream = TimedStream()
            return response

        def close(self) -> None:
            self._inner.close()

    return LimitPatientTransport(transport)
