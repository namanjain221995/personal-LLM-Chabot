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
    """PUBLIC_API_EMBED_MAX_INPUTS (2,048, OpenAI's own): one request's worth
    of inputs. A shape of the request, not a usage limit (no-timeout design,
    sidecars_and_audio): a bigger job is sharded by the caller."""
    return max(1, setting_int("PUBLIC_API_EMBED_MAX_INPUTS", 2048))


def embed_context_tokens() -> int:
    """PUBLIC_API_EMBED_CONTEXT_TOKENS (4,096): the engine's `--max-model-len`
    (docker inspect sf-local-ai-vllm-embed-1, 2026-09-13). Not
    EMBED_CONTEXT_LENGTH, which says 32,768 and is wrong against the engine."""
    return max(1, setting_int("PUBLIC_API_EMBED_CONTEXT_TOKENS", 4096))


def rerank_max_documents() -> int:
    """PUBLIC_API_RERANK_MAX_DOCUMENTS (1,000): request shape, not usage."""
    return max(1, setting_int("PUBLIC_API_RERANK_MAX_DOCUMENTS", 1000))


def rerank_context_tokens() -> int:
    """PUBLIC_API_RERANK_CONTEXT_TOKENS (4,096) — per templated pair."""
    return max(1, setting_int("PUBLIC_API_RERANK_CONTEXT_TOKENS", 4096))


#: CONTRACT §8.6: the file part of one transcription request (89 MiB) and the
#: whole multipart body (90 MiB) — the same bytes the edge and the gateway
#: allow, so a body one hop accepts is never refused by the next. Longer
#: recordings go through the Files API (`file_id`). There is NO duration
#: limit: audio of any length is transcribed in windows (audio_jobs.py).
DEFAULT_MAX_AUDIO_BYTES = 93_323_264
DEFAULT_MAX_AUDIO_BODY_BYTES = 94_371_840
#: CONTRACT §8.4/§8.5: the JSON body of embeddings and rerank (8 MiB). 2,048
#: inputs of real text do not fit the 1 MiB JSON rule of the other routes.
DEFAULT_MAX_POOLING_BODY_BYTES = 8 * 1024 * 1024


def max_audio_bytes() -> int:
    """PUBLIC_API_MAX_AUDIO_BYTES (89 MiB): the file part itself."""
    return max(1, setting_int("PUBLIC_API_MAX_AUDIO_BYTES", 93_323_264))


def max_audio_body_bytes() -> int:
    """PUBLIC_API_MAX_AUDIO_BODY_BYTES (90 MiB): the whole multipart body —
    the file plus one MiB of form fields and boundaries."""
    return max(1, setting_int("PUBLIC_API_MAX_AUDIO_BODY_BYTES", 94_371_840))


def max_json_body_bytes() -> int:
    """The CONTRACT §12 JSON body cap, the same number `models.max_body_bytes`
    reads — 1 MiB unless PUBLIC_API_MAX_BODY_BYTES says otherwise. A
    transcription sent as JSON (`file_id`) is held to it."""
    return max(1, setting_int("PUBLIC_API_MAX_BODY_BYTES", 1024 * 1024))


def max_pooling_body_bytes() -> int:
    """PUBLIC_API_MAX_POOLING_BODY_BYTES (8 MiB): the JSON body of
    `/v1/embeddings` and `/v1/rerank`, never below the JSON rule."""
    return max(max_json_body_bytes(), setting_int("PUBLIC_API_MAX_POOLING_BODY_BYTES", 8 * 1024 * 1024))


def pooling_silence_s() -> float:
    """PUBLIC_API_POOLING_SILENCE_S (600): how long one embeddings or rerank
    engine call may be silent before the engine's own /metrics witness is
    asked what happened. Not a budget: what follows is decided by the
    witness verdict, never by this clock (sidecars.py)."""
    return max(0.001, setting_float("PUBLIC_API_POOLING_SILENCE_S", 600.0))


_POOLING_ROUTES = ("/v1/embeddings", "/v1/rerank")
_AUDIO_ROUTES = ("/v1/audio/transcriptions",)


def body_cap_for(method: str, path: str) -> Optional[int]:
    """The transport body cap of the three sidecar routes, or None for any
    other request line. `models.body_cap_for` (the table main.py's body-size
    middleware asks) delegates here, so the outer cap and the route's own
    reader cannot disagree. Exact paths only."""
    if str(method or "").upper() != "POST":
        return None
    normalised = "/" + str(path or "").strip("/")
    if normalised in _POOLING_ROUTES:
        return max_pooling_body_bytes()
    if normalised in _AUDIO_ROUTES:
        return max_audio_body_bytes()
    return None


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
#: invisible no-op this platform does not ship. `file_id` names a Files API
#: file instead of a `file` part (CONTRACT §8.6); `stream` answers with
#: server-sent events.
TRANSCRIPTION_FIELDS: Tuple[str, ...] = (
    "model",
    "language",
    "response_format",
    "timestamp_granularities[]",
    "stream",
    "file_id",
)

