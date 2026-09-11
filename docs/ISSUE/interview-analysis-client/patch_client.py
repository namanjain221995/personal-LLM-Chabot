"""Patch interview_analysis/models/client.py: health-gated transport retries."""
import pathlib
import sys

p = pathlib.Path("interview_analysis/models/client.py")
s = p.read_text()
if "def is_transport_error" in s:
    print("already patched")
    sys.exit(0)

old = '''The transport is injectable (`complete=`) so the entire analysis pipeline
is testable without a model server.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar
'''
new = '''The transport is injectable (`complete=`) so the entire analysis pipeline
is testable without a model server.

OUTAGE TOLERANCE (2026-09-11). The text endpoint is a vLLM engine
tensor-parallel across two DGX Sparks; when either rank faults the pair
reloads and the port refuses connections for 9-15 minutes (measured
2026-09-01/02 and 2026-09-10). The retry loop below used to catch only
schema/JSON failures, so an `openai.APIConnectionError` escaped `chat_json`
on the first attempt and ended the run — 10 of 101 sweep jobs were stranded
that way. Now a transport-class failure (connection refused/reset, connect
timeout, a 5xx from a dying engine, a read timeout on a call that is
seconds long) waits for the endpoint's /health to answer, backs off with
jitter, and re-issues the SAME attempt, for at most `recovery_window_s`
(default 20 minutes) before raising LLMError. Schema retries are untouched:
they still bump the temperature and consume an attempt; a transport retry
does neither.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar
'''
assert old in s; s = s.replace(old, new)

old = '''_TEMPERATURE_BUMP = 0.4
_FENCE_RE = re.compile(r"^```(?:json)?\\s*|\\s*```$", re.MULTILINE)


class LLMError(Exception):
    pass


class LLMStats(BaseModel):
    calls: int = 0
    retries: int = 0
    failures: int = 0
'''
new = '''_TEMPERATURE_BUMP = 0.4
_FENCE_RE = re.compile(r"^```(?:json)?\\s*|\\s*```$", re.MULTILINE)
#: How long one job's model call may wait for the endpoint to come back
#: before it is a failure. A TP=2 reload measured 13 minutes; 20 covers it.
DEFAULT_RECOVERY_WINDOW_S = 1200.0
_HEALTH_POLL_S = 5.0
_RETRY_BASE_S = 2.0
_RETRY_CAP_S = 30.0

log = logging.getLogger("interview_analysis.models.client")


class LLMError(Exception):
    pass


class LLMStats(BaseModel):
    calls: int = 0
    retries: int = 0
    failures: int = 0
    #: Attempts re-issued after the endpoint was unreachable (not schema
    #: retries); a sweep row with these > 0 crossed an engine restart.
    transport_retries: int = 0
    #: Seconds spent waiting for the endpoint across the whole client life.
    waited_s: float = 0.0


def _causes(exc: BaseException):
    seen = 0
    while exc is not None and seen < 8:
        yield exc
        exc = exc.__cause__ or exc.__context__  # type: ignore[assignment]
        seen += 1


def is_transport_error(exc: BaseException) -> bool:
    """A failure a restart of the endpoint can fix — never a 4xx.

    openai.APIConnectionError (refused/reset/DNS/connect timeout),
    openai.APITimeoutError (this client's calls are seconds long, so a read
    timeout means the engine is wedged, not that the answer is long), a
    5xx/429 APIStatusError, a bare APIError (an engine dying mid-response),
    and raw httpx transport errors. A BadRequestError is bad again next time.
    """
    import httpx

    try:
        import openai
    except ImportError:  # pragma: no cover
        return False
    if isinstance(exc, asyncio.CancelledError):
        return False
    if isinstance(exc, openai.APIStatusError):
        status = int(getattr(exc, "status_code", 0) or 0)
        return status >= 500 or status == 429
    if isinstance(exc, openai.APIError):
        # APIConnectionError, APITimeoutError and the bare kind.
        return True
    return any(
        isinstance(e, (httpx.TransportError, ConnectionError)) for e in _causes(exc)
    )


def _root_url(base_url: str) -> str:
    root = base_url.rstrip("/")
    return root[: -len("/v1")] if root.endswith("/v1") else root


async def endpoint_answers(base_url: str, *, timeout: float = 3.0) -> bool:
    """GET {root}/health and {root}/v1/models both 200."""
    import httpx

    root = _root_url(base_url)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            if (await client.get(f"{root}/health")).status_code != 200:
                return False
            return (await client.get(f"{root}/v1/models")).status_code == 200
    except Exception:  # noqa: BLE001 — any failure is "not yet"
        return False


async def wait_for_endpoint(
    base_url: str, *, deadline_s: float, poll_s: float = _HEALTH_POLL_S, what: str = ""
) -> float:
    """Poll until the endpoint answers or the deadline passes.

    Returns the seconds waited; raises LLMError at the deadline."""
    started = time.monotonic()
    last_log = started
    while True:
        if await endpoint_answers(base_url):
            return time.monotonic() - started
        elapsed = time.monotonic() - started
        if elapsed >= deadline_s:
            raise LLMError(
                f"{what or base_url}: endpoint still unreachable after {elapsed:.0f}s"
            )
        if time.monotonic() - last_log >= 60.0:
            last_log = time.monotonic()
            log.warning("%s: waiting for %s (%.0fs of %.0fs)", what, base_url, elapsed, deadline_s)
        await asyncio.sleep(min(poll_s, max(0.0, deadline_s - elapsed)))


def _backoff_s(attempt: int) -> float:
    """2, 4, 8, 16, 30, 30 ... seconds, each x0.5-1.0 of jitter."""
    base = min(_RETRY_CAP_S, _RETRY_BASE_S * (2 ** max(0, attempt - 1)))
    return base * (0.5 + random.random() / 2)
'''
assert old in s; s = s.replace(old, new)

