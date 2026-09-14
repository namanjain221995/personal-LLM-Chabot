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

ELEVEN OPERATIONS SINCE 2026-09-13 (owner request: every model TechSara runs
on `/v1`). `POST /v1/embeddings`, `POST /v1/rerank` and
`POST /v1/audio/transcriptions` joined the eight; the Model object grew
`kind`, `endpoints`, nullable ceilings and `limits`; a Response carries the
`max_output_tokens` actually applied and `incomplete_details`; and the two
generating routes accept image parts. `public-api-surface.txt` must gain the
three new lines in the same commit — the gate is red until it does, by design.

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

import sys

from typing import Any, Dict, List

from ..apiplatform.scopes import SCOPE_DESCRIPTIONS, Scope
from ..config import _float, _int, settings
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
    {"name": "Embeddings", "description": "Turn text into vectors with techsara-embed."},
    {"name": "Rerank", "description": "Order documents by relevance to a query with techsara-rerank."},
    {"name": "Audio", "description": "Transcribe speech with techsara-whisper."},
    {"name": "Usage", "description": "This project's own token and request counters."},
    {"name": "Schema", "description": "This document."},
]

_BEARER_DESCRIPTION = (
    "An API key, sent as `Authorization: Bearer tsk_live_…`. This is the ONLY "
    "credential the API accepts: a browser session cookie is ignored, and no "
    "response ever sets `Access-Control-Allow-Credentials`."
)


def _setting_int(name: str, default: int) -> int:
    """A PUBLIC_API_* number, read now: `settings` when config.py names it,
    else the environment through config.py's own `_int` rule. The document
    states the ceilings the server enforces, so it reads them the same way."""
    value = getattr(settings, name.lower(), None)
    return int(value) if value is not None else int(_int(name, default))


def _setting_float(name: str, default: float) -> float:
    value = getattr(settings, name.lower(), None)
    return float(value) if value is not None else float(_float(name, default))


def _model_id(name: str, fallback: str) -> str:
    """A public id constant from `registry`, by name — the document's examples
    use the ids the registry declares, never a second spelling of them."""
    return str(getattr(registry, name, fallback))


def _flagship_ceilings() -> Dict[str, Any]:
    """techsara-35b's published numbers, for the prose. Empty when the
    registry cannot build (a misconfigured deployment still gets a document)."""
    try:
        for model in registry.declared_models():
            if model.id == registry.TECHSARA_35B:
                return {
                    "context_window": getattr(model, "context_window", None),
                    "max_output_tokens": getattr(model, "max_output_tokens", None),
                    "default_max_output_tokens": getattr(model, "default_max_output_tokens", None),
                }
    except Exception:  # noqa: BLE001 - the document must build regardless
        return {}
    return {}


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


def _error_responses(*codes: str, descriptions: Dict[int, str] | None = None) -> Dict[str, Any]:
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
            "description": (descriptions or {}).get(status) or _description(status, names),
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


#: What can go wrong on the three non-generating endpoints (2026-09-13). No
#: `idempotency_conflict` (the header is refused with a 400) and no
#: `model_recovering` (these engines have no recovery queue); the limit codes
#: go with the switch exactly as on the generating routes.
_SIDECAR = (
    "invalid_request_error",
    "context_length_exceeded",
    "model_not_found",
    "request_too_large",
    "quota_exceeded",
    "model_unavailable",
    "timeout",
)
_SIDECAR_LIMIT_CODES = frozenset({"quota_exceeded"})


def _sidecar(*, token_inputs: bool = True) -> tuple:
    codes = tuple(
        code
        for code in _SIDECAR
        if (_limits_enforced() or code not in _SIDECAR_LIMIT_CODES)
        and (token_inputs or code != "context_length_exceeded")
    )
    return codes


