"""`/v1` — the public endpoints of CONTRACT §7.

SIX MODELS, TWO GENERATING ROUTES (2026-09-13). `POST /v1/responses` and
`POST /v1/chat/completions` serve every chat-kind model in the registry —
techsara-35b on the main engine, techsara-8b-vision on the router,
techsara-ocr on Unlimited-OCR — with image input for the ones that see, a
1,000,000-token output ceiling on the flagship clamped to what its context
window leaves, and a per-engine capacity gate in front of the engines the chat
app shares. What each request may be is decided once, by
`publicapi/planning.py`, before admission; the embeddings, rerank and speech
routes are added from `publicapi/endpoints.py` by the hook at the bottom.

THE SHAPE OF EVERY ROUTE IS THE SAME, AND THE ORDER IS THE POINT (CONTRACT §4):

    resolve the caller → check the scope → check the origin → apply quotas
      → validate the body → execute → record usage → answer

Each step refuses before the next one costs anything. A key that may not use a
model is told so before a token is read; a project over its quota is refused
before it takes one of the ten NORMAL admission lanes that are SHARED with the
chat application, which is the whole reason the quota gate sits in front of
admission rather than behind it.

WHAT THIS SURFACE WILL NOT DO.

* **It never reads a cookie.** `/v1` reads exactly one credential, the
  `Authorization` header. A route that accepted the `ts_session` cookie as
  well would be a confused deputy: any page on the internet could drive it
  with a signed-in visitor's cookie, because a browser attaches cookies by
  origin and not by intention (CONTRACT §1). There is no `Cookie` read
  anywhere in this file and a test pins that.
* **It never sends `Access-Control-Allow-Credentials`.** That header is what
  would let a browser attach the cookie in the first place. The application's
  own `CORSMiddleware` sends it for the three first-party origins; `/v1`
  answers its own CORS and deliberately answers it differently.
* **It never discloses existence.** A model this key may not use is a 404, the
  same 404 as a model that does not exist (CONTRACT §4), and another project's
  response id is `response_not_found`, not `403`.
* **Nothing in the body may change the project, workspace, key, model target,
  limits or audit policy.** Those come from the key. `models.ResponsesRequest`
  forbids extra fields, so a hopeful `"project_id"` is a 400 rather than
  something a reader has to prove is ignored.

WHAT IT OWES, ON EVERY RESPONSE. `X-Request-Id`, so a support conversation can
name one request; the CONTRACT §12 `RateLimit` headers WHEN THE LIMITS ARE
ENFORCED; `Retry-After` on every 429 and 503; and the CONTRACT §9 envelope on
every failure, including the ones raised inside a streaming body.

UNLIMITED BY DEFAULT (owner decision, 2026-09-13). PUBLIC_API_ENFORCE_LIMITS is
false unless an operator sets it, and then no route here is refused for rate,
quota or concurrency and no `RateLimit` / `RateLimit-Policy` field is sent.
None of that is decided in this file: `_admit` still calls `quotas.reserve`
for every authenticated route (the ledgers are still written, once per
request) and the handlers still take `quotas.concurrency_slot`; the switch is
read inside those two and inside `quotas.limit_headers`, which is where both
header paths below get their fields. `Retry-After` on 503 `model_recovering`
is the engine's, and stays.

NO RESPONSE IS SILENT FOR MORE THAN 15 SECONDS (no-timeout design,
2026-09-13; CONTRACT §10 byte invariant). Once a generating request is
authenticated, validated and admitted by the quota gate:

* a STREAM sends its status line and a first frame at once —
  `response.created` on /v1/responses, `: ping` on /v1/chat/completions — and
  waits for engine capacity INSIDE the body, with a `: queued` comment every
  `events.HEARTBEAT_SECONDS` (14 s). Only the concurrency slot (when limits
  are enforced), the row write and the physical guards may still refuse
  before the status line;
* a SYNCHRONOUS call is a `keepalive.CommittedJSONResponse`: the real status
  if it finishes within PUBLIC_API_SYNC_COMMIT_S (12 s), otherwise `200` and
  a space every heartbeat, then the object — and a failure after that point
  arrives as a failed object;
* no public capacity wait this file starts ends on a clock: every public
  gate the generation holds (`_plan_gates` — `main.normal` for a normal-size
  techsara-35b answer once capacity.py has it) is waited for without a
  deadline (`_patient_gate`), and the generation waits in the shared
  admission lanes as a PATIENT request (`patient_admission`), outside chat's
  600 s bound. The one refusal left on that path is a physical guard: more
  than PUBLIC_API_GATE_MAX_WAITERS requests already waiting for one gate.

A gateway-tagged request (`gateway_protocol`) additionally gets
`X-TechSara-Run` and, on a stream, `: ts-seq=N` after each data frame.

WHAT THIS FILE DOES NOT IMPLEMENT, AND CALLS INSTEAD. Identity is
`apiplatform/resolver.py`; the four limits, the sliding window and the
`RateLimit` header syntax are `apiplatform/quotas.py`; the detached job, its
restart reconciliation and its webhook are `publicapi/background.py`; the
generation itself is `publicapi/streaming.py`, which calls
`llm.stream_chat_events` and nothing lower. Each of those is somebody's file in
this wave, and none of their decisions is re-made here. What IS here is the
ORDER they happen in, which is the part a reviewer has to be able to read in
one place.
"""
from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Tuple,
)

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.routing import APIRoute

from .. import admission, context, db, usage as usage_ledger
from ..apiplatform import idempotency, quotas, resolver
from ..apiplatform.resolver import ApiCaller
from ..apiplatform.scopes import InsufficientScopeError, Scope, requires
from . import (
    background,
    capacity,
    engines,
    errors,
    events,
    file_inputs,
    gateway_protocol,
    keepalive,
    models,
    openapi as openapi_module,
    planning,
    registry,
    streaming,
)

log = logging.getLogger(__name__)

#: CONTRACT §7, one entry per route. Declared as data so the OpenAPI document
#: and the tests read the same requirement the dependency enforces — a route
#: whose scope lives only inside its own body is a route whose scope can drift
#: from what is published.
SCOPES: Dict[str, Any] = {
    "list_models": requires(Scope.MODELS_READ),
    "get_model": requires(Scope.MODELS_READ),
    "create_response": requires(Scope.RESPONSES_WRITE),
    "get_response": requires(Scope.RESPONSES_READ),
    "cancel_response": requires(Scope.RESPONSES_WRITE),
    "create_chat_completion": requires(Scope.RESPONSES_WRITE),
    "get_usage": requires(Scope.USAGE_READ),
}

#: CONTRACT §3. The preflight carries no credential and reveals nothing, so it
#: is permissive by design; the ACTUAL request is authorized against the
#: project's `allowed_origins`.
#:
#: FILES (2026-09-13, Files design §2.1 / §12): `PUT` (a raw upload part) and
#: `DELETE` (a file) are methods a browser must be allowed to send, and the
#: part checksum headers, `Range` and `If-None-Match` are request headers the
#: file routes read. Still no credentials header: the permissiveness is safe for
#: the same reason as before.
PREFLIGHT_HEADERS: Dict[str, str] = {
    "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": (
        "authorization, content-type, idempotency-key, "
        "content-digest, x-part-sha256, range, if-none-match"
    ),
    "Access-Control-Max-Age": "600",
}

#: Headers a browser client is allowed to READ off a cross-origin response.
#: Without this a developer's browser app can see the status code and nothing
#: else — not the request id it needs to quote in a support ticket, not the
#: `Retry-After` its own retry logic depends on.
#: The file routes add what a browser download and a resumable upload read:
#: the name and range of the bytes, the validator for a 304, and the retry
#: verdict (`x-should-retry: false` on a 409 the SDKs must not repeat).
_EXPOSE_HEADERS = (
    "X-Request-Id, RateLimit, RateLimit-Policy, Retry-After, "
    "Content-Disposition, Content-Range, Accept-Ranges, ETag, x-should-retry"
)

#: CONTRACT §12: "8,192 default". The live number is
#: PUBLIC_API_DEFAULT_MAX_OUTPUT_TOKENS, read by the registry per request;
#: this constant is the documented default, kept for readers of the contract.
DEFAULT_MAX_OUTPUT_TOKENS = 8192

#: CONTRACT §8's default when the caller does not ask for one. 0.2 is what the
#: chat application asks for, and an API that answered a repeated question
#: differently from the product would be the more surprising choice. Per model
#: since 2026-09-13 (`PublicModel.default_temperature`: OCR reads at 0.0).
DEFAULT_TEMPERATURE = 0.2

#: Above this many body bytes the JSON is decoded — and the request validated,
#: which base64-decodes every image — in a worker thread. A 20 MiB image body
#: parsed on the event loop stalls every chat stream in the process for the
#: length of the parse: the shape of the 2026-09-05 Fast-mode regression.
OFF_LOOP_BODY_BYTES = 64 * 1024

#: `GET /v1/usage` over an unbounded range is a client-supplied result size —
#: OWASP API4. The same bound `db.read_usage_daily` enforces, named here so the
#: 400 says the number rather than letting a ValueError become a 500.
MAX_USAGE_DAYS = 93
DEFAULT_USAGE_DAYS = 30


def _new_request_id() -> str:
    """`req_<32 hex>` — the same spelling as `core/tracing.py`, so one id
    format is recognisable across the whole application's logs."""
    return f"req_{uuid.uuid4().hex}"


# ---------------------------------------------------- the caller seam --


async def resolve_caller(request: Request) -> ApiCaller:
    """The CONTRACT §4 identity for this request.

    `apiplatform/resolver.py` owns every rung of the ladder — key row, service
    account, project, workspace, scopes, model allowlist, IP allowlist — and
    every one of its 401s, which are deliberately byte-identical so that a
    scanner cannot learn from the refusal whether a key it holds exists. This
    function's whole job is to get there from a FastAPI request and to make
    sure nothing else can.

    THE HEADER IS THE ONLY CREDENTIAL READ. `request.cookies` is not touched
    here or anywhere else in this module, and there is no parameter on
    `resolve_api_caller` through which a cookie could reach it. A route that
    accepted the session cookie as well would be a confused deputy: any page
    on the internet could drive it with a signed-in visitor's cookie, because
    a browser attaches cookies by origin and not by intention (CONTRACT §1).

    The empty-header refusal is answered HERE, before the resolver is called,
    so an unauthenticated flood costs no database work at all — it is the
    cheapest request a public API receives and it should stay that way.

    IN A THREAD, because `resolve_api_caller` is synchronous and reads the key
    row. Called directly it would block the event loop for the length of a
    database round trip on every `/v1` request, which is the shape of the Fast
    mode regression of 2026-09-05 (TTFT 0.7 s -> 11.7 s at eight concurrent
    callers, all of it the orchestrator's own loop).

    THE ADDRESS IS THE RESOLVER'S DECISION, from the socket peer and the
    forwarding header together (`resolver.client_address`). Until 2026-09-13
    this file took the FIRST `X-Forwarded-For` entry whenever
    `AUTH_TRUST_PROXY_HEADERS` was on, from any peer — with production on
    0.0.0.0:8080, anyone on the LAN holding a leaked key could type an
    allowlisted address into the header and walk past `ip_allowlist`
    (verifier finding; AUDIT F027/F058). A forwarded address is now believed
    only when the peer is in `PUBLIC_API_TRUSTED_PROXIES`, and there is no
    second implementation of that rule here to drift from it.
    """
    authorization = request.headers.get("authorization") or ""
    if not authorization.strip():
        raise errors.invalid_api_key()
    try:
        return await db.run_in_thread(
            functools.partial(
                resolver.resolve_api_caller,
                authorization,
                peer=(request.client.host if request.client else None),
                forwarded_for=request.headers.get("x-forwarded-for"),
            )
        )
    except errors.ApiError:
        raise
    except Exception as exc:
        # The key row is read on every request (CONTRACT §5), so an outage
        # reaches THIS line before the quota gate. The same rule as `_admit`
        # (2026-09-13): refuse, as a retryable 503 — never a 401 (the key may
        # be fine) and never a 500 for what is not a bug. `PepperUnavailable`
        # is a configuration fault, not an outage, and stays a 500.
        if errors.is_database_outage(exc):
            log.warning("the key could not be resolved: database unavailable", exc_info=True)
            raise errors.database_unavailable() from None
        raise


# ---------------------------------------------------------- the quotas --
#
# `apiplatform/quotas.py` owns CONTRACT §12 end to end: the durable sliding
# window, the daily ledger, the per-project concurrency counter, the
# `Retry-After` arithmetic and the draft-ietf-httpapi-ratelimit-headers-11
# field syntax. Nothing of that is re-implemented here. What this file owns is
# the ORDER and the COVERAGE:
#
# * the gate runs before admission, because the ten NORMAL lanes are shared
#   with the chat application (CONTRACT §11);
# * EVERY authenticated route passes through it, reads included. Until
#   2026-09-13 five of the seven only published a header, so `GET /v1/usage`
#   — a 93-day range query — could be called at any rate while the header on
#   it claimed a budget that was not being spent (verifier finding).

#: The kinds this router declares to `quotas`. A read route does no
#: generation; it is metered against requests-per-minute and holds no slot.
KIND_SYNC = "sync"
KIND_STREAM = "stream"
KIND_BACKGROUND = "background"
KIND_READ = "read"


def _refusal_headers(caller: ApiCaller) -> Dict[str, str]:
    """`RateLimit` for a response we are refusing or failing, without a read.

    `remaining=0` is both true enough and free: passing it skips the window
    read, which matters most when the reason we are here is that the database
    is unhealthy (verifier finding 2026-09-13: the old helper swallowed that
    failure and the headers silently vanished).

    Empty when the limits are not enforced (owner decision 2026-09-13):
    `limit_headers` builds no field for a limit that does not exist, so a 503
    or a validation failure advertises no `RateLimit` either.
    """
    try:
        return quotas.limit_headers(caller, remaining=0)
    except Exception:  # noqa: BLE001 - a header must never turn a 429 into a 500
        log.warning("could not build RateLimit headers for a refusal", exc_info=True)
        return {}


