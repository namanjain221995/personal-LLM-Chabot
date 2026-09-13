"""The request and response types of `POST /v1/responses` (CONTRACT §8, §9).

TWO RULES DECIDE EVERY LINE BELOW.

1. **A parameter this platform cannot honour is REJECTED, never silently
   ignored** (CONTRACT §8). `extra="forbid"` is what enforces that: a caller
   who sends `top_p`, `tools`, `n`, `seed` or `logit_bias` — every one of
   which an OpenAI-shaped client library will happily attach — gets a 400
   naming the field, instead of an answer that quietly ignored the knob they
   were relying on. Accepting and dropping a sampling parameter is the failure
   mode that makes an API untrustworthy: the caller cannot tell.
2. **Nothing in the body may change the project, workspace, key, model target,
   limits or audit policy** (CONTRACT §8). Those come from the API key. There
   is deliberately no field here for any of them, and `extra="forbid"` means a
   hopeful `"workspace_id": …` is a 400 rather than something the next reader
   has to prove is ignored.

The models are pure: they read no database, take no lock, and know nothing
about which model a key may use. The per-model ceilings arrive as arguments
(`resolve_max_output_tokens`), because the registry resolves them per request
and a validator that captured a limit at import time would keep serving the
old number after a redeploy widened the window.
"""
from __future__ import annotations

import base64
import binascii
import re
from typing import Any, Dict, List, Literal, Mapping, Optional, Sequence, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from typing_extensions import Annotated

from ..config import settings
from . import errors

# --------------------------------------------------------------- limits --

#: CONTRACT §12: 1 MiB of body, refused BEFORE parsing. Read through
#: `max_body_bytes()` so this module does not become a second place where the
#: number lives.
_DEFAULT_MAX_BODY_BYTES = 1024 * 1024

#: CONTRACT §8: `metadata` is a small labelling side-channel, not storage.
MAX_METADATA_KEYS = 16
MAX_METADATA_KEY_CHARS = 64
MAX_METADATA_VALUE_CHARS = 512

#: A public model id is our own vocabulary (`techsara-35b`), never a path, a
#: URL or anything with a newline in it — it is echoed into error messages and
#: written to the usage ledger.
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-]{0,63}$")

Role = Literal["system", "user", "assistant"]

#: The five states `api_responses.status` may hold (SCHEMA-V34). The wire
#: vocabulary and the column's CHECK constraint are the same list on purpose:
#: a status that cannot be stored must never be returned.
ResponseStatus = Literal["queued", "in_progress", "completed", "failed", "cancelled"]


def max_body_bytes() -> int:
    """The body cap, read at call time.

    `getattr` rather than a hard attribute because `app/config.py` is a
    single-owner file in this programme and this package must not need an edit
    there to be deployable.

    NOT YET A DEPLOYMENT KNOB, said plainly because the docstring used to claim
    otherwise (verifier finding, 2026-09-13): there is no
    `public_api_max_body_bytes` in `app/config.py` and no
    `PUBLIC_API_MAX_BODY_BYTES` environment variable, so today this always
    returns the 1 MiB of CONTRACT §12. The `getattr` is the seam the
    config.py wave adds the setting to; until it does, changing the cap is a
    code change here.
    """
    return int(getattr(settings, "public_api_max_body_bytes", _DEFAULT_MAX_BODY_BYTES))


def _setting_int(name: str, default: int) -> int:
    # `registry.setting_int`, imported late: models.py is imported by the
    # registry's own consumers and must not form a cycle at import time.
    from .registry import setting_int

    return setting_int(name, default)


#: CONTRACT §8 (2026-09-13): a request that carries images may be larger on
#: the wire than the 1 MiB text rule, because a 896 px photo is ~200 KB and a
#: page scan several MB. Twenty mebibytes carries the per-request image limit
#: of the smaller models comfortably, and the text inside the body is still
#: held to `max_body_bytes()` after parsing (`ResponsesRequest.text_bytes`).
_DEFAULT_MAX_MEDIA_BODY_BYTES = 20 * 1024 * 1024
#: The largest single decoded image. Past this a picture is a scan at a
#: resolution the processor downsamples anyway, and ten of them would be
#: most of the body.
_DEFAULT_MAX_IMAGE_BYTES = 10 * 1024 * 1024
#: 25 MiB of audio plus multipart overhead (`POST /v1/audio/transcriptions`).
_DEFAULT_MAX_AUDIO_BODY_BYTES = 27_262_976