def _capacity_description(engine_words: str) -> str:
    """The 503 on an endpoint whose engine the chat application shares. Says
    "at capacity" in those words, and says it is not per caller."""
    wait = _setting_float("PUBLIC_API_GATE_WAIT_S", 30.0)
    return (
        f"model_unavailable — the {engine_words} is down, or at capacity: public "
        "requests to it share ONE queue for every project and key (the chat "
        f"application keeps priority), and a request that has waited {wait:g} "
        "seconds is answered with this. It is not a per-caller limit. Retry "
        "after Retry-After seconds."
    )


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


#: The one image form accepted: an inline data: URL of one of four types.
#: vLLM FETCHES a remote image URL from inside the cluster network (no
#: --allowed-media-domains is set), so an http(s) URL would let a caller probe
#: internal hosts; it is refused, and this pattern says so to a generator.
_DATA_URL_PATTERN = "^data:image/(png|jpeg|webp|gif);base64,"


def _image_description() -> str:
    image_mib = _setting_int("PUBLIC_API_MAX_IMAGE_BYTES", 10_485_760) / (1024 * 1024)
    media_mib = _setting_int("PUBLIC_API_MAX_MEDIA_BODY_BYTES", 20_971_520) / (1024 * 1024)
    text_mib = _setting_int("PUBLIC_API_MAX_BODY_BYTES", 1024 * 1024) / (1024 * 1024)
    return (
        "An image, inline. Only `data:image/png|jpeg|webp|gif;base64,…` URLs "
        "are accepted — a remote URL is refused, because the engine would "
        "fetch it from inside our network. The bytes must match the declared "
        f"type; each decoded image is at most {image_mib:g} MiB; a request "
        f"carrying images may be up to {media_mib:g} MiB on the wire while its "
        f"text still counts against the {text_mib:g} MiB text rule. The number "
        "of images per request is the model's `limits.max_images_per_request`; "
        "`techsara-ocr` takes exactly one and, with no text part, reads it with "
        "the prompt `OCR`."
    )


def _max_output_description() -> str:
    """The 1M-output rules, with the flagship's numbers read from the registry."""
    ceilings = _flagship_ceilings()
    ceiling = ceilings.get("max_output_tokens")
    default = ceilings.get("default_max_output_tokens")
    flagship = registry.TECHSARA_35B
    numbers = (
        f" For `{flagship}` the ceiling is {int(ceiling):,} and the default {int(default):,}."
        if ceiling and default
        else ""
    )
    return (
        "The most tokens to generate: 1 up to the model's `max_output_tokens` "
        "(`GET /v1/models`); above it is a 400. Omitted, the model's "
        f"`default_max_output_tokens` applies.{numbers} Input and output share "
        "the context window, so a value larger than the room the input leaves "
        "is CLAMPED to that room, not refused — the response's "
        "`max_output_tokens` says what was applied. A long generation takes "
        "hours (a million tokens at 70-100 tokens a second is roughly three), "
        "so use `stream: true` or `background: true` above about 5,000 output "
        "tokens: a synchronous request sends no byte until it finishes, and the "
        "public hostname closes a silent connection after 100 seconds."
        f"{_wall_clock_caveat()}"
    )


def _wall_clock_caveat() -> str:
    """Said while it is true, gone when it is not (adversarial review
    2026-09-13): until llm.py honours a per-request wall clock, the flagship's
    generations stop at the chat app's GEN_WALL_CLOCK_S whatever the ceiling."""
    if registry.per_request_wall_clock_live():
        return ""
    seconds = registry.chat_app_wall_clock_s()
    low, high = int(seconds * 71) // 1000 * 1000, int(seconds * 101) // 1000 * 1000
    return (
        f" NOT YET LIVE ON THIS DEPLOYMENT: a `{registry.TECHSARA_35B}` generation "
        f"is still stopped after {int(seconds):,} seconds (about {low:,}-{high:,} "
        "tokens at the measured 71-101 tokens a second), ending `failed` with "
        "`timeout` and keeping its partial output, until the per-request wall "
        "clock lands."
    )


