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

import re
from typing import Any, Dict, List, Literal, Mapping, Optional, Sequence, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

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


# ------------------------------------------------------------- request --


class _Strict(BaseModel):
    """Forbid what we cannot honour, and strip the whitespace a copy-and-paste
    body arrives with."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class InputMessage(_Strict):
    """One turn of the conversation the caller supplies.

    `content` is a string and only a string. The multimodal part list of the
    upstream shape is not accepted, because CONTRACT §7 does not expose image
    input on `/v1` yet — and a caller whose image parts were accepted and
    dropped would be charged for a prompt the model never saw.
    """

    role: Role
    content: str = Field(min_length=1)

    @field_validator("content")
    @classmethod
    def _content_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message content must not be empty")
        return value


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

    def chat_messages(self) -> List[Dict[str, str]]:
        """The request as the `messages` list `llm.stream_chat_events` takes.

        `instructions` becomes the FIRST system message, ahead of anything the
        caller put in `input`, because it is the request-level instruction and
        the later turns are the conversation it applies to.
        """
        messages: List[Dict[str, str]] = []
        if self.instructions:
            messages.append({"role": "system", "content": self.instructions})
        if isinstance(self.input, str):
            messages.append({"role": "user", "content": self.input})
        else:
            messages.extend({"role": m.role, "content": m.content} for m in self.input)
        return messages

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
        return ResponsesRequest.model_validate(payload)
    except ValidationError as exc:
        chosen = _most_specific(exc.errors())
        raise errors.invalid_request(
            str(chosen.get("msg") or "The request is not valid."),
            param=_param_from_loc(chosen.get("loc") or ()),
        ) from None


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
_LOC_NOISE = ("str", "list[InputMessage]", "InputMessage")


def _loc_parts(loc: Sequence[Any]) -> List[str]:
    return [
        str(part)
        for part in loc
        if str(part) not in _LOC_NOISE
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
    #: Present only on a failed response (`GET /v1/responses/{id}` after a
    #: failure, and the `response.failed` event). Omitted entirely otherwise,
    #: so the happy-path body is exactly the six keys of CONTRACT §9.
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
        }
        if self.error is not None:
            body["error"] = self.error.model_dump()
        return body