def max_media_body_bytes() -> int:
    """PUBLIC_API_MAX_MEDIA_BODY_BYTES (20 MiB): the wire cap on the two
    generating routes, which accept image parts."""
    return max(max_body_bytes(), _setting_int("PUBLIC_API_MAX_MEDIA_BODY_BYTES", _DEFAULT_MAX_MEDIA_BODY_BYTES))


def max_image_bytes() -> int:
    """PUBLIC_API_MAX_IMAGE_BYTES (10 MiB): the cap on ONE decoded image."""
    return max(1, _setting_int("PUBLIC_API_MAX_IMAGE_BYTES", _DEFAULT_MAX_IMAGE_BYTES))


def max_audio_body_bytes() -> int:
    """PUBLIC_API_MAX_AUDIO_BODY_BYTES (26 MiB): the wire cap on speech."""
    return max(max_body_bytes(), _setting_int("PUBLIC_API_MAX_AUDIO_BODY_BYTES", _DEFAULT_MAX_AUDIO_BODY_BYTES))


#: The routes whose body may be larger than the text rule, and the reader
#: for each cap. Anything else under `/v1` stays at `max_body_bytes()`.
_MEDIA_ROUTES = ("/v1/responses", "/v1/chat/completions")
_AUDIO_ROUTES = ("/v1/audio/transcriptions",)


def body_cap_for(method: str, path: str) -> int:
    """The transport body cap for one `/v1` request line — THE table the
    application's body-size middleware asks (`app/main.py::body_cap_for`, via
    `_public_api_body_cap`, since the 2026-09-13 integration). A new `/v1`
    route whose body may exceed `max_body_bytes()` is added here, and the
    mounted app follows with no edit to main.py."""
    normalised = "/" + str(path or "").strip("/")
    if str(method or "").upper() == "POST":
        if normalised in _MEDIA_ROUTES:
            return max_media_body_bytes()
        if normalised in _AUDIO_ROUTES:
            return max_audio_body_bytes()
    return max_body_bytes()


# ------------------------------------------------------------- request --


class _Strict(BaseModel):
    """Forbid what we cannot honour, and strip the whitespace a copy-and-paste
    body arrives with."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


#: The image types a data: URL may declare, and the magic bytes each must
#: start with. Declared-type-matches-content is checked because a vLLM image
#: processor handed a mislabelled payload answers with a 400 whose text names
#: the internal decoder, and because "image/png" around an arbitrary file is
#: the cheapest way to smuggle bytes at a decoder nobody fuzzed.
_IMAGE_MAGIC = {
    "image/png": lambda head: head.startswith(b"\x89PNG\r\n\x1a\n"),
    "image/jpeg": lambda head: head[:3] == b"\xff\xd8\xff",
    "image/gif": lambda head: head[:6] in (b"GIF87a", b"GIF89a"),
    "image/webp": lambda head: head[:4] == b"RIFF" and head[8:12] == b"WEBP",
}
_DATA_URL_RE = re.compile(r"^data:(image/(?:png|jpeg|gif|webp));base64,", re.IGNORECASE)


def validate_image_data_url(value: Any) -> str:
    """A caller's image reference → a canonical base64 data: URL, or ValueError.

    DATA URLs ONLY, AND THAT IS A SECURITY RULE, not a convenience one. vLLM
    fetches an `http(s)://` image URL itself, from inside the cluster network,
    with no `--allowed-media-domains` configured: passing a caller's URL
    through would let anyone on the internet make our engines request
    `http://192.168.x.y:port/…` and read the answer back as "what the image
    shows" (SSRF). `file:` would be worse. So nothing but `data:` reaches an
    engine, and a test proves an `http://` URL never reaches the stub engine.

    Decoded here in full: the size limit is on the IMAGE, and the magic bytes
    must match the declared type. The router runs validation of a large body
    in a worker thread, so this decode never stalls the event loop.
    """
    if not isinstance(value, str):
        raise ValueError("image_url must be a string holding a data: URL")
    stripped = value.strip()
    match = _DATA_URL_RE.match(stripped)
    if match is None:
        if stripped[:5].lower() == "data:":
            raise ValueError(
                "image_url must be a base64 data: URL of a PNG, JPEG, GIF or WebP image"
            )
        raise ValueError(
            "image_url must be a data: URL; remote image URLs are not fetched"
        )
    media_type = match.group(1).lower()
    payload = stripped[match.end():]
    limit = max_image_bytes()
    if (len(payload) * 3) // 4 > limit + 2:
        raise ValueError(f"each image may be at most {limit} bytes")
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("image_url does not hold valid base64 data") from None
    if not raw:
        raise ValueError("image_url holds no image data")
    if len(raw) > limit:
        raise ValueError(f"each image may be at most {limit} bytes")
    if not _IMAGE_MAGIC[media_type](raw[:16]):
        raise ValueError("the image data does not match its declared type")
    return f"data:{media_type};base64,{payload}"


class InputTextPart(_Strict):
    """`{"type": "input_text", "text": …}` — one piece of a message's text."""

    type: Literal["input_text"]
    text: str = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def _text_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must not be empty")
        return value