async def _admit(
    request: Request,
    caller: ApiCaller,
    *,
    kind: str,
    estimated_input_tokens: int = 0,
    max_output_tokens: Optional[int] = None,
):
    """Count this request and admit it, or raise the 429 that says which limit.

    `quotas.reserve` is ATOMIC PER PROJECT — one transaction under a
    `pg_advisory_xact_lock` that reads the window, decides and bumps. The
    check-then-write it replaced admitted 12 of 12 simultaneous requests
    against `rpm=1` on real PostgreSQL (verifier probe, 2026-09-13).

    The estimate is `context.estimate_messages`, the same rule the chat path
    uses when the engine's `/tokenize` is not reachable; the exact count is
    written afterwards by `quotas.record_usage` from what the engine reported.
    """
    try:
        reservation = await db.run_in_thread(
            functools.partial(
                quotas.reserve,
                caller,
                kind=kind,
                estimated_input_tokens=max(0, int(estimated_input_tokens or 0)),
                # The OUTPUT reservation is sized from what this request asked
                # for (2026-09-13). Without it every request reserved the
                # platform default of 8,192 output tokens at admission, so a
                # burst of short requests exhausted output_tpm after about
                # seven of them and was refused until the first ones settled.
                # None (the caller named no budget) still reserves the default.
                max_output_tokens=max_output_tokens,
            )
        )
    except errors.ApiError:
        request.state.public_rate_limit = _refusal_headers(caller)
        raise
    except Exception as exc:
        # A gate that cannot read its ledger REFUSES — an unmetered request
        # during a database incident is exactly when a burst does the most
        # harm. A database OUTAGE is a 503 with Retry-After, not the 500 it
        # was until 2026-09-13 (re-verifier: `GET /v1/models` answered
        # `internal_error` during an incident, which clients treat as
        # non-retryable). Anything else is a bug and stays a 500.
        request.state.public_rate_limit = _refusal_headers(caller)
        log.warning("the quota gate could not be consulted; refusing", exc_info=True)
        if errors.is_database_outage(exc):
            raise errors.database_unavailable() from None
        raise
    try:
        request.state.public_rate_limit = reservation.headers()
    except Exception:  # noqa: BLE001
        request.state.public_rate_limit = _refusal_headers(caller)
    return reservation


# ------------------------------------------------------------- the edge --


def _error_body(exc: errors.ApiError, request_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status, content=exc.envelope(request_id), headers=exc.headers()
    )


def _unrouted_error(status: int, method: str) -> errors.ApiError:
    """The CONTRACT §9 envelope for a request no `/v1` route answers.

    CONTRACT §9 says "error, everywhere", and until 2026-09-13 an unknown path
    or a wrong method under `/v1` came back as FastAPI's `{"detail": …}` —
    a body no client of this API can parse (verifier finding). The code is
    `invalid_request_error` because the table has no `not_found`; the STATUS
    is the honest 404 or 405, which is how the reference API this shape is
    modelled on answers an unknown URL. The path is not echoed: it is caller
    text.
    """
    if status == 405:
        message = f"This endpoint does not accept {method if method.isalpha() else 'this method'}."
    else:
        message = "There is no such endpoint. See /v1/openapi.json for the ones that exist."
    failure = errors.invalid_request(message)
    failure.status = status
    return failure


class PublicRoute(APIRoute):
    """Every `/v1` route, wrapped once.

    THREE THINGS THAT MUST BE TRUE OF EVERY RESPONSE, including the ones a
    handler never returned because it raised:

    * the CONTRACT §9 envelope on failure, with no traceback, no SQL, no path
      and no private IP. `errors.from_unexpected` DISCARDS the text of an
      exception this code did not construct rather than filtering it, because
      no regular expression is a safe filter for text nobody wrote for a
      caller;
    * `X-Request-Id`, on success and on failure alike;
    * the CONTRACT §3 CORS headers, and never
      `Access-Control-Allow-Credentials`.

    A route class rather than eight try/except blocks: the eighth one is the
    one that would be forgotten, and it would be forgotten in the route that
    was added last and reviewed least.
    """

    async def handle(self, scope, receive, send) -> None:  # noqa: ANN001
        """A method this route does not accept, in the envelope.

        Starlette answers that with a bare 405 (or, under an app, an
        HTTPException rendered as `{"detail": …}`). The router's catch-all
        OPTIONS route matches EVERY `/v1/…` path partially, so an unknown
        path lands here too — on that route a "wrong method" means "no such
        endpoint", which is a 404. A route declared later that matches FULLY
        still wins: Starlette only falls back to a partial match after every
        route has been tried.
        """
        methods = self.methods or set()
        if scope.get("type") == "http" and methods and scope.get("method") not in methods:
            request = Request(scope, receive)
            request_id = _new_request_id()
            if self.endpoint is preflight:
                failure = _unrouted_error(404, str(scope.get("method") or ""))
                response = _error_body(failure, request_id)
            else:
                failure = _unrouted_error(405, str(scope.get("method") or ""))
                response = _error_body(failure, request_id)
                response.headers["Allow"] = ", ".join(sorted(methods))
            _decorate(request, response, request_id)
            await response(scope, receive, send)
            return
        await super().handle(scope, receive, send)

    def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            request_id = _new_request_id()
            request.state.public_request_id = request_id
            # The gateway's tag, decided ONCE from the socket peer (2026-09-13):
            # every later reader asks `request.state`, never the headers, so
            # no route can honour an `x-techsara-*` header from an untrusted
            # peer by reading it itself. A tagged request's responses all
            # carry `X-TechSara-Run` — `none` unless a route names its run.
            tag = gateway_protocol.from_request(request)
            request.state.public_gateway_tag = tag
            request.state.public_internal_headers = gateway_protocol.run_headers(tag)
            try:
                response = await original(request)
            except errors.ApiError as exc:
                response = _error_body(exc, request_id)
            except InsufficientScopeError as exc:
                # The platform-core vocabulary, rendered in the wire
                # vocabulary. RFC 6750 says naming the required scope is the
                # machine-actionable thing to do, and CONTRACT §9 says the
                # body is ours, so both happen: the scope goes in the
                # `WWW-Authenticate` challenge and the envelope stays generic.
                failure = errors.insufficient_scope(
                    " ".join(sorted(s.value for s in exc.required))
                )
                response = _error_body(failure, request_id)
                response.headers["WWW-Authenticate"] = exc.challenge()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - never a traceback on the wire
                response = _error_body(
                    errors.from_unexpected(exc, request_id=request_id), request_id
                )
            _decorate(request, response, request_id)
            return response

        return handler


def _decorate(request: Request, response: Response, request_id: str) -> None:
    response.headers["X-Request-Id"] = request_id
    # Internal protocol headers: present only for a trusted, tagged request
    # (`gateway_protocol.run_headers`), and never listed in
    # Access-Control-Expose-Headers — a browser has no business reading them.
    for name, value in (getattr(request.state, "public_internal_headers", None) or {}).items():
        response.headers[name] = value
    for name, value in (getattr(request.state, "public_rate_limit", None) or {}).items():
        response.headers[name] = value
    origin = request.headers.get("origin")
    if origin:
        # Echoed, never `*`, and never with credentials: a browser that cannot
        # attach a cookie here cannot be used as a confused deputy, and a
        # developer's own page still gets to read the answer.
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Expose-Headers"] = _EXPOSE_HEADERS


router = APIRouter(prefix="/v1", route_class=PublicRoute, tags=["public"])


def gateway_tag(request: Request) -> gateway_protocol.GatewayTag:
    """The gateway's tag for this request, as `PublicRoute` decided it —
    `UNTAGGED` for a request that did not come through the handler."""
    tag = getattr(request.state, "public_gateway_tag", None)
    return tag if isinstance(tag, gateway_protocol.GatewayTag) else gateway_protocol.UNTAGGED


def name_run(request: Request, *, response_id: Optional[str] = None, job_key: Optional[str] = None) -> None:
    """Set this request's `X-TechSara-Run` (a no-op when it is not tagged).

    Public for `publicapi/endpoints.py` (T4): an audio job names itself with
    `job_key` so the gateway can re-attach with X-TechSara-Attach-Job."""
    request.state.public_internal_headers = gateway_protocol.run_headers(
        gateway_tag(request), response_id=response_id, job_key=job_key
    )


# -------------------------------------------------------------- helpers --


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "public_request_id", "") or "")


def _new_response_id() -> str:
    """`resp_<24 hex>`, the SCHEMA-V34 shape `db._api_id("resp")` writes.

    Chosen HERE, before anything is written, because the background path hands
    the id to `background.start` to write and the compatibility surface
    derives its `chatcmpl_` id from it — which is what lets an idempotent
    replay answer with the same completion id the original did.
    """
    return f"resp_{secrets.token_hex(12)}"


def _completion_id(response_id: str) -> str:
    return "chatcmpl_" + str(response_id).split("_", 1)[-1]


async def _authorize(request: Request, caller: ApiCaller, operation: str) -> None:
    """Scope, then origin, for one of THIS file's routes (`SCOPES`)."""
    await authorize_scope(request, caller, SCOPES[operation])


async def authorize_scope(request: Request, caller: ApiCaller, requirement: Any) -> None:
    """Scope, then origin. In that order, and both before any work.

    Public (2026-09-13) so `publicapi/endpoints.py` applies the SAME two
    checks to its routes with its own requirement, rather than a copy.

    The scope answers "may this credential call this endpoint at all"; the
    origin answers "may this BROWSER use this credential from where it is".
    A key used from a server sends no `Origin` and is unaffected — the check
    applies only when the request carries one, because that is the only case
    where the answer is a browser's to enforce.
    """
    requirement.check(caller.scopes)
    origin = request.headers.get("origin")
    if not origin:
        return
    # Carried on the `ApiCaller`, resolved with the key row rather than read
    # again here — and read per request, so tightening a project's origins
    # takes effect on the next call rather than whenever a cache expires.
    if caller.allowed_origins and origin not in caller.allowed_origins:
        raise errors.origin_not_allowed()


async def _overrides(caller: ApiCaller) -> Any:
    """The `public_models` rows, which may only DISABLE a declared model.

    A database that is unreachable must not be able to ADD a model, and it
    cannot: the failure path returns "no overrides", which leaves the code-level
    registry exactly as declared (CONTRACT §15).

    PER WORKSPACE (2026-09-13): the caller's workspace, so one workspace's
    super admin disabling a model no longer disables it for every customer.
    """
    try:
        return await db.run_in_thread(
            db.public_model_overrides, caller.workspace_id, list(registry.PUBLIC_MODEL_IDS)
        )
    except Exception:  # noqa: BLE001
        log.warning("public_models overrides unreadable; serving the declared set", exc_info=True)
        return None


async def _resolve_model(caller: ApiCaller, model_id: str) -> registry.PublicModel:
    """The model for this request, or 404 — never 403 (CONTRACT §4)."""
    model = registry.resolve_public_model(
        model_id, allowed=caller.models, overrides=await _overrides(caller)
    )
    if model is None:
        raise errors.model_not_found(model_id)
    return model


async def _json_body(request: Request, *, limit: Optional[int] = None) -> Any:
    """The decoded body, refused by SIZE before it is parsed (CONTRACT §8).

    Two checks, not one. `Content-Length` is the cheap refusal and it is what
    stops us reading a gigabyte; the length of what actually arrived is the
    honest one, because a chunked request declares no length at all and a lying
    one declares whatever it likes.

    `limit` is the TRANSPORT cap: 20 MiB on the generating routes since
    2026-09-13 (images), where the 1 MiB rule moves to the TEXT inside the
    body (`models.parse_responses_request`). The body length is left on
    `request.state` so the caller can validate a large one off the loop.
    """
    limit = models.max_body_bytes() if limit is None else int(limit)
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise errors.request_too_large(limit)
    raw = await request.body()
    request.state.public_body_bytes = len(raw)
    if len(raw) > limit:
        raise errors.request_too_large(limit)
    if not raw.strip():
        raise errors.invalid_request("The request body must be a JSON object.")
    try:
        if len(raw) > OFF_LOOP_BODY_BYTES:
            return await asyncio.to_thread(json.loads, raw)
        return json.loads(raw)
    except ValueError:
        # The decoder's own message names a byte offset into the caller's body,
        # which is their prompt. CONTRACT §16: we do not echo it.
        raise errors.invalid_request("The request body is not valid JSON.") from None


# ------------------------------------------------------------ recording --


