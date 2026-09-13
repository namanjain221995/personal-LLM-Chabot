"""The SSE grammar of `/v1` (CONTRACT §10) and the Chat Completions framing.

TWO WIRE DIALECTS, ONE ENGINE. `POST /v1/responses` streams the Responses
lifecycle — named events whose JSON repeats the name in `type` and numbers
itself with `sequence_number` — and `POST /v1/chat/completions` streams
anonymous `data:` chunks ending in the literal `data: [DONE]`. They are framed
by two classes here, and by nothing else, so a route can never hand-roll a
frame that is nearly right.

WHY NOT `app/sse.py`. That module is the chat application's format and
validates event names against ITS vocabulary (`token`, `meta`, `done`,
`error`). The public API's names are different and its payload rules are
stricter, so the framing is duplicated deliberately — but the heartbeat is
imported, because a keep-alive comment is the same three bytes for everyone
and there is no reason for two spellings of it.

THE INVARIANTS THIS CLASS EXISTS TO HOLD (all of CONTRACT §10):

* `sequence_number` starts at 1 and increases by exactly 1. A client uses it
  to detect a dropped or reordered frame, which it cannot do if we ever skip;
* a heartbeat is a COMMENT and consumes no sequence number — it is not an
  event, and numbering it would make a client think it had missed one;
* **exactly one terminal event**. A second terminal is a programming error and
  raises here, in the generator, rather than reaching a client as two
  contradictory endings (`response.completed` followed by `error` reads as a
  successful response that then failed, and an SDK will believe whichever it
  parses last);
* `usage` only on the terminal event. Every earlier event carries
  `usage: null`, so a client cannot be tempted to sum deltas into a bill.

WHAT A ROUTE STILL OWES. `finally: await stream.aclose()` on the generator
(CONTRACT §10), and metering from the server-side record rather than from what
the client received — an abandoned stream never delivers its terminal event,
and billing from the wire under-counts every disconnect.
"""
from __future__ import annotations

import json
import secrets
import time
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from .. import sse as _chat_sse
from .errors import ApiError

# ------------------------------------------------------------ vocabulary --

RESPONSE_CREATED = "response.created"
RESPONSE_QUEUED = "response.queued"
RESPONSE_IN_PROGRESS = "response.in_progress"
RESPONSE_OUTPUT_TEXT_DELTA = "response.output_text.delta"
RESPONSE_OUTPUT_TEXT_DONE = "response.output_text.done"
RESPONSE_COMPLETED = "response.completed"
RESPONSE_FAILED = "response.failed"
ERROR = "error"

#: Every event name `/v1/responses` may emit — CONTRACT §10 and no more. The
#: fuller upstream lifecycle (`response.output_item.added`,
#: `response.content_part.added` and their `.done` partners) is deliberately
#: absent: the contract names this set, and a client that must ignore unknown
#: events can be given the others later without a breaking change.
EVENT_NAMES: Tuple[str, ...] = (
    RESPONSE_CREATED,
    RESPONSE_QUEUED,
    RESPONSE_IN_PROGRESS,
    RESPONSE_OUTPUT_TEXT_DELTA,
    RESPONSE_OUTPUT_TEXT_DONE,
    RESPONSE_COMPLETED,
    RESPONSE_FAILED,
    ERROR,
)

#: After one of these the stream is over. CONTRACT §10 names exactly three.
TERMINAL_EVENTS: Tuple[str, ...] = (RESPONSE_COMPLETED, RESPONSE_FAILED, ERROR)

#: The only events that may legitimately be the FIRST thing a client sees.
#: `response.created` is the normal opening; the two failures are there
#: because a request can die between the 200 and the first lifecycle event
#: (the engine is gone, admission refuses) and the status line is already
#: committed by then — RFC 9112 §8 leaves an in-band error as the only signal.
_VALID_OPENERS: Tuple[str, ...] = (RESPONSE_CREATED, RESPONSE_FAILED, ERROR)

#: The lifecycle as RANKS, so it can only ever move forward (CONTRACT §10's
#: arrow diagram).
#:
#: Until 2026-09-13 the only ordering rule in this file was "the first event
#: must be an opener, and nothing may follow the terminal". Everything in
#: between was unchecked, so a route could emit a second `response.created`
#: half way down, a `response.queued` after the deltas had started, or a delta
#: after `output_text.done` — each of which renumbers or contradicts what the
#: client has already rendered, and each of which this class exists to make
#: impossible ("so a route can never hand-roll a frame that is nearly right").
#: A verifier found the gap; this is what closes it.
#:
#: A rank may be SKIPPED but never revisited. Skipping is legitimate: a
#: request that is refused before admission never reaches `in_progress`, and a
#: generation that produced no text at all has no delta to send. Going
#: backwards never is — there is no reading of "the response is queued" that
#: makes sense after the client has rendered three paragraphs of it.
_LIFECYCLE_RANK: Dict[str, int] = {
    RESPONSE_CREATED: 0,
    RESPONSE_QUEUED: 1,
    RESPONSE_IN_PROGRESS: 2,
    RESPONSE_OUTPUT_TEXT_DELTA: 3,
    RESPONSE_OUTPUT_TEXT_DONE: 4,
    RESPONSE_COMPLETED: 5,
}

