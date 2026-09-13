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
    }
)

#: Statuses that MUST carry `Retry-After` (CONTRACT §9: "`Retry-After` on every
#: 429 and 503"). Constructing one without it is a programming error, caught
#: here rather than by a client that never retries.
_RETRY_AFTER_REQUIRED = frozenset({429, 503})

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
        minimum 1 — a float here is unparseable to most HTTP clients."""
        return {} if self.retry_after is None else {"Retry-After": str(self.retry_after)}

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