def _recorder(
    caller: ApiCaller,
    *,
    route: str,
    request_id: str,
    response_id: str,
    streamed: bool,
    held: Optional[idempotency.Claim] = None,
    reservation: Any = None,
    plan: Optional[planning.GenerationPlan] = None,
    extra_meta: Optional[Callable[[], Mapping[str, Any]]] = None,
) -> streaming.OnFinish:
    """The one write per request of CONTRACT §16, plus the durable row.

    Through the EXISTING ledger (`usage.record_async`), not a second one, so
    the analytics console and the admin pages see API traffic without anybody
    inventing a parallel set of numbers. `usage.record` never raises — a
    missing telemetry row is a gap in a report, and an exception here would be
    a failed answer.

    `held` is this request's `Idempotency-Key` claim. It is finished HERE,
    when the outcome is known: a completed (or cancelled) response is attached
    so a retry replays it, and a FAILED one releases the key so a retry runs
    again — replaying a 503 for twenty-four hours to a client that did exactly
    what `Retry-After` told it to would poison the key over one bad minute.
    """
    project_id = caller.project_id
    workspace_id = caller.workspace_id
    key_id = caller.key_id

    async def finish(outcome: streaming.StreamOutcome) -> None:
        counted = outcome.usage_model()
        usage_source = (
            None
            if counted is None
            else str((outcome.usage or {}).get("source") or "engine")
        )
        input_tokens = None if counted is None else counted.input_tokens
        output_tokens = None if counted is None else counted.output_tokens
        # Files (2026-09-13): the file ids, context mode and tokens, retrieval,
        # citations and readiness wait of Files design §5.7, read at the end so
        # they describe what was actually supplied. Never fails the record.
        added: Dict[str, Any] = {}
        if extra_meta is not None:
            try:
                added = dict(extra_meta())
            except Exception:  # noqa: BLE001
                log.warning("extra usage meta for %s was not read", response_id, exc_info=True)
        await usage_ledger.record_async(
            user_id=None,
            workspace_id=workspace_id or None,
            conversation_id=None,
            generation_id=response_id,
            route=route,
            effort=streaming.PUBLIC_EFFORT,
            model=outcome.model,
            mode="api",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            ttft_ms=outcome.ttft_ms,
            duration_ms=outcome.duration_ms,
            status=(
                usage_ledger.ERROR if outcome.status == "failed" else usage_ledger.OK
            ),
            error_kind=(outcome.error.code if outcome.error is not None else ""),
            meta={
                "api_key_id": key_id,
                "project_id": project_id,
                "request_id": request_id,
                "streamed": bool(streamed),
                # 2026-09-13: "engine" when vLLM reported the counts,
                # "counted_at_stop" when a wall clock cut the stream before
                # its usage chunk and this server counted (CONTRACT §8.3).
                "usage_source": usage_source,
                # 2026-09-13: what was asked for, what the window allowed,
                # and the clock this generation ran under — the numbers a
                # "why did my 1M answer stop" conversation starts from.
                **(
                    {}
                    if plan is None
                    else {
                        "max_output_tokens_requested": plan.requested_max_output_tokens,
                        "max_output_tokens_applied": outcome.max_output_tokens,
                        "clamped": bool(
                            outcome.max_output_tokens is not None
                            and outcome.max_output_tokens < plan.requested_max_output_tokens
                        ),
                        "wall_clock_s": plan.wall_clock_s,
                    }
                ),
                **added,
            },
        )
        # The quota ledgers, ONCE per request and never per token. The
        # request counter is not touched: `quotas.reserve` counted this request
        # when it admitted it.
        try:
            # With the reservation, so the ESTIMATE `reserve` added to the
            # input-token window is swapped for the measured count rather
            # than counted twice.
            await db.run_in_thread(
                functools.partial(
                    quotas.record_usage,
                    caller,
                    input_tokens,
                    output_tokens,
                    outcome.status,
                    reservation=reservation,
                )
            )
        except Exception:  # noqa: BLE001
            log.warning("token usage for %s was not recorded", response_id, exc_info=True)
        fields: Dict[str, Any] = {
            "status": outcome.status,
            "duration_ms": outcome.duration_ms,
        }
        if outcome.ttft_ms is not None:
            fields["ttft_ms"] = outcome.ttft_ms
        if counted is not None:
            # Touched ONLY when the engine reported. NULL in this column means
            # NOT MEASURED (SCHEMA-V34), and writing an explicit None over a
            # value another writer had already recorded would erase it.
            fields["input_tokens"] = counted.input_tokens
            fields["output_tokens"] = counted.output_tokens
        if outcome.error is not None:
            fields["error_code"] = outcome.error.code
            fields["error_message"] = errors.redact(outcome.error.message)
        fields.update(background.persisted_generation_fields(outcome))
        try:
            await db.run_in_thread(
                db.update_api_response, response_id, project_id, **fields
            )
        except Exception:  # noqa: BLE001
            log.warning("the response row for %s was not updated", response_id, exc_info=True)
        await _finish_claim(held, None if outcome.status == "failed" else response_id)

    return finish


async def _claim_row(
    caller: ApiCaller,
    request_id: str,
    request_model: models.ResponsesRequest,
    response_id: str,
    *,
    status: str = "queued",
    max_output_tokens: Optional[int] = None,
) -> None:
    """The durable `api_responses` row for a synchronous or streamed request.

    Written only AFTER the concurrency slot is held, so a request the ceiling
    refuses leaves no row behind. `GET /v1/responses/{id}` is documented for
    every mode, so a synchronous caller that lost its connection can still
    read the outcome back. A background row is written by `background.start`,
    which is the one background implementation.

    `instructions_present` rather than the instructions: CONTRACT §16 keeps
    metadata and not content.
    """
    await db.run_in_thread(
        functools.partial(
            db.create_api_response,
            caller.project_id,
            caller.workspace_id,
            request_model.model,
            request_id,
            response_id=response_id,
            key_id=(caller.key_id or None),
            status=status,
            background=False,
            streamed=bool(request_model.stream),
            instructions_present=bool(request_model.instructions),
            metadata=dict(request_model.metadata or {}),
            max_output_tokens=max_output_tokens,
        )
    )


# ----------------------------------------------------------- the routes --


@router.get("/models", operation_id="listModels")
async def list_models(request: Request, caller: ApiCaller = Depends(resolve_caller)) -> Response:
    """Only the models THIS key may use (CONTRACT §7)."""
    await _authorize(request, caller, "list_models")
    await _admit(request, caller, kind=KIND_READ)
    allowed = caller.models
    overrides = await _overrides(caller)
    data = [
        model.to_wire()
        for model in registry.public_models(overrides)
        if registry.resolve_public_model(model.id, allowed=allowed, overrides=overrides)
    ]
    return JSONResponse({"object": "list", "data": data})


@router.get("/models/{model}", operation_id="getModel")
async def get_model(
    model: str, request: Request, caller: ApiCaller = Depends(resolve_caller)
) -> Response:
    """404 when this key may not use it — the same 404 as "no such model"."""
    await _authorize(request, caller, "get_model")
    await _admit(request, caller, kind=KIND_READ)
    resolved = await _resolve_model(caller, model)
    return JSONResponse(resolved.to_wire())


@dataclass
class _Parsed:
    """What the generating routes know once the body has been looked at."""

    payload: Any = None
    request_model: Optional[models.ResponsesRequest] = None
    include_usage: bool = False
    #: A refusal decided BEFORE the quota gate but raised AFTER it, so that a
    #: malformed or oversized request still counts against the rate limit
    #: (CONTRACT §4 puts quota before validation; verifier finding
    #: 2026-09-13: validation failures were unmetered).
    deferred: Optional[errors.ApiError] = None
    estimate: int = 0
    #: The generation as planned against the DECLARED registry (2026-09-13).
    plan: Optional[planning.GenerationPlan] = None
    #: A plan refusal that is not about input size (the endpoint, images,
    #: max_output_tokens). Raised only after the model has been resolved, so
    #: a model this key may not use is still the 404 first.
    plan_error: Optional[errors.ApiError] = None
    #: The body with its file parts lifted out (`apifiles.service.
    #: LiftedRequest`, 2026-09-13), or None when the body never parsed.
    lifted: Any = None
    #: Which generating route: the dialect of the lift and the plan's endpoint.
    endpoint: str = registry.ENDPOINT_RESPONSES


def _declared_for(caller: ApiCaller, model_id: str) -> Optional[registry.PublicModel]:
    """The model as the CODE declares it for this key — no database read.
    An override can only disable a model, never change its window, and the
    404 for a disabled one is still decided by `_resolve_model` later."""
    return registry.resolve_public_model(model_id, allowed=caller.models, overrides=None)


async def _parse_generating(
    request: Request,
    caller: ApiCaller,
    parse: Callable[[Any], Tuple[models.ResponsesRequest, bool]],
    endpoint: str = registry.ENDPOINT_RESPONSES,
) -> _Parsed:
    parsed = _Parsed(endpoint=endpoint)
    try:
        parsed.payload = await _json_body(request, limit=models.max_media_body_bytes())
        # Files (2026-09-13): `input_file`, `input_image.file_id`,
        # `input_video`, Chat `file` / `input_audio` and `file_context` come
        # out of the raw body FIRST, so the one validator below sees a body it
        # already accepts; their shape errors are deferred like any other 400.
        # The lift walks every message and part, so for a large body it runs
        # in a worker thread like the validator (senior fix 2026-09-14: 446 ms
        # on the loop for a 19.8 MiB body of 560,000 parts).
        if int(getattr(request.state, "public_body_bytes", 0) or 0) > OFF_LOOP_BODY_BYTES:
            parsed.lifted = await asyncio.to_thread(file_inputs.lift, parsed.payload, endpoint)
            parsed.request_model, parsed.include_usage = await asyncio.to_thread(
                parse, parsed.lifted.payload
            )
        else:
            parsed.lifted = file_inputs.lift(parsed.payload, endpoint)
            parsed.request_model, parsed.include_usage = parse(parsed.lifted.payload)
    except errors.ApiError as refusal:
        parsed.deferred = refusal
        return parsed
    declared = _declared_for(caller, parsed.request_model.model)
    if declared is None:
        # The 404 is `_generate`'s, after admission. The soft reservation
        # still needs an estimate.
        parsed.estimate = context.estimate_messages(parsed.request_model.chat_messages())
        return parsed
    if declared.engine in engines.SIDECAR_CHAT_ENGINES:
        # Narrow-only and cached for five minutes: the engine's own served
        # window may shrink the public ceiling, never widen it.
        await engines.served_window(declared.engine)
        declared = _declared_for(caller, parsed.request_model.model) or declared
    limits = getattr(caller, "limits", None)
    try:
        # Two different counts for two different decisions (2026-09-13). The
        # HARD input ceiling refuses on the byte bound, which a caller cannot
        # game down (`planning.plan_generation`, step 2); the SOFT input-TPM
        # reservation keeps the estimate, which `quotas.record_usage` settles
        # to the measured count when the request ends.
        parsed.plan = await _plan(
            parsed.request_model,
            declared,
            project_max_output_tokens=getattr(limits, "max_output_tokens", None),
            project_max_input_tokens=getattr(limits, "max_input_tokens", None),
            endpoint=endpoint,
            large=int(getattr(request.state, "public_body_bytes", 0) or 0) > OFF_LOOP_BODY_BYTES,
            files=file_inputs.pending_inputs(parsed.lifted),
        )
    except errors.ApiError as refusal:
        if refusal.code == "context_length_exceeded":
            # Refused, so it spends no input-token budget — but it still
            # spends a request, like any other refusal after the gate.
            parsed.deferred = refusal
            return parsed
        parsed.plan_error = refusal
        parsed.estimate = context.estimate_messages(parsed.request_model.chat_messages())
        return parsed
    parsed.estimate = parsed.plan.estimated_input_tokens
    return parsed


async def _plan(
    request_model: models.ResponsesRequest,
    model: registry.PublicModel,
    *,
    large: bool,
    **kwargs: Any,
) -> planning.GenerationPlan:
    """`planning.plan_generation`, off the loop for a large body: counting a
    megabyte of text byte by byte is CPU the chat streams would wait for."""
    call = functools.partial(planning.plan_generation, request_model, model, **kwargs)
    if large:
        return await asyncio.to_thread(call)
    return call()


async def _replan(
    parsed: _Parsed, caller: ApiCaller, files: planning.FileInputs
) -> planning.GenerationPlan:
    """The plan again once the request's files have resolved: the same model
    and project ceilings as the first one, with the spliced messages and the
    file counts (`planning.FileInputs`). Off the loop — the messages now carry
    the file text."""
    assert parsed.request_model is not None and parsed.plan is not None
    limits = getattr(caller, "limits", None)
    return await _plan(
        parsed.request_model,
        parsed.plan.model,
        project_max_output_tokens=getattr(limits, "max_output_tokens", None),
        project_max_input_tokens=getattr(limits, "max_input_tokens", None),
        endpoint=parsed.endpoint,
        large=True,
        files=files,
    )


def _reserved_output(parsed: _Parsed) -> Optional[int]:
    """The output reservation for admission (`planning.reservation_output_tokens`)."""
    if parsed.plan is not None:
        return planning.reservation_output_tokens(
            parsed.plan, limits_enforced=quotas.limits_enforced()
        )
    # `request_model` is None when validation failed: the 400 is DEFERRED
    # until after admission so a malformed request still counts against the
    # rate limit. Reading an attribute off it turned every invalid body into
    # a 500 (caught by the end-to-end run, 2026-09-13).
    return getattr(parsed.request_model, "max_output_tokens", None)


def _kind_of(request_model: Optional[models.ResponsesRequest]) -> str:
    if request_model is None:
        return KIND_SYNC
    if request_model.background:
        return KIND_BACKGROUND
    return KIND_STREAM if request_model.stream else KIND_SYNC


def _parse_responses(payload: Any) -> Tuple[models.ResponsesRequest, bool]:
    return models.parse_responses_request(payload), False


@router.post("/responses", operation_id="createResponse")
async def create_response(request: Request, caller: ApiCaller = Depends(resolve_caller)) -> Response:
    """Sync, streaming or background — CONTRACT §8, §10, §11, §13, §14."""
    await _authorize(request, caller, "create_response")
    parsed = await _parse_generating(
        request, caller, _parse_responses, registry.ENDPOINT_RESPONSES
    )
    reservation = await _admit(
        request,
        caller,
        kind=_kind_of(parsed.request_model),
        estimated_input_tokens=parsed.estimate,
        max_output_tokens=_reserved_output(parsed),
    )
    if parsed.deferred is not None:
        raise parsed.deferred
    return await _generate(request, caller, parsed, reservation, chat=False)


@router.get("/responses/{id}", operation_id="getResponse")
async def get_response(
    id: str, request: Request, caller: ApiCaller = Depends(resolve_caller)
) -> Response:
    """Project-scoped. Another project's id reads as missing (CONTRACT §9)."""
    await _authorize(request, caller, "get_response")
    await _admit(request, caller, kind=KIND_READ)
    row = await _response_row(caller, id)
    # A background job a restart cut off is closed here, where its poller
    # looks, rather than left `in_progress` for ever.
    row = await background.repair_if_orphaned(row) or row
    return JSONResponse(_row_to_wire(row))