class InputImagePart(_Strict):
    """`{"type": "input_image", "image_url": "data:image/…;base64,…"}`.

    `detail` accepts only `auto`: the engines choose their own resolution, and
    accepting `low` or `high` would be the silently-ignored knob CONTRACT §8
    forbids."""

    type: Literal["input_image"]
    image_url: str
    detail: Optional[Literal["auto"]] = None

    @field_validator("image_url")
    @classmethod
    def _image_is_an_inline_image(cls, value: str) -> str:
        return validate_image_data_url(value)


ContentPart = Annotated[Union[InputTextPart, InputImagePart], Field(discriminator="type")]


class InputMessage(_Strict):
    """One turn of the conversation the caller supplies.

    `content` is a string, or (2026-09-13) a list of text and image parts for
    a vision model. Images are accepted ONLY on `user` turns: an image in a
    system or assistant turn is not something the chat templates render, and
    accepting and dropping it would charge for a prompt the model never saw.
    Whether the chosen model can see at all is decided per model by the
    planner, which knows which model the key resolved to.
    """

    role: Role
    content: Union[str, List[ContentPart]]

    @field_validator("content")
    @classmethod
    def _content_is_not_blank(
        cls, value: Union[str, List[Any]]
    ) -> Union[str, List[Any]]:
        if isinstance(value, str):
            if not value.strip():
                raise ValueError("message content must not be empty")
            return value
        if not value:
            raise ValueError("message content must contain at least one part")
        return value

    @model_validator(mode="after")
    def _images_only_from_the_user(self) -> "InputMessage":
        if self.role != "user" and self.image_count():
            raise ValueError("images are accepted only in user messages")
        return self

    def image_count(self) -> int:
        if isinstance(self.content, str):
            return 0
        return sum(1 for part in self.content if isinstance(part, InputImagePart))

    def texts(self) -> List[str]:
        if isinstance(self.content, str):
            return [self.content]
        return [part.text for part in self.content if isinstance(part, InputTextPart)]

    def engine_message(self) -> Dict[str, Any]:
        """This turn in the OpenAI chat shape the engines take.

        Text-only content — a string, or parts that are all text — is sent as
        ONE string, joined with a blank line: `llm.normalize_system` folds
        only string system turns, and Qwen's template rejects a second or a
        non-leading system turn outright. Parts are kept as parts only when
        there is an image to carry.
        """
        if isinstance(self.content, str):
            return {"role": self.role, "content": self.content}
        if not self.image_count():
            return {"role": self.role, "content": "\n\n".join(self.texts())}
        parts: List[Dict[str, Any]] = []
        for part in self.content:
            if isinstance(part, InputTextPart):
                parts.append({"type": "text", "text": part.text})
            else:
                parts.append({"type": "image_url", "image_url": {"url": part.image_url}})
        return {"role": self.role, "content": parts}