def _sidecar_schemas() -> Dict[str, Any]:
    """The three non-generating endpoints' types (2026-09-13), with the
    ceilings the server enforces read from the same settings it reads."""
    embed = _model_id("TECHSARA_EMBED", "techsara-embed")
    rerank = _model_id("TECHSARA_RERANK", "techsara-rerank")
    whisper = _model_id("TECHSARA_WHISPER", "techsara-whisper")
    max_inputs = max(1, _setting_int("PUBLIC_API_EMBED_MAX_INPUTS", 256))
    embed_window = max(1, _setting_int("PUBLIC_API_EMBED_CONTEXT_TOKENS", 4096))
    max_documents = max(1, _setting_int("PUBLIC_API_RERANK_MAX_DOCUMENTS", 100))
    rerank_window = max(1, _setting_int("PUBLIC_API_RERANK_CONTEXT_TOKENS", 4096))
    audio_seconds = max(1, _setting_int("PUBLIC_API_MAX_AUDIO_SECONDS", 300))
    audio_mib = max(1, _setting_int("PUBLIC_API_MAX_AUDIO_BYTES", 26_214_400)) / (1024 * 1024)
    return {
        "EmbeddingsRequest": {
            "type": "object",
            "additionalProperties": False,
            "description": (
                "Embedded exactly as sent — no instruction is added and nothing "
                "is trimmed. For search queries this model works best with "
                "the prefix `Instruct: <task>\nQuery: `; documents are sent "
                "plain. `dimensions`, `user` and token-array input are refused "
                "by name."
            ),
            "required": ["model", "input"],
            "properties": {
                "model": {"type": "string", "maxLength": 64, "example": embed},
                "input": {
                    "description": (
                        f"A string, or 1-{max_inputs} strings. Each must be "
                        f"non-empty and at most {embed_window:,} tokens."
                    ),
                    "oneOf": [
                        {"type": "string", "minLength": 1},
                        {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": max_inputs,
                            "items": {"type": "string", "minLength": 1},
                        },
                    ],
                },
                "encoding_format": {
                    "type": "string",
                    "enum": ["float", "base64"],
                    "default": "float",
                    "description": "`base64` is little-endian float32, base64-encoded.",
                },
            },
            "example": {"model": embed, "input": ["The first passage.", "The second passage."]},
        },
        "Embedding": {
            "type": "object",
            "required": ["object", "index", "embedding"],
            "properties": {
                "object": {"type": "string", "enum": ["embedding"]},
                "index": {"type": "integer", "minimum": 0},
                "embedding": {
                    "oneOf": [
                        {"type": "array", "items": {"type": "number"}},
                        {"type": "string", "contentEncoding": "base64"},
                    ],
                    "description": "1,024 numbers, or their base64 form.",
                },
            },
        },
        "EmbeddingList": {
            "type": "object",
            "required": ["object", "data", "model", "usage"],
            "properties": {
                "object": {"type": "string", "enum": ["list"]},
                "data": {"type": "array", "items": {"$ref": "#/components/schemas/Embedding"}},
                "model": {"type": "string", "example": embed},
                "usage": {
                    "type": ["object", "null"],
                    "description": "null — never 0 — when the engine did not report counts.",
                    "required": ["prompt_tokens", "total_tokens"],
                    "properties": {
                        "prompt_tokens": {"type": "integer", "minimum": 0},
                        "total_tokens": {"type": "integer", "minimum": 0},
                    },
                },
            },
        },
        "RerankDocument": {
            "type": "object",
            "additionalProperties": False,
            "required": ["text"],
            "properties": {"text": {"type": "string", "minLength": 1}},
        },
        "RerankRequest": {
            "type": "object",
            "additionalProperties": False,
            "description": (
                "Each document is scored against the query with the model "
                "card's template; nothing is truncated. Each (query, document) "
                f"pair must fit {rerank_window:,} tokens, or the request is a "
                "400 naming the document."
            ),
            "required": ["model", "query", "documents"],
            "properties": {
                "model": {"type": "string", "maxLength": 64, "example": rerank},
                "query": {"type": "string", "minLength": 1},
                "documents": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": max_documents,
                    "items": {
                        "oneOf": [
                            {"type": "string", "minLength": 1},
                            {"$ref": "#/components/schemas/RerankDocument"},
                        ]
                    },
                },
                "top_n": {"type": ["integer", "null"], "minimum": 1},
                "return_documents": {"type": "boolean", "default": False},
                "instruction": {
                    "type": ["string", "null"],
                    "maxLength": 512,
                    "description": "What relevance means for this search. Defaults to web-passage retrieval.",
                },
            },
            "example": {
                "model": rerank,
                "query": "At what temperature does water boil at sea level?",
                "documents": [
                    "The museum opens at nine.",
                    "At sea level, water boils at 100 degrees Celsius.",
                ],
                "top_n": 1,
            },
        },
        "RerankResult": {
            "type": "object",
            "required": ["index", "relevance_score"],
            "properties": {
                "index": {"type": "integer", "minimum": 0},
                "relevance_score": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "description": "The model's probability that the document answers the query.",
                },
                "document": {"$ref": "#/components/schemas/RerankDocument"},
            },
        },
        "RerankResponse": {
            "type": "object",
            "required": ["id", "object", "model", "results", "usage"],
            "properties": {
                "id": {"type": "string", "example": "rrk_5d1e9a0b7c3f2e4d6a8b0c1d"},
                "object": {"type": "string", "enum": ["rerank"]},
                "model": {"type": "string", "example": rerank},
                "results": {
                    "type": "array",
                    "description": "Highest relevance first; equal scores by input order; cut to top_n.",
                    "items": {"$ref": "#/components/schemas/RerankResult"},
                },
                "usage": {
                    "type": ["object", "null"],
                    "required": ["input_tokens", "total_tokens"],
                    "properties": {
                        "input_tokens": {"type": "integer", "minimum": 0},
                        "total_tokens": {"type": "integer", "minimum": 0},
                    },
                },
            },
        },
        "TranscriptionRequest": {
            "type": "object",
            "additionalProperties": False,
            "required": ["file", "model"],
            "properties": {
                "file": {
                    "type": "string",
                    "contentMediaType": "application/octet-stream",
                    "description": (
                        f"The audio, at most {audio_mib:g} MiB and {audio_seconds} "
                        "seconds. The part's Content-Type must be an audio type "
                        "(audio/mpeg, audio/wav, audio/webm, audio/mp4, audio/ogg, "
                        "audio/flac, …)."
                    ),
                },
                "model": {"type": "string", "example": whisper},
                "language": {
                    "type": "string",
                    "description": (
                        "An ISO-639-1 code, or `auto` (the default). Auto-detection "
                        "is recommended: forcing a language forces the decoder, and "
                        "speech in another language comes back translated or garbled."
                    ),
                    "example": "auto",
                },
                "response_format": {
                    "type": "string",
                    "enum": ["json", "text", "verbose_json"],
                    "default": "json",
                },
                "timestamp_granularities[]": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["segment"]},
                    "description": "Only `segment`, and only with `verbose_json`.",
                },
            },
        },
        "TranscriptionUsage": {
            "type": ["object", "null"],
            "required": ["type", "seconds"],
            "properties": {
                "type": {"type": "string", "enum": ["duration"]},
                "seconds": {"type": "integer", "minimum": 0},
            },
        },
        "Transcription": {
            "type": "object",
            "required": ["text", "usage"],
            "properties": {
                "text": {"type": "string"},
                "usage": {"$ref": "#/components/schemas/TranscriptionUsage"},
            },
        },
        "TranscriptionSegment": {
            "type": "object",
            "required": ["id", "start", "end", "text"],
            "properties": {
                "id": {"type": "integer", "minimum": 0},
                "start": {"type": "number", "minimum": 0},
                "end": {"type": "number", "minimum": 0},
                "text": {"type": "string"},
            },
        },
        "VerboseTranscription": {
            "type": "object",
            "required": ["task", "language", "duration", "text", "segments", "usage"],
            "properties": {
                "task": {"type": "string", "enum": ["transcribe"]},
                "language": {
                    "type": ["string", "null"],
                    "description": "The language heard, by name in lower case (`english`).",
                },
                "duration": {"type": ["number", "null"], "minimum": 0},
                "text": {"type": "string"},
                "segments": {
                    "type": "array",
                    "items": {"$ref": "#/components/schemas/TranscriptionSegment"},
                },
                "usage": {"$ref": "#/components/schemas/TranscriptionUsage"},
            },
        },
    }