#: The one event that may legitimately repeat: the text arrives in many
#: deltas. Every other rank is a stage, and a stage happens once.
_REPEATABLE: Tuple[str, ...] = (RESPONSE_OUTPUT_TEXT_DELTA,)

#: CONTRACT §10: "at least every 15 s". `app/sse.py` reads the interval from
#: SSE_HEARTBEAT_SECONDS, which an operator may set HIGHER for the chat app;
#: the public contract is a promise to third parties, so it is capped here
#: rather than inherited.
HEARTBEAT_SECONDS: float = min(float(_chat_sse.HEARTBEAT_SECONDS), 15.0)

#: The literal last line of a Chat Completions stream. Reserved for that
#: surface: an OpenAI-derived client reading `/v1/responses` waits for
#: `response.completed` and treats `[DONE]` as noise.
DONE_SENTINEL = "data: [DONE]\n\n"


class StreamProtocolError(RuntimeError):
    """A frame was asked for that the grammar forbids — a second terminal, an
    event after the end, usage on a non-terminal. Always our bug, never the
    caller's, so it raises instead of being written to the wire."""


def _dumps(payload: Mapping[str, Any]) -> str:
    # `ensure_ascii=False` matches app/sse.py: the body is UTF-8 either way and
    # escaping every non-ASCII character triples the size of a Gujarati answer.
    # `default=str` keeps a stray datetime from killing a stream mid-generation.
    return json.dumps(dict(payload), ensure_ascii=False, default=str)


def new_item_id() -> str:
    """The id a client uses to tie deltas to the message they belong to."""
    return f"msg_{secrets.token_hex(12)}"


# -------------------------------------------------- the Responses stream --