class ResponsesRequest(_Strict):
    """The body of `POST /v1/responses`, CONTRACT §8 exactly."""

    model: str = Field(min_length=1, max_length=64)
    input: Union[str, List[InputMessage]]
    instructions: Optional[str] = None
    stream: bool = False
    background: bool = False
    #: The ceiling is per model and is checked in `resolve_max_output_tokens`,
    #: which knows which model the key resolved to. Only the floor is here.
    max_output_tokens: Optional[int] = Field(default=None, ge=1)
    temperature: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    metadata: Dict[str, str] = Field(default_factory=dict)

    @field_validator("model")
    @classmethod
    def _model_id_shape(cls, value: str) -> str:
        if not _MODEL_ID_RE.match(value):
            raise ValueError(
                "model must be a public model id: letters, digits, dot, dash, "
                "underscore or colon"
            )
        return value

    @field_validator("input")
    @classmethod
    def _input_is_not_empty(
        cls, value: Union[str, List[InputMessage]]
    ) -> Union[str, List[InputMessage]]:
        if isinstance(value, str):
            if not value.strip():
                raise ValueError("input must not be empty")
            return value
        if not value:
            raise ValueError("input must contain at least one message")
        return value

    @field_validator("instructions")
    @classmethod
    def _instructions_are_not_blank(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("instructions must not be empty when present")
        return value

    @field_validator("metadata")
    @classmethod
    def _metadata_is_small_and_flat(cls, value: Dict[str, str]) -> Dict[str, str]:
        # Values arrive typed as `str` by the annotation — pydantic v2 refuses
        # an int or a nested object rather than coercing it — so only the
        # sizes are left to check here.
        if len(value) > MAX_METADATA_KEYS:
            raise ValueError(f"metadata may contain at most {MAX_METADATA_KEYS} keys")
        for key, item in value.items():
            if not key.strip():
                raise ValueError("metadata keys must not be empty")
            if len(key) > MAX_METADATA_KEY_CHARS:
                raise ValueError(
                    f"metadata keys may be at most {MAX_METADATA_KEY_CHARS} characters"
                )
            if len(item) > MAX_METADATA_VALUE_CHARS:
                raise ValueError(
                    f"metadata values may be at most {MAX_METADATA_VALUE_CHARS} characters"
                )
        return value

    @model_validator(mode="after")
    def _stream_and_background_are_exclusive(self) -> "ResponsesRequest":
        """CONTRACT §8: both true is refused. A background response is
        delivered by `GET /v1/responses/{id}` and a webhook; there is no
        stream to attach to, so honouring `stream` would be a lie and
        ignoring it would be the silent-drop failure rule 1 forbids."""
        if self.stream and self.background:
            raise ValueError("stream and background cannot both be true")
        return self

    # ---------------------------------------------------------- helpers --

    def chat_messages(self) -> List[Dict[str, Any]]:
        """The request as the `messages` list `llm.stream_chat_events` takes.

        `instructions` becomes the FIRST system message, ahead of anything the
        caller put in `input`, because it is the request-level instruction and
        the later turns are the conversation it applies to.
        """
        messages: List[Dict[str, Any]] = []
        if self.instructions:
            messages.append({"role": "system", "content": self.instructions})
        if isinstance(self.input, str):
            messages.append({"role": "user", "content": self.input})
        else:
            messages.extend(m.engine_message() for m in self.input)
        return messages

    def image_count(self) -> int:
        if isinstance(self.input, str):
            return 0
        return sum(message.image_count() for message in self.input)

    def has_text(self) -> bool:
        """Did the caller send any text at all — instructions included?"""
        if self.instructions:
            return True
        if isinstance(self.input, str):
            return True
        return any(text.strip() for message in self.input for text in message.texts())

    def text_bytes(self) -> int:
        """UTF-8 bytes of every text the request carries — the quantity the
        1 MiB rule of CONTRACT §12 has always bounded, now that the BODY may
        be larger because of images."""
        total = len((self.instructions or "").encode("utf-8"))
        if isinstance(self.input, str):
            return total + len(self.input.encode("utf-8"))
        for message in self.input:
            for text in message.texts():
                total += len(text.encode("utf-8"))
        return total

    def resolve_max_output_tokens(self, *, ceiling: int, default: int) -> int:
        """How many tokens this request may generate.

        CONTRACT §8 says an explicit `max_output_tokens` outside `1 … model
        ceiling` is a 400, and CONTRACT §12 says the output limit is "8,192
        default, model ceiling max | clamped". Those two only agree if the
        clamp applies to the DEFAULT and the rejection applies to a value the
        caller actually sent — which is what this does. Silently clamping an
        explicit 100,000 down to the ceiling would bill for a truncated answer
        the caller believed was complete.
        """
        ceiling = max(1, int(ceiling))
        if self.max_output_tokens is None:
            return max(1, min(int(default), ceiling))
        if self.max_output_tokens > ceiling:
            raise errors.invalid_request(
                f"max_output_tokens must be between 1 and {ceiling} for this model.",
                param="max_output_tokens",
            )
        return int(self.max_output_tokens)


def parse_responses_request(payload: Any) -> ResponsesRequest:
    """A decoded JSON body → a validated request, or `ApiError` (400).

    The pydantic message is used but the offending VALUE never is: pydantic
    renders `input_value` into its own string form, and a caller's body can
    contain anything at all — including the prompt, which CONTRACT §16 says we
    do not store or echo.
    """
    if not isinstance(payload, Mapping):
        raise errors.invalid_request("The request body must be a JSON object.")
    try:
        request = ResponsesRequest.model_validate(payload)
    except ValidationError as exc:
        chosen = _most_specific(exc.errors())
        message = str(chosen.get("msg") or "The request is not valid.")
        if str(chosen.get("type") or "") in _TAG_ERRORS:
            # pydantic's sentence for a discriminated union QUOTES the tag the
            # caller sent ("Input tag 'x' found using 'type' …"): caller text,
            # which this function never echoes.
            message = "Each content part must have type input_text or input_image."
        raise errors.invalid_request(
            message,
            param=_param_from_loc(chosen.get("loc") or ()),
        ) from None
    # CONTRACT §12's text rule survives the larger media body (2026-09-13).
    limit = max_body_bytes()
    if request.text_bytes() > limit:
        raise errors.request_text_too_large(limit)
    return request


#: pydantic error types whose message quotes the discriminator VALUE sent.
_TAG_ERRORS = frozenset({"union_tag_invalid", "union_tag_not_found"})


def _most_specific(raised: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    """The error worth telling the caller about, when pydantic reports several.

    `input` is a union, so ONE bad message produces an error for each branch:
    "input should be a valid string" (the string branch) and "field required"
    at `input.0.content` (the list branch). The first is technically true and
    useless — it points at a shape the caller was not attempting. The deepest
    path is the one that describes what they actually sent, and ties keep
    pydantic's own order.
    """
    best = raised[0]
    best_depth = len(_loc_parts(best.get("loc") or ()))
    for item in raised[1:]:
        depth = len(_loc_parts(item.get("loc") or ()))
        if depth > best_depth:
            best, best_depth = item, depth
    return best


#: Markers pydantic puts in a `loc` to name which branch of a union or which
#: internal validator produced the error. They are our schema's vocabulary, not
#: a field the caller sent, so they never appear in `param`.
_LOC_NOISE = ("str", "list[InputMessage]", "InputMessage", "input_text", "input_image")


def _loc_parts(loc: Sequence[Any]) -> List[str]:
    return [
        str(part)
        for part in loc
        if str(part) not in _LOC_NOISE
        # A union branch of the content-part list renders as
        # `list[tagged-union[InputTextPart,InputImagePart]]`: schema
        # vocabulary, never a field the caller sent.
        and "[" not in str(part)
        and not str(part).startswith(("function-", "is-", "constrained-"))
    ]


def _param_from_loc(loc: Sequence[Any]) -> Optional[str]:
    """`("metadata", "customer_id")` → `"metadata.customer_id"`, the JSON path
    a caller can find in the body they sent.

    The value is caller-controlled — with `extra="forbid"` the offending key is
    the caller's OWN field name — so `ApiError` validates it through
    `errors._echoable` before it reaches a body. Nothing is done about it here;
    the note is so the next reader does not conclude this path is unguarded.
    """
    return ".".join(_loc_parts(loc)) or None


# ------------------------------------------------------------ response --


class OutputText(_Strict):
    """One piece of assistant text (CONTRACT §9)."""

    type: Literal["output_text"] = "output_text"
    text: str


class OutputMessage(_Strict):
    """The single assistant message `/v1/responses` returns today. It is a
    LIST in `Response.output` because the shape must not change when a future
    wave adds a second item kind."""

    type: Literal["message"] = "message"
    role: Literal["assistant"] = "assistant"
    content: List[OutputText]

    @classmethod
    def of(cls, text: str) -> "OutputMessage":
        return cls(content=[OutputText(text=text)])


class Usage(_Strict):
    """Tokens, as the engine reported them.

    There is no `from_counts(0, 0)` convenience here on purpose: a zero must
    only ever mean "the engine reported zero".
    """

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)

    @classmethod
    def from_llm(cls, usage: Optional[Mapping[str, Any]]) -> Optional["Usage"]:
        """`llm.get_usage()` → `Usage`, or **None** when nothing was measured.

        CONTRACT §9: usage is null — never 0 — when the engine did not report
        counts. `llm.get_usage()` returns `None` for "not measured", and a
        zero here would be both a lie and an under-charge, so the None is
        carried all the way to the wire rather than defaulted away.
        """
        if not usage:
            return None
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
        return cls(
            input_tokens=prompt,
            output_tokens=completion,
            total_tokens=prompt + completion,
        )


class ResponseError(_Strict):
    """Why a response ended in `failed`, in the same vocabulary as the HTTP
    envelope so a client has one error table for both."""

    code: str
    message: str

    @field_validator("message")
    @classmethod
    def _message_is_scrubbed(cls, value: str) -> str:
        """CONTRACT §9 applies to THIS body too.

        `Response.to_wire()` is what `GET /v1/responses/{id}` returns after a
        failure and what the `response.failed` event carries, and until
        2026-09-13 it dumped this field verbatim — a verifier demonstrated a
        container name, a private IP and a container path reaching a caller
        through it, while `errors.py`'s docstring claimed `redact()` ran over
        "every message that DOES reach the envelope". It did not run over this
        one.

        Scrubbed at CONSTRUCTION rather than at render time, so a route that
        stores this text in `api_responses.error_message` (SCHEMA-V34, a
        free-form column) writes the scrubbed form too. `errors.redact()` is
        belt and braces, not the primary defence: a message built from an
        exception nobody wrote for a caller should come from
        `errors.from_unexpected()`, which discards the text entirely.
        """
        return errors.redact(value)


class IncompleteDetails(_Strict):
    """Why a completed answer is not the whole answer. One reason exists."""

    reason: Literal["max_output_tokens"] = "max_output_tokens"

    @classmethod
    def for_finish(cls, finish_reason: Optional[str]) -> Optional["IncompleteDetails"]:
        """`length` → the ceiling was reached; anything else → None."""
        return cls() if str(finish_reason or "") == "length" else None


class Response(_Strict):
    """The success shape of CONTRACT §9 — and the object inside the terminal
    `response.completed` event, byte for byte. One type, so the streaming and
    the non-streaming bodies cannot drift apart."""

    id: str
    object: Literal["response"] = "response"
    created_at: int
    status: ResponseStatus
    model: str
    output: List[OutputMessage] = Field(default_factory=list)
    usage: Optional[Usage] = None
    #: The output ceiling APPLIED to this generation (2026-09-13). Input and
    #: output share the context window, so a request asking for more than the
    #: window has left is CLAMPED rather than refused, and this is where the
    #: caller learns what it got: the planned value on every snapshot before
    #: generation (and in a background 202), the exact value on the terminal
    #: event and on `GET /v1/responses/{id}`. Null only for rows written
    #: before V35.
    max_output_tokens: Optional[int] = Field(default=None, ge=1)
    #: `{"reason": "max_output_tokens"}` when the engine stopped because it
    #: reached that ceiling, else null. `status` stays `completed`: the V34
    #: status CHECK has no `incomplete`, a documented deviation from the
    #: upstream shape.
    incomplete_details: Optional["IncompleteDetails"] = None
    #: Present only on a failed response (`GET /v1/responses/{id}` after a
    #: failure, and the `response.failed` event). Omitted entirely otherwise.
    error: Optional[ResponseError] = None

    def to_wire(self) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "id": self.id,
            "object": self.object,
            "created_at": self.created_at,
            "status": self.status,
            "model": self.model,
            "output": [item.model_dump() for item in self.output],
            "usage": None if self.usage is None else self.usage.model_dump(),
            "max_output_tokens": self.max_output_tokens,
            "incomplete_details": (
                None if self.incomplete_details is None else self.incomplete_details.model_dump()
            ),
        }
        if self.error is not None:
            body["error"] = self.error.model_dump()
        return body


Response.model_rebuild()
