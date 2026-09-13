"""The PUBLIC OpenAPI 3.1 document — `GET /v1/openapi.json` (CONTRACT §7).

WHY THIS IS HAND-BUILT AND NOT `app.openapi()`. The application's own document
describes every internal chat, admin, analytics, upload, artifact and video
route. Serving it to a developer would publish the entire attack surface of
the product as a machine-readable map, and generating it needs the whole
application's import graph — a database pool, a key pepper, a settings file —
inside what is otherwise a lint job. This module imports `errors`, `registry`
and `scopes` and nothing else, so
`.github/workflows/scripts/api_contract.py` can build and validate the
document in a child interpreter on every commit.

The entry point that script looks for is `public_openapi()`, first in its list
(`app.publicapi.openapi:public_openapi`). Its companion,
`.github/workflows/scripts/public-api-surface.txt`, is the checked-in answer to
"what can an API key reach?", and the build is red whenever this document's
operation set differs from it in either direction. That file is not ours to
edit: a new public endpoint arrives as a reviewed line there in the same commit
as the route, or it does not arrive.

WHY 3.1 AND NOT 3.0. `nullable: true` does not exist in 3.1 — it was replaced
by `type: [..., "null"]` — and a generator reading a 3.0-ism as 3.1 silently
drops the nullability from the client's types. That matters more here than
usual: `usage` is null, never zero, whenever the engine did not report counts
(CONTRACT §9), and a generated client that types it non-nullable crashes on
the first unmetered response instead of handling it.

EVERY SCHEMA HERE IS THE ONE THE SERVER ENFORCES, read from the same modules
the routes read: the error codes come from `errors.error_codes()`, the scopes
from `apiplatform.scopes`, the model ids and ceilings from `registry`. A
document that restated them would be a second source of truth, and the second
one is always the one that is wrong.

THE LIMITS ARE READ FROM THE SAME SWITCH THE GATE READS (owner decision,
2026-09-13). With PUBLIC_API_ENFORCE_LIMITS off — the default — the API has no
request, token or concurrency limit, so this document advertises no
`rate_limit_error` on a read, no `quota_exceeded`, and no `RateLimit` header:
a client generator turns every documented 429 and header into retry and
throttling code, and a documented limit that does not exist makes a client
slow itself down for nothing. With the limits off NO route sends a 429 at
all, so none is documented. The two refusals that stay are not limits and are
not spelled as one: the SHARED engine's admission lane being full is
`503 model_unavailable` ("at capacity", retry-safe), and a request whose
Idempotency-Key is still running is `409 idempotency_conflict` with
`Retry-After` — the 409 description and its optional header say so.
"""
from __future__ import annotations

from typing import Any, Dict, List

from ..apiplatform.scopes import SCOPE_DESCRIPTIONS, Scope
from ..config import settings
from . import errors, registry

#: The published title and version. The version is the CONTRACT's, not the
#: application's: a developer pins against the API's promises, and bumping it
#: because an unrelated internal release went out would be noise in every
#: client's diff.
API_TITLE = "TechSara Developer API"
API_VERSION = "1.0.0"

#: Where the document says the API lives. Same-origin today (CONTRACT §2: the
#: `/v1` edge is served by the frontend until `api.techsarasolutions.com`
#: exists), and the second entry is listed so a client generator that offers a
#: server picker already knows about the move.
SERVERS: List[Dict[str, str]] = [
    {"url": "https://ai.techsarasolutions.com", "description": "Production"},
]

TAGS: List[Dict[str, str]] = [
    {"name": "Models", "description": "The models this API key may use."},
    {"name": "Responses", "description": "Generate, read and cancel a response."},
    {"name": "Usage", "description": "This project's own token and request counters."},
    {"name": "Schema", "description": "This document."},
]

_BEARER_DESCRIPTION = (
    "An API key, sent as `Authorization: Bearer tsk_live_…`. This is the ONLY "
    "credential the API accepts: a browser session cookie is ignored, and no "
    "response ever sets `Access-Control-Allow-Credentials`."
)


def _limits_enforced() -> bool:
    # Read at call time, like the gate (`quotas.limits_enforced`); not
    # imported from there because this module must stay importable without
    # the database layer `quotas` pulls in (the api_contract lint job).
    return bool(getattr(settings, "public_api_enforce_limits", False))