def _sidecar_paths(scope_doc: Dict[str, str], json_ok: Any) -> Dict[str, Any]:
    """The three operations added on 2026-09-13."""
    no_idempotency = (
        " `Idempotency-Key` is not accepted here (a 400 naming it): the request "
        "changes nothing a retry could repeat."
    )
    wait = _setting_float("PUBLIC_API_GATE_WAIT_S", 30.0)
    return {
        "/v1/embeddings": {
            "post": {
                "tags": ["Embeddings"],
                "operationId": "createEmbedding",
                "summary": "Create embeddings",
                "description": (
                    f"Scope `embeddings.write` — {scope_doc['embeddings.write']} "
                    "Model `techsara-embed`; any other model is a 400 naming "
                    "`model`. An input over the model's window is a 400 "
                    "`context_length_exceeded` whose `param` names it "
                    f"(`input.3`). The request waits up to {wait:g} seconds for "
                    "the shared engine before a 503." + no_idempotency
                ),
                "security": [{"bearerAuth": []}],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/EmbeddingsRequest"}
                        }
                    },
                },
                "responses": {
                    "200": json_ok("#/components/schemas/EmbeddingList"),
                    **_error_responses(
                        *_always(),
                        *_sidecar(),
                        descriptions={503: _capacity_description("embedding engine")},
                    ),
                },
            }
        },
        "/v1/rerank": {
            "post": {
                "tags": ["Rerank"],
                "operationId": "createRerank",
                "summary": "Rerank documents",
                "description": (
                    f"Scope `rerank.write` — {scope_doc['rerank.write']} Model "
                    "`techsara-rerank`. Scores are returned as the model gives "
                    "them, equal ones included." + no_idempotency
                ),
                "security": [{"bearerAuth": []}],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/RerankRequest"}
                        }
                    },
                },
                "responses": {
                    "200": json_ok("#/components/schemas/RerankResponse"),
                    **_error_responses(
                        *_always(),
                        *_sidecar(),
                        descriptions={503: _capacity_description("reranking engine")},
                    ),
                },
            }
        },
        "/v1/audio/transcriptions": {
            "post": {
                "tags": ["Audio"],
                "operationId": "createTranscription",
                "summary": "Transcribe audio",
                "description": (
                    f"Scope `audio.write` — {scope_doc['audio.write']} Model "
                    "`techsara-whisper`, `multipart/form-data`. The audio is held "
                    "in memory for the length of the request and never stored. "
                    "Public transcriptions run one at a time across the fleet and "
                    "give way to people dictating in the TechSara app; keep clips "
                    "short enough to finish inside a synchronous request." + no_idempotency
                ),
                "security": [{"bearerAuth": []}],
                "requestBody": {
                    "required": True,
                    "content": {
                        "multipart/form-data": {
                            "schema": {"$ref": "#/components/schemas/TranscriptionRequest"},
                            "encoding": {"file": {"contentType": "audio/*, video/webm, video/mp4"}},
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK — `json` and `verbose_json` as JSON, `text` as plain text.",
                        "headers": _request_id_header(),
                        "content": {
                            "application/json": {
                                "schema": {
                                    "oneOf": [
                                        {"$ref": "#/components/schemas/Transcription"},
                                        {"$ref": "#/components/schemas/VerboseTranscription"},
                                    ]
                                }
                            },
                            "text/plain": {"schema": {"type": "string"}},
                        },
                    },
                    **_error_responses(
                        *_always(),
                        *_sidecar(token_inputs=False),
                        descriptions={503: _capacity_description("speech engine")},
                    ),
                },
            }
        },
    }


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
        "InputText": {
            "type": "object",
            "additionalProperties": False,
            "required": ["type", "text"],
            "properties": {
                "type": {"type": "string", "enum": ["input_text"]},
                "text": {"type": "string", "minLength": 1},
            },
        },
        "InputImage": {
            "type": "object",
            "additionalProperties": False,
            "description": _image_description(),
            "required": ["type", "image_url"],
            "properties": {
                "type": {"type": "string", "enum": ["input_image"]},
                "image_url": {
                    "type": "string",
                    "pattern": _DATA_URL_PATTERN,
                    "description": "A data: URL. http(s) and every other scheme is refused.",
                },
                "detail": {"type": "string", "enum": ["auto"]},
            },
        },
        "InputMessage": {
            "type": "object",
            "additionalProperties": False,
            "required": ["role", "content"],
            "properties": {
                "role": {"type": "string", "enum": ["system", "user", "assistant"]},
                "content": {
                    "description": (
                        "A string, or a list of parts. Image parts are accepted "
                        "on `user` messages only, and only by a model whose "
                        "`capabilities.vision` is true."
                    ),
                    "oneOf": [
                        {"type": "string", "minLength": 1},
                        {
                            "type": "array",
                            "minItems": 1,
                            "items": {
                                "oneOf": [
                                    {"$ref": "#/components/schemas/InputText"},
                                    {"$ref": "#/components/schemas/InputImage"},
                                ]
                            },
                        },
                    ],
                },
            },
        },
        "ChatTextPart": {
            "type": "object",
            "additionalProperties": False,
            "required": ["type", "text"],
            "properties": {
                "type": {"type": "string", "enum": ["text"]},
                "text": {"type": "string", "minLength": 1},
            },
        },
        "ChatImagePart": {
            "type": "object",
            "additionalProperties": False,
            "description": _image_description(),
            "required": ["type", "image_url"],
            "properties": {
                "type": {"type": "string", "enum": ["image_url"]},
                "image_url": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["url"],
                    "properties": {
                        "url": {"type": "string", "pattern": _DATA_URL_PATTERN},
                        "detail": {"type": "string", "enum": ["auto"]},
                    },
                },
            },
        },
        "ChatMessage": {
            "type": "object",
            "additionalProperties": False,
            "required": ["role", "content"],
            "properties": {
                "role": {"type": "string", "enum": ["system", "user", "assistant"]},
                "content": {
                    "oneOf": [
                        {"type": "string", "minLength": 1},
                        {
                            "type": "array",
                            "minItems": 1,
                            "items": {
                                "oneOf": [
                                    {"$ref": "#/components/schemas/ChatTextPart"},
                                    {"$ref": "#/components/schemas/ChatImagePart"},
                                ]
                            },
                        },
                    ]
                },
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
            "required": [
                "id",
                "object",
                "created_at",
                "status",
                "model",
                "output",
                "usage",
                "max_output_tokens",
                "incomplete_details",
            ],
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
                "max_output_tokens": {
                    "type": ["integer", "null"],
                    "minimum": 1,
                    "description": (
                        "The output ceiling APPLIED to this generation, after "
                        "clamping to the room the input left in the context "
                        "window. Before generation (the 202, `response.created`, "
                        "`response.queued`, `response.in_progress`) it is the "
                        "planned value; the terminal event and a later read carry "
                        "the exact applied value, which is never larger. null "
                        "only on a response stored before this field existed."
                    ),
                },
                "incomplete_details": {
                    "type": ["object", "null"],
                    "description": (
                        "`{\"reason\": \"max_output_tokens\"}` when the model "
                        "stopped because it reached `max_output_tokens`, else "
                        "null. `status` stays `completed`: unlike the upstream "
                        "API this platform has no `incomplete` status."
                    ),
                    "required": ["reason"],
                    "properties": {"reason": {"type": "string", "enum": ["max_output_tokens"]}},
                },
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
                "max_output_tokens": 1000,
                "incomplete_details": None,
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
                    "description": (
                        "Run detached and return 202. With `stream: true` too, the "
                        "connection follows the job's events instead, and leaving it "
                        "cancels nothing. Requires `store: true`."
                    ),
                },
                "store": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "`true`: the generation is durable — it survives a service "
                        "restart and can be resumed with `GET /v1/responses/{id}"
                        "?stream=true&starting_after=N`. `false`: nothing is stored; "
                        "it is cancelled if the connection or the service goes away."
                    ),
                },
                "max_output_tokens": {
                    "type": ["integer", "null"],
                    "minimum": 1,
                    "description": _max_output_description(),
                },
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
            "description": (
                "One model this key may use. Additive over the first release: "
                "a client that reads only `id` and `capabilities` still works. "
                "The ceilings are null — never 0 — where a model has none (an "
                "embedding model generates nothing; speech is limited in "
                "seconds, see `limits`)."
            ),
            "required": [
                "id",
                "object",
                "owned_by",
                "status",
                "kind",
                "capabilities",
                "endpoints",
                "context_window",
                "max_input_tokens",
                "max_output_tokens",
                "default_max_output_tokens",
                "limits",
            ],
            "properties": {
                "id": {"type": "string", "example": model},
                "object": {"type": "string", "enum": ["model"]},
                "owned_by": {"type": "string", "example": "techsara"},
                "status": {"type": "string", "example": "available"},
                "kind": {
                    "type": "string",
                    "enum": ["chat", "embedding", "rerank", "transcription"],
                },
                "capabilities": {
                    "type": "object",
                    "required": [
                        "chat",
                        "streaming",
                        "vision",
                        "tools",
                        "embeddings",
                        "rerank",
                        "audio_transcription",
                        "ocr",
                        "background",
                    ],
                    "properties": {
                        "chat": {"type": "boolean"},
                        "streaming": {"type": "boolean"},
                        "vision": {"type": "boolean"},
                        "tools": {"type": "boolean"},
                        "embeddings": {"type": "boolean"},
                        "rerank": {"type": "boolean"},
                        "audio_transcription": {"type": "boolean"},
                        "ocr": {"type": "boolean"},
                        "background": {"type": "boolean"},
                    },
                },
                "endpoints": {
                    "type": "array",
                    "description": "The paths this model may be used on; any other is a 400.",
                    "items": {"type": "string", "example": "/v1/responses"},
                },
                "context_window": {"type": ["integer", "null"], "minimum": 1},
                "max_input_tokens": {"type": ["integer", "null"], "minimum": 1},
                "max_output_tokens": {"type": ["integer", "null"], "minimum": 1},
                "default_max_output_tokens": {"type": ["integer", "null"], "minimum": 1},
                "limits": {
                    "type": "object",
                    "description": "Per-request technical ceilings. Keys appear only where they apply.",
                    "properties": {
                        "max_images_per_request": {"type": "integer", "minimum": 1},
                        "max_inputs_per_request": {"type": "integer", "minimum": 1},
                        "max_documents_per_request": {"type": "integer", "minimum": 1},
                        "embedding_dimensions": {"type": "integer", "minimum": 1},
                        "max_audio_seconds": {"type": "integer", "minimum": 1},
                        "max_audio_bytes": {"type": "integer", "minimum": 1},
                        "response_formats": {"type": "array", "items": {"type": "string"}},
                    },
                },
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
                    "items": {"$ref": "#/components/schemas/ChatMessage"},
                },
                "stream": {"type": "boolean", "default": False},
                "store": {
                    "type": "boolean",
                    "default": True,
                    "description": "As on /v1/responses: `false` opts out of the durable event log.",
                },
                "max_tokens": {
                    "type": ["integer", "null"],
                    "minimum": 1,
                    "description": "The same ceiling and clamp as `max_output_tokens` on /v1/responses.",
                },
                "max_completion_tokens": {
                    "type": ["integer", "null"],
                    "minimum": 1,
                    "description": "An alias of `max_tokens`. Sending both is a 400.",
                },
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
                "max_output_tokens": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "TechSara extension: the output ceiling applied after "
                        "clamping to the context window. Also on the streamed "
                        "chunk that carries `finish_reason`. `finish_reason` is "
                        "`length` when it was reached."
                    ),
                },
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
        **_sidecar_schemas(),
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
        "`response.output_item.added` → `response.content_part.added` → "
        "`response.output_text.delta` (×N) → `response.output_text.done` → "
        "`response.output_text.annotation.added` (×K, only for `file_citation`s) → "
        "`response.content_part.done` → `response.output_item.done` → "
        "`response.completed`\n\n"
        "`response.queued` appears when the model is restarting and the request "
        "is waiting. The terminals are `response.completed`, `response.failed` "
        "and `error`, and **exactly one** is ever sent. `usage` is carried on "
        "the terminal event only; every earlier event carries `usage: null`. "
        "Every snapshot carries `max_output_tokens`: the planned ceiling before "
        "generation, the exact applied one on the terminal event, with "
        "`incomplete_details` saying whether it was reached. A comment line "
        "(`: ping`) arrives at least every 15 seconds for the whole life of the "
        "stream — hours, for a long generation — so an idle proxy cannot close "
        "the connection; ignore it, as every conforming SSE parser already does. "
        "A stream that ends without a terminal event did not finish: resume it "
        "with `GET /v1/responses/{id}?stream=true&starting_after=<last "
        "sequence_number>`."
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
                    "`GET /v1/responses/{id}`. With `stream: true` as well the "
                    "response is the job's event stream. Every generation with "
                    "`store: true` (the default) survives a service restart."
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
                "summary": "Read a response, or resume its stream",
                "description": (
                    "Scope `responses.read`. Scoped to the project the key "
                    "belongs to; another project's id reads as missing.\n\n"
                    "With `stream=true` the body is the response's event stream: "
                    "the events after `starting_after` (all of them without it) "
                    "are replayed, a running response is followed live, and the "
                    "stream closes after the terminal event. Only the key that "
                    "created the response (or a key of the same service account) "
                    "may stream it; any other key gets the same 404 as a missing "
                    "id. A response created with `store: false`, or whose events "
                    "are past retention, is a 400 on `stream`; `starting_after` "
                    "without `stream=true` is a 400."
                ),
                "security": [{"bearerAuth": []}],
                "parameters": [
                    {
                        "name": "id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                        "example": "resp_3f8a1c",
                    },
                    {
                        "name": "stream",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "boolean"},
                        "description": "Stream the response's events (Server-Sent Events).",
                    },
                    {
                        "name": "starting_after",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "integer", "minimum": 0},
                        "description": "Replay only the events whose `sequence_number` is greater.",
                    },
                ],
                "responses": {
                    "200": {
                        "description": "OK — the Response object, or its event stream with `stream=true`.",
                        "headers": _request_id_header(),
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/Response"}},
                            "text/event-stream": {"schema": {"type": "string"}},
                        },
                    },
                    **_error_responses(*_always(), "invalid_request_error", "response_not_found"),
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
        **_sidecar_paths(scope_doc, json_ok),
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

    FILES (files-hookup, 2026-09-13): the fourteen `/v1/files` and
    `/v1/uploads` operations and the file model-input parts are described
    exactly when the router serves them (`router.FILES_MOUNTED`), by
    `publicapi/files/openapi_doc.py`.
    """
    document = _base_openapi()
    try:
        from . import router as _router

        mounted = bool(getattr(_router, "FILES_MOUNTED", False))
    except Exception:  # noqa: BLE001 - the document must build without the router
        mounted = False
    if not mounted:
        return document
    from .files import openapi_doc as _files_doc

    return _files_doc.extend(document, sys.modules[__name__])


def _base_openapi() -> Dict[str, Any]:
    return {
        "openapi": "3.1.0",
        "info": {
            "title": API_TITLE,
            "version": API_VERSION,
            "summary": (
                "Generate text, read images, embed, rerank and transcribe with "
                "TechSara's models from your own code."
            ),
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
                    "caps still apply, and a shared engine that is at capacity "
                    "answers 503 with Retry-After — for everyone at once, never "
                    "per caller."
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
