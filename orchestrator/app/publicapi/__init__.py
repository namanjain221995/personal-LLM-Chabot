"""`/v1` — the public developer API (CONTRACT §2, §7).

The surface a third party's code talks to, reached with `Authorization: Bearer
tsk_live_…` and nothing else. It never reads the `ts_session` cookie: a route
that accepted both credentials would be a confused deputy, drivable by any
page on the internet with a signed-in visitor's cookie (CONTRACT §1).

This wave ships the CONTRACT TYPES only — no routes are mounted yet, and
nothing here imports FastAPI. The next wave builds the router on top of them.

    models.py     the §8 request and the §9 success shapes, pydantic v2,
                  `extra="forbid"` so an unhonourable field is a 400
    errors.py     the one error envelope, one `ApiError`, one factory per code
    events.py     the §10 SSE grammar and the Chat Completions framing
    registry.py   which models exist publicly, and which never can

Four properties hold across all four modules, and each is pinned by a test in
`tests/test_publicapi_contract.py`:

* a parameter the platform cannot honour is rejected, not ignored;
* no response body carries a traceback, a path, an internal hostname or a
  private IP — an unexpected exception's text is discarded, not filtered;
* a stream numbers itself from 1 by 1 and ends exactly once;
* the database may narrow the model catalogue and never widen it.

Nothing in this package reads a person's chat memory or writes to anyone's
conversation history: the API is stateless by contract (CONTRACT §8).
"""
from __future__ import annotations

from . import errors, events, models, registry
from .errors import ApiError
from .events import (
    DONE_SENTINEL,
    EVENT_NAMES,
    TERMINAL_EVENTS,
    ChatCompletionChunks,
    SequencedEvents,
    StreamProtocolError,
)
from .models import (
    InputMessage,
    OutputMessage,
    OutputText,
    Response,
    ResponsesRequest,
    Usage,
    parse_responses_request,
)
from .registry import (
    PUBLIC_MODEL_IDS,
    InternalTargetError,
    PublicModel,
    public_models,
    resolve_public_model,
)

__all__ = [
    "ApiError",
    "ChatCompletionChunks",
    "DONE_SENTINEL",
    "EVENT_NAMES",
    "InputMessage",
    "InternalTargetError",
    "OutputMessage",
    "OutputText",
    "PUBLIC_MODEL_IDS",
    "PublicModel",
    "Response",
    "ResponsesRequest",
    "SequencedEvents",
    "StreamProtocolError",
    "TERMINAL_EVENTS",
    "Usage",
    "errors",
    "events",
    "models",
    "parse_responses_request",
    "public_models",
    "registry",
    "resolve_public_model",
]