@router.post("/responses/{id}/cancel", operation_id="cancelResponse")
async def cancel_response(
    id: str, request: Request, caller: ApiCaller = Depends(resolve_caller)
) -> Response:
    """Idempotent (CONTRACT §7, §14).

    `background.request_cancel` owns the whole decision — whether a job is
    running here, whether the row is a restart orphan, and what may be written
    — because it is the module that holds the live jobs. Cancelling a response
    that has already finished is not an error and does not write: a client
    that retries a cancel after a timeout sees the same answer rather than a
    409, and a completed response can never be walked back into `cancelled`.

    None means this project has no such response, which is the 404 the
    contract owes: another project's id reads as missing, never as forbidden.
    """
    await _authorize(request, caller, "cancel_response")
    await _admit(request, caller, kind=KIND_READ)
    row = await background.request_cancel(id, caller.project_id)
    if row is None:
        raise errors.response_not_found(id)
    return JSONResponse(_row_to_wire(row))


@router.post("/chat/completions", operation_id="createChatCompletion")
async def create_chat_completion(
    request: Request, caller: ApiCaller = Depends(resolve_caller)
) -> Response:
    """The compatibility shape, on the same engine path (CONTRACT §7).

    Translated into `ResponsesRequest` and back, so there is exactly one
    validator, one quota gate, one generator and one ledger write. A second
    implementation of "the same thing in a different shape" is how two
    endpoints end up with two different ideas of what `max_tokens` means.
    """
    await _authorize(request, caller, "create_chat_completion")
    parsed = await _parse_generating(
        request, caller, _from_chat_completions, registry.ENDPOINT_CHAT_COMPLETIONS
    )
    reservation = await _admit(
        request,
        caller,
        kind=_kind_of(parsed.request_model),
        estimated_input_tokens=parsed.estimate,
        max_output_tokens=_reserved_output(parsed),
    )
    if parsed.deferred is not None:
        raise parsed.deferred
    return await _generate(request, caller, parsed, reservation, chat=True)


async def _generate(
    request: Request, caller: ApiCaller, parsed: _Parsed, reservation: Any, *, chat: bool
) -> Response:
    """Everything after the gate, for both generating routes.

    The order, and why:

    1. the model (404), then the plan's own refusals (400: endpoint, images,
       output ceiling) — cheap, and before any write;
    2. a gateway RE-ATTACH that nothing here can serve is a 404 (the gateway
       then cuts its client rather than splice a second generation onto the
       first — `_refuse_unattachable_reattach`);
    3. the `Idempotency-Key` claim, so a replay answers without a slot, a row
       or the engine;
    4. background → `background.start`, the ONE background implementation
       (it takes the project's concurrency slot for the job's whole life and
       writes the row);
    5. streaming → `_SlotStream`: the slot and the row BEFORE the status line
       (a refusal there is still a real 429/503), the engine's capacity gate
       INSIDE the body, after the opening frame;
    6. synchronous → `keepalive.CommittedJSONResponse` around the slot, the
       gate, the row and the generation — the real status when it is quick,
       a committed 200 with whitespace heartbeats when it is not.

    No capacity wait in 5 or 6 ends on a clock (no-timeout design,
    capacity_waits): every gate `_plan_gates` names is held without a deadline
    and the engine call waits in the admission lanes as a patient request.
    The caller waits, and sees bytes while it does.
    """
    request_model = parsed.request_model
    assert request_model is not None
    route = "v1_chat_completions" if chat else "v1_responses"
    request_id = _request_id(request)
    tag = gateway_tag(request)
    try:
        await _resolve_model(caller, request_model.model)
        if parsed.plan_error is not None:
            raise parsed.plan_error
        plan = parsed.plan
        if plan is None:  # pragma: no cover - a resolved model always has a plan
            raise errors.internal_error()
        # Files (2026-09-13): `files.read` for any `file_id`, before an id is
        # looked up and before a replay could answer from another key's claim.
        file_run = file_inputs.FileRun.for_request(
            parsed.lifted,
            caller=caller,
            plan=plan,
            request_id=request_id,
            request_model=request_model,
        )
        if file_run is not None:
            await file_run.authorize(request, authorize_scope)
        _refuse_unattachable_reattach(tag)
        held = await _claim_idempotency(request, caller, route, parsed.payload)
        if held is not None and not held.claimed:
            await _nothing_ran(caller, reservation, None)
            return await _replay(
                caller, held, request_model, chat=chat, include_usage=parsed.include_usage
            )
    except BaseException:
        await _nothing_ran(caller, reservation, None)
        raise

    try:
        response_id = _new_response_id()
        if RUNS_ATTACHABLE:
            name_run(request, response_id=response_id)
        created = int(time.time())
        spec = streaming.spec_from_plan(plan, response_id=response_id, created_at=created)
        on_finish = _recorder(
            caller,
            route=route,
            request_id=request_id,
            response_id=response_id,
            streamed=request_model.stream,
            held=held,
            reservation=reservation,
            plan=plan,
            extra_meta=(file_run.usage_meta if file_run is not None else None),
        )
        if file_run is not None:
            on_finish = file_run.wrap_finish(on_finish)

        if request_model.background:
            start = functools.partial(
                background.start,
                caller=caller,
                on_finish=on_finish,
                request_id=request_id,
                metadata=dict(request_model.metadata or {}),
                instructions_present=bool(request_model.instructions),
                fingerprint=(held.fingerprint if held is not None else ""),
            )
            if file_run is not None:
                # Files (senior fix 2026-09-14): the row and the 202 NOW; the
                # file wait, the context and the second plan in a detached task
                # while the row is `queued`, then this same `background.start`
                # (file_inputs.start_background).
                row = await file_inputs.start_background(
                    file_run,
                    spec=spec,
                    caller=caller,
                    on_finish=on_finish,
                    replan=functools.partial(_replan, parsed, caller),
                    start=start,
                    request_id=request_id,
                    metadata=dict(request_model.metadata or {}),
                    instructions_present=bool(request_model.instructions),
                    fingerprint=(held.fingerprint if held is not None else ""),
                )
            else:
                row = await start(spec)
            # The 202 IS the outcome a retry should replay; the job's own end
            # re-finishes the claim through `on_finish`.
            await _finish_claim(held, str(row["id"]))
            return JSONResponse(status_code=202, content=_row_to_wire(row))

        if request_model.stream:
            gate_failure = _GateFailure()
            recorded = gate_failure.recording(on_finish)
            launch = _stream_launch(
                chat=chat,
                completion_id=_completion_id(response_id),
                include_usage=parsed.include_usage,
                on_finish=recorded,
            )
            files_frames = None
            if file_run is not None:
                # The 404 / failed-file 400 before the status line; the wait,
                # the context, the second plan and the capacity gate inside
                # the stream, with heartbeats (file_inputs.stream_with_files).
                # The gate is THIS router's patient gate and the generation is
                # started by THIS router's launch (senior fix 2026-09-14): no
                # capacity wait of a file stream ends on a clock, and the day
                # streams launch durably, file streams do too.
                await file_run.precheck()
                files_frames = file_inputs.stream_with_files(
                    file_run,
                    chat=chat,
                    spec=spec,
                    on_finish=recorded,
                    replan=functools.partial(_replan, parsed, caller),
                    completion_id=_completion_id(response_id),
                    include_usage=parsed.include_usage,
                    gate=_patient_gate,
                    launch=launch,
                )
            if chat:
                opener = OPEN_WITH_PING
                refusal_frames = functools.partial(
                    _chat_gate_refusal_frames,
                    completion_id=_completion_id(response_id),
                    model=spec.model,
                    created=created,
                    include_usage=parsed.include_usage,
                )
            else:
                opener = OPEN_WITH_FIRST_FRAME
                refusal_frames = functools.partial(_responses_gate_refusal_frames, spec)
            if files_frames is not None:
                # The files stream opens with its own first frame (a progress
                # comment or `response.created`) and takes its gate itself,
                # after the plan that knows the file text.
                frames, opener = files_frames, OPEN_WITH_FIRST_FRAME
            else:
                frames = launch(spec)
            return _SlotStream(
                frames,
                caller=caller,
                request=request,
                prepare=functools.partial(
                    _claim_row,
                    caller,
                    request_id,
                    request_model,
                    response_id,
                    max_output_tokens=spec.planned,
                ),
                on_refused=functools.partial(_nothing_ran, caller, reservation, held),
                # Through `recorded`, so a chat stream whose capacity wait failed
                # before its generator started is recorded as that failure.
                on_abandoned=functools.partial(_record_abandoned, spec, recorded),
                # Every public gate this generation holds (`_plan_gates`, which
                # includes `main.normal` for a normal-size techsara-35b answer),
                # waited for in the body. None only for work no gate covers.
                # A files stream takes its gates itself, after the plan that
                # knows the file text (file_inputs.stream_with_files).
                capacity_for=(
                    functools.partial(_patient_gate, plan)
                    if _plan_gates(plan) and files_frames is None
                    else None
                ),
                # Its place in those lines, taken before the status line: the
                # waiter bound is still a real 503 there.
                reserve_place=(
                    functools.partial(_GATE_LINE.reserve, _plan_gates(plan))
                    if files_frames is None
                    else None
                ),
                patient=True,
                opener=opener,
                refusal_frames=refusal_frames,
                gate_failure=gate_failure,
                tag=tag,
            )
    except BaseException:
        if file_run is not None:
            file_run.cleanup()
        await _nothing_ran(caller, reservation, held)
        raise

    # One outcome, filled in place by the generation and read by the failed
    # body: a failure after the commit still reports the partial output and
    # the usage the engine produced.
    partial = streaming.new_outcome(spec)
    work = functools.partial(
        _run_synchronous,
        request=request,
        caller=caller,
        request_model=request_model,
        plan=plan,
        spec=spec,
        outcome=partial,
        on_finish=on_finish,
        reservation=reservation,
        held=held,
        chat=chat,
    )
    if file_run is not None:
        # Files (2026-09-13): resolved and waited for INSIDE the committed
        # response, with no deadline — it writes bytes while it waits, so a
        # file still processing no longer becomes a 409 or a 524.
        work = functools.partial(_run_synchronous_with_files, work, file_run=file_run, parsed=parsed)
    return keepalive.CommittedJSONResponse(
        work,
        failure_mode=keepalive.FAILURE_BODY,
        failed_body=functools.partial(
            _committed_failure_body, spec=spec, chat=chat, request_id=request_id, outcome=partial
        ),
        request_id=request_id,
    )


#: SEAM (assembler, 2026-09-13). Whether a generation launched here can be
#: re-attached by the gateway. False until the router launches through T2's
#: `durable.launch` / `durable.attach`: this build keeps no event log, so a
#: gateway that re-POSTed after an orchestrator restart would get a SECOND
#: generation spliced onto the first client's stream. With False, tagged
#: generations answer `X-TechSara-Run: none` — the gateway then never re-POSTs
#: a generation it may already have delivered — and a re-attach is a 404.
#: The commit that routes generations through `durable` sets it True (and
#: replaces `_refuse_unattachable_reattach` with `durable.attach`).
RUNS_ATTACHABLE = False


def _refuse_unattachable_reattach(tag: gateway_protocol.GatewayTag) -> None:
    """A gateway re-attach this build cannot serve → 404, before any work.

    The design's rule (deploy_survival, INTERNAL ATTACH PROTOCOL 5): a run that
    exists but cannot be replayed answers 404, and the gateway aborts its
    client (gateway/lib/reattach.cjs treats 404 as final). Without the durable
    layer nothing can be replayed, and a Resume-After or Attach-Job only ever
    arrives AFTER the gateway relayed part of a run — launching fresh would
    hand the client a second, different answer from byte N+1."""
    if tag.reattach and not RUNS_ATTACHABLE:
        raise errors.response_not_found()


#: How a stream opens. The first frame must leave before any capacity wait.
OPEN_WITH_FIRST_FRAME = "first_frame"  # the generator's own opener (response.created)
OPEN_WITH_PING = "ping"  # a comment (chat chunks have no opening event)


class _GateFailure:
    """What happened to a stream's capacity wait, for the outcome record.

    The streaming generator records its own outcome when it is closed, and a
    generator closed before it generated anything records `completed` with no
    text. That is wrong twice over for a wait that happens in the body:

    * a wait that FAILED sent the client a failure frame, so the record says
      `failed` with that error;
    * a client that LEFT while still waiting never had anything generated, so
      the record says `cancelled` (nothing ran, no usage) — the same truth a
      synchronous request abandoned before its generation is recorded with.

    So the row, the ledger and the idempotency claim agree with the wire.

    It also carries `queued_s`: how long a stream that had ALREADY started its
    generator (the Responses opener) waited for its gates. That generator's
    clock started before the wait, so its `ttft_ms` and `duration_ms` would
    count time in the queue as time to first token (adversarial review of
    T3-wire, 2026-09-14, low): the analytics console's /v1/responses TTFT
    read queue depth as engine speed. Subtracted here, so every dialect
    records engine time; the chat stream's generator starts after its gates
    and needs nothing.
    """

    def __init__(self) -> None:
        self.error: Optional[errors.ApiError] = None
        #: True once the gate is held (or when there is no gate to wait for).
        self.admitted = False
        #: Seconds the already-started generator spent waiting for its gates.
        self.queued_s = 0.0

    def recording(self, on_finish: streaming.OnFinish) -> streaming.OnFinish:
        async def finish(outcome: streaming.StreamOutcome) -> None:
            if self.error is not None:
                outcome.status = "failed"
                outcome.error = self.error
                outcome.client_gone = False
            elif not self.admitted and outcome.client_gone:
                outcome.status = "cancelled"
            queued_ms = int(self.queued_s * 1000)
            if queued_ms > 0:
                if outcome.ttft_ms is not None:
                    outcome.ttft_ms = max(0, int(outcome.ttft_ms) - queued_ms)
                if outcome.duration_ms is not None:
                    outcome.duration_ms = max(0, int(outcome.duration_ms) - queued_ms)
            await on_finish(outcome)

        return finish