class SequencedEvents:
    """The emitter for one `/v1/responses` stream. One instance per request.

    Every method returns the bytes to write — nothing here touches a socket,
    so the grammar can be tested without a server, and a route decides its own
    flushing.
    """

    def __init__(self, *, item_id: Optional[str] = None) -> None:
        self._sequence = 0
        self._terminal: Optional[str] = None
        self._emitted: list[str] = []
        self.item_id = item_id or new_item_id()

    # -- state ------------------------------------------------------------

    @property
    def sequence_number(self) -> int:
        """The number on the last event emitted; 0 before the first."""
        return self._sequence

    @property
    def terminal_event(self) -> Optional[str]:
        return self._terminal

    @property
    def finished(self) -> bool:
        return self._terminal is not None

    @property
    def names_emitted(self) -> Tuple[str, ...]:
        return tuple(self._emitted)

    # -- framing ----------------------------------------------------------

    def _frame(self, name: str, payload: Dict[str, Any]) -> str:
        if name not in EVENT_NAMES:
            raise StreamProtocolError(
                f"unknown public API event: {name!r} (allowed: {EVENT_NAMES})"
            )
        if self._terminal is not None:
            raise StreamProtocolError(
                f"{name!r} was emitted after the terminal event "
                f"{self._terminal!r}; exactly one terminal is allowed (CONTRACT §10)"
            )
        if not self._emitted and name not in _VALID_OPENERS:
            raise StreamProtocolError(
                f"{name!r} cannot open a stream; the first event must be one of "
                f"{_VALID_OPENERS}"
            )
        # The two failure terminals are exempt: a generation can die at any
        # point, and `response.failed` / `error` end the stream rather than
        # advancing it.
        rank = _LIFECYCLE_RANK.get(name)
        if rank is not None and self._emitted:
            previous = self._emitted[-1]
            previous_rank = _LIFECYCLE_RANK.get(previous, -1)
            if rank < previous_rank or (
                rank == previous_rank and name not in _REPEATABLE
            ):
                raise StreamProtocolError(
                    f"{name!r} cannot follow {previous!r} (CONTRACT §10): the "
                    "lifecycle only moves forward, and a stage that is not a "
                    "text delta happens once"
                )
        self._sequence += 1
        body = {"type": name, "sequence_number": self._sequence}
        body.update(payload)
        # `type` and `sequence_number` are written first and then re-asserted,
        # so a payload that happens to carry either key cannot renumber the
        # stream or mislabel the event.
        body["type"] = name
        body["sequence_number"] = self._sequence
        self._emitted.append(name)
        if name in TERMINAL_EVENTS:
            self._terminal = name
        return f"event: {name}\ndata: {_dumps(body)}\n\n"

    def _response_payload(self, response: Mapping[str, Any], *, terminal: bool) -> Dict[str, Any]:
        body = dict(response)
        if not terminal and body.get("usage") is not None:
            raise StreamProtocolError(
                "usage may only appear on the terminal event (CONTRACT §10); "
                "a non-terminal lifecycle event must carry usage: null"
            )
        # Explicit null rather than an absent key: a client reading
        # `event.response.usage` must find the field in every event and see
        # that it is not measured YET, not discover that it is missing.
        body.setdefault("usage", None)
        return {"response": body}

    # -- lifecycle --------------------------------------------------------

    def created(self, response: Mapping[str, Any]) -> str:
        return self._frame(
            RESPONSE_CREATED, self._response_payload(response, terminal=False)
        )

    def queued(self, response: Mapping[str, Any]) -> str:
        """The engine is recovering and this request is allowed to wait
        (CONTRACT §10). Emitted so a caller sees why nothing is arriving yet
        instead of deciding the connection is dead."""
        return self._frame(
            RESPONSE_QUEUED, self._response_payload(response, terminal=False)
        )

    def in_progress(self, response: Mapping[str, Any]) -> str:
        return self._frame(
            RESPONSE_IN_PROGRESS, self._response_payload(response, terminal=False)
        )

    def output_text_delta(
        self, delta: str, *, output_index: int = 0, content_index: int = 0
    ) -> str:
        """One chunk of generated text. The field names are fixed by
        STANDARDS: `item_id`, `output_index`, `content_index`, `delta` — an
        SDK generated against the public schema reads these and nothing else.
        """
        return self._frame(
            RESPONSE_OUTPUT_TEXT_DELTA,
            {
                "item_id": self.item_id,
                "output_index": int(output_index),
                "content_index": int(content_index),
                "delta": str(delta),
            },
        )

    def output_text_done(
        self, text: str, *, output_index: int = 0, content_index: int = 0
    ) -> str:
        """The finished text, once. A client that dropped a delta can recover
        the whole answer from this instead of asking for the response again."""
        return self._frame(
            RESPONSE_OUTPUT_TEXT_DONE,
            {
                "item_id": self.item_id,
                "output_index": int(output_index),
                "content_index": int(content_index),
                "text": str(text),
            },
        )

    # -- terminals --------------------------------------------------------

    def completed(self, response: Mapping[str, Any]) -> str:
        """The success terminal. `response` is the same object the
        non-streaming body returns (CONTRACT §9), so the two modes cannot
        drift; its `usage` may be null, which means not measured."""
        status = dict(response).get("status")
        if status != "completed":
            raise StreamProtocolError(
                f"response.completed must carry status 'completed', not {status!r}"
            )
        return self._frame(
            RESPONSE_COMPLETED, self._response_payload(response, terminal=True)
        )

    def failed(self, response: Mapping[str, Any]) -> str:
        """The failure terminal that still carries a response object — a
        generation that started and then died. Partial `usage` belongs here:
        the tokens were produced and the engine time was spent."""
        status = dict(response).get("status")
        if status != "failed":
            raise StreamProtocolError(
                f"response.failed must carry status 'failed', not {status!r}"
            )
        return self._frame(
            RESPONSE_FAILED, self._response_payload(response, terminal=True)
        )

    def error(self, err: ApiError) -> str:
        """The out-of-band terminal: a failure with no response object to
        describe. The payload names the same `code` the HTTP envelope would
        have used, so a client applies one retry table in both modes."""
        return self._frame(ERROR, err.stream_payload(self._sequence + 1))

    # -- keep-alive -------------------------------------------------------

    def heartbeat(self, note: str = "ping") -> str:
        """A comment frame. Carries no sequence number, is legal at any point
        including before the first event, and is dropped by every conforming
        parser."""
        return _chat_sse.sse_comment(note)


def parse_frames(stream: str) -> list[Dict[str, Any]]:
    """The wire text back into `{"event": name, "data": {...}}` records.

    Comments are skipped. Used by the tests and by anything that has to prove
    a stream conformed after the fact; it is not on the serving path.
    """
    records: list[Dict[str, Any]] = []
    for block in stream.split("\n\n"):
        if not block.strip() or block.lstrip().startswith(":"):
            continue
        name: Optional[str] = None
        data_lines: list[str] = []
        for line in block.split("\n"):
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                data_lines.append(line[len("data: ") :])
        raw = "\n".join(data_lines)
        records.append({"event": name, "data": json.loads(raw) if raw else None})
    return records


