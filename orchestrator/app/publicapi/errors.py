"""The ONE error envelope the public API ever returns (CONTRACT §9).

Every failure on `/v1` — before the stream, mid-stream, or from a background
job — is an `ApiError`, and every `ApiError` renders to exactly this body:

    {"error": {"message": …, "type": …, "code": …, "param": …, "request_id": …}}

Five keys, always present, `param` and `request_id` nullable. A caller writes
one parser and one retry table; an SDK generated from the OpenAPI document
sees one error schema.

WHY A CLOSED TABLE OF CODES. `_CODES` below is the whole vocabulary from
CONTRACT §9 — code → (HTTP status, wire `type`). Constructing an `ApiError`
with a code that is not in it raises, so a new failure mode cannot be
introduced on the wire without a contract change, and no route can invent a
status for a code that already has one.

WHY THE MESSAGE IS SCRUBBED ON ITS WAY OUT. CONTRACT §9 forbids a traceback,
SQL, an environment value, a container name, an internal hostname, a
filesystem path or a private IP in any response body. Two defences, in this
order:

1. `from_unexpected()` throws the original text away entirely. An exception
   this code did not construct — `psycopg.OperationalError: connection to
   server at "postgres" (172.18.0.4), port 5432 failed`, say — is the usual
   way a private address reaches a user, and no regular expression is a safe
   filter for text nobody wrote deliberately;
2. `redact()` runs over every message that DOES reach the envelope, including
   the ones our own factories build. It is belt and braces, not the primary
   defence: it cannot recognise a bare container name (`vllm-router` looks
   like an ordinary word), which is exactly why rule 1 discards rather than
   filters.

Nothing here imports FastAPI. These are values; the router wave turns them
into responses, and the streaming wave frames them as `event: error`.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

log = logging.getLogger(__name__)

# --------------------------------------------------------------- the table --


@dataclass(frozen=True)
class _CodeSpec:
    """The HTTP status and wire `type` that belong to one error code."""

    status: int
    type: str


#: CONTRACT §9, one row per code. `type` is the coarse class a client switches
#: on; `code` is the precise reason. The three 429s share the
#: `rate_limit_error` type on purpose: a caller's reaction to all three is the
#: same — wait for `Retry-After`, then retry — and one type means one retry
#: table rather than three near-identical branches in every SDK.
_CODES: Dict[str, _CodeSpec] = {
    "invalid_request_error": _CodeSpec(400, "invalid_request_error"),
    "context_length_exceeded": _CodeSpec(400, "invalid_request_error"),
    "invalid_api_key": _CodeSpec(401, "authentication_error"),
    "insufficient_scope": _CodeSpec(403, "permission_error"),
    "origin_not_allowed": _CodeSpec(403, "permission_error"),
    "model_not_found": _CodeSpec(404, "invalid_request_error"),
    "response_not_found": _CodeSpec(404, "invalid_request_error"),
    "idempotency_conflict": _CodeSpec(409, "invalid_request_error"),
    "request_too_large": _CodeSpec(413, "invalid_request_error"),
    "rate_limit_error": _CodeSpec(429, "rate_limit_error"),
    "quota_exceeded": _CodeSpec(429, "rate_limit_error"),
    "concurrency_limit_exceeded": _CodeSpec(429, "rate_limit_error"),
    "model_recovering": _CodeSpec(503, "service_unavailable_error"),
    "model_unavailable": _CodeSpec(503, "service_unavailable_error"),
    "timeout": _CodeSpec(504, "timeout_error"),
    "internal_error": _CodeSpec(500, "server_error"),
    # The Files API (CONTRACT §9, added 2026-09-14 when /v1/files and
    # /v1/uploads were published in §7). They used to live only in
    # `files/wire.FILE_CODES`, outside this table, which meant the OpenAPI
    # document's `code` enum, the `/docs` error page and every SDK generated
    # from the schema listed a vocabulary the server did not keep to: a client
    # switching on `code` met seven values the schema said could not exist.
    # One table again. `files/wire.FILE_CODES` keeps only the `x-should-retry`
    # default of each, and a test pins the two to the same status and type.
    "file_not_found": _CodeSpec(404, "invalid_request_error"),
    "upload_not_found": _CodeSpec(404, "invalid_request_error"),
    "file_not_ready": _CodeSpec(409, "invalid_request_error"),
    "upload_state_conflict": _CodeSpec(409, "invalid_request_error"),
    "checksum_mismatch": _CodeSpec(400, "invalid_request_error"),
    "incomplete_body": _CodeSpec(408, "invalid_request_error"),
    # `api_error`, not `service_unavailable_error`: the two 503s of that type
    # are "the model cannot serve you yet, retry", while a full disk does not
    # empty in an SDK's backoff and says `x-should-retry: false`. A client
    # that retries on `service_unavailable_error` must not loop on this one.
    "storage_unavailable": _CodeSpec(503, "api_error"),
}

#: The codes a client may retry unchanged. Published so the documentation and
#: the SDK generator read the same list the server enforces.
RETRYABLE_CODES = frozenset(
    {
        "rate_limit_error",
        "quota_exceeded",
        "concurrency_limit_exceeded",
        "model_recovering",
        "model_unavailable",
        "timeout",
        # Files (2026-09-14). A file still processing is ready later, and a
        # body cut in transit recorded nothing, so the same request succeeds
        # when sent again. `storage_unavailable` is deliberately absent: it
        # is retry-safe only when its `x-should-retry` says `true` (a purge
        # of the same bytes in progress), never for a full disk.
        "file_not_ready",
        "incomplete_body",
    }
)

#: Statuses that MUST carry `Retry-After` (CONTRACT §9: "`Retry-After` on every
#: 429 and 503"). Constructing one without it is a programming error, caught
#: here rather than by a client that never retries.
_RETRY_AFTER_REQUIRED = frozenset({429, 503})

#: The header both OpenAI SDKs obey before their own status table
#: (openai-python `_should_retry`, openai-node `shouldRetry`): `false` stops a
#: retry of a 409/429/5xx, `true` forces one. Lower case, as the SDKs read it.
SHOULD_RETRY_HEADER = "x-should-retry"

#: The floor the public OpenAPI schema types for `Retry-After` (integer,
#: minimum 1). A `Retry-After: 0` invites an immediate retry storm from the
#: caller we just throttled.
MIN_RETRY_AFTER = 1

# ----------------------------------------------------------- the scrubber --

_REDACTED = "[redacted]"

#: A traceback and everything after it. Anchored at the Python header because
#: the tail of an exception chain can be arbitrarily long.
_TRACEBACK_RE = re.compile(r"Traceback \(most recent call last\)[\s\S]*", re.IGNORECASE)

#: A statement, not a word: `select` on its own is ordinary English.
_SQL_RE = re.compile(
    r"\b(?:select\s+[\s\S]{1,200}?\s+from\s+\S+"
    r"|insert\s+into\s+\S+"
    r"|update\s+\S+\s+set\s+"
    r"|delete\s+from\s+\S+"
    r"|create\s+(?:table|index)\s+\S+"
    r"|alter\s+table\s+\S+)",
    re.IGNORECASE,
)

#: `API_KEY_PEPPER=…`, `POSTGRES_PASSWORD=…` — an environment value pasted into
#: a message. Four characters of SHOUTING_SNAKE before the `=` keeps it away
#: from ordinary prose.
_ENV_ASSIGNMENT_RE = re.compile(r"\b[A-Z][A-Z0-9_]{3,}=\S+")

_PRIVATE_IP_RE = re.compile(
    r"\b(?:"
    r"10(?:\.\d{1,3}){3}"
    r"|127(?:\.\d{1,3}){3}"
    r"|192\.168(?:\.\d{1,3}){2}"
    r"|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}"
    r"|169\.254(?:\.\d{1,3}){2}"
    r")\b"
)

#: An absolute filesystem path. The prefix list is closed on purpose: a bare
#: `/v1/responses` is a public route and must survive, while `/app/…` and
#: `/data/…` name this container's insides.
_PATH_RE = re.compile(
    r"(?<![\w.])/(?:usr|etc|home|var|opt|srv|root|tmp|proc|sys|dev|data|app|reports|mnt|media)"
    r"(?:/[^\s'\"]*)?"
)

_URL_RE = re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s'\"<>]+", re.IGNORECASE)

#: An API key secret, in the one shape this platform mints (CONTRACT §5:
#: `tsk_live_<public_id>_<secret><checksum>`). Nothing echoes a presented key
#: today — `invalid_api_key()` is deliberately one fixed sentence — so this is
#: defence in depth for the waves that put key material next to these code
#: paths: a management route that one day says "the key … was rejected" must
#: not be the thing that publishes it. The `public_id` half is safe to log on
#: its own, but the token arrives as one string and there is no safe way to
#: split it here, so the whole run of it goes.
_API_KEY_RE = re.compile(r"\btsk_(?:live|test)_[A-Za-z0-9_\-]+")

#: A host that is unmistakably ours: a compose service name has no dot at all
#: (`vllm`, `vllm-router`), and these suffixes are reserved for internal
#: naming. A public `https://docs.techsarasolutions.com/…` link survives.
_INTERNAL_SUFFIXES = (".local", ".internal", ".svc", ".lan", ".localdomain")


def _redact_url(match: "re.Match[str]") -> str:
    url = match.group(0)
    host = url.split("://", 1)[1]
    for separator in ("/", "?", "#"):
        host = host.split(separator, 1)[0]
    host = host.rsplit("@", 1)[-1]  # credentials in the URL are never public
    host = host.split(":", 1)[0].strip("[]").lower()
    internal = (
        "@" in url.split("://", 1)[1].split("/", 1)[0]
        or "." not in host
        or host.endswith(_INTERNAL_SUFFIXES)
        or bool(_PRIVATE_IP_RE.fullmatch(host))
        or host == "localhost"
    )
    return _REDACTED if internal else url


def redact(message: str) -> str:
    """Strip from a message the things CONTRACT §9 forbids in a response body.

    Order matters: the traceback rule eats the rest of the string, so it runs
    first and the cheaper rules never see text that is already gone.
    """
    text = str(message or "")
    text = _TRACEBACK_RE.sub(_REDACTED, text)
    text = _SQL_RE.sub(_REDACTED, text)
    text = _ENV_ASSIGNMENT_RE.sub(_REDACTED, text)
    # Before the URL rule: a key pasted into a query string is a key first and
    # a URL second, and `_redact_url` keeps a public https:// host intact.
    text = _API_KEY_RE.sub(_REDACTED, text)
    text = _URL_RE.sub(_redact_url, text)
    text = _PRIVATE_IP_RE.sub(_REDACTED, text)
    text = _PATH_RE.sub(_REDACTED, text)
    return text


#: What a caller-supplied identifier must look like before it is echoed back in
#: a message. Echoing an unvalidated model id into an error body is how a log
#: viewer ends up rendering someone else's newline-injected text.
_ECHOABLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-]{0,63}$")


def _echoable(value: Optional[str]) -> Optional[str]:
    value = (value or "").strip()
    return value if _ECHOABLE_RE.match(value) else None


# ------------------------------------------------------------- ApiError --


class ApiError(Exception):
    """One failure, already decided: status, code, wire type, optional param.

    Raised anywhere under `/v1` and caught once, at the edge. It carries the
    HTTP status so a route never has to map a code to a number a second time
    and get a different answer.
    """

    #: `x-should-retry` (2026-09-13, no-timeout design sdk_and_docs): None
    #: sends no header and leaves the SDKs to their status table; False tells
    #: openai-python and openai-node NOT to retry a status they would
    #: otherwise retry (a 409 or 5xx), which is how a failure that must not
    #: run the model twice says so. Set through `no_retry`.
    #:
    #: A CLASS attribute, not one set in `__init__`: a subclass that builds
    #: itself without calling it (apifiles.service._PendingCodeError, for the
    #: codes the table does not hold yet) must still render `headers()` — the
    #: first draft set it per instance and broke four Files API tests.
    should_retry: Optional[bool] = None

    def __init__(
        self,
        code: str,
        message: str,
        *,
        param: Optional[str] = None,
        retry_after: Optional[float] = None,
    ) -> None:
        spec = _CODES.get(code)
        if spec is None:
            raise ValueError(
                f"unknown public API error code: {code!r} "
                f"(allowed: {', '.join(sorted(_CODES))})"
            )
        if retry_after is None and spec.status in _RETRY_AFTER_REQUIRED:
            raise ValueError(
                f"a {spec.status} must carry Retry-After (CONTRACT §9); "
                f"code {code!r} was raised without one"
            )
        super().__init__(message)
        self.code = code
        self.status = spec.status
        self.type = spec.type
        self.message = str(message)
        # `param` IS caller-controlled text on the validation path: with
        # `extra="forbid"` pydantic puts the caller's own field NAME in `loc`,
        # and `models._param_from_loc` turns that into this value. Echoing it
        # unchecked is the rule `_echoable` exists to enforce, applied until
        # now to `model`, `scope` and `response_id` but not to this field —
        # so a body with a key of `"x\nevent: error\n… /etc/passwd 10.0.0.7"`
        # put a filesystem path and a private IP into an error body, against
        # CONTRACT §9 (verifier finding, 2026-09-13). Validated HERE, at the
        # one boundary both `envelope()` and `stream_payload()` cross, rather
        # than at each of the callers that build a param.
        #
        # A name that is not plainly safe becomes None: the caller still gets
        # pydantic's own sentence ("extra inputs are not permitted"), which
        # tells them what to fix without us rendering their text.
        self.param = _echoable(param)
        self.retry_after: Optional[int] = (
            None if retry_after is None else max(MIN_RETRY_AFTER, int(math.ceil(retry_after)))
        )

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE_CODES

    def envelope(self, request_id: str = "") -> Dict[str, Any]:
        """The response body, scrubbed. `request_id` is echoed so a support
        conversation can name one request; blank becomes null rather than an
        empty string, which reads as "we have no id" instead of "the id is ''"."""
        return {
            "error": {
                "message": redact(self.message),
                "type": self.type,
                "code": self.code,
                "param": self.param,
                "request_id": (request_id or "").strip() or None,
            }
        }

    def headers(self) -> Dict[str, str]:
        """Headers this failure must carry. `Retry-After` is seconds, integer,
        minimum 1 — a float here is unparseable to most HTTP clients.
        `x-should-retry` only when a caller decided it (`no_retry`)."""
        headers: Dict[str, str] = {}
        if self.retry_after is not None:
            headers["Retry-After"] = str(self.retry_after)
        if self.should_retry is not None:
            headers[SHOULD_RETRY_HEADER] = "true" if self.should_retry else "false"
        return headers

    def stream_payload(self, sequence_number: int) -> Dict[str, Any]:
        """The `event: error` data of CONTRACT §10 / STANDARDS: type, code,
        message, param, sequence_number — and nothing else. The envelope's
        `request_id` is not repeated here because the header carried it before
        the first byte of the stream."""
        return {
            "type": "error",
            "code": self.code,
            "message": redact(self.message),
            "param": self.param,
            "sequence_number": int(sequence_number),
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ApiError({self.code!r}, status={self.status}, param={self.param!r})"


# ------------------------------------------------------------ factories --
#
# One per code in CONTRACT §9. The default messages are the public wording:
# short, British-flavoured, and deliberately incurious about whether the thing
# asked for exists (see `model_not_found`).


def invalid_request(message: str, *, param: Optional[str] = None) -> ApiError:
    return ApiError("invalid_request_error", message, param=param)


def invalid_api_key(message: str = "The API key is invalid.") -> ApiError:
    """Missing, malformed, unknown, revoked or expired — deliberately ONE
    message. Telling a caller which of those it was tells an attacker whether
    a key they hold exists, which is the whole game for a leaked-key scanner."""
    return ApiError("invalid_api_key", message)


def insufficient_scope(scope: Optional[str] = None) -> ApiError:
    name = _echoable(scope)
    message = (
        f"The API key does not have the `{name}` scope."
        if name
        else "The API key does not have the scope this endpoint requires."
    )
    return ApiError("insufficient_scope", message)


def origin_not_allowed() -> ApiError:
    """The rejected origin is NOT echoed. It is attacker-controlled text, and
    the caller already knows what it sent."""
    return ApiError(
        "origin_not_allowed",
        "This origin is not in the project's allowed origins.",
    )


def model_not_found(model: Optional[str] = None) -> ApiError:
    """404, never 403 — CONTRACT §4. A key that may not use a model and a
    model that does not exist must be indistinguishable, or the API becomes a
    directory of what we run."""
    name = _echoable(model)
    message = (
        f"The model `{name}` does not exist or you do not have access to it."
        if name
        else "The model does not exist or you do not have access to it."
    )
    return ApiError("model_not_found", message, param="model")


def response_not_found(response_id: Optional[str] = None) -> ApiError:
    name = _echoable(response_id)
    message = (
        f"No response with id `{name}` was found for this project."
        if name
        else "No response with that id was found for this project."
    )
    return ApiError("response_not_found", message)


def idempotency_conflict() -> ApiError:
    return ApiError(
        "idempotency_conflict",
        "This Idempotency-Key was already used with a different request body.",
        param="Idempotency-Key",
    )


def request_too_large(limit_bytes: int) -> ApiError:
    return ApiError(
        "request_too_large",
        f"The request body is larger than the {int(limit_bytes)} byte limit.",
    )


def request_text_too_large(limit_bytes: int) -> ApiError:
    """The text of a request over CONTRACT §12's 1 MiB rule (2026-09-13).

    A request that carries images may be up to PUBLIC_API_MAX_MEDIA_BODY_BYTES
    on the wire, so the byte cap on the BODY no longer bounds the prompt. The
    text inside it — instructions and every text part — still must fit the
    original mebibyte, and the message says so rather than naming a body
    limit the caller did not exceed."""
    return ApiError(
        "request_too_large",
        f"The text in this request is larger than the {int(limit_bytes)} byte "
        "limit. Images do not count towards it.",
    )


def context_length_exceeded(
    *,
    requested: Optional[int] = None,
    limit: Optional[int] = None,
    upper_bound: bool = False,
) -> ApiError:
    """`upper_bound=True` when `requested` is a bound the prompt cannot exceed
    (its UTF-8 byte length) rather than a count. The router refuses on that
    bound since 2026-09-13, and "the input is N tokens" would then be a false
    statement a developer would debug against."""
    if requested is not None and limit is not None and upper_bound:
        message = (
            f"The input may be up to {int(requested)} tokens (counted as one "
            f"token per UTF-8 byte), which is more than the {int(limit)} token "
            "limit for this request."
        )
    elif requested is not None and limit is not None:
        message = (
            f"The input is {int(requested)} tokens, which is more than the "
            f"model's {int(limit)} token limit."
        )
    else:
        message = "The input is longer than the model's context limit."
    return ApiError("context_length_exceeded", message, param="input")


def rate_limit(retry_after: float) -> ApiError:
    return ApiError(
        "rate_limit_error",
        "The rate limit for this project has been reached.",
        retry_after=retry_after,
    )


def quota_exceeded(retry_after: float) -> ApiError:
    return ApiError(
        "quota_exceeded",
        "The token quota for this project has been used up.",
        retry_after=retry_after,
    )


def concurrency_limit_exceeded(retry_after: float) -> ApiError:
    return ApiError(
        "concurrency_limit_exceeded",
        "Too many requests are in flight for this project.",
        retry_after=retry_after,
    )


def model_at_capacity(retry_after: float) -> ApiError:
    """The shared engine's admission queue is full (2026-09-13).

    Not a per-client limit — the owner removed every rate, token, quota and
    concurrency limit from /v1 — but the one physical fact that remains: a
    single engine serves the chat application and the API together, and when
    its admission lanes are full a request cannot start yet. That is a 503
    with Retry-After in the model_unavailable family, and it says plainly that
    retrying is safe; a 429 "concurrency limit" would name a limit that does
    not exist."""
    return ApiError(
        "model_unavailable",
        "The model is at capacity right now. This request is safe to retry.",
        retry_after=retry_after,
    )


def idempotency_in_progress(retry_after: float = 2) -> ApiError:
    """The first attempt with this Idempotency-Key is still running
    (2026-09-13). A 409, as the Idempotency-Key draft answers an outstanding
    request — not a 429, which read as a rate limit the API no longer has."""
    return ApiError(
        "idempotency_conflict",
        "A request with this Idempotency-Key is still running. Retry shortly to receive its result.",
        param="Idempotency-Key",
        retry_after=retry_after,
    )


def model_recovering(retry_after: float) -> ApiError:
    """The engine is restarting and the request is safe to send again. Named
    apart from `model_unavailable` because retrying THIS one works."""
    return ApiError(
        "model_recovering",
        "The model is restarting. This request is safe to retry.",
        retry_after=retry_after,
    )


def model_unavailable(retry_after: float = 30) -> ApiError:
    return ApiError(
        "model_unavailable",
        "The model is not available at the moment.",
        retry_after=retry_after,
    )


#: The one sentence of a request whose input files were still being prepared
#: when the service restarted (`preparation_interrupted`). Matched by value
#: nowhere: the durable runtime marks its own run, the file wait its own row.
PREPARATION_INTERRUPTED_MESSAGE = (
    "The service restarted while this request's files were being prepared. "
    "Nothing was generated or charged; send the request again."
)

#: What that failure tells the caller to wait: the new process is usually up
#: within the ~97 s restart window, and every hop in front retries a refused
#: connect for 110 s, so a short hint is enough.
PREPARATION_INTERRUPTED_RETRY_AFTER_S = 2


def preparation_interrupted() -> ApiError:
    """A request ended by a restart BEFORE it generated anything: its files
    (a wait for processing, the context build) were still being prepared.

    WHY A FAILURE AND NOT A RESUME (release review 2026-09-14). A durable run
    resumes from its stored spec, and the spec of a request with files exists
    only once the files are prepared: what the next process would need to
    prepare them again (the request body, the caller's credential) is not
    stored. So the restart ends it at once, retry-safe (`model_unavailable`,
    `x-should-retry` left to the status: nothing ran, the Idempotency-Key is
    released), instead of holding the old process's shutdown for its full
    grace and then cutting the connection with nothing said. The files keep
    processing across the restart, so the retry continues from there."""
    error = ApiError(
        "model_unavailable",
        PREPARATION_INTERRUPTED_MESSAGE,
        retry_after=PREPARATION_INTERRUPTED_RETRY_AFTER_S,
    )
    error.should_retry = True
    return error


#: How long a caller is told to wait when the platform's own database is out.
#: Short: a pool that timed out under a burst is usually back within seconds,
#: and a longer figure turns a blip into minutes of refused traffic.
DATABASE_RETRY_AFTER_S = 5


def database_unavailable(retry_after: float = DATABASE_RETRY_AFTER_S) -> ApiError:
    """The platform cannot reach its ledger, so it refuses — as a 503.

    WHY 503 AND NOT 500 (re-verifier finding, 2026-09-13): the quota gate
    fails CLOSED during a database incident, which is right, but it surfaced
    as `500 internal_error`, which every client reads as "a bug, do not
    retry". The request is safe to retry and will succeed once the database
    is back, which is exactly what a 503 with `Retry-After` says.

    WHY THE `model_unavailable` CODE. CONTRACT §9 is the closed list of codes
    and has no `service_unavailable`; a new code is a contract change that
    must be made in CONTRACT.md first. `model_unavailable` is the documented
    503 a client already retries (it is in `RETRYABLE_CODES`), and from the
    caller's side the effect is identical: the model cannot be used right
    now. The MESSAGE does not claim the engine is down, and names nothing
    internal.
    """
    return ApiError(
        "model_unavailable",
        "The service is temporarily unavailable. Retry after the Retry-After interval.",
        retry_after=retry_after,
    )


def is_database_outage(exc: BaseException) -> bool:
    """True when `exc` (or anything in its cause chain) is the database being
    unreachable — a connection failure, a pool timeout, a server shutdown —
    rather than a bug. `psycopg_pool.PoolTimeout` is an `OperationalError`.

    A `ProgrammingError` or an `IntegrityError` is deliberately NOT an outage:
    retrying a broken query does not fix it, and a 503 would hide the bug
    behind a retry loop.
    """
    try:
        import psycopg
    except Exception:  # noqa: BLE001 - no driver, no outage to classify
        return False
    outage = (psycopg.OperationalError, psycopg.InterfaceError)
    seen = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, outage):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


def timeout(seconds: Optional[float] = None) -> ApiError:
    message = (
        f"The response took longer than the {int(seconds)} second limit."
        if seconds
        else "The response took longer than the time limit."
    )
    return ApiError("timeout", message)


def internal_error(message: str = "Something went wrong on our side.") -> ApiError:
    return ApiError("internal_error", message)


def from_unexpected(exc: BaseException, *, request_id: str = "") -> ApiError:
    """Any exception this code did not construct, turned into a 500.

    THE ORIGINAL TEXT IS DISCARDED, not filtered. A `psycopg` connection error
    names the host and the private IP; a `KeyError` names an internal field; a
    file error names a path inside the container. `redact()` catches the
    shapes it knows, but a bare container name (`vllm-router`) is
    indistinguishable from an ordinary word, so the only safe filter for text
    nobody wrote for a caller is to drop all of it. The detail goes to the
    server log, keyed by request id, which is where an engineer should read it.
    """
    if isinstance(exc, ApiError):
        return exc
    log.exception(
        "unhandled error on the public API (request_id=%s)", (request_id or "-")
    )
    return internal_error()


def no_retry(error: ApiError) -> ApiError:
    """Mark `error` so its response carries `x-should-retry: false`.

    For the failures a retry would make WORSE rather than merely repeat
    (no-timeout design, sdk_and_docs "x-should-retry: false on"): a 500 after
    a generation started for a request with no Idempotency-Key (the retry
    runs the model again from nothing), a 409 caused by a different body or
    credential, and the second engine fault of one run. Returns the same
    object, so it can wrap a `raise`.
    """
    error.should_retry = False
    return error


def committed_failure_error(exc: BaseException, *, request_id: str = "") -> Dict[str, Any]:
    """The `error` object of a failed body written AFTER a committed 200.

    The envelope's four wire fields without `request_id` (the header carried
    it before the first byte): the same `code` and scrubbed `message` the HTTP
    envelope would have used, so a client applies one table whether the
    failure arrived as a status or in a committed body.
    """
    failure = from_unexpected(exc, request_id=request_id)
    return {
        "message": redact(failure.message),
        "type": failure.type,
        "code": failure.code,
        "param": failure.param,
    }


def chat_completion_failure_body(
    exc: BaseException,
    *,
    completion_id: str,
    created: int,
    model: str,
    usage: Optional[Mapping[str, Any]] = None,
    max_output_tokens: Optional[int] = None,
    request_id: str = "",
) -> Dict[str, Any]:
    """A `chat.completion` that failed after its 200 was committed.

    `choices: []` and a top-level `error` (no-timeout design, edge_100s 2):
    the shape the compatibility dialect's own streaming error chunk uses, so
    a client that already unwraps `error` from a chunk reads this the same
    way, and one that only reads `choices[0]` fails loudly on an empty list
    instead of rendering an answer that never came. `usage` is what the
    engine reported for the tokens it did produce (None: not measured, never
    zero).
    """
    return {
        "id": str(completion_id),
        "object": "chat.completion",
        "created": int(created),
        "model": str(model),
        "choices": [],
        "usage": None if usage is None else dict(usage),
        "max_output_tokens": max_output_tokens,
        "error": committed_failure_error(exc, request_id=request_id),
    }


def envelope_from(exc: BaseException, *, request_id: str = "") -> Dict[str, Any]:
    """`exception → response body` in one step, for the edge handler."""
    return from_unexpected(exc, request_id=request_id).envelope(request_id)


def status_for(code: str) -> int:
    """The HTTP status a code maps to. One table, read by the router, the
    documentation and the OpenAPI generator alike."""
    spec = _CODES.get(code)
    if spec is None:
        raise ValueError(f"unknown public API error code: {code!r}")
    return spec.status


def type_for(code: str) -> str:
    spec = _CODES.get(code)
    if spec is None:
        raise ValueError(f"unknown public API error code: {code!r}")
    return spec.type


def error_codes() -> Mapping[str, _CodeSpec]:
    """The whole vocabulary, read-only — for the OpenAPI document and `/docs`,
    so neither can drift from what the server actually raises."""
    return dict(_CODES)
