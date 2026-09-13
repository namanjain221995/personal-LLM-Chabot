"""`/v1` — the eight public endpoints of CONTRACT §7.

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
name one request; the CONTRACT §12 `RateLimit` headers; `Retry-After` on every
429 and 503; and the CONTRACT §9 envelope on every failure, including the ones
raised inside a streaming body.

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
    Mapping,
    Optional,
    Tuple,
)

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.routing import APIRoute

from .. import context, db, usage as usage_ledger
from ..apiplatform import idempotency, quotas, resolver
from ..apiplatform.resolver import ApiCaller
from ..apiplatform.scopes import InsufficientScopeError, Scope, requires
from . import (
    background,
    errors,
    events,
    models,
    openapi as openapi_module,
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
PREFLIGHT_HEADERS: Dict[str, str] = {
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "authorization, content-type, idempotency-key",
    "Access-Control-Max-Age": "600",
}

#: Headers a browser client is allowed to READ off a cross-origin response.
#: Without this a developer's browser app can see the status code and nothing
#: else — not the request id it needs to quote in a support ticket, not the
#: `Retry-After` its own retry logic depends on.
_EXPOSE_HEADERS = "X-Request-Id, RateLimit, RateLimit-Policy, Retry-After"

#: CONTRACT §12: "8,192 default, model ceiling max | clamped".
DEFAULT_MAX_OUTPUT_TOKENS = 8192

#: CONTRACT §8's default when the caller does not ask for one. 0.2 is what the
#: chat application asks for, and an API that answered a repeated question
#: differently from the product would be the more surprising choice.
DEFAULT_TEMPERATURE = 0.2

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
    """Scope, then origin. In that order, and both before any work.

    The scope answers "may this credential call this endpoint at all"; the
    origin answers "may this BROWSER use this credential from where it is".
    A key used from a server sends no `Origin` and is unaffected — the check
    applies only when the request carries one, because that is the only case
    where the answer is a browser's to enforce.
    """
    SCOPES[operation].check(caller.scopes)
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


async def _json_body(request: Request) -> Any:
    """The decoded body, refused by SIZE before it is parsed (CONTRACT §8).

    Two checks, not one. `Content-Length` is the cheap refusal and it is what
    stops us reading a gigabyte; the length of what actually arrived is the
    honest one, because a chunked request declares no length at all and a lying
    one declares whatever it likes.
    """
    limit = models.max_body_bytes()
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise errors.request_too_large(limit)
    raw = await request.body()
    if len(raw) > limit:
        raise errors.request_too_large(limit)
    if not raw.strip():
        raise errors.invalid_request("The request body must be a JSON object.")
    try:
        return json.loads(raw)
    except ValueError:
        # The decoder's own message names a byte offset into the caller's body,
        # which is their prompt. CONTRACT §16: we do not echo it.
        raise errors.invalid_request("The request body is not valid JSON.") from None


def _context_limit_error(
    caller: ApiCaller, model_id: str, bounded_input_tokens: int
) -> Optional[errors.ApiError]:
    """CONTRACT §8/§12: input over the model's ceiling is a 400, BEFORE admission.

    `bounded_input_tokens` MUST be a count the real prompt cannot exceed —
    `context.upper_bound_messages`, never `context.estimate_messages`. A hard
    ceiling decided on the 3-chars-per-token estimate was gameable DOWN
    (re-verifier probe, 2026-09-13): the Qwen pre-tokenizer isolates every
    digit, so `input="7"*2900` estimated at 974 tokens, passed a project
    `max_input_tokens=1000` and reached the engine as ~2,900 real tokens; a
    1 MiB digit body (~1.05M tokens) estimated at ~350k and passed the 1M
    window. A byte-level BPE spends at most one token per UTF-8 byte, so the
    byte length is a bound no caller can push below the truth.

    THE TRADE-OFF, accepted deliberately: a legitimate prose prompt runs at
    roughly 3-4 bytes per token (more for non-Latin scripts), so a prompt
    whose REAL count is inside the ceiling but whose byte length is up to a
    few times the ceiling is refused. A false refusal is a 400 the developer
    can act on (shorten, or raise the project ceiling); a false admission is
    a documented hard limit that does not hold and an oversized prompt in
    the lanes shared with the chat app. The engine's `/tokenize` would be
    exact, but it is another dependency on the hot path and `/v1` calls
    `stream_chat_events` and nothing lower (CONTRACT §11).

    Never raised anywhere until 2026-09-13 (verifier finding): a prompt over
    the context window went through the quota gate and into the shared
    admission lanes, and came back as whatever the engine's refusal mapped to
    — usually a 500.

    The ceiling is the smaller of the model's (`registry`, read from the
    settings the engine was started with) and the project's
    `max_input_tokens`. The project's value obeys the platform rule that 0
    means ZERO and only None inherits. The registry's 0 is different: it means
    the deployment never told us its window, and inventing one would refuse
    requests the engine would serve, so that one is skipped.

    Resolved against the DECLARED registry (no database read): an override
    can only disable a model, never change its window, and the 404 for a
    model this key may not use is still decided by `_resolve_model` later.
    """
    declared = registry.resolve_public_model(model_id, allowed=caller.models, overrides=None)
    if declared is None:
        return None
    ceilings = []
    if int(declared.max_input_tokens or 0) > 0:
        ceilings.append(int(declared.max_input_tokens))
    project_ceiling = getattr(caller.limits, "max_input_tokens", None)
    if project_ceiling is not None:
        ceilings.append(max(0, int(project_ceiling)))
    if not ceilings:
        return None
    limit = min(ceilings)
    if int(bounded_input_tokens) > limit:
        return errors.context_length_exceeded(
            requested=int(bounded_input_tokens), limit=limit, upper_bound=True
        )
    return None


def _max_tokens(
    request_model: models.ResponsesRequest,
    model: registry.PublicModel,
    caller: Optional[ApiCaller] = None,
) -> int:
    """How many tokens this request may generate — VALIDATED EARLY.

    Called before the durable row is written, not with the spec afterwards: an
    explicit `max_output_tokens` over the ceiling is a 400 (CONTRACT §8), and
    a 400 that has already inserted an `api_responses` row leaves a permanent
    record of a request that never ran. The ceiling is the model's, narrowed
    by the project's `max_output_tokens` when it sets one (0 means zero).
    """
    ceiling = max(1, int(model.max_output_tokens or DEFAULT_MAX_OUTPUT_TOKENS))
    project_ceiling = getattr(getattr(caller, "limits", None), "max_output_tokens", None)
    if project_ceiling is not None:
        if int(project_ceiling) < 1:
            raise errors.invalid_request(
                "This project may not generate output tokens.", param="max_output_tokens"
            )
        ceiling = min(ceiling, int(project_ceiling))
    return request_model.resolve_max_output_tokens(
        ceiling=ceiling, default=DEFAULT_MAX_OUTPUT_TOKENS
    )


def _spec(
    request_model: models.ResponsesRequest,
    model: registry.PublicModel,
    response_id: str,
    created_at: int,
    max_tokens: int,
) -> streaming.GenerationSpec:
    return streaming.GenerationSpec(
        response_id=response_id,
        model=model.id,
        messages=request_model.chat_messages(),
        max_tokens=max_tokens,
        temperature=(
            DEFAULT_TEMPERATURE
            if request_model.temperature is None
            else float(request_model.temperature)
        ),
        created_at=created_at,
    )


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
        input_tokens = None if counted is None else counted.input_tokens
        output_tokens = None if counted is None else counted.output_tokens
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


async def _parse_generating(
    request: Request, caller: ApiCaller, parse: Callable[[Any], Tuple[models.ResponsesRequest, bool]]
) -> _Parsed:
    parsed = _Parsed()
    try:
        parsed.payload = await _json_body(request)
        parsed.request_model, parsed.include_usage = parse(parsed.payload)
    except errors.ApiError as refusal:
        parsed.deferred = refusal
        return parsed
    messages = parsed.request_model.chat_messages()
    # Two different counts for two different decisions (2026-09-13). The HARD
    # ceiling refuses on the byte bound, which a caller cannot game down (see
    # `_context_limit_error`). The SOFT input-TPM reservation keeps the
    # estimate: `quotas.record_usage(reservation=)` settles it to the measured
    # count when the request ends, so an under-estimate there self-corrects,
    # and reserving the byte bound would spend 3-4x a prose prompt's budget
    # for the length of the request.
    too_long = _context_limit_error(
        caller, parsed.request_model.model, context.upper_bound_messages(messages)
    )
    estimate = context.estimate_messages(messages)
    if too_long is not None:
        # Refused, so it spends no input-token budget — but it still spends a
        # request, like any other refusal after the gate.
        parsed.deferred = too_long
        return parsed
    parsed.estimate = estimate
    return parsed


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
    parsed = await _parse_generating(request, caller, _parse_responses)
    reservation = await _admit(
        request,
        caller,
        kind=_kind_of(parsed.request_model),
        estimated_input_tokens=parsed.estimate,
        # `request_model` is None when validation failed: the 400 is DEFERRED
        # until after admission so a malformed request still counts against
        # the rate limit. Reading an attribute off it turned every invalid
        # body into a 500 (caught by the end-to-end run, 2026-09-13).
        max_output_tokens=getattr(parsed.request_model, "max_output_tokens", None),
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
    parsed = await _parse_generating(request, caller, _from_chat_completions)
    reservation = await _admit(
        request,
        caller,
        kind=_kind_of(parsed.request_model),
        estimated_input_tokens=parsed.estimate,
        # `request_model` is None when validation failed: the 400 is DEFERRED
        # until after admission so a malformed request still counts against
        # the rate limit. Reading an attribute off it turned every invalid
        # body into a 500 (caught by the end-to-end run, 2026-09-13).
        max_output_tokens=getattr(parsed.request_model, "max_output_tokens", None),
    )
    if parsed.deferred is not None:
        raise parsed.deferred
    return await _generate(request, caller, parsed, reservation, chat=True)


async def _generate(
    request: Request, caller: ApiCaller, parsed: _Parsed, reservation: Any, *, chat: bool
) -> Response:
    """Everything after the gate, for both generating routes.

    The order, and why:

    1. the model (404) and the output ceiling (400) — cheap, and before any
       write;
    2. the `Idempotency-Key` claim, so a replay answers without a slot, a row
       or the engine;
    3. background → `background.start`, the ONE background implementation
       (it takes the project's concurrency slot for the job's whole life and
       writes the row);
    4. streaming → `_SlotStream`, which takes the slot INSIDE the response
       call that also releases it;
    5. synchronous → the slot around the row and the generation, in this
       coroutine.
    """
    request_model = parsed.request_model
    assert request_model is not None
    route = "v1_chat_completions" if chat else "v1_responses"
    request_id = _request_id(request)
    try:
        model = await _resolve_model(caller, request_model.model)
        max_tokens = _max_tokens(request_model, model, caller)
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
        created = int(time.time())
        spec = _spec(request_model, model, response_id, created, max_tokens)
        on_finish = _recorder(
            caller,
            route=route,
            request_id=request_id,
            response_id=response_id,
            streamed=request_model.stream,
            held=held,
            reservation=reservation,
        )

        if request_model.background:
            row = await background.start(
                spec,
                caller=caller,
                on_finish=on_finish,
                request_id=request_id,
                metadata=dict(request_model.metadata or {}),
                instructions_present=bool(request_model.instructions),
                fingerprint=(held.fingerprint if held is not None else ""),
            )
            # The 202 IS the outcome a retry should replay; the job's own end
            # re-finishes the claim through `on_finish`.
            await _finish_claim(held, str(row["id"]))
            return JSONResponse(status_code=202, content=_row_to_wire(row))

        if request_model.stream:
            if chat:
                frames = streaming.chat_completions_sse(
                    spec,
                    completion_id=_completion_id(response_id),
                    include_usage=parsed.include_usage,
                    on_finish=on_finish,
                )
            else:
                frames = streaming.responses_sse(spec, on_finish=on_finish)
            return _SlotStream(
                frames,
                caller=caller,
                request=request,
                prepare=functools.partial(
                    _claim_row, caller, request_id, request_model, response_id
                ),
                on_refused=functools.partial(_nothing_ran, caller, reservation, held),
                on_abandoned=functools.partial(_record_abandoned, spec, on_finish),
            )
    except BaseException:
        await _nothing_ran(caller, reservation, held)
        raise

    # Two failure regions, two different truths (re-verifier, 2026-09-13).
    # Before `run_to_completion` starts — the slot refused, or the row not
    # written — NOTHING RAN: the key is released and the estimate given back.
    # Once it has started, the engine may have generated: a cancellation (a
    # shutdown, a client gone under a server that cancels) is recorded through
    # `on_finish` with whatever usage the engine reported, so the tokens are
    # charged, the reservation is settled to the measured count and the row
    # leaves `in_progress`. The old single `except BaseException` handed the
    # spent tokens back as "nothing ran" and never touched the row.
    partial = streaming.new_outcome(spec)
    generating = False
    try:
        with quotas.concurrency_slot(caller, KIND_SYNC):
            await _claim_row(caller, request_id, request_model, response_id, status="in_progress")
            generating = True
            outcome = await streaming.run_to_completion(spec, outcome=partial)
    except BaseException as exc:
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
        raise
    await on_finish(outcome)
    if outcome.error is not None:
        # A synchronous call that could not reach the engine is the documented
        # 503 with a Retry-After (CONTRACT §9), not a 200 carrying a failure a
        # client has to notice.
        raise outcome.error
    if not chat:
        return JSONResponse(outcome.response().to_wire())
    counted = outcome.usage_model()
    return JSONResponse(
        _chat_body(
            completion_id=_completion_id(response_id),
            created=created,
            model=spec.model,
            content=outcome.text,
            finish_reason=outcome.chat_finish_reason(),
            usage=counted,
        )
    )


def _chat_body(
    *,
    completion_id: str,
    created: int,
    model: str,
    content: Optional[str],
    finish_reason: str,
    usage: Optional[models.Usage],
) -> Dict[str, Any]:
    return {
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
    return JSONResponse(
        {
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
    )


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
    """An SSE response that takes its concurrency slot IN THE CODE THAT RELEASES IT.

    THE LEAK THIS REPLACES (verifier finding, 2026-09-13). The slot used to be
    taken in the handler and released in the `finally` of the body generator.
    A client that sends the POST and resets the connection before the body's
    first step leaves that generator unstarted — and an unstarted generator's
    `finally` never runs, so the slot was never given back. Repeated, that
    drives a project's concurrency to zero until a restart.

    Now `__call__` — the one method Starlette always awaits once the handler
    has returned this object — holds the slot in a `with` around the whole
    response. Taken before the status line is sent, so a refusal is still the
    documented 429 with `Retry-After` rather than a 200 whose stream dies; and
    released by the same `with`, however the response ends: finished, failed,
    disconnected before the first byte, or cancelled.

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
    ) -> None:
        super().__init__(frames, media_type="text/event-stream", headers=streaming.SSE_HEADERS)
        self._frames = frames
        self._caller = caller
        self._request = request
        self._prepare = prepare
        self._on_refused = on_refused
        self._on_abandoned = on_abandoned
        self._kind = kind
        self.started = False
        self.body_iterator = self._tracked()

    async def _tracked(self) -> AsyncIterator[str]:
        self.started = True
        try:
            async for frame in self._frames:
                yield frame
        finally:
            # `async for` has no teardown of its own: closing THIS generator
            # does not close the one it iterates, and the engine stream inside
            # that one would stay open until garbage collection.
            await self._frames.aclose()

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
                    await self._prepare()
                except Exception as exc:  # noqa: BLE001 - still before the status line
                    await _shielded(self._on_refused(), "release the idempotency key")
                    failure = exc if isinstance(exc, errors.ApiError) else errors.from_unexpected(exc)
                    await self._refusal(failure)(scope, receive, send)
                    return
                try:
                    await super().__call__(scope, receive, send)
                finally:
                    await _shielded(self._close(), "close the stream")
        except errors.ApiError as refusal:
            if admitted:
                raise
            await _shielded(self._on_refused(), "release the idempotency key")
            await self._refusal(refusal)(scope, receive, send)

    async def _close(self) -> None:
        with contextlib.suppress(Exception):
            await self.body_iterator.aclose()
        if not self.started:
            await self._on_abandoned()


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
    return models.Response(
        id=str(row["id"]),
        created_at=_row_created(row),
        status=str(row.get("status") or "queued"),  # type: ignore[arg-type]
        model=str(row.get("model") or ""),
        output=[models.OutputMessage.of(text)] if text else [],
        usage=_row_usage(row),
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
    # honest and cheap; running the model a second time is neither.
    return errors.ApiError(
        "rate_limit_error",
        "A request with this Idempotency-Key is still running.",
        param="Idempotency-Key",
        retry_after=2,
    )


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
                finish_reason="stop",
                usage=_row_usage(row),
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
            yield chunks.stop("stop")
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
_CHAT_FIELDS = ("model", "messages", "stream", "max_tokens", "temperature", "stream_options")


def _from_chat_completions(payload: Any) -> Tuple[models.ResponsesRequest, bool]:
    """The compatibility body → the one validated request type.

    `max_tokens` is the older spelling of `max_output_tokens` and is mapped;
    `stream_options.include_usage` is not a generation parameter at all but a
    framing one, so it is taken out here and handed to the chunk builder.
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
    body: Dict[str, Any] = {
        "model": payload.get("model"),
        "input": payload.get("messages"),
        "stream": bool(payload.get("stream", False)),
    }
    if payload.get("max_tokens") is not None:
        body["max_output_tokens"] = payload["max_tokens"]
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