def _responses_gate_refusal_frames(spec: streaming.GenerationSpec, failure: errors.ApiError) -> List[str]:
    """`response.failed`, numbered after the `response.created` already sent."""
    emitter = events.SequencedEvents.resume_from(1, events.RESPONSE_CREATED, item_id=spec.item_id)
    wire = models.Response(
        id=spec.response_id,
        created_at=spec.created_at,
        status="failed",
        model=spec.model,
        output=[],
        usage=None,
        max_output_tokens=spec.planned,
        error=models.ResponseError(code=failure.code, message=failure.message),
    ).to_wire()
    return [emitter.failed(wire)]


def _chat_gate_refusal_frames(
    failure: errors.ApiError,
    *,
    completion_id: str,
    model: str,
    created: int,
    include_usage: bool,
) -> List[str]:
    """The compatibility dialect's error chunk, then `data: [DONE]`."""
    chunks = events.ChatCompletionChunks(
        completion_id=completion_id, model=model, created=created, include_usage=include_usage
    )
    return [chunks.error_chunk(failure), chunks.done()]


async def _run_synchronous(
    *,
    request: Request,
    caller: ApiCaller,
    request_model: models.ResponsesRequest,
    plan: planning.GenerationPlan,
    spec: streaming.GenerationSpec,
    outcome: streaming.StreamOutcome,
    on_finish: streaming.OnFinish,
    reservation: Any,
    held: Optional[idempotency.Claim],
    chat: bool,
    slot_held: bool = False,
) -> Response:
    """The work of a synchronous generation, run by `CommittedJSONResponse`.

    `slot_held`: the caller already holds this request's concurrency slot
    (`_run_synchronous_with_files` takes it before the file wait), so it is not
    taken twice.

    Two failure regions, two different truths (re-verifier, 2026-09-13).
    Before the generation starts — the slot refused, the gate abandoned, or
    the row not written — NOTHING RAN: the key is released and the estimate
    given back. Once it has started, the engine may have generated: a
    cancellation (the client left, which `CommittedJSONResponse` turns into a
    cancel of this task, or a shutdown) is recorded through `on_finish` with
    whatever usage the engine reported, so the tokens are charged, the
    reservation is settled to the measured count and the row leaves
    `in_progress`.

    The outcome object is filled IN PLACE (`run_to_completion(outcome=…)`),
    which is what lets `_committed_failure_body` put the partial output into a
    failed body after the 200 was committed.
    """
    request_id = _request_id(request)
    response_id = spec.response_id
    partial = outcome
    generating = False
    began = time.monotonic()
    try:
        with (contextlib.nullcontext() if slot_held else quotas.concurrency_slot(caller, KIND_SYNC)):
            async with _patient_gate(plan):
                await _claim_row(
                    caller,
                    request_id,
                    request_model,
                    response_id,
                    status="in_progress",
                    max_output_tokens=spec.planned,
                )
                generating = True
                # Not `run_to_completion_watching`: the committed response owns
                # the one reader of ASGI `receive` and cancels this task when
                # the client leaves. Two readers would race for the disconnect.
                with patient_admission():
                    finished = await streaming.run_to_completion(spec, outcome=partial)
    except BaseException as exc:
        if isinstance(exc, asyncio.CancelledError):
            _note_client_gone(request, time.monotonic() - began)
        if not generating:
            await _nothing_ran(caller, reservation, held)
            raise
        if isinstance(exc, (asyncio.CancelledError, GeneratorExit)):
            partial.status = "cancelled"
            partial.client_gone = True
        else:
            partial.status = "failed"
            partial.error = errors.from_unexpected(exc, request_id=request_id)
        # Shielded (`streaming._settle`): we are very likely running under the
        # cancellation that brought us here.
        await streaming.settle(on_finish, partial)
        if isinstance(exc, (asyncio.CancelledError, GeneratorExit)):
            raise
        raise _generation_failure(partial.error, held) from None
    await on_finish(finished)
    if finished.error is not None:
        # A synchronous call that could not reach the engine is the documented
        # 503 with a Retry-After (CONTRACT §9) when it fails inside the commit
        # window; after it, `_committed_failure_body` renders the same error
        # as a failed object.
        raise _generation_failure(finished.error, held)
    if not chat:
        return JSONResponse(finished.response().to_wire())
    counted = finished.usage_model()
    return JSONResponse(
        _chat_body(
            completion_id=_completion_id(response_id),
            created=spec.created_at,
            model=spec.model,
            content=finished.text,
            finish_reason=finished.chat_finish_reason(),
            usage=counted,
            max_output_tokens=finished.max_output_tokens,
        )
    )


async def _run_synchronous_with_files(
    work: Callable[..., Awaitable[Response]],
    *,
    file_run: file_inputs.FileRun,
    parsed: _Parsed,
) -> Response:
    """`_run_synchronous` for a request with file parts: take the concurrency
    slot, prepare the files (no deadline: the committed response heartbeats),
    plan again with their text and the engine's count of it, run on that plan,
    then attach `file_citation` annotations to the answer.

    The slot comes FIRST (senior fix 2026-09-14): a file wait is a request in
    flight, so an operator who turns PUBLIC_API_ENFORCE_LIMITS on bounds the
    waiting requests too, not only the generating ones."""
    run = work.keywords
    caller, spec, partial = run["caller"], run["spec"], run["outcome"]
    slot = contextlib.ExitStack()
    try:
        slot.enter_context(quotas.concurrency_slot(caller, KIND_SYNC))
        await file_run.prepare(file_inputs.DELIVERY_SYNC, no_deadline=True)
        plan = await _replan(parsed, caller, await file_run.planning_inputs())
    except BaseException:
        slot.close()
        file_run.cleanup()
        await _nothing_ran(caller, run["reservation"], run["held"])
        raise
    final = streaming.spec_from_plan(plan, response_id=spec.response_id, created_at=spec.created_at)
    try:
        with slot:
            response = await work(plan=plan, spec=final, slot_held=True)
    finally:
        # Idempotent; `wrap_finish` already cleaned when the generation ran.
        file_run.cleanup()
    try:
        body = json.loads(bytes(response.body))
    except (AttributeError, ValueError):  # pragma: no cover - always a JSONResponse
        return response
    if run["chat"]:
        return JSONResponse(file_run.chat_body(body, partial.text))
    return JSONResponse(file_run.response_wire(body, partial.text))


def _stream_launch(
    *,
    chat: bool,
    completion_id: str,
    include_usage: bool,
    on_finish: streaming.OnFinish,
) -> Callable[[streaming.GenerationSpec], AsyncIterator[str]]:
    """THE stream launch of this router, for a stream with files and without
    (senior fix 2026-09-14). One function, so the commit that starts streams
    through the durable runtime (`RUNS_ATTACHABLE`) changes both at once — a
    file stream is never the one generation left without resume."""

    def launch(spec: streaming.GenerationSpec) -> AsyncIterator[str]:
        if chat:
            return streaming.chat_completions_sse(
                spec, completion_id=completion_id, include_usage=include_usage, on_finish=on_finish
            )
        return streaming.responses_sse(spec, on_finish=on_finish)

    return launch


def _generation_failure(
    failure: Optional[errors.ApiError], held: Optional[idempotency.Claim]
) -> errors.ApiError:
    """The error a failed synchronous generation raises.

    `x-should-retry: false` on a 500 for a request with no Idempotency-Key
    (no-timeout design, sdk_and_docs): the engine may have generated, nothing
    ties a retry to this run, and both SDKs would otherwise retry a 500 and
    run the model again from nothing."""
    error = failure if failure is not None else errors.internal_error()
    if error.status == 500 and held is None:
        errors.no_retry(error)
    return error


def _committed_failure_body(
    exc: BaseException,
    *,
    spec: streaming.GenerationSpec,
    chat: bool,
    request_id: str,
    outcome: Optional[streaming.StreamOutcome] = None,
) -> Dict[str, Any]:
    """The failed object a synchronous generation sends after its 200 was
    committed (no-timeout design, edge_100s 2).

    /v1/responses: the Response itself, `status: "failed"`, with its error
    and its usage, and `output: []`. /v1/chat/completions: a `chat.completion`
    with `choices: []` and `error`.

    WHY NO PARTIAL TEXT IN `output` (adversarial review of T3-wire,
    2026-09-14). Before the commit existed this failure was a 5xx, and the
    SDKs raised. After it, a body carrying the partial message made the
    canonical `client.responses.create(...).output_text` return a truncated
    answer WITHOUT raising (measured with openai-python 3.13.0: status
    `failed`, output_text `'The capital of Fra'`) — a caller that does not
    check `status` would ship half an answer as a whole one. With `output: []`
    `output_text` is empty, like the chat dialect's `choices: []`. The cost,
    stated: the partial text of a failed synchronous call is not returned
    anywhere (CONTRACT §16 stores no synchronous text), while `usage` still
    reports what the engine generated — a failed answer is retried, not
    salvaged.
    """
    failure = errors.from_unexpected(exc, request_id=request_id)
    usage: Optional[Dict[str, Any]] = None
    ceiling: Optional[int] = spec.planned
    if outcome is not None:
        usage = outcome.usage
        ceiling = outcome.max_output_tokens or spec.planned
    if not chat:
        return models.Response(
            id=spec.response_id,
            created_at=spec.created_at,
            status="failed",
            model=spec.model,
            output=[],
            usage=models.Usage.from_llm(usage),
            max_output_tokens=ceiling,
            error=models.ResponseError(code=failure.code, message=failure.message),
        ).to_wire()
    counted = models.Usage.from_llm(usage)
    return errors.chat_completion_failure_body(
        failure,
        completion_id=_completion_id(spec.response_id),
        created=spec.created_at,
        model=spec.model,
        usage=(
            None
            if counted is None
            else {
                "prompt_tokens": counted.input_tokens,
                "completion_tokens": counted.output_tokens,
                "total_tokens": counted.total_tokens,
            }
        ),
        max_output_tokens=ceiling,
        request_id=request_id,
    )


def _plan_gates(plan: planning.GenerationPlan) -> List[str]:
    """Every public capacity gate one router generation holds, in the order
    they are taken.

    WHY NOT JUST `plan.gate_engine` (adversarial review of T3-wire,
    2026-09-14, high). A normal-size techsara-35b answer plans no gate engine,
    so the router took no gate at all, and its only wait was inside
    `llm.stream_chat_events`: the NORMAL admission lane, bounded by
    ADMISSION_NORMAL_WAIT_S (600 s), which then failed the generation — a
    clock ending a public wait. It also skipped T2's `main.normal` gate, the
    6-of-10 NORMAL-slot cap that keeps a public flood from crowding chat out
    (design capacity_waits). `capacity.gates_for` is T2's single answer to
    "which gates, in which order" (the durable runner asks it too), so the
    router asks it rather than re-deriving it.
    """
    # Assembler, 2026-09-14: the pre-T2 fallback (the plan's own gate only)
    # is gone; T2's capacity.py is in the tree.
    return list(capacity.gates_for(plan.engine, plan.gate_engine))


#: PUBLIC_API_GATE_MAX_WAITERS when it is unset. See `gate_max_waiters`.
DEFAULT_GATE_MAX_WAITERS = 10_000

#: What a caller refused by the waiter bound is told to wait: the fd guard's
#: number (T1 resources), inside both SDKs' honoured Retry-After caps.
GATE_CROWDED_RETRY_AFTER_S = 30


def gate_max_waiters() -> int:
    """PUBLIC_API_GATE_MAX_WAITERS (10,000; 0 or less switches the bound off):
    how many /v1 generations may already be waiting for ONE public gate before
    the next one is refused, before its status line, with 503
    `model_unavailable` and Retry-After.

    A PHYSICAL GUARD, NOT A LIMIT (design capacity_waits PHYSICAL GUARDS;
    adversarial review of T3-wire, 2026-09-14). No wait ends on a clock, and
    usage limits are off by default, so nothing else bounds how many requests
    sit in a line. Each waiter costs a socket, a task and a wake-up per
    heartbeat on the event loop chat shares; the fd guard (70% of the soft
    limit) trips only in the hundreds of thousands. Measured on this code
    (T3-wire hand-over, 2026-09-14; waiters started together, so their 14 s
    heartbeats coincide): 10,000 waiting generations on one gate gave a worst
    event-loop lag of 24-33 ms on today's capacity.py and 86-89 ms on T2's,
    2% CPU, +127-138 MiB RSS; 20,000 gave 217-233 ms and +255-280 MiB.
    Hence 10,000."""
    return registry.setting_int("PUBLIC_API_GATE_MAX_WAITERS", DEFAULT_GATE_MAX_WAITERS)


class _Place:
    """One request's place in the lines of its gates, until it is admitted
    to all of them or gives up. `release` is idempotent."""

    __slots__ = ("_line", "_gates", "_held")

    def __init__(self, line: "_GateLine", gates: Tuple[str, ...]) -> None:
        self._line = line
        self._gates = gates
        self._held = True

    def release(self) -> None:
        if not self._held:
            return
        self._held = False
        for gate in self._gates:
            left = self._line.waiting.get(gate, 0) - 1
            if left > 0:
                self._line.waiting[gate] = left
            else:
                self._line.waiting.pop(gate, None)


class _GateLine:
    """How many router generations wait for each public gate (O(1) per
    request: capacity's own counts walk every waiter).

    `reserve` checks and counts in one step with no await in between, so a
    burst arriving in the same tick cannot all pass the check before any of
    them is counted."""

    def __init__(self) -> None:
        self.waiting: Dict[str, int] = {}

    def reserve(self, gates: List[str]) -> _Place:
        unique = tuple(dict.fromkeys(gates))
        limit = gate_max_waiters()
        if limit > 0:
            for gate in unique:
                if self.waiting.get(gate, 0) >= limit:
                    log.warning(
                        "refusing a /v1 generation: %d requests already wait for the %s gate "
                        "(PUBLIC_API_GATE_MAX_WAITERS)",
                        self.waiting.get(gate, 0), gate,
                    )
                    raise errors.model_at_capacity(retry_after=GATE_CROWDED_RETRY_AFTER_S)
        for gate in unique:
            self.waiting[gate] = self.waiting.get(gate, 0) + 1
        return _Place(self, unique)

    def reset_for_tests(self) -> None:
        self.waiting.clear()


