"""Request and response types for `/v1/embeddings`, `/v1/rerank` and
`/v1/audio/transcriptions` (owner request 2026-09-13: every model TechSara runs
on the public API).

THE SAME TWO RULES AS `models.py`, FOR THE SAME REASONS. A parameter the
platform cannot honour is REJECTED — `dimensions` on an embedding (the
engine serves 1,024 dimensions and nothing else), `user`, token-array input,
`temperature` or `prompt` on a transcription — so a caller relying on a knob
learns at once that it does nothing here, instead of receiving an answer that
quietly ignored it. And nothing in a body can name the project, key, engine or
limits: those come from the API key, and there is no field to put them in.

ONE DIFFERENCE FROM `models.py`, DELIBERATE: no `str_strip_whitespace`. An
embedding of `"  query"` is not the embedding of `"query"`, and a reranker
document's leading whitespace is part of the document. The caller's text is
embedded and scored exactly as sent; blank-only strings are refused instead of
being silently turned into empty ones.

THE NUMBERS ARE READ AT CALL TIME, never captured at import: each limit below
is a function that reads `settings` (the `PUBLIC_API_*` names the design gives
config.py) and falls back to the environment with config.py's own
blank-means-default rule, so this module works before config.py carries them
and a redeploy that moves a number moves it here too.
"""
from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Literal, Mapping, Optional, Sequence, Tuple, Union

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError, field_validator

from ..config import _float, _int, settings
from . import errors

# ------------------------------------------------------------- settings --


def setting_int(name: str, default: int) -> int:
    """`settings.<name.lower()>` when config.py defines it, else the
    environment parsed BY config.py's own `_int` (blank → the default), so the
    value cannot change on the day the attribute is added to `Settings` — the
    same rule `registry.setting_int` applies."""
    value = getattr(settings, name.lower(), None)
    if value is not None:
        return int(value)
    return int(_int(name, int(default)))


def setting_float(name: str, default: float) -> float:
    """As `setting_int`, with config.py's `_float` rule."""
    value = getattr(settings, name.lower(), None)
    if value is not None:
        return float(value)
    return float(_float(name, float(default)))


#: Qwen3-Embedding-0.6B's hidden size (config.json `hidden_size` 1024). A fact
#: about the checkpoint, not a knob: `dimensions` is refused rather than
#: honoured by truncation, because a Matryoshka cut the model was not asked
#: for is a different embedding space than the one the caller indexed with.
EMBEDDING_DIMENSIONS = 1024

#: The reranker's instruction is a sentence, not a second document.
RERANK_INSTRUCTION_MAX_CHARS = 512

#: The formats the speech engine answers (compose/whisper/server.py). `srt` and
#: `vtt` are OpenAI formats the engine does not produce; building them here
#: would be a subtitle writer nobody reviewed, so they are refused.
TRANSCRIPTION_FORMATS: Tuple[str, ...] = ("json", "text", "verbose_json")


def embed_max_inputs() -> int:
    """PUBLIC_API_EMBED_MAX_INPUTS (256): one request's worth of inputs."""
    return max(1, setting_int("PUBLIC_API_EMBED_MAX_INPUTS", 256))


def embed_context_tokens() -> int:
    """PUBLIC_API_EMBED_CONTEXT_TOKENS (4,096): the engine's `--max-model-len`
    (docker inspect sf-local-ai-vllm-embed-1, 2026-09-13). Not
    EMBED_CONTEXT_LENGTH, which says 32,768 and is wrong against the engine."""
    return max(1, setting_int("PUBLIC_API_EMBED_CONTEXT_TOKENS", 4096))


def rerank_max_documents() -> int:
    """PUBLIC_API_RERANK_MAX_DOCUMENTS (100)."""
    return max(1, setting_int("PUBLIC_API_RERANK_MAX_DOCUMENTS", 100))


def rerank_context_tokens() -> int:
    """PUBLIC_API_RERANK_CONTEXT_TOKENS (4,096) — per templated pair."""
    return max(1, setting_int("PUBLIC_API_RERANK_CONTEXT_TOKENS", 4096))


def max_audio_seconds() -> int:
    """PUBLIC_API_MAX_AUDIO_SECONDS (300). Half the engine's own 600: at the
    measured ~7 s of audio per wall-second a 300 s clip is ~43 s of decoding,
    which with a 30 s capacity wait still finishes inside Cloudflare's 100 s
    origin timeout for a synchronous request (2026-09-13 design)."""
    return max(1, setting_int("PUBLIC_API_MAX_AUDIO_SECONDS", 300))