#: The 409 and 503 descriptions name the two refusals that are not limits, so
#: a reader of the schema does not have to guess which 409 is retryable and
#: which 503 is "busy" rather than "down" (2026-09-13).
_DESCRIPTIONS = {
    409: (
        "idempotency_conflict: the same Idempotency-Key with a different body "
        "(do not retry), or with the same body while the first request is "
        "still running (carries Retry-After; retry to collect its answer)."
    ),
    503: (
        "model_recovering; model_unavailable — the engine is restarting, down, "
        "or at capacity (its queue is shared with the chat application). "
        "Retry after Retry-After seconds."
    ),
}


def _description(status: int, names: List[str]) -> str:
    if status in _DESCRIPTIONS and set(names) <= {"idempotency_conflict", "model_recovering", "model_unavailable"}:
        return _DESCRIPTIONS[status]
    return "; ".join(sorted(names))


def _error_responses(*codes: str) -> Dict[str, Any]:
    """`{status: response}` for the codes an operation can actually raise.

    Grouped by STATUS, because that is what a client switches on first, with
    every code that shares a status named in the description — `429` is three
    different reasons (rate, quota, concurrency) and a caller's reaction to all
    three is the same wait-and-retry, which is exactly why they share a type.
    """
    by_status: Dict[int, List[str]] = {}
    for code in codes:
        by_status.setdefault(errors.status_for(code), []).append(code)
    out: Dict[str, Any] = {}
    for status, names in sorted(by_status.items()):
        out[str(status)] = {
            "description": _description(status, names),
            "headers": (
                {
                    "Retry-After": {
                        "description": "Seconds to wait before retrying.",
                        "schema": {"type": "integer", "minimum": errors.MIN_RETRY_AFTER},
                    }
                }
                if status in (429, 503)
                else {
                    "Retry-After": {
                        "description": (
                            "Present only when the first request with this "
                            "Idempotency-Key is still running."
                        ),
                        "required": False,
                        "schema": {"type": "integer", "minimum": errors.MIN_RETRY_AFTER},
                    }
                }
                if status == 409
                else {}
            ),
            "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/ErrorEnvelope"}}
            },
        }
    return out


#: Every authenticated operation can fail this way, so it is written once.
#: `rate_limit_error` is here because every authenticated route spends one
#: request of the project's requests-per-minute — reads included, since
#: 2026-09-13 (before that five routes were unmetered and this document
#: advertised no 429 on them, which was at least consistent).
_ALWAYS = (
    "invalid_api_key",
    "insufficient_scope",
    "origin_not_allowed",
    "rate_limit_error",
    "internal_error",
)
#: The codes above that exist only because a usage limit is enforced. Dropped
#: from the document when PUBLIC_API_ENFORCE_LIMITS is off (2026-09-13).
_ALWAYS_LIMIT_CODES = frozenset({"rate_limit_error"})
#: Everything that can go wrong on the way to the engine.
_GENERATION = (
    "invalid_request_error",
    "context_length_exceeded",
    "model_not_found",
    "idempotency_conflict",
    "request_too_large",
    "rate_limit_error",
    "quota_exceeded",
    "concurrency_limit_exceeded",
    "model_recovering",
    "model_unavailable",
    "timeout",
)
#: All three 429 codes go with the limits (2026-09-13). They used to stay:
#: the Idempotency-Key "still running" answer was a `rate_limit_error` and a
#: full admission lane a `concurrency_limit_exceeded`. Those are now a 409
#: and a 503, so with the limits off nothing on a generating route is a 429.
_GENERATION_LIMIT_CODES = frozenset({"rate_limit_error", "quota_exceeded", "concurrency_limit_exceeded"})


def _always() -> tuple:
    if _limits_enforced():
        return _ALWAYS
    return tuple(code for code in _ALWAYS if code not in _ALWAYS_LIMIT_CODES)


def _generation() -> tuple:
    if _limits_enforced():
        return _GENERATION
    return tuple(code for code in _GENERATION if code not in _GENERATION_LIMIT_CODES)


def _request_id_header() -> Dict[str, Any]:
    headers: Dict[str, Any] = {
        "X-Request-Id": {
            "description": "Echoed on every response; quote it in a support request.",
            "schema": {"type": "string"},
        },
    }
    # Only when a limit exists to describe (owner decision 2026-09-13): the
    # server sends no RateLimit field with the limits off.
    if _limits_enforced():
        headers["RateLimit"] = {
            "description": 'Remaining requests in the window, RFC 9239 style: `"requests";r=59;t=41`.',
            "schema": {"type": "string"},
        }
    return headers