#: The same request sent as `application/json` (a Files API `file_id` needs no
#: multipart body; CONTRACT §8.6). `timestamp_granularities` is the JSON
#: spelling of the repeated form field.
TRANSCRIPTION_JSON_FIELDS: Tuple[str, ...] = (
    "model",
    "language",
    "response_format",
    "timestamp_granularities",
    "stream",
    "file_id",
)

#: A `file_id` is a string of at most this many characters. Its SHAPE is not
#: judged here: a malformed id gets the same 404 as another project's or a
#: deleted one (Files design §7.2 rule 2), which the route decides.
FILE_ID_MAX_CHARS = 128


class TranscriptionRequest(_Forbid):
    model: str = Field(min_length=1, max_length=64)
    language: Optional[str] = None
    response_format: Literal["json", "text", "verbose_json"] = "json"
    timestamp_granularities: List[Literal["segment"]] = Field(default_factory=list)
    stream: bool = False
    file_id: Optional[str] = None

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


def language_name(code: Optional[str]) -> Optional[str]:
    """An ISO code (`en`) → the lower-case language name verbose_json carries
    (`english`, CONTRACT §8.6), from `app/asr.py`'s table. A value that is not
    a known code is returned lower-cased as it is (it may already be a name);
    None stays None."""
    if not code:
        return None
    cleaned = str(code).strip().lower()
    try:
        from .. import asr

        for name, value in getattr(asr, "_LANGUAGE_CODES", {}).items():
            if str(value).lower() == cleaned:
                return str(name).lower()
    except Exception:  # noqa: BLE001 - a missing table must not fail a transcript
        pass
    return cleaned or None


def _parse_stream_flag(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str) and raw.strip().lower() in ("true", "false"):
        return raw.strip().lower() == "true"
    raise errors.invalid_request("stream must be true or false.", param="stream")


def _finish_transcription(values: Dict[str, Any], granularities: Sequence[Any]) -> TranscriptionRequest:
    """The checks both body shapes share, then the model."""
    if "model" not in values or values.get("model") in (None, ""):
        raise errors.invalid_request("model is required.", param="model")
    if not isinstance(values["model"], str):
        raise errors.invalid_request("model must be a string.", param="model")
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
        if not isinstance(language, str):
            raise errors.invalid_request("language must be a string.", param="language")
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
    if "stream" in values:
        values["stream"] = _parse_stream_flag(values["stream"])
    file_id = values.get("file_id")
    if file_id is not None:
        if not isinstance(file_id, str) or not file_id.strip() or len(file_id) > FILE_ID_MAX_CHARS:
            raise errors.invalid_request("file_id must be the id of a file in this project.", param="file_id")
        values["file_id"] = file_id.strip()
    values["timestamp_granularities"] = list(granularities)
    try:
        return TranscriptionRequest.model_validate(values)
    except ValidationError as exc:
        raise _refuse_validation(exc) from None


def parse_transcription_form(fields: Mapping[str, Sequence[str]]) -> TranscriptionRequest:
    """The text fields of a transcription form → a validated request, or 400.

    The file part is checked by the route (it needs the byte caps and the
    content-type allowlist); this is everything else.
    """
    for name in fields:
        if name not in TRANSCRIPTION_FIELDS:
            raise errors.invalid_request(f"Unsupported field: {name}.", param=name)
    values: Dict[str, Any] = {}
    for name in ("model", "language", "response_format", "stream", "file_id"):
        if name in fields:
            values[name] = fields[name][0]
    granularities = list(fields.get("timestamp_granularities[]") or [])
    return _finish_transcription(values, granularities)


def parse_transcription_json(payload: Any) -> TranscriptionRequest:
    """`application/json` `{model, file_id, language, response_format, stream}`
    → a validated request naming a file, or 400 (CONTRACT §8.6)."""
    body = _require_object(payload)
    for name in body:
        if name not in TRANSCRIPTION_JSON_FIELDS:
            raise errors.invalid_request(f"Unsupported field: {name}.", param=str(name))
    values: Dict[str, Any] = {name: body[name] for name in ("model", "language", "response_format", "stream", "file_id") if name in body}
    granularities = body.get("timestamp_granularities") or []
    if not isinstance(granularities, list):
        raise errors.invalid_request(
            "timestamp_granularities must be a list.", param="timestamp_granularities"
        )
    if values.get("response_format") is not None and not isinstance(values["response_format"], str):
        raise errors.invalid_request("response_format must be a string.", param="response_format")
    request = _finish_transcription(values, granularities)
    if request.file_id is None:
        raise errors.invalid_request(
            "file_id is required when the body is JSON; send multipart/form-data to upload a file.",
            param="file_id",
        )
    return request


def transcription_body(result: Any, request: TranscriptionRequest) -> Union[str, Dict[str, Any]]:
    """A finished `audio_jobs.TranscriptResult` → the public shape for the
    requested format. verbose_json names the language (`english`), as OpenAI
    does and CONTRACT §8.6 promises; the job keeps the code."""
    body = result.body(request.response_format)
    if isinstance(body, dict) and request.response_format == "verbose_json":
        body["language"] = language_name(body.get("language"))
    return body