_GATE_LINE = _GateLine()


@contextlib.contextmanager
def patient_admission() -> Any:
    """Run the enclosed engine call as a PATIENT admission waiter.

    WHY (adversarial review of T3-wire, 2026-09-14, high). Inside
    `llm.stream_chat_events` a public generation waits for a slot in the
    admission lanes it shares with chat, and a non-patient waiter gives up
    after ADMISSION_NORMAL_WAIT_S (600 s) — a failed generation, ended by a
    clock. T1's admission.py keeps a patient line (no time limit, outside
    chat's waiting-depth bound, served after chat) chosen by a ContextVar.
    Set here, around the call, it reaches the generation's producer task:
    asyncio copies the context when `Generation.stream()` creates it. The
    durable runner passes `admission_patient=True` instead; streaming.py's
    functions the router calls take no such argument.
    """
    # Assembler, 2026-09-14: the pre-T1 no-op fallback is gone.
    with admission.as_patient(True):
        yield


@contextlib.asynccontextmanager
async def _hold_patiently(gate: str, plan: planning.GenerationPlan) -> AsyncIterator[None]:
    """One public gate, held without a deadline: T2's `capacity.hold` with
    `wait_s=None`. Assembler, 2026-09-14: the pre-T2 re-queue shim (one week
    per finite hold) is gone with the capacity.py that needed it."""
    async with capacity.hold(
        gate,
        weight_tokens=plan.gate_weight_tokens,
        wait_s=None,
        yield_to_chat=plan.yield_to_chat,
        work=plan,
    ):
        yield


@contextlib.asynccontextmanager
async def _patient_gate(
    plan: planning.GenerationPlan, *, place: Optional[_Place] = None
) -> AsyncIterator[None]:
    """Every gate of `_plan_gates(plan)`, in order, waited on WITHOUT a
    deadline.

    No capacity wait on /v1 ends because of the clock (no-timeout design,
    capacity_waits RULE): a caller waits for as long as the engine is busy,
    and the byte invariant — `: queued` comments in a stream, whitespace in a
    committed body — keeps every hop in between from mistaking the wait for a
    dead connection. It ends on admission, on the client leaving (the task is
    cancelled, and `capacity.hold` gives a cancelled waiter's place back), or
    on a failure that is not "busy".

    `place` is the request's place in the lines, reserved before its status
    line (`_SlotStream`); without one, it is reserved here — for a
    synchronous call that is inside the commit window, so the waiter bound's
    refusal is still a real 503. Released once every gate is held.
    """
    gates = _plan_gates(plan)
    if not gates:
        yield
        return
    if place is None:
        place = _GATE_LINE.reserve(gates)
    try:
        async with contextlib.AsyncExitStack() as stack:
            for gate in gates:
                await stack.enter_async_context(_hold_patiently(gate, plan))
            place.release()
            yield
    finally:
        place.release()


#: Public for the callers outside this file that wait for a generation's
#: gates the router's way (the Files hookup's streams, T4): the same gates,
#: the same order, the same waiter bound.
patient_gate = _patient_gate
plan_gates = _plan_gates


#: What the Stainless SDKs (openai-python, openai-node) send: which attempt
#: of one call this is (0 first), and — only when a per-request timeout was
#: passed — that timeout in seconds. Read as numbers; never logged verbatim.
SDK_RETRY_COUNT_HEADER = "x-stainless-retry-count"
SDK_TIMEOUT_HEADER = "x-stainless-timeout"


def _header_number(request: Request, name: str) -> Optional[float]:
    raw = request.headers.get(name)
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value >= 0 and value == value and value != float("inf") else None


def _note_client_gone(request: Request, waited_s: float) -> None:
    """Say so when a synchronous generation loses its client, and louder when
    the client was an SDK retry.

    WHY (adversarial review of T3-wire, 2026-09-14). The committed response's
    whitespace keeps a READ timeout alive (openai-python), but openai-node 7.x
    counts its `timeout` (600 s by default) across the whole response: a
    synchronous answer longer than that fails however many heartbeats arrive,
    and the SDK retries twice — each retry a new generation, because nothing
    ties it to the first until implicit attach (T3's durable work) exists.
    Measured, scaled (timeout 3 s, an 8 s answer): openai-node 7.15.0 failed
    with APIConnectionTimeoutError after 10.3 s and started the engine 3
    times; 6.49.0 and openai-python 3.13.0 completed with 1 start each. The
    fix for the caller is to stream, use background, or raise `timeout`; this
    line and `public_api_sync_client_gone_total{attempt}` make the waste
    visible to the operator meanwhile. `X-Stainless-Timeout` is quoted only
    when present: openai-node 7.15.0 sent none for a client-level timeout."""
    retry = _header_number(request, SDK_RETRY_COUNT_HEADER)
    declared = _header_number(request, SDK_TIMEOUT_HEADER)
    attempt = "first" if not retry else "retry"
    log.log(
        logging.WARNING if retry else logging.INFO,
        "a synchronous /v1 generation (%s) lost its client after %.0f s (SDK attempt %s%s); "
        "a retry starts a new generation — answers longer than a client's total timeout "
        "should stream or use background",
        _request_id(request),
        waited_s,
        "?" if retry is None else int(retry),
        "" if declared is None else f", declared timeout {declared:.0f} s",
    )
    with contextlib.suppress(Exception):
        from .. import metrics

        metrics.inc(
            "public_api_sync_client_gone_total",
            "synchronous /v1 generations whose client disconnected before the answer",
            attempt=attempt,
        )


def _chat_body(
    *,
    completion_id: str,
    created: int,
    model: str,
    content: Optional[str],
    finish_reason: str,
    usage: Optional[models.Usage],
    max_output_tokens: Optional[int] = None,
) -> Dict[str, Any]:
    body = {
        "id": completion_id,
        "object": "chat.completion",
        "created": int(created),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
        # Null, never zero, for the same reason as everywhere else: a proxy
        # doing cost accounting would book the zero (CONTRACT §9).
        "usage": (
            None
            if usage is None
            else {
                "prompt_tokens": usage.input_tokens,
                "completion_tokens": usage.output_tokens,
                "total_tokens": usage.total_tokens,
            }
        ),
    }
    # A top-level EXTENSION (2026-09-13): the output ceiling actually applied,
    # which the window may have clamped below the `max_tokens` sent.
    # OpenAI-derived clients ignore a key they do not know.
    body["max_output_tokens"] = max_output_tokens
    return body


@router.get("/usage", operation_id="getUsage")
async def get_usage(request: Request, caller: ApiCaller = Depends(resolve_caller)) -> Response:
    """This project's own daily counters, over a BOUNDED range (CONTRACT §7)."""
    await _authorize(request, caller, "get_usage")
    await _admit(request, caller, kind=KIND_READ)
    start, end = _usage_range(request)
    project_id = caller.project_id
    try:
        rows = await db.run_in_thread(
            db.read_usage_daily, project_id, start, end, max_days=MAX_USAGE_DAYS
        )
    except ValueError as exc:
        raise errors.invalid_request(str(exc), param="start_date") from None
    body: Dict[str, Any] = {
            "object": "list",
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "data": [
                {
                    "date": str(row.get("day")),
                    "requests": int(row.get("requests") or 0),
                    "input_tokens": int(row.get("input_tokens") or 0),
                    "output_tokens": int(row.get("output_tokens") or 0),
                    "errors": int(row.get("errors") or 0),
                    "rate_limited": int(row.get("rate_limited") or 0),
                }
                for row in rows
            ],
        }
    # Files design §2.16 / §11 (files-hookup, 2026-09-13): storage is ACCOUNTED,
    # not metered — files, stored bytes (distinct blobs with a live file),
    # derived bytes and uploads in progress. Omitted, never zeroed, when the
    # Files API is not mounted or the read fails: a 0 would be a claim.
    storage = await _files_storage(project_id)
    if storage is not None:
        body["storage"] = storage
    return JSONResponse(body)


async def _files_storage(project_id: str) -> Optional[Dict[str, Any]]:
    if not FILES_MOUNTED:
        return None
    try:
        from ..apifiles import accounting as _files_accounting

        return dict(await _files_accounting.project_storage(project_id))
    except Exception:  # noqa: BLE001 - usage must answer without the files half
        log.warning("files storage for /v1/usage was not read", exc_info=True)
        return None


@router.get("/openapi.json", operation_id="getOpenapi")
async def get_openapi() -> Response:
    """The public schema, and only the public schema (CONTRACT §7).

    No credential: a developer must be able to point a client generator at it
    before they have a key, and it describes nothing a key would not already
    tell them. It is built by `openapi.py` from the same route table this file
    declares, never from `app.openapi()`, which would publish every internal
    chat, admin and analytics route.
    """
    return JSONResponse(openapi_module.public_openapi())


@router.api_route(
    "",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"],
    include_in_schema=False,
)
async def bare_root(request: Request) -> Response:
    """`/v1` with nothing after it: the CONTRACT §9 404, never a redirect.

    ADDED 2026-09-13 (the end-to-end run). No route matched `/v1` exactly, but
    the preflight catch-all below matches `/v1/`, so Starlette's
    `redirect_slashes` answered `GET /v1` with `307 Location: …/v1/`. A
    redirect is the wrong answer on a JSON API — clients do not expect one, it
    carries a Location built from the Host the caller used — and the public
    edge (frontend/app/v1) correctly refuses to follow an upstream redirect,
    so through the front door the caller got a 500. There is nothing at the
    root of the API; say so in the one shape a client can parse.
    """
    raise _unrouted_error(404, request.method)


@router.options("/{rest:path}", include_in_schema=False)
async def preflight(rest: str, request: Request) -> Response:
    """CONTRACT §3.

    Permissive BY DESIGN and only here: a preflight carries no credential and
    reveals nothing, and refusing one would make every browser-based client
    fail before its key was ever read. The ACTUAL request is what
    `_authorize()` checks against the project's `allowed_origins`.

    `Access-Control-Allow-Credentials` is absent, which is what makes the
    permissiveness safe: a browser that cannot attach a cookie cannot be used
    to drive this surface with somebody's session.

    ALSO THE `/v1` CATCH-ALL. Its path matches every `/v1/…` request, so for
    any other method it is the partial match Starlette falls back to when no
    route matches fully — and `PublicRoute.handle` answers that with the
    CONTRACT §9 404 rather than FastAPI's `{"detail": …}`.
    """
    origin = request.headers.get("origin") or "*"
    headers = dict(PREFLIGHT_HEADERS)
    headers["Access-Control-Allow-Origin"] = origin
    headers["Vary"] = "Origin"
    headers["Access-Control-Expose-Headers"] = _EXPOSE_HEADERS
    return Response(status_code=204, headers=headers)


# ------------------------------------------------------ route plumbing --


async def _shielded(awaitable: Awaitable[Any], what: str) -> None:
    """Run cleanup to completion even if the caller is being cancelled.

    A disconnect cancels the task that is streaming; the row write and the
    generator close that follow must not be cancelled with it, or the request
    is left `queued` forever and its engine generator open until GC.
    """
    task = asyncio.ensure_future(awaitable)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - cleanup must never mask the response
        log.warning("public API cleanup failed: %s", what, exc_info=True)