def _schemas() -> Dict[str, Any]:
    """The types on the wire. Each is the shape `app/publicapi/models.py`
    validates or renders — not a restatement of it."""
    model = registry.PUBLIC_MODEL_IDS[0]
    return {
        "ErrorEnvelope": {
            "type": "object",
            "description": (
                "The ONE error shape, everywhere, including inside a stream. A "
                "client writes one parser and one retry table."
            ),
            "required": ["error"],
            "properties": {
                "error": {
                    "type": "object",
                    "required": ["message", "type", "code", "param", "request_id"],
                    "properties": {
                        "message": {"type": "string"},
                        "type": {
                            "type": "string",
                            "enum": sorted({spec.type for spec in errors.error_codes().values()}),
                        },
                        "code": {"type": "string", "enum": sorted(errors.error_codes())},
                        "param": {
                            "type": ["string", "null"],
                            "description": "The field at fault, when there is one.",
                        },
                        "request_id": {"type": ["string", "null"]},
                    },
                }
            },
            "example": {
                "error": {
                    "message": "The API key is invalid.",
                    "type": "authentication_error",
                    "code": "invalid_api_key",
                    "param": None,
                    "request_id": "req_8b1f0c2e",
                }
            },
        },
        "Usage": {
            "type": ["object", "null"],
            "description": (
                "Token counts as the engine reported them. **null — never 0 — "
                "when nothing was measured**: a zero would be a lie and an "
                "under-charge."
            ),
            "required": ["input_tokens", "output_tokens", "total_tokens"],
            "properties": {
                "input_tokens": {"type": "integer", "minimum": 0},
                "output_tokens": {"type": "integer", "minimum": 0},
                "total_tokens": {"type": "integer", "minimum": 0},
            },
        },
        "InputMessage": {
            "type": "object",
            "required": ["role", "content"],
            "properties": {
                "role": {"type": "string", "enum": ["system", "user", "assistant"]},
                "content": {"type": "string", "minLength": 1},
            },
        },
        "OutputText": {
            "type": "object",
            "required": ["type", "text"],
            "properties": {
                "type": {"type": "string", "enum": ["output_text"]},
                "text": {"type": "string"},
            },
        },
        "OutputMessage": {
            "type": "object",
            "required": ["type", "role", "content"],
            "properties": {
                "type": {"type": "string", "enum": ["message"]},
                "role": {"type": "string", "enum": ["assistant"]},
                "content": {
                    "type": "array",
                    "items": {"$ref": "#/components/schemas/OutputText"},
                },
            },
        },
        "Response": {
            "type": "object",
            "required": ["id", "object", "created_at", "status", "model", "output", "usage"],
            "properties": {
                "id": {"type": "string", "example": "resp_3f8a1c"},
                "object": {"type": "string", "enum": ["response"]},
                "created_at": {"type": "integer", "description": "Unix seconds."},
                "status": {
                    "type": "string",
                    "enum": ["queued", "in_progress", "completed", "failed", "cancelled"],
                },
                "model": {"type": "string", "example": model},
                "output": {
                    "type": "array",
                    "items": {"$ref": "#/components/schemas/OutputMessage"},
                },
                "usage": {"$ref": "#/components/schemas/Usage"},
                "error": {
                    "type": ["object", "null"],
                    "description": "Present only on a failed response.",
                    "properties": {
                        "code": {"type": "string"},
                        "message": {"type": "string"},
                    },
                },
            },
            "example": {
                "id": "resp_3f8a1c",
                "object": "response",
                "created_at": 1789200000,
                "status": "completed",
                "model": model,
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Retrieval-augmented generation puts a search step in front of the model…",
                            }
                        ],
                    }
                ],
                "usage": {"input_tokens": 37, "output_tokens": 112, "total_tokens": 149},
            },
        },
        "ResponsesRequest": {
            "type": "object",
            "additionalProperties": False,
            "description": (
                "A parameter this platform cannot honour is REJECTED, never "
                "silently ignored: an unknown field is a 400 naming it. Nothing "
                "here can change the project, key, model target or limits — "
                "those come from the API key."
            ),
            "required": ["model", "input"],
            "properties": {
                "model": {"type": "string", "maxLength": 64, "example": model},
                "input": {
                    "description": "A string, or a list of messages.",
                    "oneOf": [
                        {"type": "string", "minLength": 1},
                        {
                            "type": "array",
                            "minItems": 1,
                            "items": {"$ref": "#/components/schemas/InputMessage"},
                        },
                    ],
                },
                "instructions": {
                    "type": ["string", "null"],
                    "description": "Becomes the first system message.",
                },
                "stream": {"type": "boolean", "default": False},
                "background": {
                    "type": "boolean",
                    "default": False,
                    "description": "`stream` and `background` both true is refused.",
                },
                "max_output_tokens": {"type": ["integer", "null"], "minimum": 1},
                "temperature": {"type": ["number", "null"], "minimum": 0.0, "maximum": 2.0},
                "metadata": {
                    "type": "object",
                    "additionalProperties": {"type": "string", "maxLength": 512},
                    "description": "At most 16 string keys of 64 characters.",
                },
            },
            "example": {
                "model": model,
                "input": "Explain retrieval-augmented generation.",
                "instructions": "Answer in British English.",
                "stream": False,
                "max_output_tokens": 1000,
                "temperature": 0.2,
                "metadata": {"customer_request_id": "abc-123"},
            },
        },
        "Model": {
            "type": "object",
            "required": ["id", "object", "owned_by", "status", "capabilities"],
            "properties": {
                "id": {"type": "string", "example": model},
                "object": {"type": "string", "enum": ["model"]},
                "owned_by": {"type": "string", "example": "techsara"},
                "status": {"type": "string", "example": "available"},
                "capabilities": {
                    "type": "object",
                    "properties": {
                        "chat": {"type": "boolean"},
                        "streaming": {"type": "boolean"},
                        "vision": {"type": "boolean"},
                        "tools": {"type": "boolean"},
                        "embeddings": {"type": "boolean"},
                    },
                },
                "max_input_tokens": {"type": "integer"},
                "max_output_tokens": {"type": "integer"},
            },
        },
        "ModelList": {
            "type": "object",
            "required": ["object", "data"],
            "properties": {
                "object": {"type": "string", "enum": ["list"]},
                "data": {"type": "array", "items": {"$ref": "#/components/schemas/Model"}},
            },
        },
        "ChatCompletionRequest": {
            "type": "object",
            "additionalProperties": False,
            "description": (
                "The compatibility shape, on the same engine path. Only the "
                "fields listed are honoured; anything else is a 400."
            ),
            "required": ["model", "messages"],
            "properties": {
                "model": {"type": "string", "example": model},
                "messages": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"$ref": "#/components/schemas/InputMessage"},
                },
                "stream": {"type": "boolean", "default": False},
                "max_tokens": {"type": ["integer", "null"], "minimum": 1},
                "temperature": {"type": ["number", "null"], "minimum": 0.0, "maximum": 2.0},
                "stream_options": {
                    "type": ["object", "null"],
                    "additionalProperties": False,
                    "properties": {"include_usage": {"type": "boolean"}},
                },
            },
        },
        "ChatCompletion": {
            "type": "object",
            "required": ["id", "object", "created", "model", "choices", "usage"],
            "properties": {
                "id": {"type": "string"},
                "object": {"type": "string", "enum": ["chat.completion"]},
                "created": {"type": "integer"},
                "model": {"type": "string"},
                "choices": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "index": {"type": "integer"},
                            "message": {
                                "type": "object",
                                "properties": {
                                    "role": {"type": "string"},
                                    "content": {"type": "string"},
                                },
                            },
                            "finish_reason": {"type": ["string", "null"]},
                        },
                    },
                },
                "usage": {
                    "type": ["object", "null"],
                    "properties": {
                        "prompt_tokens": {"type": "integer"},
                        "completion_tokens": {"type": "integer"},
                        "total_tokens": {"type": "integer"},
                    },
                },
            },
        },
        "UsageList": {
            "type": "object",
            "required": ["object", "start_date", "end_date", "data"],
            "properties": {
                "object": {"type": "string", "enum": ["list"]},
                "start_date": {"type": "string", "format": "date"},
                "end_date": {"type": "string", "format": "date"},
                "data": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "date": {"type": "string", "format": "date"},
                            "requests": {"type": "integer"},
                            "input_tokens": {"type": "integer"},
                            "output_tokens": {"type": "integer"},
                            "errors": {"type": "integer"},
                            "rate_limited": {"type": "integer"},
                        },
                    },
                },
            },
        },
    }