def max_audio_bytes() -> int:
    """PUBLIC_API_MAX_AUDIO_BYTES (25 MiB): the file part itself."""
    return max(1, setting_int("PUBLIC_API_MAX_AUDIO_BYTES", 26_214_400))


def max_audio_body_bytes() -> int:
    """PUBLIC_API_MAX_AUDIO_BODY_BYTES (26 MiB): the whole multipart body —
    the file plus one MiB of form fields and boundaries."""
    return max(1, setting_int("PUBLIC_API_MAX_AUDIO_BODY_BYTES", 27_262_976))


def max_json_body_bytes() -> int:
    """The CONTRACT §12 JSON body cap, the same number `models.max_body_bytes`
    reads — 1 MiB unless PUBLIC_API_MAX_BODY_BYTES says otherwise."""
    return max(1, setting_int("PUBLIC_API_MAX_BODY_BYTES", 1024 * 1024))


# ---------------------------------------------------------- validation --

#: The same public-id shape `models.py` enforces: the id is echoed into
#: messages and written to the usage ledger.
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-]{0,63}$")


class _Forbid(BaseModel):
    """Forbid what we cannot honour. Whitespace is NOT stripped (module
    docstring)."""

    model_config = ConfigDict(extra="forbid")


def _model_id(value: str) -> str:
    if not _MODEL_ID_RE.match(value or ""):
        raise ValueError(
            "model must be a public model id: letters, digits, dot, dash, underscore or colon"
        )
    return value


def _param_from_loc(loc: Sequence[Any]) -> Optional[str]:
    """`("documents", 3, "text")` → `"documents.3.text"`, without pydantic's
    union-branch markers. Echoed only through `errors.ApiError`'s own
    `_echoable` check, because with `extra="forbid"` the name is the caller's."""
    parts = [
        str(part)
        for part in loc
        if str(part) not in ("str", "RerankDocument")
        and not str(part).startswith(("function-", "is-", "constrained-", "list["))
    ]
    return ".".join(parts) or None


def _refuse_validation(exc: ValidationError) -> errors.ApiError:
    raised = exc.errors()
    # The DEEPEST path describes what the caller sent; a union reports one
    # error per branch and the shallow one points at a shape they were not
    # attempting (the rule `models._most_specific` states).
    chosen = max(raised, key=lambda item: len(item.get("loc") or ()))
    message = str(chosen.get("msg") or "The request is not valid.")
    if message.startswith("Value error, "):
        message = message[len("Value error, "):]
    return errors.invalid_request(message, param=_param_from_loc(chosen.get("loc") or ()))


def _require_object(payload: Any) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise errors.invalid_request("The request body must be a JSON object.")
    return payload


# ------------------------------------------------------------ embeddings --


class EmbeddingsRequest(_Forbid):
    """`POST /v1/embeddings` — the OpenAI shape, less what the engine cannot do."""

    model: str = Field(min_length=1, max_length=64)
    input: Union[str, List[str]]
    encoding_format: Literal["float", "base64"] = "float"

    @field_validator("model")
    @classmethod
    def _model_shape(cls, value: str) -> str:
        return _model_id(value)

    def inputs(self) -> List[str]:
        return [self.input] if isinstance(self.input, str) else list(self.input)


def parse_embeddings_request(payload: Any) -> EmbeddingsRequest:
    """A decoded body → a validated request, or a 400 naming the field.

    Token-array input (`[[1, 2, 3]]` or `[1, 2, 3]`) is refused by NAME before
    pydantic sees it: the OpenAI shape allows it, and "input should be a valid
    string" would tell the caller nothing about why.
    """
    body = _require_object(payload)
    raw = body.get("input")
    if isinstance(raw, list) and any(
        isinstance(item, (int, float, list)) and not isinstance(item, bool) for item in raw
    ):
        raise errors.invalid_request(
            "Token-array input is not supported. Send text: a string or a list of strings.",
            param="input",
        )
    try:
        request = EmbeddingsRequest.model_validate(body)
    except ValidationError as exc:
        raise _refuse_validation(exc) from None
    inputs = request.inputs()
    if not inputs:
        raise errors.invalid_request("input must contain at least one string.", param="input")
    limit = embed_max_inputs()
    if len(inputs) > limit:
        raise errors.invalid_request(
            f"input may contain at most {limit} strings per request.", param="input"
        )
    for index, text in enumerate(inputs):
        if not text.strip():
            param = "input" if isinstance(request.input, str) else f"input.{index}"
            raise errors.invalid_request("An input string must not be empty.", param=param)
    return request


