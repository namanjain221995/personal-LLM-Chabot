"""A non-streaming `/v1` response that can never go silent (2026-09-13).

THE PROBLEM, MEASURED. Every hop between a developer and this process kills a
response that says nothing for long enough, and a synchronous generation says
nothing until it has finished:

* Cloudflare answers 524 when the origin has not sent a first byte within
  125 s (the no-timeout design assumes the stricter 100 s);
* Node's fetch (undici, inside openai-node and the Next edge) gives up on
  headers after 300 s;
* openai-python's default read timeout is 600 s of silence.

A 400 s answer therefore failed for every default client, however healthy the
engine was, and the only server-side cure is bytes.

THE RULE (no-timeout design, edge_100s point 2; CONTRACT §10 byte invariant).
From the moment this response is called — authentication, validation and the
quota gate are behind it — the client receives a first body byte within
PUBLIC_API_SYNC_COMMIT_S (12 s by default, never above 15 s) and never waits
more than 15 s between bytes (a keep-alive every `events.HEARTBEAT_SECONDS`,
14 s):

* the work finishes within the commit window → the REAL status and body, as if
  this class did not exist (a 400 is a 400, a 503 carries its Retry-After);
* it does not → the response COMMITS: `200`, `Cache-Control: no-store,
  no-transform`, one space immediately and one every heartbeat, then the JSON
  object. JSON allows leading whitespace (RFC 8259 §2) and both SDKs parse it
  (measured, sdk_and_docs);
* a failure AFTER the commit cannot change the status line any more, so it
  is expressed the way the route chose:
    - `failure_mode="body"` (the generation routes): a well-formed failed
      object — a Response with `status: "failed"`, its error and its partial
      output, or a `chat.completion` with `choices: []` and `error`. A client
      that checks the object sees the failure; the tokens it was charged for
      are in `usage`;
    - `failure_mode="abort"` (embeddings, rerank, transcription): the
      connection is dropped mid-body, so the client sees an incomplete read
      and its SDK retries. Those routes are deterministic or cached, so a
      retry is cheap and correct; a failed object in a 200 would be parsed as
      an empty result.

WHY NOT COMMIT AT ONCE. A request that is answered quickly — including every
refusal — keeps its honest status, its `Retry-After` and its `x-should-retry`
header. The commit window costs nothing on a fast answer and is exactly the
byte invariant on a slow one.

WHY THIS CLASS WATCHES THE CLIENT. A non-streaming handler is not cancelled by
uvicorn when its client resets the connection (adversarial review
2026-09-13), so an abandoned request used to hold its capacity gate, its
admission slot and the engine until a generation nobody would read had
finished. The work runs as a task here, next to a watcher on ASGI `receive`
(whose next message, once the body has been read, is `http.disconnect`); a
disconnect cancels the work, which records its own outcome — and nothing more
is sent. A failed write after the commit is treated the same way.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Mapping, Optional

from starlette.background import BackgroundTask
from starlette.responses import JSONResponse, Response
from starlette.types import Receive, Scope, Send

from . import errors, events, registry

log = logging.getLogger(__name__)

#: A committed failure is a well-formed failed object in the body.
FAILURE_BODY = "body"
#: A committed failure drops the connection so the client's SDK retries.
FAILURE_ABORT = "abort"
FAILURE_MODES = (FAILURE_BODY, FAILURE_ABORT)

#: The byte a committed response writes while it waits. A space rather than a
#: newline: some line-oriented loggers in the middle of a path flush per line,
#: and a body of newlines reads as an empty log record per heartbeat.
KEEPALIVE_BYTE = b" "

#: The headers of a committed response, in addition to the route's own
#: (`X-Request-Id`, CORS, `X-TechSara-Run`). `no-transform` stops a
#: compressing intermediary from buffering the whitespace to recompress it —
#: which would turn fifteen seconds of heartbeats back into silence — and
#: `X-Accel-Buffering: no` asks the same of an nginx-shaped proxy.
COMMITTED_HEADERS = {
    "Cache-Control": "no-store, no-transform",
    "X-Accel-Buffering": "no",
}

#: The commit window. The byte invariant's CEILING is 15 s (CONTRACT §10) and
#: a setting may never exceed it; the DEFAULT sits below it, deliberately
#: (2026-09-13, T3-wire, a measured deviation from the design's 15):
#:
#: * timers fire late. At exactly 15 s the real-time proof measured the first
#:   byte at 15.07 s after the request — over the promise, by scheduling alone;
#: * the clock here starts after authentication and the quota gate, which read
#:   the database, while every hop in front starts its clock at the request;
#: * the v1-gateway self-commits a silent sync call after 15 s of ITS silence
#:   and then drops the orchestrator's later head — X-Request-Id, the
#:   rate-limit fields, x-should-retry and X-TechSara-Run (gateway team's open
#:   low finding). Committing first, with margin, closes that race from this
#:   side instead of moving the gateway's promise.
DEFAULT_COMMIT_S = 12.0
MAX_COMMIT_S = 15.0

#: Headers that belong to one particular body and must not be copied from the
#: route's decoration onto a different one.
_BODY_HEADERS = (b"content-length", b"content-type", b"transfer-encoding")


def sync_commit_s() -> float:
    """PUBLIC_API_SYNC_COMMIT_S (12 s), clamped to [0, 15]. Read per call, so
    a test's monkeypatch and an operator's restart both apply at once."""
    value = registry.setting_float("PUBLIC_API_SYNC_COMMIT_S", DEFAULT_COMMIT_S)
    return min(MAX_COMMIT_S, max(0.0, float(value)))


def heartbeat_s() -> float:
    """The gap between keep-alive bytes: the public SSE heartbeat,
    `events.HEARTBEAT_SECONDS` (14 s, so a late timer still lands inside the
    15 s promise). Read at call time."""
    return min(MAX_COMMIT_S, max(0.001, float(events.HEARTBEAT_SECONDS)))


class CommittedResponseAborted(Exception):
    """Raised out of the ASGI call to make the server drop a committed
    connection mid-body (`failure_mode="abort"`).

    WHY AN EXCEPTION AND NOT A SILENT RETURN. Under uvicorn both close the
    transport mid-chunk — an incomplete read, which every client retries — but
    an exception is the one signal a pure ASGI wrapper cannot mistake for a
    finished body.

    MEASURED LIMIT (2026-09-13, T3-wire): a Starlette `BaseHTTPMiddleware`
    between this response and the server SWALLOWS it. It re-streams the body
    through a memory stream, ends that stream cleanly when the app raises, and
    re-raises only after the outer response has completed — so the client gets
    a complete whitespace-only 200 (and the same happens to any SSE stream
    that raises mid-body). `app/main.py`'s `_reject_cross_site_writes` is such
    a middleware and wraps `/v1`; it must become plain ASGI for `/v1` before an
    abort can reach a caller (hand-over, needs_integration).
    """


Work = Callable[[], Awaitable[Any]]
FailedBody = Callable[[BaseException], Optional[Mapping[str, Any]]]


class CommittedJSONResponse(Response):
    """A JSON response that commits to `200` and heartbeats if its work is slow.

    `work` is an async callable, started when the response is CALLED (not when
    it is constructed: a handler that builds this object and then raises must
    not have started a generation). It returns a Starlette `Response`
    (normally a `JSONResponse`) or any JSON-serialisable value, and raises
    `errors.ApiError` (or anything else) to fail.

    `failed_body(exc)` builds the failed object for `failure_mode="body"`; if
    it is missing, raises or returns None, the committed response is aborted
    instead — a client must never receive a whitespace-only "success".

    The route's decoration (`X-Request-Id`, CORS, the rate-limit fields,
    `X-TechSara-Run`) is set on THIS object's headers by `PublicRoute` after
    the handler returns, and is carried onto whichever response is finally
    sent: the committed one, or the real one when the work was quick.
    """

    media_type = "application/json"

    def __init__(
        self,
        work: Work,
        *,
        failure_mode: str = FAILURE_BODY,
        failed_body: Optional[FailedBody] = None,
        request_id: str = "",
        commit_s: Optional[float] = None,
        heartbeat_s: Optional[float] = None,
        headers: Optional[Mapping[str, str]] = None,
        background: Optional[BackgroundTask] = None,
    ) -> None:
        if failure_mode not in FAILURE_MODES:
            raise ValueError(f"failure_mode must be one of {FAILURE_MODES}, not {failure_mode!r}")
        # Built like StreamingResponse: no `body` attribute, so `init_headers`
        # adds no Content-Length — the committed body's length is unknown when
        # the status line goes out.
        self.status_code = 200
        self.background = background
        self.init_headers(headers)
        self._work = work
        self._failure_mode = failure_mode
        self._failed_body = failed_body
        self._request_id = request_id
        self._commit_s = commit_s
        self._heartbeat_s = heartbeat_s
        #: Observable outcome, for the router's log line and for tests.
        self.committed = False
        self.client_gone = False
        self.heartbeats = 0
        self.failed_after_commit: Optional[BaseException] = None

    # -- the pieces --------------------------------------------------------

    def _merge_decoration(self, response: Response) -> Response:
        """Copy this object's route headers onto `response`, without
        overriding a header `response` already sets for itself (an error's
        `Retry-After`, a JSON body's `Content-Length`)."""
        present = {name for name, _ in response.raw_headers}
        for name, value in self.raw_headers:
            if name in _BODY_HEADERS or name in present:
                continue
            response.raw_headers.append((name, value))
        return response

    def _request_id_value(self) -> str:
        if self._request_id:
            return self._request_id
        for name, value in self.raw_headers:
            if name == b"x-request-id":
                return value.decode("latin-1")
        return ""

    def _real_response(self, result: Any, failure: Optional[BaseException]) -> Response:
        """What a quick answer sends: the real status and body."""
        if failure is not None:
            error = errors.from_unexpected(failure, request_id=self._request_id_value())
            response: Response = JSONResponse(
                status_code=error.status,
                content=error.envelope(self._request_id_value()),
                headers=error.headers(),
            )
        elif isinstance(result, Response):
            response = result
        else:
            response = JSONResponse(result)
        return self._merge_decoration(response)

    def _committed_start(self) -> dict:
        headers = [(n, v) for n, v in self.raw_headers if n not in _BODY_HEADERS]
        names = {n for n, _ in headers}
        headers.append((b"content-type", b"application/json"))
        for name, value in COMMITTED_HEADERS.items():
            key = name.lower().encode("latin-1")
            if key not in names:
                headers.append((key, value.encode("latin-1")))
        return {"type": "http.response.start", "status": 200, "headers": headers}

    @staticmethod
    def _body_bytes(result: Any) -> Optional[bytes]:
        """The success body of a finished work, or None when `result` is a
        response that must be treated as a failure (a non-2xx status)."""
        if isinstance(result, Response):
            if result.status_code >= 400:
                return None
            body = getattr(result, "body", None)
            if body is None:
                return None
            return bytes(body)
        return json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")

    def _failure_for(self, result: Any, failure: Optional[BaseException]) -> BaseException:
        if failure is not None:
            return failure
        status = getattr(result, "status_code", 500)
        log.warning("a committed /v1 response's work answered %s after the commit", status)
        return errors.internal_error()

    # -- the ASGI call ------------------------------------------------------

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        loop = asyncio.get_running_loop()
        commit_s = sync_commit_s() if self._commit_s is None else max(0.0, float(self._commit_s))
        beat_s = heartbeat_s() if self._heartbeat_s is None else max(0.001, float(self._heartbeat_s))
        work = asyncio.ensure_future(self._work())
        watcher: Optional[asyncio.Future] = asyncio.ensure_future(_disconnected(receive))
        try:
            finished, watcher = await _wait(work, watcher, commit_s, loop)
            if not finished and self._gone(watcher):
                await self._abandon(work)
                return
            if finished:
                result, failure = _result_of(work)
                await self._real_response(result, failure)(scope, receive, send)
                return

            # The commit: 200, and the first byte now.
            try:
                await send(self._committed_start())
                await send({"type": "http.response.body", "body": KEEPALIVE_BYTE, "more_body": True})
            except OSError:
                await self._abandon(work)
                return
            self.committed = True
            while True:
                finished, watcher = await _wait(work, watcher, beat_s, loop)
                if finished:
                    break
                if self._gone(watcher):
                    await self._abandon(work)
                    return
                try:
                    await send({"type": "http.response.body", "body": KEEPALIVE_BYTE, "more_body": True})
                except OSError:
                    await self._abandon(work)
                    return
                self.heartbeats += 1

            result, failure = _result_of(work)
            body = None if failure is not None else self._body_bytes(result)
            if body is None:
                exc = self._failure_for(result, failure)
                self.failed_after_commit = exc
                body = self._committed_failure_body(exc)
                if body is None:
                    log.warning(
                        "a committed /v1 response failed after its status line (%s); "
                        "dropping the connection so the client retries",
                        getattr(exc, "code", type(exc).__name__),
                    )
                    raise CommittedResponseAborted(getattr(exc, "code", type(exc).__name__))
            try:
                await send({"type": "http.response.body", "body": body, "more_body": False})
            except OSError:
                self.client_gone = True
                return
        finally:
            for task in (work, watcher):
                if task is not None and not task.done():
                    task.cancel()
            pending = [t for t in (work, watcher) if t is not None and not t.done()]
            if pending:
                await asyncio.wait(pending)
        if self.background is not None:
            await self.background()

    def _committed_failure_body(self, exc: BaseException) -> Optional[bytes]:
        if self._failure_mode != FAILURE_BODY or self._failed_body is None:
            return None
        try:
            payload = self._failed_body(exc)
        except Exception:  # noqa: BLE001 - a broken builder must abort, never send a blank success
            log.warning("the failed body of a committed /v1 response could not be built", exc_info=True)
            return None
        if payload is None:
            return None
        return json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":")).encode("utf-8")

    def _gone(self, watcher: Optional[asyncio.Future]) -> bool:
        return watcher is not None and watcher.done() and not watcher.cancelled() and watcher.exception() is None

    async def _abandon(self, work: asyncio.Future) -> None:
        """The client left: stop the work and let it record its own outcome.

        `wait`, not `await`: the work's CancelledError is its own, and one
        aimed at THIS task must still propagate."""
        self.client_gone = True
        if not work.done():
            work.cancel()
        await asyncio.wait({work})
        if not work.cancelled() and work.exception() is not None:
            log.debug("abandoned /v1 work ended with %s", type(work.exception()).__name__)