def _stream_description() -> str:
    """CONTRACT §10, written out for a developer rather than referenced.

    An SSE body cannot be described by a JSON schema, so the grammar is prose
    — and prose a client author can implement from, which means naming the
    order, the numbering and the exactly-one-terminal rule rather than saying
    "server-sent events".
    """
    return (
        "When `stream` is true the body is `text/event-stream`. Each event is "
        "`event: <name>` plus a JSON `data` whose `type` repeats the name and "
        "whose `sequence_number` starts at 1 and increases by exactly 1:\n\n"
        "`response.created` → `response.in_progress` → "
        "`response.output_text.delta` (×N) → `response.output_text.done` → "
        "`response.completed`\n\n"
        "`response.queued` appears when the model is restarting and the request "
        "is waiting. The terminals are `response.completed`, `response.failed` "
        "and `error`, and **exactly one** is ever sent. `usage` is carried on "
        "the terminal event only; every earlier event carries `usage: null`. A "
        "comment line (`: ping`) arrives at least every 15 seconds so an idle "
        "proxy cannot close the connection — ignore it, as every conforming SSE "
        "parser already does."
    )


def _paths() -> Dict[str, Any]:
    scope_doc = {scope.value: SCOPE_DESCRIPTIONS[scope] for scope in Scope}
    json_ok = lambda ref: {  # noqa: E731 - a local alias, read once each use
        "description": "OK",
        "headers": _request_id_header(),
        "content": {"application/json": {"schema": {"$ref": ref}}},
    }
    return {
        "/v1/models": {
            "get": {
                "tags": ["Models"],
                "operationId": "listModels",
                "summary": "List the models this key may use",
                "description": f"Scope `models.read` — {scope_doc['models.read']}",
                "security": [{"bearerAuth": []}],
                "responses": {
                    "200": json_ok("#/components/schemas/ModelList"),
                    **_error_responses(*_always()),
                },
            }
        },
        "/v1/models/{model}": {
            "get": {
                "tags": ["Models"],
                "operationId": "getModel",
                "summary": "Read one model",
                "description": (
                    "Scope `models.read`. A model this key may not use returns "
                    "**404**, the same answer as a model that does not exist: "
                    "the API is not a directory of what we run."
                ),
                "security": [{"bearerAuth": []}],
                "parameters": [
                    {
                        "name": "model",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                        "example": registry.PUBLIC_MODEL_IDS[0],
                    }
                ],
                "responses": {
                    "200": json_ok("#/components/schemas/Model"),
                    **_error_responses(*_always(), "model_not_found"),
                },
            }
        },
        "/v1/responses": {
            "post": {
                "tags": ["Responses"],
                "operationId": "createResponse",
                "summary": "Create a response",
                "description": (
                    f"Scope `responses.write`. {_stream_description()}\n\n"
                    "`background: true` returns **202** with a response id "
                    "before the work starts; read it back with "
                    "`GET /v1/responses/{id}`. `stream` and `background` "
                    "cannot both be true."
                ),
                "security": [{"bearerAuth": []}],
                "parameters": [
                    {
                        "name": "Idempotency-Key",
                        "in": "header",
                        "required": False,
                        "schema": {"type": "string", "maxLength": 255},
                        "description": (
                            "Retry safely. The same key with the same body "
                            "returns the original response; with a different "
                            "body it is a 409."
                        ),
                    }
                ],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/ResponsesRequest"}
                        }
                    },
                },
                "responses": {
                    "200": json_ok("#/components/schemas/Response"),
                    "202": {
                        "description": "Accepted — a background response.",
                        "headers": _request_id_header(),
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/Response"}
                            }
                        },
                    },
                    **_error_responses(*_always(), *_generation()),
                },
            }
        },
        "/v1/responses/{id}": {
            "get": {
                "tags": ["Responses"],
                "operationId": "getResponse",
                "summary": "Read a response",
                "description": (
                    "Scope `responses.read`. Scoped to the project the key "
                    "belongs to; another project's id reads as missing."
                ),
                "security": [{"bearerAuth": []}],
                "parameters": [
                    {
                        "name": "id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                        "example": "resp_3f8a1c",
                    }
                ],
                "responses": {
                    "200": json_ok("#/components/schemas/Response"),
                    **_error_responses(*_always(), "response_not_found"),
                },
            }
        },
        "/v1/responses/{id}/cancel": {
            "post": {
                "tags": ["Responses"],
                "operationId": "cancelResponse",
                "summary": "Cancel a response",
                "description": (
                    "Scope `responses.write`. Idempotent: cancelling a response "
                    "that has already finished is not an error and does not "
                    "change it."
                ),
                "security": [{"bearerAuth": []}],
                "parameters": [
                    {
                        "name": "id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {
                    "200": json_ok("#/components/schemas/Response"),
                    **_error_responses(*_always(), "response_not_found"),
                },
            }
        },
        "/v1/chat/completions": {
            "post": {
                "tags": ["Responses"],
                "operationId": "createChatCompletion",
                "summary": "Create a chat completion (compatibility shape)",
                "description": (
                    "Scope `responses.write`. The same engine path as "
                    "`/v1/responses`, framed the way an existing client library "
                    "expects: when `stream` is true the body is a sequence of "
                    "anonymous `data:` chunks ending with the literal "
                    "`data: [DONE]`. Ask for token counts with "
                    "`stream_options.include_usage`, which adds one final "
                    "chunk carrying `usage` and no choices."
                ),
                "security": [{"bearerAuth": []}],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/ChatCompletionRequest"}
                        }
                    },
                },
                "responses": {
                    "200": json_ok("#/components/schemas/ChatCompletion"),
                    **_error_responses(*_always(), *_generation()),
                },
            }
        },
        "/v1/usage": {
            "get": {
                "tags": ["Usage"],
                "operationId": "getUsage",
                "summary": "Read this project's usage",
                "description": (
                    "Scope `usage.read`. Daily counters for the key's own "
                    "project over an inclusive range of at most 93 days."
                ),
                "security": [{"bearerAuth": []}],
                "parameters": [
                    {
                        "name": "start_date",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "string", "format": "date"},
                        "example": "2026-09-01",
                    },
                    {
                        "name": "end_date",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "string", "format": "date"},
                        "example": "2026-09-13",
                    },
                ],
                "responses": {
                    "200": json_ok("#/components/schemas/UsageList"),
                    **_error_responses(*_always(), "invalid_request_error"),
                },
            }
        },
        "/v1/openapi.json": {
            "get": {
                "tags": ["Schema"],
                "operationId": "getOpenapi",
                "summary": "This document",
                "description": (
                    "No credential required: a client generator must be usable "
                    "before a key exists, and this describes nothing a key "
                    "would not already reveal."
                ),
                "security": [],
                "responses": {
                    "200": {
                        "description": "The public OpenAPI 3.1 document.",
                        "content": {"application/json": {"schema": {"type": "object"}}},
                    }
                },
            }
        },
    }