class EmbeddingObject(_Forbid):
    object: Literal["embedding"] = "embedding"
    index: int
    embedding: Union[List[float], str]


class EmbeddingsUsage(_Forbid):
    prompt_tokens: int
    total_tokens: int


class EmbeddingsResponse(_Forbid):
    object: Literal["list"] = "list"
    data: List[EmbeddingObject]
    model: str
    #: Null — never 0 — when the engine did not report counts (CONTRACT §9).
    usage: Optional[EmbeddingsUsage] = None


# ---------------------------------------------------------------- rerank --


class RerankDocument(_Forbid):
    text: str


class RerankRequest(_Forbid):
    """`POST /v1/rerank` — the Cohere/Jina shape."""

    model: str = Field(min_length=1, max_length=64)
    query: str
    documents: List[Union[str, RerankDocument]]
    top_n: Optional[StrictInt] = Field(default=None, ge=1)
    return_documents: StrictBool = False
    instruction: Optional[str] = Field(default=None, max_length=RERANK_INSTRUCTION_MAX_CHARS)

    @field_validator("model")
    @classmethod
    def _model_shape(cls, value: str) -> str:
        return _model_id(value)

    @field_validator("query")
    @classmethod
    def _query_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must not be empty")
        return value

    @field_validator("instruction")
    @classmethod
    def _instruction_is_not_blank(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not value.strip():
            raise ValueError("instruction must not be empty when present")
        return value

    def texts(self) -> List[str]:
        return [doc if isinstance(doc, str) else doc.text for doc in self.documents]


def parse_rerank_request(payload: Any) -> RerankRequest:
    body = _require_object(payload)
    try:
        request = RerankRequest.model_validate(body)
    except ValidationError as exc:
        raise _refuse_validation(exc) from None
    texts = request.texts()
    if not texts:
        raise errors.invalid_request("documents must contain at least one document.", param="documents")
    limit = rerank_max_documents()
    if len(texts) > limit:
        raise errors.invalid_request(
            f"documents may contain at most {limit} documents per request.", param="documents"
        )
    for index, text in enumerate(texts):
        if not text.strip():
            raise errors.invalid_request("A document must not be empty.", param=f"documents.{index}")
    return request


class RerankResult(_Forbid):
    index: int
    relevance_score: float
    document: Optional[RerankDocument] = None

    def to_wire(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"index": self.index, "relevance_score": self.relevance_score}
        if self.document is not None:
            out["document"] = {"text": self.document.text}
        return out


class RerankUsage(_Forbid):
    input_tokens: int
    total_tokens: int


class RerankResponse(_Forbid):
    id: str
    object: Literal["rerank"] = "rerank"
    model: str
    results: List[RerankResult]
    usage: Optional[RerankUsage] = None

    def to_wire(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "object": self.object,
            "model": self.model,
            "results": [result.to_wire() for result in self.results],
            "usage": None if self.usage is None else self.usage.model_dump(),
        }


def ranked(
    scores: Sequence[float], texts: Sequence[str], *, top_n: Optional[int], return_documents: bool
) -> List[RerankResult]:
    """Scores in document order → results, highest first, ties by index.

    Equal scores are returned as they are. The chat client refuses a pool of
    identical scores as "degenerate" (app/rerank.py) because its caller has a
    fallback order to keep; a public caller asked for scores and gets them.
    """
    order = sorted(range(len(scores)), key=lambda i: (-float(scores[i]), i))
    if top_n is not None:
        order = order[: int(top_n)]
    return [
        RerankResult(
            index=i,
            relevance_score=float(scores[i]),
            document=(RerankDocument(text=texts[i]) if return_documents else None),
        )
        for i in order
    ]


# -------------------------------------------------------- transcriptions --

#: The form fields `/v1/audio/transcriptions` honours. `temperature`, `prompt`
#: and everything else OpenAI's shape allows are refused by name: the engine
#: exposes neither, and a prompt that is accepted and dropped is exactly the
#: invisible no-op this platform does not ship.
TRANSCRIPTION_FIELDS: Tuple[str, ...] = (
    "model",
    "language",
    "response_format",
    "timestamp_granularities[]",
)


class TranscriptionRequest(_Forbid):
    model: str = Field(min_length=1, max_length=64)
    language: Optional[str] = None
    response_format: Literal["json", "text", "verbose_json"] = "json"
    timestamp_granularities: List[Literal["segment"]] = Field(default_factory=list)

    @field_validator("model")
    @classmethod
    def _model_shape(cls, value: str) -> str:
        return _model_id(value)

    @property
    def verbose(self) -> bool:
        return self.response_format == "verbose_json"


def _whisper_codes() -> frozenset:
    """The ISO codes Whisper identifies, from `app/asr.py`'s table (generated
    from the model's own tokenizer). Imported lazily: `asr` imports `metrics`,
    and this module must stay cheap to import."""
    try:
        from .. import asr

        return frozenset(str(code) for code in getattr(asr, "_LANGUAGE_CODES", {}).values())
    except Exception:  # noqa: BLE001 - a missing table must not refuse every language
        return frozenset()


def parse_transcription_form(fields: Mapping[str, Sequence[str]]) -> TranscriptionRequest:
    """The text fields of a transcription form → a validated request, or 400.

    The file part is checked by the route (it needs the byte caps and the
    content-type allowlist); this is everything else.
    """
    for name in fields:
        if name not in TRANSCRIPTION_FIELDS:
            raise errors.invalid_request(f"Unsupported field: {name}.", param=name)
    values: Dict[str, Any] = {}
    for name in ("model", "language", "response_format"):
        if name in fields:
            values[name] = fields[name][0]
    granularities = list(fields.get("timestamp_granularities[]") or [])
    if "model" not in values:
        raise errors.invalid_request("model is required.", param="model")
    response_format = values.get("response_format")
    if response_format is not None and response_format not in TRANSCRIPTION_FORMATS:
        raise errors.invalid_request(
            "response_format must be one of json, text or verbose_json.", param="response_format"
        )
    for granularity in granularities:
        if granularity != "segment":
            raise errors.invalid_request(
                "Only segment timestamps are available.", param="timestamp_granularities"
            )
    if granularities and response_format != "verbose_json":
        raise errors.invalid_request(
            "timestamp_granularities[] requires response_format verbose_json.",
            param="timestamp_granularities",
        )
    language = values.get("language")
    if language is not None:
        cleaned = language.strip().lower()
        if cleaned in ("", "auto"):
            values["language"] = None
        else:
            codes = _whisper_codes()
            if not re.match(r"^[a-z]{2,3}$", cleaned) or (codes and cleaned not in codes):
                raise errors.invalid_request(
                    "language must be an ISO-639-1 code the speech model knows, or auto.",
                    param="language",
                )
            values["language"] = cleaned
    values["timestamp_granularities"] = granularities
    try:
        return TranscriptionRequest.model_validate(values)
    except ValidationError as exc:
        raise _refuse_validation(exc) from None


def transcription_body(
    engine_reply: Mapping[str, Any], request: TranscriptionRequest
) -> Union[str, Dict[str, Any]]:
    """The engine's reply → the public shape for the requested format.

    Built from named fields only: `processing_ms`, `no_speech_prob` and the
    engine's `language_code` stay server-side, and a key the engine adds next
    month does not appear on the wire until somebody puts it here.
    """
    text = str(engine_reply.get("text") or "").strip()
    duration = _duration(engine_reply)
    # Null, never zero, when the engine did not say how long the audio was —
    # the rule every usage object on this API follows (CONTRACT §9).
    usage = (
        None if duration is None else {"type": "duration", "seconds": int(math.ceil(duration))}
    )
    if request.response_format == "text":
        return text
    if request.response_format == "json":
        return {"text": text, "usage": usage}
    segments = []
    raw = engine_reply.get("segments")
    for position, segment in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(segment, Mapping):
            continue
        segments.append(
            {
                "id": position,
                "start": float(segment.get("start") or 0.0),
                "end": float(segment.get("end") or 0.0),
                "text": str(segment.get("text") or ""),
            }
        )
    language = engine_reply.get("language")
    return {
        "task": "transcribe",
        "language": str(language).strip().lower() if language else None,
        "duration": duration,
        "text": text,
        "segments": segments,
        "usage": usage,
    }


def _duration(engine_reply: Mapping[str, Any]) -> Optional[float]:
    try:
        value = float(engine_reply.get("duration"))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value >= 0 else None