class _SlotStream(StreamingResponse):
    """An SSE response that takes its concurrency slot IN THE CODE THAT RELEASES IT,
    sends its first frame at once, and waits for capacity in the body.

    THE LEAK THIS REPLACED (verifier finding, 2026-09-13). The slot used to be
    taken in the handler and released in the `finally` of the body generator.
    A client that sends the POST and resets the connection before the body's
    first step leaves that generator unstarted — and an unstarted generator's
    `finally` never runs, so the slot was never given back. Now `__call__` —
    the one method Starlette always awaits once the handler has returned this
    object — holds the slot in a `with` around the whole response: taken
    before the status line (a refusal is still the documented 429 with
    `Retry-After`), released however the response ends.

    THE BYTE INVARIANT (no-timeout design, 2026-09-13). Until then the
    engine's capacity gate was taken here too, BEFORE the status line, for up
    to PUBLIC_API_GATE_WAIT_S of silence — and then a 503. Now the status line
    and a first frame leave immediately (`response.created`, or `: ping` for
    the compatibility dialect, whose chunks have no opening event), and the
    gate is waited for INSIDE the body with a `: queued` comment at least every
    `events.HEARTBEAT_SECONDS`, with no deadline. The engine is not asked for
    anything until the gate is held: the generation's own generator is only
    advanced past its opener afterwards.

    A tagged request from the gateway also gets `: ts-seq=N` after every data
    frame (`gateway_protocol.tag_frames`), in the same write as the frame.

    The same method closes the body generator explicitly (a started one gets
    its `finally`, which closes the engine stream and records the outcome) and,
    for one that never started, records the request as abandoned so its row
    does not sit at `queued` for ever.
    """

    def __init__(
        self,
        frames: AsyncIterator[str],
        *,
        caller: ApiCaller,
        request: Request,
        prepare: Callable[[], Awaitable[None]],
        on_refused: Callable[[], Awaitable[None]],
        on_abandoned: Callable[[], Awaitable[None]],
        kind: str = KIND_STREAM,
        capacity_for: Optional[Callable[[], Any]] = None,
        opener: str = OPEN_WITH_FIRST_FRAME,
        refusal_frames: Optional[Callable[[errors.ApiError], List[str]]] = None,
        gate_failure: Optional["_GateFailure"] = None,
        tag: gateway_protocol.GatewayTag = gateway_protocol.UNTAGGED,
        heartbeat_s: Optional[float] = None,
        reserve_place: Optional[Callable[[], _Place]] = None,
        patient: bool = False,
    ) -> None:
        super().__init__(frames, media_type="text/event-stream", headers=streaming.SSE_HEADERS)
        self._frames = frames
        self._caller = caller
        self._request = request
        self._prepare = prepare
        self._on_refused = on_refused
        self._on_abandoned = on_abandoned
        self._kind = kind
        #: The engine's capacity gate: an async context manager factory,
        #: entered INSIDE the body after the opener (None: no public gate).
        self._capacity_for = capacity_for
        self._opener = opener
        self._refusal_frames = refusal_frames
        self._gate_failure = gate_failure
        self._heartbeat_s = heartbeat_s
        #: Takes this request's place in its gates' lines (the waiter bound)
        #: before the status line; `capacity_for` then waits from that place.
        self._reserve_place = reserve_place
        self._place: Optional[_Place] = None
        #: Whether the body runs under `patient_admission()`.
        self._patient = patient
        self.started = False
        body: AsyncIterator[str] = self._tracked()
        if tag.tagged:
            body = gateway_protocol.tag_frames(body)
        self.body_iterator = body

    def _beat_s(self) -> float:
        value = self._heartbeat_s if self._heartbeat_s is not None else events.HEARTBEAT_SECONDS
        return max(0.001, min(15.0, float(value)))

    async def _tracked(self) -> AsyncIterator[str]:
        self.started = True
        factory = self._capacity_for
        if factory is not None and self._place is not None:
            factory = functools.partial(factory, place=self._place)
        holder = _GateHolder(factory) if factory is not None else None
        #: Whether the generation's own generator has been advanced. A chat
        #: stream opens with our comment and only starts it after the gate, so
        #: a wait that fails, or a client that leaves while waiting, ends with
        #: that generator UNSTARTED — and an unstarted generator's `finally`
        #: (which records the outcome) never runs.
        frames_started = False
        try:
            if self._opener == OPEN_WITH_FIRST_FRAME:
                frames_started = True
                try:
                    opening = await self._frames.__anext__()
                except StopAsyncIteration:
                    return
                yield opening
            else:
                yield events.SequencedEvents().heartbeat()
            if holder is not None:
                queued_from = time.monotonic()
                try:
                    async with contextlib.aclosing(holder.wait(self._beat_s())) as ticks:
                        async for _tick in ticks:
                            yield events.queued_comment()
                    if self._gate_failure is not None and frames_started:
                        self._gate_failure.queued_s = time.monotonic() - queued_from
                except errors.ApiError as failure:
                    # A capacity wait that failed for a reason other than
                    # "busy" (which never ends it). The status line is long
                    # gone, so the failure is the stream's terminal frame.
                    if self._gate_failure is not None:
                        self._gate_failure.error = failure
                    for frame in (self._refusal_frames(failure) if self._refusal_frames else ()):
                        yield frame
                    return
            if self._gate_failure is not None:
                self._gate_failure.admitted = True
            frames_started = True
            # The generation's producer task is created inside this block, so
            # it uses the ticket the gate holder's task was given (one
            # accounting of long public work, PR #65).
            with admission.adopt_preadmission(holder.carried if holder is not None else None):
                async for frame in self._frames:
                    yield frame
        finally:
            # `async for` has no teardown of its own: closing THIS generator
            # does not close the one it iterates, and the engine stream inside
            # that one would stay open until garbage collection. Closed BEFORE
            # the gate is given back, so the next holder never overlaps a
            # stream that is still open on the engine.
            try:
                if frames_started:
                    await self._frames.aclose()
                else:
                    await _shielded(self._on_abandoned(), "record a stream that never generated")
            finally:
                if holder is not None:
                    await holder.release()

    def _refusal(self, failure: errors.ApiError) -> JSONResponse:
        request_id = _request_id(self._request) or _new_request_id()
        response = _error_body(failure, request_id)
        _decorate(self._request, response, request_id)
        return response

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        admitted = False
        try:
            with quotas.concurrency_slot(self._caller, self._kind):
                admitted = True
                try:
                    if self._reserve_place is not None:
                        self._place = self._reserve_place()
                    await self._prepare()
                except Exception as exc:  # noqa: BLE001 - still before the status line
                    self._release_place()
                    await _shielded(self._on_refused(), "release the idempotency key")
                    failure = exc if isinstance(exc, errors.ApiError) else errors.from_unexpected(exc)
                    await self._refusal(failure)(scope, receive, send)
                    return
                try:
                    with patient_admission() if self._patient else contextlib.nullcontext():
                        await super().__call__(scope, receive, send)
                finally:
                    await _shielded(self._close(), "close the stream")
                    self._release_place()
        except errors.ApiError as refusal:
            if admitted:
                raise
            await _shielded(self._on_refused(), "release the idempotency key")
            await self._refusal(refusal)(scope, receive, send)

    def _release_place(self) -> None:
        if self._place is not None:
            self._place.release()

    async def _close(self) -> None:
        with contextlib.suppress(Exception):
            await self.body_iterator.aclose()
        if not self.started:
            with contextlib.suppress(Exception):
                await self._frames.aclose()
            await self._on_abandoned()


class _GateHolder:
    """Hold a capacity gate from a helper task while a stream keeps talking.

    WHY A TASK. `async with gate:` inside the body generator would block the
    generator for the whole wait, and a blocked generator cannot yield the
    `: queued` comment that keeps every proxy on the path from reading the
    wait as a dead connection. So the gate is entered by a task that signals
    admission and then parks until `release()`; the body waits for that signal
    one heartbeat at a time.

    Released on every path: admitted and finished, cancelled while waiting
    (the body was closed — `capacity.hold` gives a cancelled waiter's place
    back), or failed.
    """

    def __init__(self, factory: Callable[[], Any]) -> None:
        self._factory = factory
        self.carried: Any = None
        self._admitted = asyncio.Event()
        self._done = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    async def _hold(self) -> None:
        async with self._factory():
            #: The pre-admission `capacity.hold` made in THIS task's context,
            #: for the body to adopt (admission.preadmission_carry: without it
            #: a long answer was admitted twice).
            self.carried = admission.preadmission_carry()
            self._admitted.set()
            await self._done.wait()

    async def wait(self, heartbeat_s: float) -> AsyncIterator[None]:
        """Yield once per heartbeat until admitted; raise what the gate raised."""
        self._task = asyncio.ensure_future(self._hold())
        while not self._admitted.is_set():
            signal = asyncio.ensure_future(self._admitted.wait())
            try:
                await asyncio.wait({self._task, signal}, timeout=heartbeat_s, return_when=asyncio.FIRST_COMPLETED)
            finally:
                signal.cancel()
            if self._admitted.is_set():
                return
            if self._task.done():
                failure = None if self._task.cancelled() else self._task.exception()
                if isinstance(failure, errors.ApiError):
                    raise failure
                raise errors.model_unavailable(retry_after=streaming.UNAVAILABLE_RETRY_AFTER)
            yield None

    async def release(self) -> None:
        self._done.set()
        task = self._task
        if task is None:
            return
        if not self._admitted.is_set() and not task.done():
            task.cancel()
        await asyncio.wait({task})
        if not task.cancelled() and task.exception() is not None and self._admitted.is_set():
            log.warning("releasing a public capacity gate failed", exc_info=task.exception())


async def _record_abandoned(spec: streaming.GenerationSpec, on_finish: streaming.OnFinish) -> None:
    """The outcome of a stream whose client left before its body began.

    Nothing was generated and nothing was sent, so `cancelled` — the caller
    stopped it — with no usage (None: not measured, never 0).
    """
    outcome = streaming.StreamOutcome(
        response_id=spec.response_id,
        model=spec.model,
        created_at=spec.created_at,
        status="cancelled",
        client_gone=True,
        duration_ms=0,
    )
    await on_finish(outcome)


async def _response_row(caller: ApiCaller, response_id: str) -> Dict[str, Any]:
    project_id = caller.project_id
    row = await db.run_in_thread(db.get_api_response, response_id, project_id)
    if row is None:
        raise errors.response_not_found(response_id)
    return row


def _row_to_wire(row: Mapping[str, Any]) -> Dict[str, Any]:
    """An `api_responses` row → the CONTRACT §9 body.

    `output_text` is only ever populated for a background response (SCHEMA-V34)
    — a synchronous answer is not stored, per CONTRACT §16 — so a completed
    synchronous response reads back with its status, its counts and an empty
    `output`. That is the honest shape: we did not keep the text, and an empty
    list says so where a fabricated one would not.
    """
    text = str(row.get("output_text") or "")
    error = None
    if row.get("error_code"):
        error = models.ResponseError(
            code=str(row["error_code"]), message=str(row.get("error_message") or "")
        )
    status = str(row.get("status") or "queued")
    stored_ceiling = row.get("max_output_tokens")
    return models.Response(
        id=str(row["id"]),
        created_at=_row_created(row),
        status=status,  # type: ignore[arg-type]
        model=str(row.get("model") or ""),
        output=[models.OutputMessage.of(text)] if text else [],
        usage=_row_usage(row),
        # V35: null only for a row written before the column existed.
        max_output_tokens=(int(stored_ceiling) if stored_ceiling else None),
        incomplete_details=(
            models.IncompleteDetails.for_finish(row.get("finish_reason"))
            if status == "completed"
            else None
        ),
        error=error,
    ).to_wire()


def _row_created(row: Mapping[str, Any]) -> int:
    # `db` hands temporal columns back as ISO strings; until 2026-09-13 this
    # asked the string for `.timestamp()` and every stored response read back
    # with `created_at: 0`.
    created = background.as_datetime(row.get("created_at"))
    return int(created.timestamp()) if created is not None else 0


def _row_usage(row: Mapping[str, Any]) -> Optional[models.Usage]:
    if row.get("input_tokens") is None and row.get("output_tokens") is None:
        return None
    prompt = int(row.get("input_tokens") or 0)
    completion = int(row.get("output_tokens") or 0)
    return models.Usage(
        input_tokens=prompt, output_tokens=completion, total_tokens=prompt + completion
    )


def _usage_range(request: Request) -> Tuple[date, date]:
    """`start_date`/`end_date`, ISO, inclusive, bounded (CONTRACT §7, §12)."""
    today = date.today()
    end = _parse_day(request.query_params.get("end_date"), today, "end_date")
    default_start = end - timedelta(days=DEFAULT_USAGE_DAYS - 1)
    start = _parse_day(request.query_params.get("start_date"), default_start, "start_date")
    if end < start:
        raise errors.invalid_request(
            "end_date must not be before start_date.", param="end_date"
        )
    if (end - start).days + 1 > MAX_USAGE_DAYS:
        raise errors.invalid_request(
            f"The range must be at most {MAX_USAGE_DAYS} days.", param="start_date"
        )
    return start, end


def _parse_day(value: Optional[str], fallback: date, param: str) -> date:
    if value is None or not value.strip():
        return fallback
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise errors.invalid_request(
            f"{param} must be an ISO date, YYYY-MM-DD.", param=param
        ) from None


# -------------------------------------------------------- idempotency --


async def _claim_idempotency(
    request: Request, caller: ApiCaller, endpoint: str, payload: Any
) -> Optional[idempotency.Claim]:
    """CONTRACT §13, the claim. None when the header is absent.

    Through `apiplatform/idempotency.py`, whose `claim` is `INSERT … ON
    CONFLICT DO NOTHING RETURNING` plus the two cases a bare insert gets wrong:
    a key used with a different body (409), and a key whose original attempt
    FAILED and left nothing to replay (claimed again, so the retry runs).
    """
    raw = request.headers.get("idempotency-key")
    try:
        key = idempotency.normalise_key(raw)
    except idempotency.IdempotencyKeyError as exc:
        raise errors.invalid_request(str(exc), param="Idempotency-Key") from None
    if key is None:
        return None
    return await db.run_in_thread(
        idempotency.claim, caller.project_id, endpoint, key, payload
    )


async def _finish_claim(held: Optional[idempotency.Claim], response_id: Optional[str]) -> None:
    if held is None or not held.claimed:
        return
    try:
        await db.run_in_thread(idempotency.finish, held, response_id)
    except Exception:  # noqa: BLE001
        log.warning("an idempotency claim could not be finished", exc_info=True)


async def _nothing_ran(
    caller: ApiCaller, reservation: Any, held: Optional[idempotency.Claim]
) -> None:
    """Undo what admission reserved for a request that never reached the engine.

    `quotas.reserve` adds the ESTIMATED input tokens to the minute window when
    it admits a generation, and only `record_usage` with that reservation
    takes them back out. A request refused after the gate — an unknown model,
    a concurrency 429, a replay — would otherwise keep a prompt's worth of
    input-TPM budget spent for the rest of the minute. The request itself
    stays counted: it was a request. `cancelled` because nothing ran, which
    also keeps it out of the error-rate figure.
    """
    await _finish_claim(held, None)
    if reservation is None or not int(getattr(reservation, "reserved_input_tokens", 0) or 0):
        return
    try:
        await db.run_in_thread(
            functools.partial(
                quotas.record_usage, caller, 0, 0, "cancelled", reservation=reservation
            )
        )
    except Exception:  # noqa: BLE001
        log.warning("an unused input-token reservation was not returned", exc_info=True)


def _still_running() -> errors.ApiError:
    # The first attempt is still running. Telling the caller to come back is
    # honest and cheap; running the model a second time is neither. A 409
    # (2026-09-13): as a 429 `rate_limit_error` it named a rate limit the API
    # no longer has.
    return errors.idempotency_in_progress(retry_after=2)


def _row_error(row: Mapping[str, Any]) -> errors.ApiError:
    """The stored failure of a replayed request, as the error it was."""
    code = str(row.get("error_code") or "internal_error")
    message = str(row.get("error_message") or "The original request failed.")
    try:
        status = errors.status_for(code)
    except ValueError:
        return errors.internal_error()
    retry_after = 1 if status in (429, 503) else None
    return errors.ApiError(code, message, retry_after=retry_after)