def public_openapi() -> Dict[str, Any]:
    """The whole document. Rebuilt on every call, never cached.

    The model ceilings inside it come from `registry`, which reads them from
    `settings` at call time — the main model has been swapped under a running
    deployment more than once, and a document cached at import would keep
    publishing the old window until somebody restarted the process.
    """
    return {
        "openapi": "3.1.0",
        "info": {
            "title": API_TITLE,
            "version": API_VERSION,
            "summary": "Generate text with TechSara's models from your own code.",
            "description": (
                "Authenticate with `Authorization: Bearer tsk_live_…`. Every "
                "response carries `X-Request-Id`. Every failure — including one "
                "that happens mid-stream — is the same envelope: "
                "`{\"error\": {\"message\", \"type\", \"code\", \"param\", "
                "\"request_id\"}}`.\n\n"
                "Token counts are **null, never 0**, when the engine did not "
                "report them."
                + (
                    ""
                    if _limits_enforced()
                    else "\n\nThere are no request, token-per-minute, daily or "
                    "concurrency limits: usage is recorded (see `/v1/usage`) "
                    "but never refused. The model's context window, "
                    "`max_output_tokens` ceilings and the request body size "
                    "cap still apply."
                )
            ),
            "contact": {"name": "TechSara Solutions", "url": "https://techsarasolutions.com"},
        },
        "servers": SERVERS,
        "tags": TAGS,
        "security": [{"bearerAuth": []}],
        "paths": _paths(),
        "components": {
            "securitySchemes": {
                "bearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "tsk_live_<public_id>_<secret>",
                    "description": _BEARER_DESCRIPTION,
                }
            },
            "schemas": _schemas(),
        },
    }


#: The alias `api_contract.py` tries second. Same document, so a rename in the
#: CI script's list cannot silently start validating something else.
build_openapi = public_openapi