# --------------------------------------------- Chat Completions framing --


class ChatCompletionChunks:
    """`POST /v1/chat/completions` streaming, the compatibility dialect.

    Different in every way that matters from the Responses stream, and all of
    the differences are load-bearing for an existing client library:

    * chunks are anonymous — `data:` with no `event:` line, and no
      `sequence_number`;
    * the stream ends with the literal `data: [DONE]` line. On the Responses
      surface that sentinel is forbidden; here it is required;
    * when the caller asked for usage (`stream_options.include_usage`), ONE
      extra chunk goes out before `[DONE]` whose `choices` is empty and whose
      `usage` is populated. Every other chunk carries `usage: null`. LiteLLM-
      style proxies and the OpenAI SDKs key off exactly this shape, and a
      deviation breaks cost accounting silently rather than loudly.
    """

    def __init__(
        self,
        *,
        completion_id: str,
        model: str,
        created: Optional[int] = None,
        include_usage: bool = False,
    ) -> None:
        self.id = completion_id
        self.model = model
        self.created = int(created if created is not None else time.time())
        self.include_usage = bool(include_usage)
        self._done = False
        self._stopped = False
        self._usage_sent = False
        self._first = True

    @property
    def finished(self) -> bool:
        return self._done

    def _chunk(self, choices: Iterable[Mapping[str, Any]], usage: Any = None) -> str:
        if self._done:
            raise StreamProtocolError(
                "a chunk was framed after data: [DONE]; the stream is over"
            )
        body = {
            "id": self.id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
            "choices": [dict(choice) for choice in choices],
            "usage": usage,
        }
        return f"data: {_dumps(body)}\n\n"

    def delta(self, text: str, *, index: int = 0) -> str:
        """One chunk of text. The first one also announces the assistant role,
        which is what the reference wire format does and what several clients
        use to open their message object."""
        if self._stopped:
            raise StreamProtocolError("a delta was framed after the finish_reason chunk")
        payload: Dict[str, Any] = {"content": str(text)}
        if self._first:
            payload = {"role": "assistant", "content": str(text)}
            self._first = False
        return self._chunk([{"index": int(index), "delta": payload, "finish_reason": None}])

    def stop(self, finish_reason: str = "stop", *, index: int = 0) -> str:
        """The chunk that names why generation ended: `stop`, or `length` when
        the answer hit `max_tokens`. A client that never sees one must treat
        the answer as truncated."""
        if self._stopped:
            raise StreamProtocolError("the finish_reason chunk was framed twice")
        self._stopped = True
        self._first = False
        return self._chunk([{"index": int(index), "delta": {}, "finish_reason": finish_reason}])

    def usage_chunk(self, usage: Optional[Mapping[str, Any]]) -> str:
        """The final, choice-less chunk. Only when the caller asked for usage:
        a client that did not request it does not expect a chunk with no
        choices and several will crash on one."""
        if not self.include_usage:
            raise StreamProtocolError(
                "a usage chunk was framed without stream_options.include_usage"
            )
        if self._usage_sent:
            raise StreamProtocolError("the usage chunk was framed twice")
        self._usage_sent = True
        return self._chunk([], usage=None if usage is None else dict(usage))

    def error_chunk(self, err: ApiError) -> str:
        """A failure, in the anonymous dialect.

        CONTRACT §9 says the error envelope is the same "everywhere, including
        mid-stream", and until 2026-09-13 this class had no error frame at all
        — which forced a route serving the compatibility surface to hand-roll
        the one thing that must not differ between the two dialects (verifier
        finding). The payload is `{"error": {…}}`, the shape an OpenAI-derived
        client already unwraps, carrying the same `code` and the same scrubbed
        message the HTTP envelope would have used.

        It does NOT end the stream: `data: [DONE]` still follows, because a
        client library reading this dialect waits for that sentinel and hangs
        without it. `_chunk`'s own guard keeps it from being framed after one.
        """
        if self._done:
            raise StreamProtocolError(
                "an error chunk was framed after data: [DONE]; the stream is over"
            )
        payload = err.stream_payload(0)
        payload.pop("sequence_number", None)
        payload.pop("type", None)
        body = {
            "id": self.id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
            "choices": [],
            "usage": None,
            "error": {
                "message": payload["message"],
                "type": err.type,
                "code": err.code,
                "param": err.param,
            },
        }
        return f"data: {_dumps(body)}\n\n"

    def done(self) -> str:
        """`data: [DONE]`, exactly once, last."""
        if self._done:
            raise StreamProtocolError("data: [DONE] was framed twice")
        self._done = True
        return DONE_SENTINEL

    def heartbeat(self, note: str = "ping") -> str:
        return _chat_sse.sse_comment(note)