async def _replay(
    caller: ApiCaller,
    held: idempotency.Claim,
    request_model: models.ResponsesRequest,
    *,
    chat: bool,
    include_usage: bool,
) -> Response:
    """CONTRACT §13: the ORIGINAL response, in the original's shape.

    Until 2026-09-13 a replay on `/v1/chat/completions` came back in the
    Responses envelope, which breaks every OpenAI-derived client on exactly
    the retry idempotency exists for, and a replay of a streamed request came
    back as JSON (verifier finding). Now: the same dialect, the same
    completion id (derived from the response id), and SSE for a stream.

    The generated text of a synchronous or streamed request is not kept
    (CONTRACT §16), so a replay carries the status, the usage and the finish,
    and `content: null` / no delta rather than a fabricated answer.
    """
    if not held.response_id:
        raise _still_running()
    row = await _response_row(caller, held.response_id)
    status = str(row.get("status") or "")
    if row.get("background"):
        return JSONResponse(status_code=202, content=_row_to_wire(row))
    if status not in ("completed", "failed", "cancelled"):
        raise _still_running()
    if status == "failed" and not request_model.stream:
        raise _row_error(row)
    if status == "cancelled" and request_model.stream:
        # No terminal event of CONTRACT §10 describes "cancelled"; the row
        # itself is the honest answer.
        return JSONResponse(_row_to_wire(row))
    text = str(row.get("output_text") or "") or None
    if not request_model.stream:
        if not chat:
            return JSONResponse(_row_to_wire(row))
        return JSONResponse(
            _chat_body(
                completion_id=_completion_id(str(row["id"])),
                created=_row_created(row),
                model=str(row.get("model") or ""),
                content=text,
                finish_reason=streaming.chat_finish_reason(row.get("finish_reason")),
                usage=_row_usage(row),
                max_output_tokens=(int(row["max_output_tokens"]) if row.get("max_output_tokens") else None),
            )
        )
    return StreamingResponse(
        _replay_frames(row, chat=chat, include_usage=include_usage),
        media_type="text/event-stream",
        headers=streaming.SSE_HEADERS,
    )


async def _replay_frames(
    row: Mapping[str, Any], *, chat: bool, include_usage: bool
) -> AsyncIterator[str]:
    wire = _row_to_wire(row)
    status = str(row.get("status") or "")
    if chat:
        chunks = events.ChatCompletionChunks(
            completion_id=_completion_id(str(row["id"])),
            model=str(row.get("model") or ""),
            created=_row_created(row),
            include_usage=include_usage,
        )
        if status == "failed":
            yield chunks.error_chunk(_row_error(row))
        else:
            yield chunks.stop(
                streaming.chat_finish_reason(row.get("finish_reason")),
                max_output_tokens=(int(row["max_output_tokens"]) if row.get("max_output_tokens") else None),
            )
            if include_usage:
                counted = _row_usage(row)
                yield chunks.usage_chunk(
                    None
                    if counted is None
                    else {
                        "prompt_tokens": counted.input_tokens,
                        "completion_tokens": counted.output_tokens,
                        "total_tokens": counted.total_tokens,
                    }
                )
        yield chunks.done()
        return
    emitter = events.SequencedEvents()
    opening = dict(wire, status="queued", usage=None, output=[], error=None)
    yield emitter.created(opening)
    if status == "completed":
        yield emitter.completed(wire)
    else:
        yield emitter.failed(wire)


# ------------------------------------------- the compatibility request --

#: The Chat Completions fields this platform honours. Everything else is a 400
#: naming the field, for the reason CONTRACT §8 gives: a sampling parameter
#: that is accepted and dropped cannot be detected by the caller, and an API
#: whose knobs might be decorative is an API nobody can build on.
_CHAT_FIELDS = (
    "model",
    "messages",
    "stream",
    "max_tokens",
    "max_completion_tokens",
    "temperature",
    "stream_options",
)


def _chat_content(content: Any, where: str) -> Any:
    """Chat Completions content → the Responses content the one validator reads.

    A string stays a string. A part list may hold `{"type": "text", "text"}`
    and `{"type": "image_url", "image_url": {"url", "detail"?}}` and nothing
    else; each becomes its Responses twin (`input_text` / `input_image`), so
    the data-URL rule, the size limit and the magic-byte check are applied by
    exactly one piece of code for both dialects.
    """
    if not isinstance(content, list):
        return content
    converted = []
    for index, part in enumerate(content):
        param = f"{where}.{index}"
        if not isinstance(part, Mapping):
            raise errors.invalid_request("Each content part must be an object.", param=param)
        kind = part.get("type")
        if kind == "text":
            unknown = sorted(key for key in part if key not in ("type", "text"))
            if unknown:
                raise errors.invalid_request(
                    f"Unsupported field in a text part: {unknown[0]}.", param=param
                )
            converted.append({"type": "input_text", "text": part.get("text")})
        elif kind == "image_url":
            unknown = sorted(key for key in part if key not in ("type", "image_url"))
            image = part.get("image_url")
            if unknown or not isinstance(image, Mapping):
                raise errors.invalid_request(
                    "An image_url part must be {\"type\": \"image_url\", "
                    "\"image_url\": {\"url\": \"data:…\"}}.",
                    param=param,
                )
            extra = sorted(key for key in image if key not in ("url", "detail"))
            if extra:
                raise errors.invalid_request(
                    f"Unsupported field in image_url: {extra[0]}.", param=f"{param}.image_url"
                )
            item: Dict[str, Any] = {"type": "input_image", "image_url": image.get("url")}
            if image.get("detail") is not None:
                item["detail"] = image.get("detail")
            converted.append(item)
        else:
            raise errors.invalid_request(
                "Each content part must have type text or image_url.", param=param
            )
    return converted


def _from_chat_completions(payload: Any) -> Tuple[models.ResponsesRequest, bool]:
    """The compatibility body → the one validated request type.

    `max_tokens` and its newer spelling `max_completion_tokens` both map to
    `max_output_tokens`, and sending both is a 400 rather than a guess at
    which one was meant; `stream_options.include_usage` is not a generation
    parameter at all but a framing one, so it is taken out here and handed to
    the chunk builder.
    """
    if not isinstance(payload, Mapping):
        raise errors.invalid_request("The request body must be a JSON object.")
    unknown = [key for key in payload if key not in _CHAT_FIELDS]
    if unknown:
        raise errors.invalid_request(
            f"Unsupported field: {sorted(unknown)[0]}.", param=sorted(unknown)[0]
        )
    options = payload.get("stream_options") or {}
    if not isinstance(options, Mapping):
        raise errors.invalid_request(
            "stream_options must be an object.", param="stream_options"
        )
    unknown_options = [key for key in options if key != "include_usage"]
    if unknown_options:
        raise errors.invalid_request(
            f"Unsupported stream option: {sorted(unknown_options)[0]}.",
            param="stream_options",
        )
    if payload.get("max_tokens") is not None and payload.get("max_completion_tokens") is not None:
        raise errors.invalid_request(
            "Send max_tokens or max_completion_tokens, not both.",
            param="max_completion_tokens",
        )
    messages = payload.get("messages")
    if isinstance(messages, list):
        messages = [
            (
                {**message, "content": _chat_content(message.get("content"), f"messages.{index}.content")}
                if isinstance(message, Mapping) and "content" in message
                else message
            )
            for index, message in enumerate(messages)
        ]
    body: Dict[str, Any] = {
        "model": payload.get("model"),
        "input": messages,
        "stream": bool(payload.get("stream", False)),
    }
    ceiling = payload.get("max_tokens")
    if ceiling is None:
        ceiling = payload.get("max_completion_tokens")
    if ceiling is not None:
        body["max_output_tokens"] = ceiling
    if payload.get("temperature") is not None:
        body["temperature"] = payload["temperature"]
    return models.parse_responses_request(body), bool(options.get("include_usage", False))


# ------------------------------------------------------- app-level wiring --


def install_error_handlers(app: Any) -> None:
    """Give the APPLICATION the same envelope for an `ApiError` raised outside
    a route — from a middleware, say.

    NOT what answers an unknown `/v1` path or a wrong method: that is
    `PublicRoute.handle` via the catch-all `preflight` route, which works
    whether or not this is installed. As of 2026-09-13 `app/main.py` does not
    call this (it belongs to another owner); nothing on `/v1` depends on it.
    """

    @app.exception_handler(errors.ApiError)
    async def _api_error(request: Request, exc: errors.ApiError) -> Response:  # noqa: ANN202
        request_id = _request_id(request) or _new_request_id()
        response = _error_body(exc, request_id)
        _decorate(request, response, request_id)
        return response


# ---------------------------------------------- helpers other files use --
#
# PUBLIC NAMES for the seams `publicapi/endpoints.py` builds on (2026-09-13):
# the same order and the same refusals as the routes above, rather than a copy
# of them in a second file that would drift.


async def admit(
    request: Request,
    caller: ApiCaller,
    *,
    kind: str,
    estimated_input_tokens: int = 0,
    max_output_tokens: Optional[int] = None,
):
    """`_admit`: count this request once and admit it."""
    return await _admit(
        request,
        caller,
        kind=kind,
        estimated_input_tokens=estimated_input_tokens,
        max_output_tokens=max_output_tokens,
    )


async def resolve_model(caller: ApiCaller, model_id: str) -> registry.PublicModel:
    """`_resolve_model`: the model for this request, or 404 — never 403."""
    return await _resolve_model(caller, model_id)


def request_id(request: Request) -> str:
    """The `X-Request-Id` this request carries."""
    return _request_id(request)


def error_response(exc: errors.ApiError, request_id: str) -> JSONResponse:
    """The CONTRACT §9 envelope for `exc`, with its Retry-After."""
    return _error_body(exc, request_id)


# ---------------------------------------------------- the endpoints hook --

#: Whether `publicapi/endpoints.py` added its routes, and why not when it did
#: not — so a test and an operator can ask instead of inferring it from a 404.
ENDPOINTS_MOUNTED = False
ENDPOINTS_MOUNT_ERROR: Optional[str] = None


def _register_endpoints() -> None:
    """Add `/v1/embeddings`, `/v1/rerank` and `/v1/audio/transcriptions`.

    LAST IN THE FILE, and guarded. Last because those routes build on the
    helpers above, and a module importing this one half-way through would
    read a half-built router. Guarded because several engineers share this
    tree (tests/test_publicapi_mount.py's "will not import" rule): a broken
    or half-written endpoints module logs and leaves the routes of this file
    serving, instead of taking `/v1` — or, through main.py's own guard, the
    whole developer platform — down with it.
    """
    global ENDPOINTS_MOUNTED, ENDPOINTS_MOUNT_ERROR
    try:
        from . import endpoints as _endpoints

        _endpoints.register(router)
    except Exception as exc:  # noqa: BLE001 - see the docstring
        ENDPOINTS_MOUNTED = False
        ENDPOINTS_MOUNT_ERROR = type(exc).__name__
        log.warning(
            "the /v1 embeddings, rerank and transcription routes are not mounted (%s)",
            type(exc).__name__,
            exc_info=True,
        )
        return
    ENDPOINTS_MOUNTED = True
    ENDPOINTS_MOUNT_ERROR = None


_register_endpoints()


# -------------------------------------------------------- the files hook --

#: Whether `publicapi/files/routes.py` added `/v1/files` and `/v1/uploads`,
#: and why not when it did not (files-hookup, 2026-09-13).
FILES_MOUNTED = False
FILES_MOUNT_ERROR: Optional[str] = None
#: The dependencies the routes were registered with — the developer console's
#: Files routes reuse them (`apiplatform/console_files.py`), so both surfaces
#: share one purge per blob and one processing view.
FILES_DEPENDENCIES: Any = None


def _register_files() -> None:
    """Add the fourteen file and upload routes (Files design §2.1).

    Guarded and last, like `_register_endpoints` and for the same reasons. The
    dependencies are the platform's own — `router_dependencies()` wires this
    file's `resolve_caller`, `authorize_scope` (with `files.read` /
    `files.write`), `admit` and the usage ledger — plus the processing half:
    the File object's `processing` view, derived data and the events stream
    (routes 6–8 exist only when these are given), and the purge that stops
    processing before bytes go. `assembler` wakes the jobs' cpu lane, which
    runs assembly in the same slots as extraction; it is a kick, not a second
    runner (a separate `AssembleRunner` would add slots beyond
    PUBLIC_API_FILES_CPU_JOBS).

    Mounting the routes does not start processing: the lifespan starts
    `apifiles.jobs`, `retention` and `uploads_sweep` (`app/main.py`).
    """
    global FILES_MOUNTED, FILES_MOUNT_ERROR, FILES_DEPENDENCIES
    try:
        import dataclasses
        import types

        from ..apifiles import derived as _files_derived
        from ..apifiles import events as _files_events
        from ..apifiles import jobs as _files_jobs
        from ..apifiles import retention as _files_retention
        from .files import routes as _files_routes

        if FILES_DEPENDENCIES is None:
            # Once per process: registration is idempotent, and a second call
            # must keep the dependencies (and in-flight purges) the routes
            # were registered with.
            FILES_DEPENDENCIES = dataclasses.replace(
                _files_routes.router_dependencies(),
                processing_view=_files_jobs.file_processing_view,
                derived=_files_derived,
                events=_files_events.stream_file_events,
                purge_blob=_files_retention.purge_blob,
                assembler=types.SimpleNamespace(kick=lambda: _files_jobs.enqueue("assemble")),
            )
        _files_routes.register(router, FILES_DEPENDENCIES)
    except Exception as exc:  # noqa: BLE001 - see the docstring
        FILES_MOUNTED = False
        FILES_MOUNT_ERROR = type(exc).__name__
        log.warning(
            "the /v1 files and uploads routes are not mounted (%s)",
            type(exc).__name__,
            exc_info=True,
        )
        return
    FILES_MOUNTED = True
    FILES_MOUNT_ERROR = None


_register_files()