old = '''        complete: CompleteFn | None = None,
        max_attempts: int = 3,
    ):
        self.models = models
        self.max_attempts = max_attempts
        self.stats = LLMStats()
'''
new = '''        complete: CompleteFn | None = None,
        max_attempts: int = 3,
        recovery_window_s: float = DEFAULT_RECOVERY_WINDOW_S,
    ):
        self.models = models
        self.max_attempts = max_attempts
        #: Per call: how long to keep waiting for an unreachable endpoint
        #: before the call is a failure. 0 = one attempt, no waiting.
        self.recovery_window_s = float(recovery_window_s)
        self.stats = LLMStats()
'''
assert old in s; s = s.replace(old, new)

old = '''        if key not in self._openai_clients:
            self._openai_clients[key] = AsyncOpenAI(
                base_url=endpoint.base_url, api_key="local", timeout=300
            )
        return self._openai_clients[key]
'''
new = '''        if key not in self._openai_clients:
            # SDK retries OFF: chat_json's transport loop is the one retry
            # layer, and it is the only one that waits for /health. The SDK's
            # own retry (2 attempts, 0.5-8 s apart) was what made every
            # transport failure cost ~1 s and then escape.
            self._openai_clients[key] = AsyncOpenAI(
                base_url=endpoint.base_url, api_key="local", timeout=300, max_retries=0
            )
        return self._openai_clients[key]
'''
assert old in s; s = s.replace(old, new)

old = '''        semaphore = self._semaphore("vision" if images else "text")
        last_error: Exception | None = None

        async with semaphore:
            for attempt in range(self.max_attempts):
                self.stats.calls += 1
                try:
                    raw = await self._complete(
                        endpoint,
                        messages,
                        max_tokens=max_tokens,
                        temperature=temperature + attempt * _TEMPERATURE_BUMP,
                    )
                    return schema.model_validate(parse_json_loose(raw))
                except (LLMError, ValidationError, json.JSONDecodeError) as exc:
                    last_error = exc
                    self.stats.retries += 1
        self.stats.failures += 1
        raise LLMError(
            f"{endpoint.name}: {self.max_attempts} attempts failed for "
            f"{schema.__name__}: {last_error}"
        )
'''
new = '''        semaphore = self._semaphore("vision" if images else "text")
        last_error: Exception | None = None
        what = f"{endpoint.name}/{schema.__name__}"

        async with semaphore:
            for attempt in range(self.max_attempts):
                self.stats.calls += 1
                try:
                    raw = await self._issue(
                        endpoint,
                        messages,
                        max_tokens=max_tokens,
                        temperature=temperature + attempt * _TEMPERATURE_BUMP,
                        what=what,
                    )
                    return schema.model_validate(parse_json_loose(raw))
                except (LLMError, ValidationError, json.JSONDecodeError) as exc:
                    last_error = exc
                    self.stats.retries += 1
        self.stats.failures += 1
        raise LLMError(
            f"{endpoint.name}: {self.max_attempts} attempts failed for "
            f"{schema.__name__}: {last_error}"
        )

    async def _issue(
        self,
        endpoint: ModelEndpointConfig,
        messages: list[dict],
        *,
        max_tokens: int,
        temperature: float,
        what: str,
    ) -> str:
        """One completion, re-issued through an endpoint outage.

        A transport failure does not consume a schema attempt and does not
        bump the temperature: the request was fine, the engine was not there.
        Bounded by `recovery_window_s`; past it, an LLMError that names the
        endpoint so the sweep row says "unavailable", not "schema".
        """
        started = time.monotonic()
        transport_attempt = 0
        while True:
            try:
                return await self._complete(
                    endpoint, messages, max_tokens=max_tokens, temperature=temperature
                )
            except (LLMError, ValidationError, json.JSONDecodeError):
                raise
            except Exception as exc:  # noqa: BLE001 — classified; re-raised when not transport
                if not is_transport_error(exc):
                    raise
                elapsed = time.monotonic() - started
                remaining = self.recovery_window_s - elapsed
                if remaining <= 0:
                    self.stats.waited_s += elapsed
                    raise LLMError(
                        f"{what}: endpoint {endpoint.base_url} unavailable after "
                        f"{elapsed:.0f}s ({transport_attempt + 1} attempt(s)): "
                        f"{type(exc).__name__}: {str(exc)[:160]}"
                    ) from exc
                transport_attempt += 1
                self.stats.transport_retries += 1
                log.warning(
                    "%s: %s: %s — waiting for the endpoint (attempt %d, %.0fs of %.0fs used)",
                    what, type(exc).__name__, str(exc)[:160], transport_attempt,
                    elapsed, self.recovery_window_s,
                )
                waited = await wait_for_endpoint(
                    endpoint.base_url or "", deadline_s=remaining, what=what
                )
                pause = min(_backoff_s(transport_attempt), max(0.0, remaining - waited))
                self.stats.waited_s += waited + pause
                if pause > 0:
                    await asyncio.sleep(pause)
'''
assert old in s; s = s.replace(old, new)
p.write_text(s)
print("client.py patched")