async def _disconnected(receive: Receive) -> None:
    """Returns when the client has gone; raises if `receive` cannot be read.

    Once the request body has been read, the next ASGI message is
    `http.disconnect`. A stray `http.request` message (an empty trailing body
    chunk) is not a disconnect; the short sleep keeps a receive that answers
    at once from spinning the loop."""
    while True:
        message = await receive()
        if message.get("type") == "http.disconnect":
            return
        await asyncio.sleep(0.05)


async def _wait(
    work: asyncio.Future,
    watcher: Optional[asyncio.Future],
    timeout: float,
    loop: asyncio.AbstractEventLoop,
) -> "tuple[bool, Optional[asyncio.Future]]":
    """Wait up to `timeout` for the work, or for the client to leave.

    Returns (work finished, the watcher still worth watching). A watcher that
    RAISED (a `receive` that cannot be read) is dropped rather than read as a
    disconnect, and the remaining time is still waited."""
    deadline = loop.time() + max(0.0, timeout)
    while True:
        waits = {work} if watcher is None else {work, watcher}
        remaining = max(0.0, deadline - loop.time())
        done, _pending = await asyncio.wait(waits, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
        if work in done:
            return True, watcher
        if watcher is not None and watcher in done:
            if watcher.cancelled() or watcher.exception() is not None:
                watcher = None
                if loop.time() < deadline:
                    continue
                return False, None
            return False, watcher
        return False, watcher


def _result_of(work: asyncio.Future) -> "tuple[Any, Optional[BaseException]]":
    if work.cancelled():
        return None, errors.internal_error()
    failure = work.exception()
    if failure is not None:
        return None, failure
    return work.result(), None
