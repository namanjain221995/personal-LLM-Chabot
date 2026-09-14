"""Durable `/v1` generations: launch, attach, follow, suspend, resume (2026-09-13).

WHY. With ~5 orchestrator deploys a day, a 3-hour public generation crosses a
deploy with probability ~46% (design deploy_survival), and until this module
every deploy killed it: the generation lived in the coroutine of the request
that started it. The owner's goal is "no timeouts on /v1", and a request that
dies because the process holding it was replaced is a timeout by another
name. So a main-model generation is now a DETACHED RUNNER owning a lease on
its `api_responses` row, writing every event to a write-ahead log
(`api_response_events`) before any reader sees it; readers FOLLOW the log.
A SIGTERM suspends (flush, release the lease); another process claims the row
and resumes the answer by `continue_final_message`, so the text can only be
extended, never regenerated.

THE PIECES, in the order a request meets them:

* `launch(...)` — make the row durable, take the lease (background runs are
  rows only: the dispatcher claims them when their gate has room), write the
  spec (images as content-addressed blobs), start the runner.
* `attach(...)` — find a run by response id, gateway attempt token or the
  implicit-attach match, with the creator rule: the run's own key, or a key
  of the same service account. Anything else is `AttachForbidden`, which the
  route renders exactly as a missing response (another project's run is
  indistinguishable from missing because the project-scoped lookup happens
  first, in the route).
* `Handle.follow(after)` — committed records after `after`, heartbeats while
  nothing commits, closes after the terminal record. Local runs wake in
  process after commit; runs owned by another process are served by ONE
  shared poller per process (`poll_many`, one statement per second).
* the runner (`_drive`) — gate, quarantine wait, dispatch, liveness guard,
  interrupt classification, counters, `wait_not_bad`, continuation. Interrupts
  are not failures; only the design's counters end a run.
* the writer — every 100 ms, one transaction, a SAVEPOINT per job.
* the lease loop — every 15 s: renew all local leases in one UPDATE, re-check
  every local key in one batch (`still_authorised`), notice cross-process
  cancels.
* sweeps — lapsed background runs (staggered 30 s, oldest first, after the
  chat continuity sweep), suspended-unread TTL (900 s → cancelled with no
  engine work), event retention, the blob reaper.
* `suspend_all(reason)` / `request_suspend()` — SIGTERM: aclose engine
  streams, flush, write missing specs, release leases, abort every reader so
  the gateway sees an incomplete body and re-attaches.

INTERFACES FROM OTHER TEAMS, and the shims used until they land (every shim is
marked `SHIM(Tn)` for the assembler):
* T1 llm.stream_chat_events kwargs (wall_clock_s, read_timeout_s,
  continue_final_message, admission_patient, on_dispatch) — detected by
  `streaming._accepted_keywords`; without `continue_final_message` a resume is
  impossible and a suspended run fails `model_unavailable` (the kill switch's
  behaviour).
* T1 engine_state helpers — `liveness.EngineStateView`.
* T1 admission.register_yield / kv_ledger — called directly (assembler,
  2026-09-14: the getattr fallbacks are removed).
* T3 resolver.still_authorised — `durable_store.still_authorised_shim`.
* T3 events grammar (output_item/content_part events, to_record/from_record)
  — `RecordBuilder` here produces the existing CONTRACT §10 grammar; T3 may
  swap the builder without touching the log format (event name + JSON data).
* T3 router — calls `launch`/`attach`/`Handle`; `render_responses_frame` and
  `ChatRenderer` turn records into wire frames (`: ts-seq=N` for trusted
  gateway-tagged requests). WIRED 2026-09-14: every `store: true` generation
  (sync, stream, background; both dialects) launches here, a gateway re-POST,
  an Idempotency-Key retry and an SDK retry attach here, and
  `GET /v1/responses/{id}?stream=true&starting_after=N` follows the log
  (router.py, "durable foreground runs"). The grammar now includes the item
  and content-part events and `response.output_text.annotation.added`
  (`RecordBuilder`, FILE CITATIONS ON A DURABLE RUN).
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import dataclasses
import functools
import inspect
import logging
import os
import socket
import time
import uuid
from dataclasses import dataclass
from typing import (
    Any, AsyncIterator, Awaitable, Callable, Deque, Dict, List, Mapping, Optional,
    Sequence, Set, Tuple,
)

from .. import db
from ..config import settings
from . import blobs, capacity, durable_store, errors, events, liveness, registry, streaming

log = logging.getLogger(__name__)

#: This process's lease identity. Hostname + pid + a random suffix: a pid is
#: reused across container restarts and two processes must never share one.
OWNER = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

DIALECT_RESPONSES = "responses"
DIALECT_CHAT = "chat"

#: The design's continuation floor: less room than this is an output-limit stop.
MIN_OUTPUT_TOKENS = 256
CONTEXT_SAFETY_MARGIN = 512
#: Committed records kept in memory per run for fast local replay; a follower
#: further behind reads from the log.
TAIL_RECORDS = 512
#: Byte cost charged to the pending buffer for a non-delta record.
_RECORD_OVERHEAD = 256
#: How many attempt entries `metadata.attempts` keeps (bounded row size).
MAX_ATTEMPT_ENTRIES = 50
#: Re-dispatch backoff after connect failures (design: 2 → 60 s).
CONNECT_BACKOFF_S = 2.0
CONNECT_BACKOFF_MAX_S = 60.0
#: Shortest stagger the tests may configure.
_MIN_LOOP_S = 0.01
#: How long a freshly launched (or claimed) run may go without a reader
#: before it counts as orphaned (2026-09-14 review P2). A route starts
#: iterating its body within milliseconds of `launch`; a run nobody picked up
#: after a second belongs to a client that disconnected before its body
#: started, and without this it generated for nobody and an SDK retry could
#: never find it (`find_implicit` needs `orphaned_at`).
LAUNCH_ATTACH_GRACE_S = 1.0
#: How long `suspend_all` waits for the stopped runners to record their
#: attempts before it releases the leases (a SIGTERM budget, not a clock on
#: any request).
SUSPEND_RECORD_WAIT_S = 2.0
#: Queued background rows the dispatcher looks at per engine per pass
#: (2026-09-14 review P5: one global LIMIT 50 let fifty main rows hide a
#: router row whose gate was free).
DISPATCH_SCAN_PER_ENGINE = 200


# ------------------------------------------------------------ settings --


def _bool_setting(name: str, default: bool) -> bool:
    value = getattr(settings, name.lower(), None)
    if value is None:
        raw = os.environ.get(name, "")
        if not raw.strip():
            return default
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def resume_enabled() -> bool:
    """PUBLIC_API_RESUME_ENABLED (true). The kill switch: false makes a
    suspended run fail `model_unavailable`; its log stays readable."""
    return _bool_setting("PUBLIC_API_RESUME_ENABLED", True)


def lease_ttl_s() -> float:
    return max(1.0, registry.setting_float("PUBLIC_API_LEASE_TTL_S", 60.0))


def lease_heartbeat_s() -> float:
    return max(_MIN_LOOP_S, registry.setting_float("PUBLIC_API_LEASE_HEARTBEAT_S", 15.0))


def flush_s() -> float:
    return max(_MIN_LOOP_S, registry.setting_float("PUBLIC_API_EVENT_FLUSH_S", 0.1))


def pending_max_bytes() -> int:
    return max(1024, registry.setting_int("PUBLIC_API_PENDING_EVENTS_MAX_BYTES", 64 * 1024 * 1024))


def follower_poll_s() -> float:
    return max(_MIN_LOOP_S, registry.setting_float("PUBLIC_API_FOLLOWER_POLL_S", 1.0))


def suspended_claim_poll_s() -> float:
    return max(_MIN_LOOP_S, registry.setting_float("PUBLIC_API_SUSPENDED_CLAIM_POLL_S", 5.0))


def max_followers() -> int:
    return max(1, registry.setting_int("PUBLIC_API_MAX_FOLLOWERS", 8))


def stream_orphan_grace_s() -> float:
    return max(0.0, registry.setting_float("PUBLIC_API_STREAM_ORPHAN_GRACE_S", 600.0))


def unkeyed_orphan_grace_s() -> float:
    return max(0.0, registry.setting_float("PUBLIC_API_UNKEYED_ORPHAN_GRACE_S", 120.0))


def suspended_unread_ttl_s() -> float:
    return max(0.0, registry.setting_float("PUBLIC_API_SUSPENDED_UNREAD_TTL_S", 900.0))


def resume_stagger_s() -> float:
    return max(0.0, registry.setting_float("PUBLIC_API_RESUME_STAGGER_S", 30.0))


def sweep_s() -> float:
    return max(_MIN_LOOP_S, registry.setting_float("PUBLIC_API_LAPSED_SWEEP_S", 30.0))


def implicit_attach_window_s() -> float:
    return max(0.0, registry.setting_float("PUBLIC_API_IMPLICIT_ATTACH_WINDOW_S", 3600.0))


def event_retention_s() -> float:
    return max(0.0, registry.setting_float("PUBLIC_API_EVENT_RETENTION_S", 3600.0))


def heartbeat_s() -> float:
    return float(events.HEARTBEAT_SECONDS)


def retained_hook_bytes() -> int:
    """PUBLIC_API_RETAINED_HOOK_BYTES (64 MiB). The prompt bytes this process
    may keep reachable through the `on_finish` closures of QUEUED background
    runs (2026-09-14 review, high): a router recorder closure captures its
    GenerationPlan, i.e. the whole prompt, and 10,000 queued 4 MiB prompts
    would otherwise live in the orchestrator's RSS until each job ran. Past
    the budget the closure is dropped and the run records through its
    `RecorderRef` (or the runtime recorder) when it settles."""
    return max(0, registry.setting_int("PUBLIC_API_RETAINED_HOOK_BYTES", 64 * 1024 * 1024))


def max_yields_without_progress() -> int:
    """PUBLIC_API_MAX_YIELDS_WITHOUT_PROGRESS (3). After this many chat
    yields with no first token in between, a run keeps its place until its
    next first token (2026-09-14 review P9: a re-prefill pre-empted by every
    chat LONG turn otherwise spun for ever, burning prefill with no output)."""
    return max(1, registry.setting_int("PUBLIC_API_MAX_YIELDS_WITHOUT_PROGRESS", 3))


def quarantine_clear_tokens() -> int:
    """PUBLIC_API_QUARANTINE_CLEAR_TOKENS (2048). A quarantined attempt that
    has decoded this many tokens has shown it is not the poison: its engine
    fault count goes back to 0 (2026-09-14 review P4)."""
    return max(1, registry.setting_int("PUBLIC_API_QUARANTINE_CLEAR_TOKENS", 2048))


def quarantine_clear_s() -> float:
    """PUBLIC_API_QUARANTINE_CLEAR_S (300). ...or has kept decoding this long
    after its first token (a slow decode proves the same thing)."""
    return max(0.0, registry.setting_float("PUBLIC_API_QUARANTINE_CLEAR_S", 300.0))


def sidecar_max_stalled_attempts() -> int:
    """Router/OCR: a silent end (lost, stalled, an engine 5xx) is re-dispatched
    ONCE, then the run fails retryably (design liveness_guard SIDECARS)."""
    return 2


# ------------------------------------------------------------- errors --


class AttachForbidden(Exception):
    """The run exists but the caller is not its creator (nor a key of the
    creator's service account). The route renders 404 on GET streams and 409
    with x-should-retry:false on a same-key attach — never a distinguishing
    message."""


class NotStreamable(Exception):
    """The run cannot be replayed: store:false, or its events are past
    retention. The route renders 400 param `stream` AFTER the 404 checks."""


class FollowerAborted(Exception):
    """The reader must end its body WITHOUT a terminal frame (a suspend, a
    lease loss): the gateway sees an incomplete read and re-attaches."""


class FollowerEvicted(FollowerAborted):
    """A ninth follower arrived; the oldest is closed."""


# ------------------------------------------------------------- records --

Record = Tuple[int, str, Dict[str, Any]]
HEARTBEAT = object()


def public_data(data: Mapping[str, Any]) -> Dict[str, Any]:
    """A stored record's data without the private `_`-prefixed keys."""
    return {k: v for k, v in data.items() if not str(k).startswith("_")}


def render_responses_frame(record: Record, *, tagged: bool = False) -> str:
    """`event: name\\ndata: {...}\\n\\n`, plus the internal `: ts-seq=N`
    comment on a trusted gateway-tagged request (never `id:`/`retry:`)."""
    import json

    seq, name, data = record
    frame = f"event: {name}\ndata: {json.dumps(public_data(data), ensure_ascii=False, default=str)}\n\n"
    if tagged:
        frame += f": ts-seq={int(seq)}\n\n"
    return frame


class ChatRenderer:
    """Chat Completions chunks from the same log. SHIM(T3): events.py's
    chat-chunk rendering replaces this when it lands; the mapping is the one
    `streaming.chat_completions_sse` has always produced."""

    def __init__(self, *, completion_id: str, model: str, created: int, include_usage: bool) -> None:
        self.chunks = events.ChatCompletionChunks(
            completion_id=completion_id, model=model, created=created, include_usage=include_usage
        )
        self.include_usage = include_usage

    def render(self, record: Record, *, tagged: bool = False) -> str:
        seq, name, data = record
        out: List[str] = []
        if self.chunks.finished:
            return ""
        if name == events.RESPONSE_OUTPUT_TEXT_DELTA:
            text = str(data.get("delta") or "")
            if text:
                out.append(self.chunks.delta(text))
        elif name == events.RESPONSE_COMPLETED:
            response = data.get("response") or {}
            incomplete = response.get("incomplete_details") or {}
            finish = "length" if incomplete.get("reason") == "max_output_tokens" else "stop"
            out.append(self.chunks.stop(
                finish, max_output_tokens=response.get("max_output_tokens"),
                annotations=response_annotations(response),
            ))
            if self.include_usage:
                usage = response.get("usage")
                out.append(self.chunks.usage_chunk(None if usage is None else {
                    "prompt_tokens": usage.get("input_tokens"),
                    "completion_tokens": usage.get("output_tokens"),
                    "total_tokens": usage.get("total_tokens"),
                }))
            out.append(self.chunks.done())
        elif name in (events.RESPONSE_FAILED, events.ERROR):
            response = data.get("response") or {}
            err = response.get("error") or data
            code = str(err.get("code") or "model_unavailable")
            try:
                failure = errors.ApiError(code, str(err.get("message") or ""), retry_after=30)
            except ValueError:
                failure = errors.model_unavailable()
            out.append(self.chunks.error_chunk(failure))
            out.append(self.chunks.done())
        text = "".join(out)
        if text and tagged:
            text += f": ts-seq={int(seq)}\n\n"
        return text


def response_annotations(response: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """The annotations a terminal Response snapshot carries on its
    `output_text` part (empty when none)."""
    for item in response.get("output") or []:
        for part in (item or {}).get("content") or []:
            if (part or {}).get("type") == "output_text":
                return [dict(a) for a in (part.get("annotations") or [])]
    return []


# --------------------------------------------------------- file citations --
#
# FILE CITATIONS ON A DURABLE RUN (2026-09-14, gap 4 of the no-timeout
# release). A request with files resolves its `file_citation` annotations
# against the pairs of content the model was actually shown
# (`apifiles.citations.CitationIndex`, built with the context). A durable run
# may be settled by a DIFFERENT process than the one that built that index
# (a deploy in between), so the index travels with the spec as JSON under
# `extra["file_citations"]` and the settling process rebuilds it. The
# annotations are then emitted as `response.output_text.annotation.added`
# records after `output_text.done` and repeated on the part, the item and the
# terminal snapshot — the same grammar `streaming.responses_sse` frames for a
# non-durable stream.
#
# THE INTERFACE WITH THE FILES TEAM: `CitationIndex.to_json()` /
# `CitationIndex.from_json(data)` when the class has them; until then the two
# functions below read and rebuild the index's registered entries through its
# public `register`/`add_page`/`add_rows`/`add_span` methods, and the
# round-trip is pinned by a test so a change to the class fails loudly.

FILE_CITATIONS_KEY = "file_citations"


def citation_index_to_json(index: Any) -> Optional[Dict[str, Any]]:
    """A `CitationIndex` as JSON, or None when there is nothing to cite."""
    if index is None:
        return None
    native = getattr(index, "to_json", None)
    if callable(native):
        return native()
    entries = []
    for label, entry in dict(getattr(index, "_by_label", {}) or {}).items():
        entries.append({
            "label": label, "file_id": entry.file_id, "filename": entry.filename, "unit": entry.unit,
            "numbers": sorted({int(k) for k in entry.numbers}),
            "row_blocks": [list(block) for block in entry.row_blocks],
            "spans": [list(span) for span in entry.spans],
        })
    return {"entries": entries} if entries else None


def citation_index_from_json(data: Mapping[str, Any]) -> Any:
    from ..apifiles import citations

    native = getattr(citations.CitationIndex, "from_json", None)
    if callable(native):
        return native(data)
    index = citations.CitationIndex()
    for entry in data.get("entries") or []:
        label = str(entry["label"])
        index.register(label, file_id=entry.get("file_id"), filename=str(entry.get("filename") or ""),
                       unit=str(entry["unit"]))
        for number in entry.get("numbers") or []:
            index.add_page(label, int(number))
        for first, last, page in entry.get("row_blocks") or []:
            index.add_rows(label, int(first), int(last), int(page))
        for start, end in entry.get("spans") or []:
            index.add_span(label, float(start), float(end))
    return index


def file_annotations(extra: Mapping[str, Any], text: str) -> List[Dict[str, Any]]:
    """The `file_citation` annotations of `text` for a run whose spec carries
    a citation index. Never raises: a citation that cannot be resolved is
    plain text, not a failed answer."""
    data = (extra or {}).get(FILE_CITATIONS_KEY)
    if not data or not text:
        return []
    try:
        from ..apifiles import citations

        return [dict(a) for a in citations.annotate(text, citation_index_from_json(data)).annotations]
    except Exception:  # noqa: BLE001
        log.warning("file citations of a durable run were not computed", exc_info=True)
        return []


class RecordBuilder:
    """The CONTRACT §10 Responses grammar as log records. SHIM(T3): T3's
    events.to_record (with output_item/content_part events) may replace the
    methods; the stored shape (name, JSON data with `type` and
    `sequence_number`) is the contract with the log."""

    def __init__(self, run: "Run") -> None:
        self.run = run

    def _record(self, name: str, payload: Dict[str, Any]) -> List[Any]:
        seq = self.run._assign_seq()
        body: Dict[str, Any] = {"type": name, "sequence_number": seq}
        body.update(payload)
        body["type"] = name
        body["sequence_number"] = seq
        return [seq, name, body]

    def response(self, status: str, **kwargs: Any) -> Dict[str, Any]:
        return streaming._wire(self.run.spec, status, **kwargs)

    def created(self) -> List[Any]:
        return self._record(events.RESPONSE_CREATED, {"response": self.response("queued")})

    def queued(self) -> List[Any]:
        return self._record(events.RESPONSE_QUEUED, {"response": self.response("queued")})

    def in_progress(self) -> List[Any]:
        return self._record(events.RESPONSE_IN_PROGRESS, {"response": self.response("in_progress")})

    def item_added(self) -> List[Any]:
        return self._record(events.RESPONSE_OUTPUT_ITEM_ADDED, events.output_item_added_payload(self.run.spec.item_id))

    def part_added(self) -> List[Any]:
        return self._record(events.RESPONSE_CONTENT_PART_ADDED, events.content_part_added_payload(self.run.spec.item_id))

    def annotation_added(self, index: int, annotation: Mapping[str, Any]) -> List[Any]:
        return self._record(
            events.RESPONSE_OUTPUT_TEXT_ANNOTATION_ADDED,
            events.annotation_added_payload(self.run.spec.item_id, index, annotation),
        )

    def part_done(self, text: str, annotations: Sequence[Mapping[str, Any]]) -> List[Any]:
        return self._record(
            events.RESPONSE_CONTENT_PART_DONE, events.content_part_done_payload(self.run.spec.item_id, text, annotations)
        )

    def item_done(self, text: str, annotations: Sequence[Mapping[str, Any]]) -> List[Any]:
        return self._record(
            events.RESPONSE_OUTPUT_ITEM_DONE, events.output_item_done_payload(self.run.spec.item_id, text, annotations)
        )

    def delta(self, text: str, tokens: int) -> List[Any]:
        return self._record(
            events.RESPONSE_OUTPUT_TEXT_DELTA,
            {"item_id": self.run.spec.item_id, "output_index": 0, "content_index": 0,
             "delta": text, durable_store.TOKENS_KEY: int(tokens)},
        )

    def text_done(self, text: str) -> List[Any]:
        return self._record(
            events.RESPONSE_OUTPUT_TEXT_DONE,
            {"item_id": self.run.spec.item_id, "output_index": 0, "content_index": 0, "text": text},
        )

    def completed(self, *, annotations: Sequence[Mapping[str, Any]] = (), **kwargs: Any) -> List[Any]:
        response = events.with_annotations(self.response("completed", **kwargs), annotations)
        return self._record(events.RESPONSE_COMPLETED, {"response": response})

    def failed(self, *, should_retry: Optional[bool] = None, **kwargs: Any) -> List[Any]:
        payload: Dict[str, Any] = {"response": self.response("failed", **kwargs)}
        if should_retry is not None:
            payload["_should_retry"] = bool(should_retry)
        return self._record(events.RESPONSE_FAILED, payload)


# ---------------------------------------------------------- spec codec --


def spec_to_json(spec: streaming.GenerationSpec, store: Optional[blobs.BlobStore] = None) -> Tuple[Dict[str, Any], List[Tuple[str, int]]]:
    """GenerationSpec → JSON with images externalised to blobs."""
    body = dataclasses.asdict(spec)
    stored_blobs: List[Tuple[str, int]] = []
    if store is not None:
        body["messages"], stored_blobs = blobs.externalize_images(spec.messages, store)
    return body, stored_blobs


def spec_from_json(body: Mapping[str, Any], store: Optional[blobs.BlobStore] = None) -> streaming.GenerationSpec:
    names = {f.name for f in dataclasses.fields(streaming.GenerationSpec)}
    fields_ = {k: v for k, v in dict(body).items() if k in names}
    if store is not None:
        fields_["messages"] = blobs.internalize_images(fields_.get("messages") or [], store)
    return streaming.GenerationSpec(**fields_)


# ----------------------------------------------------------------- run --


@dataclass
class Caller:
    """The part of an `ApiCaller` durability needs. Built by `caller_of`."""

    project_id: str
    workspace_id: str
    key_id: str
    service_account_id: Optional[str] = None


def caller_of(api_caller: Any) -> Caller:
    return Caller(
        project_id=str(api_caller.project_id),
        workspace_id=str(api_caller.workspace_id),
        key_id=str(getattr(api_caller, "key_id", "") or ""),
        service_account_id=getattr(api_caller, "service_account_id", None) or None,
    )


class _Reader:
    __slots__ = ("run_id", "evicted", "kind", "started")

    def __init__(self, run_id: str, kind: str) -> None:
        self.run_id = run_id
        self.kind = kind
        self.evicted = False
        self.started = time.monotonic()


class Run:
    """One durable generation as THIS process holds it."""

    def __init__(
        self,
        *,
        spec: streaming.GenerationSpec,
        project_id: str,
        workspace_id: str,
        key_id: Optional[str],
        dialect: str,
        background: bool,
        keyed: bool,
        streamed: bool,
        extra: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.spec = spec
        self.id = spec.response_id
        self.project_id = project_id
        self.workspace_id = workspace_id
        self.key_id = key_id
        self.dialect = dialect
        self.background = background
        self.keyed = keyed
        self.streamed = streamed
        self.extra: Dict[str, Any] = dict(extra or {})
        self.builder = RecordBuilder(self)
        # -- the log --
        self.assigned_seq = 0
        self.committed_seq = 0
        self._pending: List[List[Any]] = []
        self._inflight: List[List[Any]] = []
        self.pending_bytes = 0
        self._tail: Deque[Record] = collections.deque(maxlen=TAIL_RECORDS)
        self._changed = asyncio.Event()
        self.spec_written = False
        # -- progress --
        self.pieces: List[str] = []
        self.generated_tokens = 0
        self.attempt = 0
        self.stalled_attempts = 0
        self.engine_fault_attempts = 0
        self.yields = 0
        self.attempts: List[Dict[str, Any]] = []
        self.input_tokens: Optional[int] = None
        self.recomputed_prompt_tokens = 0
        self.all_usage_reported = True
        self.first_token_at: Optional[float] = None
        self.started_monotonic = time.monotonic()
        self.last_incident_id: Optional[str] = None
        self.resumed = False
        self.emitted_first_token_this_attempt = False
        # -- control --
        self.status = "queued"
        self.terminal = False
        self.terminal_row: Optional[Dict[str, Any]] = None
        self.outcome: Optional[streaming.StreamOutcome] = None
        self.done = asyncio.Event()
        self.stop_reason: Optional[str] = None
        self.stop_event = asyncio.Event()
        self.lease_held = False
        self.aborted = False
        self.generation: Any = None
        self.task: Optional[asyncio.Task] = None
        self.readers: List[_Reader] = []
        self.ever_followed = False
        self.orphan_task: Optional[asyncio.Task] = None
        self.on_finish: Optional[streaming.OnFinish] = None
        self.store_paused = False
        self.yield_requested = False
        self.queued_announced = False
        self.connect_failures = 0
        self.sidecar_down_since: Optional[float] = None
        #: Serialisable recorder (stored with the spec); see RecorderRef.
        self.recorder_ref: Optional[Dict[str, Any]] = None
        #: Chat yields since this run's last first token (review P9).
        self.yields_without_progress = 0
        #: Set by `abort_readers`: a synchronous waiter wakes on it at once
        #: instead of at its next heartbeat (review P7).
        self.abort_event = asyncio.Event()
        #: Serialises the orphaned_at mark/clear writes (review P2).
        self.orphan_lock = asyncio.Lock()
        self.orphan_marked = False
        self.orphan_write: Optional[asyncio.Future] = None
        #: The dispatcher's gate group this run is the FIFO representative of
        #: until it holds its gates (review P5).
        self.dispatch_key: Optional[Tuple[str, ...]] = None
        #: Times this run's quarantine was cleared by decoding (review P4).
        self.quarantine_cleared = 0
        #: Whether `response.output_item.added` / `content_part.added` are in
        #: the log (they precede the first delta, in the same pending batch).
        self.item_open = False
        #: Called once when the run holds its gates (`response.in_progress`)
        #: or ends, whichever is first: the router's waiter count
        #: (PUBLIC_API_GATE_MAX_WAITERS) gives its place back there.
        self._admitted_callbacks: List[Callable[[], None]] = []
        self.admitted = False

    # -- log ------------------------------------------------------------

    def _assign_seq(self) -> int:
        self.assigned_seq += 1
        return self.assigned_seq

    def add_record(self, record: List[Any]) -> None:
        self._pending.append(record)
        self.pending_bytes += _RECORD_OVERHEAD

    def open_item(self) -> None:
        """`output_item.added` + `content_part.added`, once, before any text."""
        if self.item_open:
            return
        self.item_open = True
        self.add_record(self.builder.item_added())
        self.add_record(self.builder.part_added())

    def on_admitted(self, callback: Callable[[], None]) -> None:
        """Run `callback` when this run holds its gates or ends (at once if
        either already happened)."""
        if self.admitted or self.terminal:
            with contextlib.suppress(Exception):
                callback()
            return
        self._admitted_callbacks.append(callback)

    def mark_admitted(self) -> None:
        self.admitted = True
        callbacks, self._admitted_callbacks = self._admitted_callbacks, []
        for callback in callbacks:
            with contextlib.suppress(Exception):
                callback()

    def add_delta(self, text: str, tokens: int = 1) -> None:
        """Coalesce into the last pending delta (≤ one event per flush)."""
        if not text:
            return
        self.open_item()
        self.pieces.append(text)
        self.generated_tokens += int(tokens)
        if self._pending and self._pending[-1][1] == events.RESPONSE_OUTPUT_TEXT_DELTA:
            data = self._pending[-1][2]
            data["delta"] = str(data["delta"]) + text
            data[durable_store.TOKENS_KEY] = int(data.get(durable_store.TOKENS_KEY) or 0) + int(tokens)
        else:
            self._pending.append(self.builder.delta(text, tokens))
        self.pending_bytes += len(text.encode("utf-8", "surrogatepass"))

    def take_pending(self) -> List[Record]:
        self._inflight = self._pending
        self._pending = []
        return [(int(r[0]), str(r[1]), r[2]) for r in self._inflight]

    def restore_inflight(self) -> None:
        self._pending = self._inflight + self._pending
        self._inflight = []

    def commit_inflight(self) -> None:
        records = [(int(r[0]), str(r[1]), r[2]) for r in self._inflight]
        self._inflight = []
        self._commit(records)

    def _commit(self, records: Sequence[Record]) -> None:
        if not records:
            return
        for record in records:
            self._tail.append(record)
        self.committed_seq = max(self.committed_seq, max(r[0] for r in records))
        self.pending_bytes = sum(
            _RECORD_OVERHEAD if r[1] != events.RESPONSE_OUTPUT_TEXT_DELTA
            else len(str(r[2].get("delta") or "").encode("utf-8", "surrogatepass"))
            for r in self._pending
        )
        self.wake()

    def wake(self) -> None:
        changed = self._changed
        self._changed = asyncio.Event()
        changed.set()

    def tail_after(self, position: int) -> Optional[List[Record]]:
        """Records after `position` from memory, or None when memory does not
        reach back that far (the follower reads the log instead)."""
        if position >= self.committed_seq:
            return []
        if not self._tail or self._tail[0][0] > position + 1:
            return None
        return [r for r in self._tail if r[0] > position]

    @property
    def text(self) -> str:
        return "".join(self.pieces)

    # -- control --------------------------------------------------------

    def request_stop(self, reason: str) -> None:
        if self.stop_reason is None or reason in ("lease_lost", "cancel", "revoked"):
            self.stop_reason = reason
        self.stop_event.set()

    def clear_stop(self) -> None:
        self.stop_reason = None
        self.stop_event = asyncio.Event()

    def abort_readers(self) -> None:
        self.aborted = True
        self.abort_event.set()
        self.wake()


class Handle:
    """What a route holds: follow, wait, detach. Never a reference to the
    runner task itself."""

    def __init__(self, runtime: "Runtime", response_id: str, run: Optional[Run]) -> None:
        self._runtime = runtime
        self.id = response_id
        self.run = run

    async def follow(self, after: int = 0, *, heartbeat: Optional[float] = None) -> AsyncIterator[Any]:
        async for item in self._runtime.follow(self.id, after, heartbeat=heartbeat):
            yield item

    async def wait(
        self,
        *,
        heartbeat: Optional[float] = None,
        on_heartbeat: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> Dict[str, Any]:
        """Wait for the terminal state and return the settled row (the sync
        path). A local run is waited on without replaying its log; a run
        owned elsewhere is followed. `on_heartbeat` is where the committed
        JSON response writes its whitespace (T3 keepalive.py)."""
        runtime = self._runtime
        beat = heartbeat_s() if heartbeat is None else float(heartbeat)
        while True:
            run = runtime.runs.get(self.id)
            if run is None:
                async for item in self.follow(0, heartbeat=beat):
                    if item is HEARTBEAT and on_heartbeat is not None:
                        await on_heartbeat()
                row = await db.run_in_thread(durable_store.get_run, self.id)
                return row or {}
            reader = runtime._add_reader(self.id, run, "sync")
            # ONE waiter for the whole wait: a shielded wait per heartbeat
            # would leave a pending task behind every 15 s of a 3-hour call.
            # The abort waiter (2026-09-14 review P7) wakes the loop the
            # moment `suspend_all` aborts readers, so the body ends within
            # milliseconds of a SIGTERM instead of at the next heartbeat —
            # which held the gateway's re-attach and uvicorn's graceful
            # shutdown for up to 15 s. Heartbeats stay on their own clock.
            done = asyncio.ensure_future(run.done.wait())
            aborted = asyncio.ensure_future(run.abort_event.wait())
            loop = asyncio.get_running_loop()
            next_beat = loop.time() + beat
            try:
                while not run.terminal:
                    if run.aborted:
                        raise FollowerAborted()
                    if self.id not in runtime.runs:
                        if runtime._suspended_under_reader(run):
                            raise FollowerAborted()
                        break
                    await asyncio.wait({done, aborted}, timeout=max(0.0, next_beat - loop.time()),
                                       return_when=asyncio.FIRST_COMPLETED)
                    if run.terminal or run.aborted:
                        continue
                    if loop.time() >= next_beat:
                        next_beat = loop.time() + beat
                        if on_heartbeat is not None:
                            await on_heartbeat()
            finally:
                done.cancel()
                aborted.cancel()
                runtime._remove_reader(run, reader)
            if run.terminal:
                if run.terminal_row is not None:
                    return dict(run.terminal_row)
                row = await db.run_in_thread(durable_store.get_run, self.id)
                return row or {}


    async def result(
        self,
        *,
        heartbeat: Optional[float] = None,
        on_heartbeat: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> streaming.StreamOutcome:
        """Wait, then the outcome a synchronous body is rendered from
        (`StreamOutcome.response()` / the chat body). When this process
        settled the run it is the runner's own outcome; when another process
        did (a deploy in between), it is rebuilt from the settled row and the
        stored log — text from the deltas, usage from the row, the terminal
        record's error."""
        row = await self.wait(heartbeat=heartbeat, on_heartbeat=on_heartbeat)
        run = self.run
        if run is not None and run.outcome is not None:
            return run.outcome
        return await db.run_in_thread(outcome_from_log, row)


async def sse_frames(
    handle: Handle,
    *,
    after: int = 0,
    tagged: bool = False,
    chat: Optional[ChatRenderer] = None,
    heartbeat: Optional[float] = None,
) -> AsyncIterator[str]:
    """The body of every durable SSE response — a live stream, a gateway
    re-attach (`after` = X-TechSara-Resume-After) and
    `GET /v1/responses/{id}?stream=true&starting_after=N` — as wire frames:
    committed records after `after`, `: ping` on silence, closed after the
    terminal record. `tagged` adds the internal `: ts-seq=N` comment (trusted
    gateway requests only); `chat` renders the Chat Completions dialect.

    `FollowerAborted` PROPAGATES: the route must let the body end without a
    terminal chunk (an incomplete chunked read), which is what tells the
    gateway to re-attach instead of believing the answer ended."""
    async for item in handle.follow(after, heartbeat=heartbeat):
        if item is HEARTBEAT:
            yield events.SequencedEvents().heartbeat()
            continue
        if chat is not None:
            frame = chat.render(item, tagged=tagged)
            if frame:
                yield frame
            continue
        yield render_responses_frame(item, tagged=tagged)


def outcome_from_log(row: Mapping[str, Any]) -> streaming.StreamOutcome:
    """A settled run's outcome from its row and stored events (sync helper)."""
    records = durable_store.list_events(str(row["id"]), 0, 1_000_000)
    text = "".join(str(r[2].get("delta") or "") for r in records if r[1] == events.RESPONSE_OUTPUT_TEXT_DELTA)
    error = None
    for record in reversed(records):
        if record[1] == events.RESPONSE_FAILED:
            failure = (record[2].get("response") or {}).get("error") or {}
            try:
                error = errors.ApiError(str(failure.get("code") or "model_unavailable"),
                                        str(failure.get("message") or ""), retry_after=30)
            except ValueError:
                error = errors.model_unavailable()
            if record[2].get("_should_retry") is not None:
                setattr(error, "should_retry", bool(record[2]["_should_retry"]))
            break
    if error is None and row.get("status") == "failed":
        error = errors.model_unavailable()
    usage = None
    if row.get("input_tokens") is not None or row.get("output_tokens") is not None:
        usage = {"prompt_tokens": int(row.get("input_tokens") or 0),
                 "completion_tokens": int(row.get("output_tokens") or 0)}
    created = row.get("created_at")
    try:
        from . import background

        moment = background.as_datetime(created)
        created_at = int(moment.timestamp()) if moment is not None else 0
    except Exception:  # noqa: BLE001
        created_at = 0
    return streaming.StreamOutcome(
        response_id=str(row["id"]), model=str(row.get("model") or ""), created_at=created_at,
        status=str(row.get("status") or "failed"), text=text, usage=usage, error=error,
        ttft_ms=row.get("ttft_ms"), duration_ms=row.get("duration_ms"),
        finish_reason=row.get("finish_reason"), max_output_tokens=row.get("max_output_tokens"),
    )


# ------------------------------------------------------------- runtime --

GenerationFactory = Callable[..., Any]
Authoriser = Callable[[Sequence[str]], Awaitable[Set[str]]]
Recorder = Callable[[Dict[str, Any], streaming.StreamOutcome], Awaitable[None]]


def _default_factory(spec: streaming.GenerationSpec, **kwargs: Any) -> Any:
    return streaming.Generation(spec, **kwargs)


@dataclass(frozen=True)
class RecorderRef:
    """A SERIALISABLE recorder: a registered factory name and JSON args
    (2026-09-14 review, high). Stored with the spec, so whichever process
    settles the run — this one, or the one that resumed it after a deploy —
    builds the recorder at settle time, and a queued background row keeps no
    closure (and no prompt) in memory. `args` must be JSON: ids, the route,
    the request id, a reservation id — never the plan."""

    name: str
    args: Mapping[str, Any] = dataclasses.field(default_factory=dict)

    def to_json(self) -> Dict[str, Any]:
        return {"name": self.name, "args": dict(self.args)}


def handle_for(response_id: str) -> "Handle":
    """A handle on a run by id, local or not (a stream following a background
    job it just queued)."""
    return Handle(RUNTIME, response_id, RUNTIME.runs.get(response_id))


#: name → factory(args) → async recorder(row, outcome). T3 registers the
#: router's ("router.v1") at import; "usage" is the runtime's own.
_RECORDER_FACTORIES: Dict[str, Callable[[Mapping[str, Any]], "Recorder"]] = {}


def register_recorder(name: str, factory: Callable[[Mapping[str, Any]], "Recorder"]) -> None:
    """Register a recorder factory for `RecorderRef(name, args)`."""
    _RECORDER_FACTORIES[str(name)] = factory


def _prompt_bytes(value: Any, _depth: int = 0) -> int:
    """An estimate of the bytes a spec's messages keep alive (text and inline
    image data), for the retained-hook budget. Bounded recursion."""
    if isinstance(value, str):
        return len(value)
    if isinstance(value, (bytes, bytearray)):
        return len(value)
    if _depth > 8:
        return 0
    if isinstance(value, Mapping):
        return sum(_prompt_bytes(v, _depth + 1) for v in value.values())
    if isinstance(value, (list, tuple)):
        return sum(_prompt_bytes(v, _depth + 1) for v in value)
    return 0


async def default_recorder(row: Dict[str, Any], outcome: streaming.StreamOutcome) -> None:
    """The usage-ledger row for a run settled by a process that did NOT
    launch it (the launching request's recorder died with its process). One
    `usage_events` row per response, keyed by the response id — the ledger's
    unique generation id makes a second write a no-op. The row fields
    (tokens, status, error) were already written by `durable_store.finish`
    in the settling transaction. Quota counters are not touched: with the
    owner's PUBLIC_API_ENFORCE_LIMITS=false there is no reservation to settle.
    Since 2026-09-14 every run the router launches carries the `router.v1`
    RecorderRef (router._router_recorder_factory), which settles the quota and
    the Idempotency-Key too; this recorder remains for a run launched without
    one (a tool, a test)."""
    from .. import usage as usage_ledger

    counted = streaming.models.Usage.from_llm(outcome.usage)
    meta = row.get("metadata") or {}
    await usage_ledger.record_async(
        user_id=None,
        workspace_id=str(row.get("workspace_id") or "") or None,
        conversation_id=None,
        generation_id=str(row["id"]),
        route="v1_chat_completions" if row.get("dialect") == DIALECT_CHAT else "v1_responses",
        effort=streaming.PUBLIC_EFFORT,
        model=str(row.get("model") or outcome.model),
        mode="api",
        input_tokens=None if counted is None else counted.input_tokens,
        output_tokens=None if counted is None else counted.output_tokens,
        ttft_ms=outcome.ttft_ms,
        duration_ms=outcome.duration_ms,
        status=usage_ledger.ERROR if outcome.status == "failed" else usage_ledger.OK,
        error_kind=(outcome.error.code if outcome.error is not None else ""),
        meta={
            "api_key_id": row.get("key_id"),
            "project_id": row.get("project_id"),
            "request_id": row.get("request_id"),
            "streamed": bool(row.get("streamed")),
            "usage_source": None if outcome.usage is None else str(outcome.usage.get("source") or "engine"),
            "resume_count": meta.get("resume_count"),
            "recomputed_prompt_tokens": meta.get("recomputed_prompt_tokens"),
            "settled_by": "durable",
        },
    )


async def _default_authoriser(key_ids: Sequence[str], model: Optional[str] = None) -> Set[str]:
    """The mid-run authorisation re-check, WITH the run's model (2026-09-14
    review P8: `None` here meant narrowing a key's or project's
    allowed_models never stopped a running job, even across deploys)."""
    try:
        from ..apiplatform import resolver

        native = getattr(resolver, "still_authorised", None)
        if callable(native):
            result = native(list(key_ids), model)
            if asyncio.iscoroutine(result):
                result = await result
            return set(result)
    except Exception:  # noqa: BLE001 - fall through to the shim
        log.debug("resolver.still_authorised unavailable", exc_info=True)
    return await db.run_in_thread(durable_store.still_authorised_shim, list(key_ids), model)


def _accepts_model(fn: Callable[..., Any]) -> bool:
    """Whether an authoriser takes `(key_ids, model)` (tests may still pass
    the one-argument form)."""
    try:
        params = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return False
    if any(p.kind == p.VAR_POSITIONAL for p in params):
        return True
    positional = [p for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    return len(positional) >= 2


class Runtime:
    """The per-process durable machinery. `RUNTIME` is the one instance."""

    def __init__(self) -> None:
        self.owner = OWNER
        self.runs: Dict[str, Run] = {}
        self.factory: GenerationFactory = _default_factory
        self.authoriser: Authoriser = _default_authoriser
        self.recorder: Optional[Recorder] = default_recorder
        self.view: Optional[liveness.EngineView] = None
        self.clock: Callable[[], float] = time.monotonic
        self.blob_store: Optional[blobs.BlobStore] = None
        self.started = False
        self.stopping = False
        self._tasks: Set[asyncio.Task] = set()
        self._loops: List[asyncio.Task] = []
        self._flush_lock: Optional[asyncio.Lock] = None
        self._remote: Dict[str, List["_RemoteFollower"]] = {}
        self._launched_here: Set[str] = set()
        #: `on_finish` of background runs launched by this process, kept until
        #: the dispatcher claims them (the run object is built at claim time).
        #: A run resumed by ANOTHER process uses that process's recorder.
        self._local_hooks: Dict[str, streaming.OnFinish] = {}
        #: Prompt bytes each retained hook keeps reachable (the budget).
        self._hook_bytes: Dict[str, int] = {}
        self._retained_hook_bytes = 0
        #: Project concurrency slots (`quotas.SlotLease`) of runs launched
        #: here, released on EVERY way a run leaves this process
        #: (`forget_local`) — review 2026-09-14: durable background launches
        #: took no slot at all, so a project's limit never applied to them.
        self._slots: Dict[str, Any] = {}
        #: gate group → the dispatcher-claimed run still waiting for those
        #: gates: ONE FIFO representative per group (review P5).
        self._gate_waiters: Dict[Tuple[str, ...], str] = {}
        #: Response ids with a claim in flight in this process.
        self._claiming: Set[str] = set()
        #: Cancels that arrived while that claim was in flight.
        self._cancel_pending: Set[str] = set()
        self._last_stagger_claim: float = float("-inf")
        self._started_at = time.monotonic()
        self.continuity_done: Optional[asyncio.Event] = None
        self.store_ok = True
        #: The quarantined run in an attempt right now — held for the WHOLE
        #: attempt: a quarantined run runs alone among quarantined runs, which
        #: is what makes a second implication meaningful (an innocent run
        #: killed by a poison prompt must not be implicated twice).
        self._quarantined_dispatched: Optional[str] = None
        #: True until that run emits its first token: other RESUMED runs wait
        #: meanwhile, so its prefill is the only resumed prefill on the engine.
        self._quarantine_prefill = False
        self._resumed_awaiting_first_token: Set[str] = set()
        self._dispatch_changed: Optional[asyncio.Event] = None
        self.serving = None
        self.dispatch_wake: Optional[asyncio.Event] = None
        self.stats: Dict[str, int] = collections.defaultdict(int)
        #: Scrape router/OCR /metrics during their attempts (tests turn it off:
        #: the fake engines have no metrics endpoint to scrape).
        self.witnesses_enabled = True
        self._claim_attempts: Dict[str, float] = {}
        self._adhoc_poller: Optional[asyncio.Task] = None

    # -- configuration --------------------------------------------------

    def configure(
        self,
        *,
        factory: Optional[GenerationFactory] = None,
        authoriser: Optional[Authoriser] = None,
        recorder: Optional[Recorder] = None,
        view: Optional[liveness.EngineView] = None,
        clock: Optional[Callable[[], float]] = None,
        blob_store: Optional[blobs.BlobStore] = None,
        owner: Optional[str] = None,
    ) -> None:
        if factory is not None:
            self.factory = factory
        if authoriser is not None:
            self.authoriser = authoriser
        if recorder is not None:
            self.recorder = recorder
        if view is not None:
            self.view = view
        if clock is not None:
            self.clock = clock
        if blob_store is not None:
            self.blob_store = blob_store
        if owner is not None:
            self.owner = owner

    def _lock(self) -> asyncio.Lock:
        if self._flush_lock is None:
            self._flush_lock = asyncio.Lock()
        return self._flush_lock

    def _view(self) -> liveness.EngineView:
        return self.view if self.view is not None else liveness.default_view()

    def _blobs(self) -> blobs.BlobStore:
        if self.blob_store is None:
            self.blob_store = blobs.BlobStore()
        return self.blob_store

    def _spawn(self, coro: Awaitable[Any]) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _changed(self) -> asyncio.Event:
        if self._dispatch_changed is None:
            self._dispatch_changed = asyncio.Event()
        return self._dispatch_changed

    def _dispatch_state_changed(self) -> None:
        event = self._changed()
        self._dispatch_changed = asyncio.Event()
        event.set()

    # -- lifecycle ------------------------------------------------------

    async def start(self, *, continuity_done: Optional[asyncio.Event] = None) -> None:
        """Start the writer, lease, poller, dispatcher and sweep loops.
        Idempotent. `continuity_done` is chat's continuity sweep: background
        resumes wait for it (or 30 s)."""
        if self.started:
            return
        # Review 2026-09-14 P6: V36 is T1's migration; this is only a check
        # that it is there (catalog reads, no lock), applying the DDL only
        # when a column or table is missing. It NEVER raises out of start:
        # an exception here failed the orchestrator lifespan, chat included.
        # A runtime that cannot see its schema stays inactive (`is_active`
        # false), so background jobs take the legacy path.
        try:
            await db.run_in_thread(durable_store.ensure_schema, attempts=3)
        except Exception:  # noqa: BLE001 - see above
            log.error("durable runtime not started: its schema is unavailable", exc_info=True)
            return
        self.started = True
        self.stopping = False
        self._started_at = time.monotonic()
        self.continuity_done = continuity_done
        self.dispatch_wake = asyncio.Event()
        self._loops = [
            asyncio.ensure_future(self._writer_loop()),
            asyncio.ensure_future(self._lease_loop()),
            asyncio.ensure_future(self._poller_loop()),
            asyncio.ensure_future(self._dispatch_loop()),
            asyncio.ensure_future(self._sweep_loop()),
        ]
        self._register_ready_edge()

    def _register_ready_edge(self) -> None:
        """The engine READY edge wakes the dispatcher (same rules as a sweep)."""
        try:
            from .. import engine_state

            engine_state.on_ready(lambda: self.dispatch_wake.set() if self.dispatch_wake else None)
        except Exception:  # noqa: BLE001
            pass

    async def stop(self) -> None:
        self.stopping = True
        if self._adhoc_poller is not None:
            self._loops.append(self._adhoc_poller)
            self._adhoc_poller = None
        for task in self._loops:
            task.cancel()
        for task in self._loops:
            with contextlib.suppress(BaseException):
                await task
        self._loops = []
        pending = [t for t in self._tasks if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending, timeout=5)
        self.started = False

    def request_suspend(self, reason: str = "restart") -> None:
        """Signal-handler safe: schedule `suspend_all` on the running loop."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.call_soon_threadsafe(lambda: self._spawn(self.suspend_all(reason)))

    async def suspend_all(self, reason: str = "restart") -> int:
        """SIGTERM: stop every local run, flush, write specs, release leases,
        abort readers. Returns how many runs were released. Idempotent."""
        self.stopping = True
        runs = [run for run in self.runs.values() if not run.terminal]
        for run in runs:
            run.request_stop(reason)
        closes = [self._close_generation(run) for run in runs]
        if closes:
            await asyncio.gather(*closes, return_exceptions=True)
        # Let each runner record the attempt the stop just ended BEFORE the
        # leases go (2026-09-14, found by the foreground restart test, 1 run
        # in 8): `_record_attempt` writes `metadata.attempts` under the lease,
        # and a release that won the race turned it into a no-op — the resumed
        # run then settled with `resume_count: 0` and without the first
        # attempt's prompt count, under-reporting the re-prefill. Bounded: a
        # runner stuck on an unreachable database must not hold a SIGTERM.
        runners = [run.task for run in runs if run.task is not None and not run.task.done()]
        if runners:
            await asyncio.wait(runners, timeout=SUSPEND_RECORD_WAIT_S)
        released: Set[str] = set()
        try:
            await self.flush(runs)
            missing = [run for run in runs if not run.spec_written and run.lease_held]
            for run in missing:
                await self._write_spec(run)
            held = [run.id for run in runs if run.lease_held]
            released = await db.run_in_thread(durable_store.release, self.owner, held, reason)
        except Exception:  # noqa: BLE001 - a DB outage at shutdown: leases lapse in 60 s
            log.warning("durable suspend could not reach the store", exc_info=True)
        for run in runs:
            run.lease_held = False
            run.abort_readers()
            self.runs.pop(run.id, None)
        for run in runs:
            if run.task is not None and not run.task.done():
                run.task.cancel()
        # Queued rows launched here are resumed by whichever process claims
        # them next: this one keeps neither their hooks nor their slots.
        for rid in set(self._slots) | set(self._local_hooks) | set(self._launched_here) | {r.id for r in runs}:
            self.forget_local(rid)
        self._dispatch_state_changed()
        return len(released)

    async def _close_generation(self, run: Run) -> None:
        generation = run.generation
        if generation is not None:
            with contextlib.suppress(BaseException):
                await generation.aclose()

    def stop_run(self, run: Run, reason: str) -> None:
        """Ask a run to stop AND close its engine stream now. Waiting for the
        runner to notice between chunks would take up to a heartbeat (15 s)
        on a silent prefill — too slow for a chat yield (design: ≤1 s) and a
        revoked key alike. Closing the generation ends its iteration at once;
        the runner then handles `reason`."""
        run.request_stop(reason)
        if run.generation is not None:
            self._spawn(self._close_generation(run))

    # -- launch ---------------------------------------------------------

    async def launch(
        self,
        spec: streaming.GenerationSpec,
        *,
        caller: Caller,
        dialect: str = DIALECT_RESPONSES,
        background: bool = False,
        streamed: bool = False,
        keyed: bool = False,
        attempt_token: Optional[str] = None,
        body_sha256: Optional[str] = None,
        on_finish: Optional[streaming.OnFinish] = None,
        request_id: str = "",
        metadata: Optional[Mapping[str, Any]] = None,
        instructions_present: bool = False,
        fingerprint: str = "",
        extra: Optional[Mapping[str, Any]] = None,
        slot: Any = None,
        recorder_ref: Optional[RecorderRef] = None,
        retain_hook: bool = False,
    ) -> Handle:
        """Launch a durable run. Raises ApiError (disk guard) before any row.

        If a run with the same gateway `attempt_token` already exists in the
        project, the existing run is attached instead (creator rule applies).

        `slot` is the caller's project concurrency lease (`quotas.take_slot`);
        the runtime releases it when the run settles, is cancelled, or leaves
        this process — however that happens. The caller releases it only if
        this call raises. `recorder_ref` is the serialisable recorder, stored
        with the spec; with it, a background launch keeps no `on_finish`
        closure while queued — unless `retain_hook` asks to keep it within
        the retained-hook budget anyway (the router does for a request with
        files: its closure also removes the request's inline file renders,
        which a recorder rebuilt from JSON cannot), the ref then being the
        recorder of a run settled elsewhere."""
        if not self.started:
            await self.start()
            if not self.started:
                raise errors.model_unavailable()
        blobs.ensure_free_disk(self._blobs().root)
        if attempt_token:
            existing = await db.run_in_thread(durable_store.find_by_attempt, caller.project_id, attempt_token)
            if existing is not None:
                # No new run: the slot taken for one goes straight back.
                self._release_slot(slot)
                return await self._attach_attempt(existing, caller, body_sha256)
        row = await db.run_in_thread(db.get_api_response, spec.response_id, caller.project_id)
        if row is None:
            await db.run_in_thread(
                db.create_api_response,
                caller.project_id, caller.workspace_id, spec.model, request_id or spec.response_id,
                response_id=spec.response_id, key_id=(caller.key_id or None), status="queued",
                background=bool(background), streamed=bool(streamed), fingerprint=fingerprint,
                instructions_present=instructions_present, metadata=dict(metadata or {}),
                max_output_tokens=spec.planned,
            )
        run = Run(
            spec=spec, project_id=caller.project_id, workspace_id=caller.workspace_id,
            key_id=caller.key_id or None, dialect=dialect, background=background, keyed=keyed,
            streamed=streamed, extra=extra,
        )
        run.on_finish = on_finish
        run.recorder_ref = recorder_ref.to_json() if recorder_ref is not None else None
        owner = None if background else self.owner
        import psycopg

        try:
            marked = await db.run_in_thread(
                durable_store.mark_durable, spec.response_id, dialect=dialect, item_id=spec.item_id,
                engine=spec.engine, owner=owner, lease_ttl_s=lease_ttl_s(),
                attempt_token=attempt_token, body_sha256=body_sha256,
            )
        except psycopg.errors.UniqueViolation:
            # Two re-POSTs of one gateway attempt raced: the other one won.
            # The row this call just wrote is nobody's — remove it, attach.
            if row is None:
                await db.run_in_thread(durable_store.discard_unlaunched, spec.response_id)
            existing = await db.run_in_thread(durable_store.find_by_attempt, caller.project_id, attempt_token or "")
            if existing is None:
                raise
            self._release_slot(slot)
            return await self._attach_attempt(existing, caller, body_sha256)
        if marked is None:
            raise errors.internal_error()
        if background or keyed or attempt_token:
            await self._write_spec(run)
        if background:
            # Rows only: the dispatcher claims it when its gate has room.
            self._launched_here.add(run.id)
            if slot is not None:
                self._slots[run.id] = slot
            if on_finish is not None and (recorder_ref is None or retain_hook):
                self._retain_hook(run.id, on_finish, spec)
            if self.dispatch_wake is not None:
                self.dispatch_wake.set()
            return Handle(self, run.id, None)
        run.lease_held = True
        run.attempt = int(marked.get("attempt") or 1)
        self.runs[run.id] = run
        if slot is not None:
            self._slots[run.id] = slot
        run.add_record(run.builder.created())
        if spec.engine == registry.ENGINE_MAIN and streaming.engine_is_recovering():
            run.queued_announced = True
            run.add_record(run.builder.queued())
        run.task = self._spawn(self._drive(run))
        self._start_orphan_timer(run, attach_grace=LAUNCH_ATTACH_GRACE_S)
        return Handle(self, run.id, run)

    def _retain_hook(self, response_id: str, on_finish: streaming.OnFinish, spec: streaming.GenerationSpec) -> None:
        """Keep a queued background run's `on_finish` while the retained
        prompt bytes stay within PUBLIC_API_RETAINED_HOOK_BYTES; past it the
        closure is dropped (logged, counted) and the run records through its
        RecorderRef / the runtime recorder when it settles."""
        size = _prompt_bytes(spec.messages)
        if self._retained_hook_bytes + size > retained_hook_bytes():
            self.stats["hooks_not_retained"] += 1
            log.warning(
                "durable background run %s: on_finish not retained (%d prompt bytes over the %d byte budget); "
                "it records through the runtime recorder", response_id, size, retained_hook_bytes(),
            )
            return
        self._local_hooks[response_id] = on_finish
        self._hook_bytes[response_id] = size
        self._retained_hook_bytes += size

    @staticmethod
    def _release_slot(slot: Any) -> None:
        if slot is not None:
            with contextlib.suppress(Exception):
                slot.release()

    def _take_hook(self, response_id: str) -> Optional[streaming.OnFinish]:
        hook = self._local_hooks.pop(response_id, None)
        self._retained_hook_bytes = max(0, self._retained_hook_bytes - self._hook_bytes.pop(response_id, 0))
        return hook

    def forget_local(self, response_id: str) -> None:
        """Everything this process keeps for a run it launched or claimed:
        the queued hook, the launched-here mark, the dispatcher
        representative, and the project slot (released). Called on EVERY
        path by which a run stops being this process's business — settled,
        cancelled while queued, unrunnable, claimed elsewhere, suspended,
        lease lost — and idempotent (SlotLease.release is)."""
        self._launched_here.discard(response_id)
        self._take_hook(response_id)
        for key, rid in list(self._gate_waiters.items()):
            if rid == response_id:
                self._gate_waiters.pop(key, None)
        slot = self._slots.pop(response_id, None)
        if slot is not None:
            with contextlib.suppress(Exception):
                slot.release()

    def local_accounting(self) -> Dict[str, int]:
        """Counts for tests and a health payload."""
        return {
            "hooks": len(self._local_hooks), "hook_bytes": self._retained_hook_bytes,
            "slots": len(self._slots), "launched_here": len(self._launched_here),
        }

    async def attach_attempt(
        self, existing: Mapping[str, Any], caller: Caller, body_sha256: Optional[str]
    ) -> Handle:
        """The router's gateway re-attach (`_attach_attempt`, public)."""
        return await self._attach_attempt(existing, caller, body_sha256)

    async def _attach_attempt(
        self, existing: Mapping[str, Any], caller: Caller, body_sha256: Optional[str]
    ) -> Handle:
        """A gateway re-POST of one attempt attaches only when the attempt
        token, the key and the raw body's sha256 all match (design internal
        attach protocol); anything else is refused like another credential."""
        if (existing.get("body_sha256") or None) != (body_sha256 or None):
            raise AttachForbidden()
        return await self.attach_row(existing, caller)

    async def _write_spec(self, run: Run) -> None:
        body, stored = await db.run_in_thread(spec_to_json, run.spec, self._blobs())
        body = {"spec": body, "dialect": run.dialect, "background": run.background,
                "keyed": run.keyed, "streamed": run.streamed, "extra": run.extra,
                "workspace_id": run.workspace_id}
        if run.recorder_ref is not None:
            body["recorder"] = dict(run.recorder_ref)
        await db.run_in_thread(durable_store.put_spec, run.id, body, stored)
        run.spec_written = True

    # -- attach ---------------------------------------------------------

    @staticmethod
    def creator_allows(row: Mapping[str, Any], caller: Caller) -> bool:
        """The run's key, or a key of the same service account."""
        if str(row.get("project_id") or "") != caller.project_id:
            return False
        run_key = row.get("key_id")
        if run_key and caller.key_id and str(run_key) == caller.key_id:
            return True
        account = row.get("key_service_account_id")
        return bool(account) and bool(caller.service_account_id) and str(account) == str(caller.service_account_id)

    async def attach(
        self,
        response_id: str,
        *,
        caller: Caller,
    ) -> Handle:
        """Attach to a run the caller created. Raises AttachForbidden for
        another credential and for a missing run (identical to the caller),
        NotStreamable when it cannot be replayed."""
        row = await db.run_in_thread(durable_store.get_run, response_id)
        if row is None or str(row.get("project_id")) != caller.project_id:
            raise AttachForbidden()
        return await self.attach_row(row, caller)

    async def attach_implicit(
        self, *, caller: Caller, dialect: str, body_sha256: str, retry_count: int
    ) -> Optional[Handle]:
        """SDK retry (x-stainless-retry-count ≥ 1) of the same key, route and
        body, to an orphaned or suspended run created within the window."""
        if retry_count < 1 or not caller.key_id or not body_sha256:
            return None
        row = await db.run_in_thread(
            durable_store.find_implicit, caller.project_id, caller.key_id, dialect, body_sha256,
            window_s=implicit_attach_window_s(),
        )
        if row is None:
            return None
        local = self.runs.get(str(row["id"]))
        if (local is None or local.terminal) and not row.get("background"):
            # Not running here: resuming it needs its stored spec. A foreground
            # run whose process CRASHED (no SIGTERM, so no suspend wrote the
            # spec of an unkeyed run) cannot be resumed, and attaching would
            # only settle it failed and hand the retry that failure — the
            # retry launches fresh instead (2026-09-14).
            stored = await db.run_in_thread(durable_store.has_spec, [str(row["id"])])
            if str(row["id"]) not in stored:
                return None
        return await self.attach_row(row, caller)

    async def attach_row(self, row: Mapping[str, Any], caller: Caller) -> Handle:
        if not self.creator_allows(row, caller):
            raise AttachForbidden()
        response_id = str(row["id"])
        if not row.get("resumable"):
            raise NotStreamable()
        status = str(row.get("status") or "")
        local = self.runs.get(response_id)
        if local is not None and not local.terminal:
            return Handle(self, response_id, local)
        # A local run that has just settled is still in `runs` while its
        # recorder runs: it is answered from the store like any settled run
        # (retention applies), not handed out as live.
        if status in durable_store.TERMINAL_STATUSES or (local is not None and local.terminal):
            if not await db.run_in_thread(durable_store.events_retained, response_id):
                raise NotStreamable()
            return Handle(self, response_id, None)
        if (
            self.started and not self.stopping
            and not row.get("background") and not (row.get("lease_owner") and row.get("lease_live"))
        ):
            # Suspended, lapsed: resume lazily, here — only in a runtime whose
            # writer and lease loops run (a replay served before `start`, or
            # during shutdown, follows the log without claiming the run).
            run = await self.claim_and_resume(response_id)
            if run is not None:
                return Handle(self, response_id, run)
        return Handle(self, response_id, None)

    # -- claim and resume ------------------------------------------------

    async def claim_and_resume(self, response_id: str) -> Optional[Run]:
        """Claim a run and start its runner here. ONE claim per run at a time
        in this process (found 2026-09-14 while testing the review fixes): the
        store lets an owner re-take its own lease, so two coroutines of one
        process — the dispatcher and a GET attach, or the follower poller —
        claiming the same row concurrently each started a runner, and the run
        was generated twice. The run already running here is returned; a claim
        already in flight answers None (the caller follows remotely and is
        switched to the local run when it appears)."""
        local = self.runs.get(response_id)
        if local is not None and not local.terminal:
            return local
        if response_id in self._claiming:
            return None
        self._claiming.add(response_id)
        try:
            return await self._claim_and_resume(response_id)
        finally:
            self._claiming.discard(response_id)

    async def _claim_and_resume(self, response_id: str) -> Optional[Run]:
        if self.stopping:
            return None
        claim = await db.run_in_thread(
            durable_store.claim, response_id, self.owner, lease_ttl_s=lease_ttl_s()
        )
        if claim is None:
            # Terminal already, or another process holds a live lease: it is
            # not this process's run to keep a hook or a slot for.
            self.forget_local(response_id)
            return None
        row = claim.row
        stored = await db.run_in_thread(durable_store.get_spec, response_id)
        caller_keys = [str(row.get("key_id"))] if row.get("key_id") else []
        workspace_id = str(row.get("workspace_id") or "")
        if stored is None:
            await self._settle_unrunnable(row, claim, errors.model_unavailable(), reason="spec missing")
            return None
        try:
            spec = await db.run_in_thread(spec_from_json, stored["spec"], self._blobs())
        except Exception:  # noqa: BLE001 - a missing blob is not the same prompt
            log.warning("durable run %s could not rebuild its spec", response_id, exc_info=True)
            await self._settle_unrunnable(row, claim, errors.model_unavailable(), reason="spec unreadable")
            return None
        run = Run(
            spec=spec, project_id=str(row["project_id"]), workspace_id=workspace_id,
            key_id=row.get("key_id"), dialect=str(stored.get("dialect") or row.get("dialect") or DIALECT_RESPONSES),
            background=bool(row.get("background")), keyed=bool(stored.get("keyed")),
            streamed=bool(stored.get("streamed")), extra=stored.get("extra") or {},
        )
        run.spec_written = True
        run.lease_held = True
        run.on_finish = self._take_hook(response_id)
        run.recorder_ref = stored.get("recorder") if isinstance(stored.get("recorder"), dict) else None
        run.resumed = claim.last_sequence > 0
        run.assigned_seq = claim.last_sequence
        run.committed_seq = claim.last_sequence
        if claim.emitted_text:
            run.pieces = [claim.emitted_text]
        # The item events precede the first delta in the same pending batch,
        # so a log with a delta has them (an OCR re-read keeps them too: its
        # discard starts at the first delta).
        run.item_open = bool(claim.emitted_text) or claim.first_delta_sequence is not None
        run.generated_tokens = claim.generated_tokens
        run.attempt = int(row.get("attempt") or 1)
        run.stalled_attempts = int(row.get("stalled_attempts") or 0)
        run.engine_fault_attempts = int(row.get("engine_fault_attempts") or 0)
        run.yields = int(row.get("yields") or 0)
        run.last_incident_id = row.get("last_incident_id")
        run.ever_followed = bool(row.get("ever_followed"))
        run.status = str(row.get("status") or "queued")
        meta = row.get("metadata") or {}
        run.attempts = list(meta.get("attempts") or [])
        for entry in run.attempts:
            prompt = entry.get("prompt_tokens")
            if prompt is not None:
                before = int(entry.get("tokens_before") or 0)
                if run.input_tokens is None:
                    run.input_tokens = max(0, int(prompt) - before) if entry.get("continuing") else int(prompt)
                if entry.get("continuing"):
                    run.recomputed_prompt_tokens += int(prompt)
            if entry.get("dispatched") and prompt is None:
                run.all_usage_reported = False
        if caller_keys:
            allowed = await self._authorise(caller_keys, spec.model)
            if caller_keys[0] not in allowed:
                self.runs[run.id] = run
                await self._settle_revoked(run)
                return None
        if row.get("cancel_requested") or response_id in self._cancel_pending:
            # A cancel written while another process held the run, which
            # suspended before its lease tick read the flag — or one that
            # arrived while this very claim was in flight: honoured here,
            # before any engine work.
            self.runs[run.id] = run
            await self._settle(run, "cancelled")
            return None
        if spec.engine == registry.ENGINE_OCR and claim.emitted_text:
            # OCR is durable only while queued (design): a page it had started
            # reading is re-read from scratch when nobody has seen the partial
            # transcript, and fails retryably (partial kept) when somebody has
            # — an OCR continuation is not a faithful re-read. Decided BEFORE
            # the run is visible to a reader here, from the stored flag, so no
            # reader can be handed deltas that are then discarded.
            first_delta = int(claim.first_delta_sequence or claim.last_sequence)
            discarded = (not claim.row.get("ever_followed")) and await db.run_in_thread(
                durable_store.discard_output_after, self.owner, run.id, first_delta - 1
            )
            if not discarded:
                self.runs[run.id] = run
                await self._settle(run, "failed", error=errors.model_unavailable())
                return None
            run.pieces = []
            run.generated_tokens = 0
            run.assigned_seq = run.committed_seq = first_delta - 1
        if not resume_enabled() and (claim.last_sequence > 0 or run.attempt > 1):
            self.runs[run.id] = run
            await self._settle(run, "failed", error=errors.model_unavailable())
            return None
        self.runs[run.id] = run
        if claim.last_sequence == 0:
            run.add_record(run.builder.created())
        run.task = self._spawn(self._drive(run))
        if not run.background:
            self._start_orphan_timer(run, attach_grace=LAUNCH_ATTACH_GRACE_S)
        self.stats["claims"] += 1
        return run

    async def _settle_unrunnable(self, row: Mapping[str, Any], claim: durable_store.Claim, error: errors.ApiError, *, reason: str) -> None:
        log.warning("durable run %s cannot resume: %s", row.get("id"), reason)
        fields = {"status": "failed", "error_code": error.code, "error_message": errors.redact(error.message)}
        try:
            await db.run_in_thread(durable_store.finish, self.owner, str(row["id"]), [], fields)
        finally:
            self.forget_local(str(row["id"]))

    # -- the writer -----------------------------------------------------

    async def flush(self, also: Sequence[Run] = ()) -> bool:
        """One write-ahead flush of every local run's pending records.

        `also`: runs to flush even if they already left `runs` —
        `suspend_all` passes the runs it is suspending, whose runners drop
        them from `runs` as soon as their attempts end (2026-09-14: once the
        suspend waited for the runners to record their attempts, their last
        unflushed deltas were otherwise never written, and the resume
        re-generated text that had been produced)."""
        async with self._lock():
            batches: Dict[str, List[Record]] = {}
            runs: Dict[str, Run] = {}
            candidates = {id(run): run for run in list(self.runs.values()) + list(also)}
            for run in candidates.values():
                if run.terminal or not run.lease_held or not run._pending:
                    continue
                if not run.spec_written:
                    try:
                        await self._write_spec(run)
                    except Exception:  # noqa: BLE001 - retried next tick
                        log.debug("lazy spec write failed for %s", run.id, exc_info=True)
                        continue
                batches[run.id] = run.take_pending()
                runs[run.id] = run
            if not batches:
                return True
            try:
                result = await db.run_in_thread(durable_store.append, self.owner, batches)
            except Exception:  # noqa: BLE001 - a database outage: keep the buffer
                log.warning("durable event flush failed; keeping %d runs' events pending", len(batches), exc_info=True)
                for run in runs.values():
                    run.restore_inflight()
                self.store_ok = False
                self._enforce_pending_cap()
                return False
            for rid in result.committed:
                runs[rid].commit_inflight()
            for rid in result.lost:
                run = runs[rid]
                run.restore_inflight()
                self._lease_lost(run)
            self.store_ok = True
            self._dispatch_state_changed()
            return True

    def _enforce_pending_cap(self) -> None:
        """Pending bytes above the cap (a database outage): suspend the
        largest runs with reason 'store'. They keep their buffer and resume
        when a flush succeeds."""
        cap = pending_max_bytes()
        live = [run for run in self.runs.values() if not run.terminal]
        total = sum(run.pending_bytes for run in live)
        for run in sorted(live, key=lambda r: r.pending_bytes, reverse=True):
            if total <= cap:
                break
            if not run.store_paused:
                run.store_paused = True
                self.stop_run(run, "store")
                self.stats["store_suspends"] += 1
                total -= run.pending_bytes

    def _lease_lost(self, run: Run) -> None:
        if run.terminal:
            return
        log.warning("durable run %s lost its lease; stopping", run.id)
        run.lease_held = False
        self.stop_run(run, "lease_lost")
        run.abort_readers()
        self.stats["lease_lost"] += 1

    async def _writer_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(flush_s())
                await self.flush()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.warning("durable writer tick failed", exc_info=True)

    # -- leases and authorisation ----------------------------------------

    async def lease_tick(self) -> None:
        """ONE renew statement for every local lease, ONE authorisation batch
        for every local key, cancels noticed from the same statement."""
        held = [run for run in self.runs.values() if run.lease_held and not run.terminal]
        if not held:
            return
        renewed = await db.run_in_thread(
            durable_store.renew_leases, self.owner, [r.id for r in held], lease_ttl_s=lease_ttl_s(),
            generated_tokens={r.id: r.generated_tokens for r in held},
        )
        for run in held:
            info = renewed.get(run.id)
            if info is None:
                self._lease_lost(run)
                continue
            if info.get("cancel_requested"):
                self.stop_run(run, "cancel")
        by_model: Dict[str, List[Run]] = collections.defaultdict(list)
        for run in held:
            if run.key_id and run.id in renewed:
                by_model[str(run.spec.model)].append(run)
        # One authorisation batch per MODEL among local runs (at most the six
        # public models): the re-check includes allowed_models (review P8).
        for model, runs in by_model.items():
            keys = sorted({str(run.key_id) for run in runs})
            allowed = await self._authorise(keys, model)
            for run in runs:
                if str(run.key_id) not in allowed:
                    self.stop_run(run, "revoked")

    async def _authorise(self, key_ids: Sequence[str], model: Optional[str]) -> Set[str]:
        fn = self.authoriser
        if _accepts_model(fn):
            return set(await fn(list(key_ids), model))
        return set(await fn(list(key_ids)))

    async def _lease_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(lease_heartbeat_s())
                await self.lease_tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.warning("durable lease tick failed", exc_info=True)

    # -- the runner -----------------------------------------------------

    def _gates(self, spec: streaming.GenerationSpec) -> List[str]:
        return capacity.gates_for(spec.engine, spec.gate_engine)

    async def _drive(self, run: Run) -> None:
        """The runner. Never raises; every exit settles, suspends or stops."""
        try:
            await self._drive_inner(run)
        except asyncio.CancelledError:
            if not run.terminal and run.stop_reason is None:
                run.request_stop("shutdown")
        except Exception as exc:  # noqa: BLE001 - a runner never dies unrecorded
            log.warning("durable runner %s failed", run.id, exc_info=True)
            if not run.terminal and run.lease_held:
                with contextlib.suppress(BaseException):
                    await self._settle(run, "failed", error=errors.from_unexpected(exc))
        finally:
            self._resumed_awaiting_first_token.discard(run.id)
            if self._quarantined_dispatched == run.id:
                self._quarantined_dispatched = None
                self._quarantine_prefill = False
            self._dispatch_state_changed()
            if not run.terminal and run.stop_reason in ("lease_lost", "restart", "shutdown"):
                self._drop_run(run)
            if run.id not in self.runs:
                self.forget_local(run.id)

    async def _drive_inner(self, run: Run) -> None:
        view = self._view()
        grace = liveness.EngineDownGrace(view, clock=self.clock)
        in_progress_sent = run.committed_seq > 0 and run.resumed
        while True:
            reason = run.stop_reason
            if reason is not None:
                if await self._handle_stop(run, reason):
                    return
                continue
            # The quarantine turn BEFORE the gates: a run waiting (possibly
            # for minutes) to be allowed alone must not sit on a main.normal
            # slot that fresh public work could use meanwhile.
            # MAIN MODEL ONLY: the quarantine reads the controller (serving
            # time, recovery budget), which router/OCR do not have. A sidecar
            # implicated once is re-dispatched at once; a second coinciding
            # restart fails it with should_retry false (review 2026-09-14).
            if run.spec.engine == registry.ENGINE_MAIN and (
                run.engine_fault_attempts >= 1
                or (run.resumed and self._quarantined_dispatched and self._quarantine_prefill)
            ):
                self._leave_gate_group(run)
                if not await self._wait_dispatch_turn(run, grace):
                    continue
            try:
                async with contextlib.AsyncExitStack() as stack:
                    if not await self._take_gates(run, stack):
                        continue
                    run.mark_admitted()
                    if not in_progress_sent:
                        run.add_record(run.builder.in_progress())
                        in_progress_sent = True
                        run.status = "in_progress"
                        with contextlib.suppress(Exception):
                            await db.run_in_thread(
                                durable_store.update_progress, self.owner, run.id, status="in_progress"
                            )
                    messages, max_tokens, continuing = self._attempt_shape(run)
                    if max_tokens is not None and max_tokens < 1:
                        await self._settle_length(run)
                        return
                    if continuing and not self._room_for_continuation(run):
                        await self._settle_length(run)
                        return
                    outcome = await self._attempt(run, messages, max_tokens, continuing, grace)
            finally:
                if self._quarantined_dispatched == run.id:
                    self._quarantined_dispatched = None
                    self._quarantine_prefill = False
                    self._dispatch_state_changed()
            if outcome == "wait":
                outcome = await self._after_interrupt(run, grace)
            if outcome == "done":
                return

    def _attempt_shape(self, run: Run) -> Tuple[List[Dict[str, Any]], Optional[int], bool]:
        spec = run.spec
        emitted = run.text
        if not emitted:
            return list(spec.messages), None, False
        messages = list(spec.messages) + [{"role": "assistant", "content": emitted}]
        return messages, int(spec.max_tokens) - int(run.generated_tokens), True

    def _room_for_continuation(self, run: Run) -> bool:
        spec = run.spec
        window = int(spec.context_window or 0)
        if window <= 0:
            return True
        counted = int(spec.estimated_input_tokens or 0) + int(run.generated_tokens)
        room = window - counted - int(spec.context_reserve or 0)
        return room >= MIN_OUTPUT_TOKENS + CONTEXT_SAFETY_MARGIN

    async def _take_gates(self, run: Run, stack: contextlib.AsyncExitStack) -> bool:
        front = run.yields > 0

        def queued(position: int, waited: float) -> None:
            # CONTRACT §10: say why nothing is arriving. Once, and only before
            # `response.in_progress` (the grammar only moves forward).
            if run.status == "queued" and not run.queued_announced:
                run.queued_announced = True
                run.add_record(run.builder.queued())

        try:
            for gate in self._gates(run.spec):
                try:
                    await stack.enter_async_context(
                        capacity.hold(
                            gate, weight_tokens=run.spec.gate_weight_tokens, wait_s=None,
                            yield_to_chat=run.spec.yield_to_chat, abandon=run.stop_event, front=front,
                            on_wait=queued,
                            # A main gate admits the answer into admission's
                            # LONG_OUTPUT lane (patiently: no wait_s) and hands
                            # the ticket to the attempt below — one accounting
                            # of long public work (capacity.py, PR #65).
                            work=run.spec,
                        )
                    )
                except capacity.Abandoned:
                    return False
            return True
        finally:
            # Admitted (or given up): the group's next background row may take
            # this run's place in the line.
            self._leave_gate_group(run)

    def _leave_gate_group(self, run: Run) -> None:
        key = run.dispatch_key
        if key is None:
            return
        run.dispatch_key = None
        if self._gate_waiters.get(key) == run.id:
            self._gate_waiters.pop(key, None)
            if self.dispatch_wake is not None:
                self.dispatch_wake.set()

    async def _wait_dispatch_turn(self, run: Run, grace: liveness.EngineDownGrace) -> bool:
        """Quarantine (design liveness_guard COUNTERS): a run implicated once
        dispatches only after 300 s of proven serving, with ≥2 recoveries
        left in the controller's budget, while no other quarantined run is in
        an attempt and no other resumed run is still waiting for its first
        token. A resumed run waits while a quarantined run is prefilling.
        Waits, never fails: the client keeps receiving heartbeats."""
        view = self._view()
        if self.serving is None:
            self.serving = liveness.ServingTracker(view, clock=self.clock)
        quarantined = run.engine_fault_attempts >= 1
        while True:
            if run.stop_reason is not None:
                return False
            changed = self._changed()
            if quarantined:
                others = self._resumed_awaiting_first_token - {run.id}
                if (
                    self._quarantined_dispatched in (None, run.id)
                    and not others
                    and self.serving.quarantine_ready(self.clock())
                ):
                    self._quarantined_dispatched = run.id
                    self._quarantine_prefill = True
                    return True
            elif not (self._quarantined_dispatched not in (None, run.id) and self._quarantine_prefill):
                return True
            waiters = [asyncio.ensure_future(changed.wait()), asyncio.ensure_future(run.stop_event.wait())]
            try:
                await asyncio.wait(waiters, timeout=1.0, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for waiter in waiters:
                    waiter.cancel()

    async def _attempt(
        self,
        run: Run,
        messages: List[Dict[str, Any]],
        max_tokens: Optional[int],
        continuing: bool,
        grace: liveness.EngineDownGrace,
    ) -> str:
        """One dispatch. Returns 'done' (settled or stopped for good) or
        'retry' (loop again: interrupted, yielded, store-paused)."""
        spec = run.spec
        view = self._view()
        witness_key: Optional[str] = None
        if spec.engine == registry.ENGINE_MAIN:
            guard: Any = liveness.MainGuard(view, clock=self.clock)
        else:
            # Router/OCR: the engine's own /metrics counters are the witness
            # (one reference-counted scrape loop per engine, 15 s).
            witness = None
            with contextlib.suppress(Exception):
                from . import engines

                resolved = engines.target(spec.engine)
                if resolved is not None and resolved.base_url and self.witnesses_enabled:
                    witness = liveness.sampler().acquire(spec.engine, engines.metrics_root(resolved))
                    witness_key = spec.engine
            guard = liveness.SidecarGuard(witness, clock=self.clock)
        if run.resumed or run.attempt > 1:
            self._resumed_awaiting_first_token.add(run.id)
        tokens_before = run.generated_tokens
        quarantined_attempt = spec.engine == registry.ENGINE_MAIN and run.engine_fault_attempts >= 1
        run.emitted_first_token_this_attempt = False
        attempt_first_token_at: Optional[float] = None
        run._last_continuing = continuing
        run._last_tokens_before = tokens_before
        run._last_prompt = None
        self._register_yield(run)
        generation = self.factory(
            spec,
            guard=guard,
            messages=messages,
            max_tokens=max_tokens,
            continue_final_message=continuing,
            admission_patient=True,
            admission_run_id=run.id,
            heartbeat_s=heartbeat_s(),
        )
        run.generation = generation
        first = True
        try:
            async for chunk in generation.stream():
                if run.stop_reason is not None:
                    break
                if getattr(chunk, "kind", "") != streaming.TOKEN_KIND:
                    continue
                text = str(getattr(chunk, "text", "") or "")
                if not text:
                    continue
                if first:
                    first = False
                    grace.connected()
                    run.connect_failures = 0
                    run.sidecar_down_since = None
                    run.emitted_first_token_this_attempt = True
                    run.yields_without_progress = 0
                    attempt_first_token_at = self.clock()
                    if run.first_token_at is None:
                        run.first_token_at = time.monotonic()
                    self._resumed_awaiting_first_token.discard(run.id)
                    if self._quarantined_dispatched == run.id:
                        self._quarantine_prefill = False
                    self._dispatch_state_changed()
                run.add_delta(text, 1)
                if quarantined_attempt and run.engine_fault_attempts >= 1 and attempt_first_token_at is not None:
                    self._maybe_clear_quarantine(run, run.generated_tokens - tokens_before,
                                                 self.clock() - attempt_first_token_at)
        finally:
            with contextlib.suppress(BaseException):
                await generation.aclose()
            if witness_key is not None:
                with contextlib.suppress(Exception):
                    liveness.sampler().release(witness_key)
            run.generation = None
            self._unregister_yield(run)
            self._resumed_awaiting_first_token.discard(run.id)
            if self._quarantined_dispatched == run.id:
                self._quarantined_dispatched = None
                self._quarantine_prefill = False
            self._dispatch_state_changed()
        new_tokens = run.generated_tokens - tokens_before
        self._note_usage(run, generation, new_tokens, continuing=continuing, tokens_before=tokens_before)
        verdict: Optional[liveness.Verdict] = getattr(generation, "interrupt", None)
        dispatched = getattr(guard, "dispatched_at", None) is not None
        error = getattr(generation, "error", None)
        finish_reason = getattr(generation, "finish_reason", None)

        if (
            run.stop_reason in ("yield", "store", "orphaned")
            and error is None
            and verdict is None
            and finish_reason in ("stop", "length")
        ):
            # The engine had already finished when the stop arrived: settle
            # the answer rather than "continue" a complete one.
            if run.stop_reason != "orphaned":
                run.clear_stop()
            run.stop_reason = None
            await self._record_attempt(run, "completed", dispatched, new_tokens, guard, False)
            await self._settle(run, "completed", finish_reason=finish_reason, generation=generation)
            return "done"
        if run.stop_reason is not None:
            end_reason = run.stop_reason
            await self._record_attempt(run, end_reason, dispatched, new_tokens, guard, False)
            return "retry"
        if error is not None and verdict is None:
            name = type(error).__name__
            if name == "ContinuationRoomExhausted":
                await self._settle_length(run)
                return "done"
            if name == "ContinuationUnsupported":
                await self._settle(run, "failed", error=errors.model_unavailable())
                return "done"
            if _is_connection_error(error, spec.engine):
                verdict = guard.classify_error(self.clock(), error)
                if verdict.reason == liveness.REASON_CONNECT:
                    # A sidecar that refused the connection (or answered
                    # 502/503/504) never had the request: not dispatched,
                    # whatever on_dispatch said.
                    dispatched = False
                elif verdict.reason == liveness.REASON_ENGINE_ERROR:
                    # The engine ANSWERED with a 5xx: the request reached it.
                    dispatched = True
                if not dispatched and not new_tokens:
                    grace.connect_failed()
                    run.connect_failures += 1
            else:
                await self._record_attempt(run, "error", dispatched, new_tokens, guard, False)
                await self._settle(run, "failed", error=streaming.engine_error(error, spec.engine))
                return "done"
        if verdict is None and finish_reason == _wall_clock_finish():
            # SHIM(T1): an llm without wall_clock_s=0 still cuts at its own
            # GEN_WALL_CLOCK_S. On a durable run that is a resumable
            # interruption, never a failure and never counted.
            verdict = liveness.Verdict(True, "liveness", False, "chat wall clock")
        if verdict is None:
            await self._record_attempt(run, "completed", dispatched, new_tokens, guard, False)
            await self._settle(run, "completed", finish_reason=finish_reason, generation=generation)
            return "done"

        # -- an interrupt: not a failure unless a counter says so --
        implicated = bool(verdict.implicated)
        change = liveness.attempt_counts(liveness.AttemptEnd(
            reason=verdict.reason, dispatched=dispatched, new_tokens=new_tokens,
            evidence_covered=bool(getattr(guard, "evidence_covered", True)), implicated=implicated,
        ))
        if change.stalled is None:
            run.stalled_attempts = 0
        else:
            run.stalled_attempts += change.stalled
        run.engine_fault_attempts += change.engine_fault
        if implicated:
            run.last_incident_id = view.incident_id(self.clock()) or run.last_incident_id
        await self._record_attempt(run, verdict.reason, dispatched, new_tokens, guard, implicated)
        self.stats[f"interrupt_{verdict.reason}"] += 1
        if run.engine_fault_attempts >= 2:
            await self._settle(run, "failed", error=errors.model_unavailable(), should_retry=False)
            return "done"
        stalled_limit = (
            liveness.max_stalled_attempts() if spec.engine == registry.ENGINE_MAIN else sidecar_max_stalled_attempts()
        )
        if run.stalled_attempts >= stalled_limit:
            await self._settle(run, "failed", error=errors.model_unavailable())
            return "done"
        if spec.engine == registry.ENGINE_OCR and run.text:
            if run.ever_followed:
                await self._settle(run, "failed", error=errors.model_unavailable())
                return "done"
            await self._discard_ocr_output(run)
        return "wait"

    def _maybe_clear_quarantine(self, run: Run, decoded_tokens: int, decoding_s: float) -> None:
        """A quarantined attempt that has decoded `quarantine_clear_tokens()`
        tokens, or kept decoding for `quarantine_clear_s()` after its first
        token, has shown it is not the poison: engine_fault_attempts goes
        back to 0 (2026-09-14 review P4). Without this the counter never
        decayed, so a multi-hour generation that happened to span two
        UNRELATED engine wedges — each implicates every silent in-flight run
        — was failed with should_retry false and its partial work lost. A
        poison prompt still fails: it crashes the engine before decoding
        that far, so its second implication lands while still quarantined.
        The run also leaves the one "quarantined run in an attempt" slot so
        another quarantined run may dispatch."""
        if decoded_tokens < quarantine_clear_tokens() and decoding_s < quarantine_clear_s():
            return
        run.engine_fault_attempts = 0
        run.quarantine_cleared += 1
        self.stats["quarantine_cleared"] += 1
        if self._quarantined_dispatched == run.id:
            self._quarantined_dispatched = None
            self._quarantine_prefill = False
            self._dispatch_state_changed()
        if run.lease_held:
            self._spawn(db.run_in_thread(
                durable_store.update_progress, self.owner, run.id, engine_fault_attempts=0,
                metadata={"quarantine_cleared": run.quarantine_cleared},
            ))

    async def _after_interrupt(self, run: Run, grace: liveness.EngineDownGrace) -> str:
        """Between attempts, with every gate given back: wait until the
        engine is no longer proven bad (the engine-down grace is the only
        thing that can end this wait), back off 2 → 60 s after connect
        failures, then resume by continuation."""
        if run.connect_failures:
            delay = min(CONNECT_BACKOFF_MAX_S, CONNECT_BACKOFF_S * (2 ** (run.connect_failures - 1)))
            if run.spec.engine != registry.ENGINE_MAIN:
                # A sidecar has no controller: its grace is continuous connect
                # failure (restarted or absent witness) for the grace period.
                now = self.clock()
                if run.sidecar_down_since is None:
                    run.sidecar_down_since = now
                elif now - run.sidecar_down_since >= liveness.engine_down_grace_s():
                    await self._settle(run, "failed", error=errors.model_unavailable())
                    return "done"
            with contextlib.suppress(asyncio.TimeoutError):
                async with asyncio.timeout(delay):
                    await run.stop_event.wait()
            if run.stop_reason is not None:
                return "retry"
        if run.spec.engine == registry.ENGINE_MAIN:
            try:
                ok = await liveness.wait_not_bad(self._view(), abandon=run.stop_event, grace=grace, clock=self.clock)
            except liveness.EngineDownExpired:
                await self._settle(run, "failed", error=errors.model_unavailable())
                return "done"
            if not ok:
                return "retry"
        if not resume_enabled():
            await self._settle(run, "failed", error=errors.model_unavailable())
            return "done"
        run.resumed = True
        return "retry"

    async def _discard_ocr_output(self, run: Run) -> None:
        delta_start = next(
            (r[0] for r in list(run._tail) if r[1] == events.RESPONSE_OUTPUT_TEXT_DELTA), None
        )
        await self.flush()
        after = (delta_start - 1) if delta_start else run.committed_seq
        ok = await db.run_in_thread(durable_store.discard_output_after, self.owner, run.id, after)
        if ok:
            run.pieces = []
            run.generated_tokens = 0
            run.assigned_seq = after
            run.committed_seq = after
            run._tail = collections.deque([r for r in run._tail if r[0] <= after], maxlen=TAIL_RECORDS)

    def _note_usage(
        self, run: Run, generation: Any, new_tokens: int, *, continuing: bool = False, tokens_before: int = 0
    ) -> None:
        """Input is counted ONCE (design TERMINAL): the first reported prompt,
        or — when the first attempt ended before vLLM's usage chunk — a
        continuation's reported prompt less the output it re-prefilled. Every
        continuation prompt is also added to `recomputed_prompt_tokens`, the
        re-prefill cost the deploy report shows."""
        usage = getattr(generation, "usage", None) or None
        prompt = None if not usage or usage.get("source") else usage.get("prompt_tokens")
        run._last_prompt = None if prompt is None else int(prompt)
        if prompt is None:
            if new_tokens or getattr(generation, "interrupt", None) is not None:
                run.all_usage_reported = False
            return
        if run.input_tokens is None:
            run.input_tokens = max(0, int(prompt) - int(tokens_before)) if continuing else int(prompt)
        if continuing:
            run.recomputed_prompt_tokens += int(prompt)

    async def _record_attempt(
        self, run: Run, reason: str, dispatched: bool, new_tokens: int, guard: Any, implicated: bool
    ) -> None:
        entry = {
            "n": run.attempt, "reason": reason, "dispatched": bool(dispatched),
            "new_tokens": int(new_tokens), "implicated": bool(implicated),
            "prompt_tokens": getattr(run, "_last_prompt", None),
            "continuing": bool(getattr(run, "_last_continuing", False)),
            "tokens_before": int(getattr(run, "_last_tokens_before", 0)),
            "owner": self.owner,
        }
        run.attempts = (run.attempts + [entry])[-MAX_ATTEMPT_ENTRIES:]
        run.attempt += 1
        if not run.lease_held:
            return
        with contextlib.suppress(Exception):
            await db.run_in_thread(
                durable_store.update_progress, self.owner, run.id,
                stalled_attempts=run.stalled_attempts, engine_fault_attempts=run.engine_fault_attempts,
                yields=run.yields, generated_tokens=run.generated_tokens,
                recomputed_prompt_tokens=run.recomputed_prompt_tokens or None,
                last_incident_id=run.last_incident_id,
                metadata={"attempts": run.attempts, "resume_count": max(0, len(run.attempts) - 1)},
            )

    async def _handle_stop(self, run: Run, reason: str) -> bool:
        """True when the runner must exit; False to loop (yield, store)."""
        if reason == "cancel":
            await self._settle(run, "cancelled")
            return True
        if reason == "revoked":
            await self._settle_revoked(run)
            return True
        if reason == "orphaned":
            await self._settle(run, "cancelled")
            return True
        if reason == "yield":
            run.yields += 1
            run.yields_without_progress += 1
            self.stats["yields"] += 1
            with contextlib.suppress(Exception):
                from .. import metrics

                metrics.inc("public_api_yields_total", "public /v1 runs suspended so a chat LONG turn could run")
            run.clear_stop()
            await self._wait_yield_condition(run)
            run.resumed = True
            return False
        if reason == "store":
            run.clear_stop()
            while not self.store_ok or run.pending_bytes > pending_max_bytes() // 2:
                if run.stop_reason is not None:
                    return False
                await asyncio.sleep(max(flush_s(), 0.05))
                await self.flush()
            run.store_paused = False
            run.resumed = True
            return False
        # restart / shutdown / lease_lost / liveness suspend: suspend_all or
        # the lease path already released; this runner simply ends.
        return True

    async def _wait_yield_condition(self, run: Run) -> None:
        """Re-queue after a yield: no chat LONG request on the lane and the
        KV ledger fits, polled at the video pipeline's pace."""
        while run.stop_reason is None:
            if not capacity.chat_long_admission_present() and _ledger_fits(run):
                return
            with contextlib.suppress(asyncio.TimeoutError):
                async with asyncio.timeout(capacity.YIELD_STEP_S):
                    await run.stop_event.wait()

    def _register_yield(self, run: Run) -> None:
        try:
            from .. import admission

            admission.register_yield(run.id, lambda: self._yield(run))
        except Exception:  # noqa: BLE001
            log.warning("could not register the yield callback of %s", run.id, exc_info=True)

    def _unregister_yield(self, run: Run) -> None:
        try:
            from .. import admission

            admission.unregister_yield(run.id)
        except Exception:  # noqa: BLE001
            log.debug("could not unregister the yield callback of %s", run.id, exc_info=True)

    def request_yield(self, response_id: str) -> bool:
        run = self.runs.get(response_id)
        if run is None or run.terminal:
            return False
        return self._yield(run)

    def _yield(self, run: Run) -> bool:
        """Yield to a chat LONG turn — unless this run has already yielded
        PUBLIC_API_MAX_YIELDS_WITHOUT_PROGRESS times without a first token
        in between (2026-09-14 review P9). Each yield throws its re-prefill
        away (up to ~800 s at a full window), so a run pre-empted by every
        chat turn made no progress at all, for ever. Past the bound it keeps
        its place until its next first token, then may yield again. Returns
        whether the yield was taken (the admission callback reads it)."""
        if run.terminal:
            return False
        if run.yields_without_progress >= max_yields_without_progress() and not run.emitted_first_token_this_attempt:
            self.stats["yields_refused"] += 1
            return False
        self.stop_run(run, "yield")
        return True

    # -- settlement -------------------------------------------------------

    def _usage(self, run: Run, generation: Any = None) -> Optional[Dict[str, Any]]:
        attempts_dispatched = [a for a in run.attempts if a.get("dispatched")]
        single = len(attempts_dispatched) <= 1 and not run.resumed
        engine_usage = getattr(generation, "usage", None) if generation is not None else None
        if single and engine_usage:
            return dict(engine_usage)
        prompt = run.input_tokens
        if prompt is None and run.spec.estimated_input_tokens is not None:
            prompt = int(run.spec.estimated_input_tokens)
        if prompt is None and not run.generated_tokens:
            return None
        return {
            "prompt_tokens": int(prompt or 0),
            "completion_tokens": int(run.generated_tokens),
            "calls": max(1, len(attempts_dispatched)),
            "source": "engine" if run.all_usage_reported and run.input_tokens is not None else streaming.USAGE_COUNTED_AT_STOP,
        }

    async def _settle_length(self, run: Run) -> None:
        """The resume near the window end: an output-limit stop (V35
        finish_reason length → completed with incomplete_details)."""
        await self._settle(run, "completed", finish_reason="length")

    async def _settle_revoked(self, run: Run) -> None:
        try:
            from ..apiplatform import resolver

            failure = resolver._refuse()
        except Exception:  # noqa: BLE001
            failure = errors.invalid_api_key()
        await self._settle(run, "failed", error=failure, should_retry=False)

    async def _settle(
        self,
        run: Run,
        status: str,
        *,
        error: Optional[errors.ApiError] = None,
        finish_reason: Optional[str] = None,
        generation: Any = None,
        should_retry: Optional[bool] = None,
    ) -> Optional[Dict[str, Any]]:
        """Settle ONCE: terminal records and row in one transaction, then the
        recorder and (background) the webhook."""
        if run.terminal:
            return run.terminal_row
        text = run.text
        usage = self._usage(run, generation)
        applied = None
        if generation is not None:
            with contextlib.suppress(Exception):
                applied = int(generation.applied_max_output_tokens())
        max_output = applied if applied else run.spec.planned
        records: List[List[Any]] = []
        if status == "completed":
            if not run.item_open:
                run.item_open = True
                records.append(run.builder.item_added())
                records.append(run.builder.part_added())
            annotations = file_annotations(run.extra, text)
            records.append(run.builder.text_done(text))
            for index, annotation in enumerate(annotations):
                records.append(run.builder.annotation_added(index, annotation))
            records.append(run.builder.part_done(text, annotations))
            records.append(run.builder.item_done(text, annotations))
            records.append(run.builder.completed(
                text=text, usage=usage, max_output_tokens=max_output, finish_reason=finish_reason,
                annotations=annotations,
            ))
        elif status == "failed":
            failure = error or errors.model_unavailable()
            if should_retry is not None:
                setattr(failure, "should_retry", bool(should_retry))
            records.append(run.builder.failed(
                text=text, usage=usage, error=failure, max_output_tokens=max_output, should_retry=should_retry,
            ))
        counted = streaming.models.Usage.from_llm(usage)
        fields: Dict[str, Any] = {
            "status": status,
            "duration_ms": int((time.monotonic() - run.started_monotonic) * 1000),
            "generated_tokens": int(run.generated_tokens),
            "stalled_attempts": run.stalled_attempts,
            "engine_fault_attempts": run.engine_fault_attempts,
            "yields": run.yields,
            "max_output_tokens": int(max_output) if max_output and int(max_output) >= 1 else None,
        }
        if fields["max_output_tokens"] is None:
            fields.pop("max_output_tokens")
        if run.recomputed_prompt_tokens:
            fields["recomputed_prompt_tokens"] = int(run.recomputed_prompt_tokens)
        if run.last_incident_id:
            fields["last_incident_id"] = run.last_incident_id
        if counted is not None:
            fields["input_tokens"] = counted.input_tokens
            fields["output_tokens"] = counted.output_tokens
        if finish_reason in ("stop", "length"):
            fields["finish_reason"] = finish_reason
        if status == "failed":
            failure = error or errors.model_unavailable()
            fields["error_code"] = failure.code
            fields["error_message"] = errors.redact(failure.message)
        if run.background and text:
            fields["output_text"] = text
        metadata = {
            "attempts": run.attempts,
            "resume_count": max(0, len([a for a in run.attempts if a.get("dispatched")]) - 1),
            "yields": run.yields,
        }
        if run.recomputed_prompt_tokens:
            metadata["recomputed_prompt_tokens"] = run.recomputed_prompt_tokens
        if should_retry is not None:
            metadata["should_retry"] = bool(should_retry)
        row: Optional[Dict[str, Any]] = None
        delay = 0.2
        while True:
            async with self._lock():
                pending = run.take_pending()
                final = [(int(r[0]), str(r[1]), r[2]) for r in records]
                try:
                    row = await db.run_in_thread(
                        durable_store.finish, self.owner if run.lease_held else None, run.id,
                        pending + final, fields, metadata=metadata,
                    )
                except Exception:  # noqa: BLE001 - the database is out: retry
                    run.restore_inflight()
                    log.warning("durable settle of %s failed; retrying", run.id, exc_info=True)
                    row = False  # type: ignore[assignment]
                else:
                    if row is not None:
                        run._inflight = [list(r) for r in pending] + records
                        run.commit_inflight()
            if row is False:
                if not run.lease_held and run.stop_reason == "lease_lost":
                    return None
                await asyncio.sleep(delay)
                delay = min(5.0, delay * 2)
                continue
            break
        run.terminal = True
        run.status = status
        run.lease_held = False
        run.terminal_row = row
        outcome = streaming.StreamOutcome(
            response_id=run.id, model=run.spec.model, created_at=run.spec.created_at, status=status,
            text=text, usage=usage, error=error if status == "failed" else None,
            ttft_ms=None if run.first_token_at is None else int((run.first_token_at - run.started_monotonic) * 1000),
            duration_ms=fields["duration_ms"], finish_reason=finish_reason,
            max_output_tokens=fields.get("max_output_tokens"),
        )
        run.outcome = outcome
        run.wake()
        run.done.set()
        run.mark_admitted()
        self._dispatch_state_changed()
        if row is None:
            # Somebody else settled (or owns) it: nothing to record here.
            self._drop_run(run)
            return None
        try:
            await self._record(run, row, outcome)
        finally:
            self._drop_run(run)
        return row

    def _drop_run(self, run: Run) -> None:
        """Remove THIS run object (never a newer run of the same id that
        this process claimed since) and forget its local accounting."""
        if self.runs.get(run.id) is run:
            self.runs.pop(run.id, None)
        run.mark_admitted()
        if run.id not in self.runs:
            self.forget_local(run.id)

    async def _record(self, run: Run, row: Dict[str, Any], outcome: streaming.StreamOutcome) -> None:
        hook = run.on_finish
        run.on_finish = None  # the closure (and the prompt it holds) goes now
        try:
            if hook is not None:
                await hook(outcome)
            elif run.recorder_ref and run.recorder_ref.get("name") in _RECORDER_FACTORIES:
                recorder = _RECORDER_FACTORIES[str(run.recorder_ref["name"])](dict(run.recorder_ref.get("args") or {}))
                await recorder(row, outcome)
            elif self.recorder is not None:
                await self.recorder(row, outcome)
        except Exception:  # noqa: BLE001 - recording never changes the outcome
            log.warning("durable recorder raised for %s", run.id, exc_info=True)
        if run.background:
            with contextlib.suppress(Exception):
                from . import background

                await background._notify(row, run.workspace_id)

    # -- followers --------------------------------------------------------

    def _add_reader(self, response_id: str, run: Run, kind: str) -> _Reader:
        """Register a reader of a LOCAL run: the follower cap (the oldest is
        closed), the orphan timer cancelled, `ever_followed` recorded."""
        reader = _Reader(response_id, kind)
        followers = [r for r in run.readers if r.kind == "follow" and not r.evicted]
        if kind == "follow" and len(followers) >= max_followers():
            oldest = min(followers, key=lambda r: r.started)
            oldest.evicted = True
            run.wake()
        run.readers.append(reader)
        if run.orphan_task is not None:
            run.orphan_task.cancel()
            run.orphan_task = None
        if run.orphan_marked:
            run.orphan_marked = False
            self._spawn(self._write_orphan_state(run, orphaned=False))
        if not run.ever_followed:
            run.ever_followed = True
            self._spawn(db.run_in_thread(durable_store.mark_followed, run.id))
        return reader

    def _remove_reader(self, run: Run, reader: _Reader) -> None:
        with contextlib.suppress(ValueError):
            run.readers.remove(reader)
        if run.readers or run.terminal or run.background or run.aborted:
            return
        self._start_orphan_timer(run, attach_grace=0.0)

    @staticmethod
    def _orphan_grace(run: Run) -> float:
        if run.keyed or (run.dialect == DIALECT_RESPONSES and run.streamed):
            return stream_orphan_grace_s()
        return unkeyed_orphan_grace_s()

    def _start_orphan_timer(self, run: Run, *, attach_grace: float) -> None:
        """Start the orphan clock of a run with no reader. From `launch` and
        `claim_and_resume` too (2026-09-14 review P2), not only when the last
        reader leaves: a client that disconnected before its body started
        never adds a reader, so the old timer never started, the run
        generated up to its planned output for nobody, and — orphaned_at
        never written — the SDK's retry could not attach and launched a
        second generation. `attach_grace` lets the route's body attach
        before anything is written."""
        if run.background or run.terminal or run.readers:
            return
        if run.orphan_task is not None and not run.orphan_task.done():
            return
        run.orphan_task = self._spawn(self._orphan_timer(run, self._orphan_grace(run), attach_grace=attach_grace))

    async def _write_orphan_state(self, run: Run, *, orphaned: bool) -> None:
        """The orphaned_at mark and clear, strictly in the order asked: a
        clear waits for a mark still in flight (its thread cannot be
        cancelled), so a reader that attaches during the mark never leaves
        orphaned_at set behind it."""
        async with run.orphan_lock:
            previous = run.orphan_write
            if previous is not None and not previous.done():
                with contextlib.suppress(BaseException):
                    await asyncio.shield(previous)
            fields = {"orphaned_at_now": True} if orphaned else {"orphaned_clear": True}
            future = asyncio.ensure_future(
                db.run_in_thread(durable_store.update_progress, self.owner, run.id, **fields)
            )
            run.orphan_write = future
            with contextlib.suppress(Exception):
                await asyncio.shield(future)

    async def _orphan_timer(self, run: Run, grace: float, *, attach_grace: float = 0.0) -> None:
        waited = 0.0
        if attach_grace > 0:
            waited = min(float(attach_grace), float(grace))
            await asyncio.sleep(waited)
            if run.readers or run.terminal:
                return
        run.orphan_marked = True
        await self._write_orphan_state(run, orphaned=True)
        await asyncio.sleep(max(0.0, float(grace) - waited))
        if not run.readers and not run.terminal:
            self.stats["orphan_cancels"] += 1
            self.stop_run(run, "orphaned")

    def _suspended_under_reader(self, run: Run) -> bool:
        """A run that left `runs` WITHOUT settling because this process is
        suspending it (SIGTERM) or lost its lease.

        WHY (found 2026-09-14 by the foreground restart test, 1 run in 3). The
        runner drops such a run from `runs` as soon as its attempt ends
        (`_drive`'s finally) — which can be BEFORE `suspend_all` reaches
        `abort_readers` (it awaits the flush and the lease release first). A
        follower woken in between saw "not in runs, not aborted" and switched
        to following remotely, through a runtime that was stopping: no claim,
        no events, only heartbeats, so the client's connection stayed open
        until the process exited (holding uvicorn's graceful shutdown) and the
        gateway never learnt to re-attach. Such a reader must end with
        `FollowerAborted`, exactly as if `abort_readers` had come first."""
        if run.terminal:
            return False
        return self.stopping or run.aborted or run.stop_reason in ("restart", "shutdown", "lease_lost")

    async def follow(self, response_id: str, after: int = 0, *, heartbeat: Optional[float] = None) -> AsyncIterator[Any]:
        """Committed records after `after`, HEARTBEAT on silence; ends after
        the terminal state. Raises FollowerAborted/FollowerEvicted. A
        stopping runtime never starts following a run remotely: its reader is
        aborted so the client re-attaches to the next process."""
        beat = heartbeat_s() if heartbeat is None else float(heartbeat)
        position = int(after)
        while True:
            run = self.runs.get(response_id)
            if run is None and self.stopping:
                raise FollowerAborted()
            if run is not None:
                done = False
                async for item in self._follow_local(run, position, beat):
                    if item is _SWITCH:
                        break
                    if isinstance(item, tuple):
                        position = item[0]
                    yield item
                else:
                    done = True
                if done:
                    return
                continue
            finished = False
            async for item in self._follow_remote(response_id, position, beat):
                if item is _SWITCH:
                    break
                if isinstance(item, tuple):
                    position = item[0]
                yield item
            else:
                finished = True
            if finished:
                return

    async def _follow_local(self, run: Run, position: int, beat: float) -> AsyncIterator[Any]:
        reader = self._add_reader(run.id, run, "follow")
        try:
            while True:
                changed = run._changed
                if reader.evicted:
                    raise FollowerEvicted()
                if run.aborted and not run.terminal:
                    raise FollowerAborted()
                if run.committed_seq > position:
                    records = run.tail_after(position)
                    if records is None:
                        records = await db.run_in_thread(durable_store.list_events, run.id, position, 1000)
                    for record in records:
                        if record[0] <= position:
                            continue
                        position = record[0]
                        yield record
                    continue
                if run.terminal:
                    return
                if run.id not in self.runs:
                    if self._suspended_under_reader(run):
                        raise FollowerAborted()
                    yield _SWITCH
                    return
                try:
                    async with asyncio.timeout(beat):
                        await changed.wait()
                except asyncio.TimeoutError:
                    yield HEARTBEAT
        finally:
            self._remove_reader(run, reader)

    def _ensure_poller(self) -> None:
        """A remote follower needs the shared poller even in a runtime whose
        loops were never started (a route serving a replay before `start`,
        a tool): one poll task, created on demand and ended by `stop`."""
        if any(not task.done() for task in self._loops):
            return
        if self._adhoc_poller is None or self._adhoc_poller.done():
            self._adhoc_poller = asyncio.ensure_future(self._poller_loop())

    async def _follow_remote(self, response_id: str, position: int, beat: float) -> AsyncIterator[Any]:
        self._ensure_poller()
        followers = self._remote.setdefault(response_id, [])
        live = [f for f in followers if not f.reader.evicted]
        if len(live) >= max_followers():
            oldest = min(live, key=lambda f: f.reader.started)
            oldest.reader.evicted = True
            oldest.queue.put_nowait(_EVICT)
        follower = _RemoteFollower(response_id, position, _Reader(response_id, "follow"))
        followers.append(follower)
        with contextlib.suppress(Exception):
            await db.run_in_thread(durable_store.mark_followed, response_id)
        try:
            while True:
                try:
                    async with asyncio.timeout(beat):
                        item = await follower.queue.get()
                except asyncio.TimeoutError:
                    yield HEARTBEAT
                    continue
                if item is _END:
                    return
                if item is _EVICT:
                    raise FollowerEvicted()
                if item is _SWITCH:
                    yield _SWITCH
                    return
                if isinstance(item, tuple) and item[0] > follower.position:
                    follower.position = item[0]
                    yield item
        finally:
            with contextlib.suppress(ValueError):
                self._remote.get(response_id, []).remove(follower)
            if not self._remote.get(response_id):
                self._remote.pop(response_id, None)

    async def poll_remote_once(self) -> int:
        """One shared poll for every remote follower. Returns statements run."""
        if not self._remote:
            return 0
        positions: Dict[str, int] = {}
        for rid, followers in self._remote.items():
            if rid in self.runs:
                for follower in followers:
                    follower.queue.put_nowait(_SWITCH)
                continue
            if followers:
                positions[rid] = min(f.position for f in followers)
        if not positions:
            return 0
        results = await db.run_in_thread(durable_store.poll_many, positions)
        now = time.monotonic()
        for rid, followers in list(self._remote.items()):
            result = results.get(rid)
            if result is None:
                for follower in followers:
                    follower.queue.put_nowait(_END)
                continue
            for follower in followers:
                for record in result.events:
                    if record[0] > follower.queued:
                        follower.queue.put_nowait(record)
                        follower.queued = record[0]
            complete = len(result.events) < 500
            if result.status in durable_store.TERMINAL_STATUSES and complete:
                for follower in followers:
                    follower.queue.put_nowait(_END)
            elif result.status in durable_store.OPEN_STATUSES and not result.lease_live and not self.stopping:
                last = self._claim_attempts.get(rid, float("-inf"))
                if now - last >= suspended_claim_poll_s():
                    self._claim_attempts[rid] = now
                    row = await db.run_in_thread(durable_store.get_run, rid)
                    if row is not None and not row.get("background"):
                        run = await self.claim_and_resume(rid)
                        if run is not None:
                            for follower in followers:
                                follower.queue.put_nowait(_SWITCH)
        return 1

    async def _poller_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(follower_poll_s())
                await self.poll_remote_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.warning("durable follower poll failed", exc_info=True)

    # -- dispatcher and sweeps --------------------------------------------

    def _continuity_ready(self) -> bool:
        if self.continuity_done is not None and self.continuity_done.is_set():
            return True
        return time.monotonic() - self._started_at >= resume_stagger_s()

    async def dispatch_once(self) -> int:
        """Put queued background rows into their gates' lines. Rows launched
        by this process go at once; rows found by the sweep (a restart's
        leftovers) go one per stagger interval, oldest first, after
        continuity.

        ONE FIFO REPRESENTATIVE PER GATE GROUP (2026-09-14 review P5). The
        dispatcher used to claim a row only when `capacity.has_room` said the
        gate would admit AT ONCE — false whenever any foreground request was
        waiting, so a steady foreground backlog starved background work for
        ever — and it scanned one global LIMIT 50, so fifty main rows behind
        a full gate hid a router row whose gate was free. Now the scan is per
        engine (`due_for_resume(per_engine=...)`), and for each gate group
        the oldest due row is claimed whenever no earlier claimed row of that
        group is still waiting for its gates: its runner joins the gate's own
        FIFO queue behind whoever is already there, and when it is admitted
        (`_leave_gate_group`) the next row of the group is claimed. So a
        queued job waits in line like everyone else, while 10,000 queued rows
        still cost at most one waiting coroutine per gate group per process.
        Ungated rows (no gate planned) are claimed without a representative."""
        if self.stopping:
            return 0
        rows = await db.run_in_thread(
            durable_store.due_for_resume, limit=DISPATCH_SCAN_PER_ENGINE * 4, per_engine=DISPATCH_SCAN_PER_ENGINE,
        )
        claimed = 0
        placed: Set[Tuple[str, ...]] = set()
        for row in rows:
            rid = str(row["id"])
            if rid in self.runs:
                continue
            gates = tuple(capacity.gates_for(
                str(row.get("engine") or registry.ENGINE_MAIN), row.get("gate_engine") or None
            ))
            if gates:
                if gates in placed:
                    continue
                waiting = self._gate_waiters.get(gates)
                if waiting is not None:
                    run_waiting = self.runs.get(waiting)
                    if waiting in self._claiming or (
                        run_waiting is not None and not run_waiting.terminal and run_waiting.dispatch_key == gates
                    ):
                        placed.add(gates)
                        continue
                    self._gate_waiters.pop(gates, None)
            if rid not in self._launched_here:
                if not self._continuity_ready():
                    continue
                now = time.monotonic()
                if now - self._last_stagger_claim < resume_stagger_s():
                    continue
                self._last_stagger_claim = now
            if rid in self._claiming:
                continue
            if gates:
                # Reserved BEFORE the claim's await: a concurrent pass must
                # not put a second representative of this group in line.
                self._gate_waiters[gates] = rid
                placed.add(gates)
            run = await self._claim_and_resume_guarded(rid)
            self._launched_here.discard(rid)
            if run is not None and not run.terminal and rid in self.runs:
                claimed += 1
                if gates:
                    run.dispatch_key = gates
            else:
                if run is not None:
                    claimed += 1
                if gates and self._gate_waiters.get(gates) == rid:
                    self._gate_waiters.pop(gates, None)
                    placed.discard(gates)
        return claimed

    async def _claim_and_resume_guarded(self, response_id: str) -> Optional[Run]:
        """The dispatcher's claim: a NEW runner or None (never a run that was
        already running here, which the dispatcher must not count)."""
        if response_id in self.runs:
            return None
        return await self.claim_and_resume(response_id)

    async def _dispatch_loop(self) -> None:
        while True:
            try:
                wake = self.dispatch_wake
                if wake is not None:
                    with contextlib.suppress(asyncio.TimeoutError):
                        async with asyncio.timeout(min(1.0, sweep_s())):
                            await wake.wait()
                    wake.clear()
                await self.dispatch_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.warning("durable dispatch failed", exc_info=True)
                await asyncio.sleep(1.0)

    async def sweep_once(self) -> Dict[str, int]:
        """Suspended-unread TTL, event retention, blob reaper."""
        out = {"unread_cancelled": 0, "events_purged": 0, "blobs_reaped": 0, "local_forgotten": 0}
        # Hooks and slots of rows launched here that are no longer this
        # process's to run: settled or cancelled by another path (another
        # process, an operator), or claimed by another process (a blue/green
        # overlap). Without this they stayed until this process exited.
        tracked = [rid for rid in set(self._slots) | set(self._local_hooks) | set(self._launched_here)
                   if rid not in self.runs]
        if tracked:
            with contextlib.suppress(Exception):
                mine = await db.run_in_thread(durable_store.open_unclaimed, tracked, self.owner)
                for rid in tracked:
                    if rid not in mine and rid not in self.runs:
                        self.forget_local(rid)
                        out["local_forgotten"] += 1
        for rid in await db.run_in_thread(durable_store.suspended_unread, ttl_s=suspended_unread_ttl_s()):
            if rid in self.runs:
                continue
            row = await db.run_in_thread(durable_store.get_run, rid)
            if row is None:
                continue
            meta = row.get("metadata") or {}
            attempts = list(meta.get("attempts") or [])
            prompt = next((a.get("prompt_tokens") for a in attempts if a.get("prompt_tokens") is not None), None)
            fields: Dict[str, Any] = {"status": "cancelled"}
            if row.get("generated_tokens") is not None or prompt is not None:
                fields["output_tokens"] = int(row.get("generated_tokens") or 0)
                if prompt is not None:
                    fields["input_tokens"] = int(prompt)
            settled = await db.run_in_thread(durable_store.finish, None, rid, [], fields)
            if settled is not None:
                out["unread_cancelled"] += 1
                self.forget_local(rid)
        with contextlib.suppress(Exception):
            from . import background

            out["foreground_interrupted"] = len(await db.run_in_thread(
                functools.partial(
                    durable_store.fail_interrupted_foreground, created_before=background.PROCESS_STARTED_AT,
                    error_code=background.INTERRUPTED_CODE, error_message=background.INTERRUPTED_MESSAGE,
                )
            ))
        out["events_purged"] = await db.run_in_thread(durable_store.purge_events, retention_s=event_retention_s())
        with contextlib.suppress(Exception):
            counts = await db.run_in_thread(durable_store.durable_counts)
            from .. import metrics

            metrics.set_gauge("public_api_quarantined_runs", counts["quarantined"],
                              "open durable /v1 runs implicated in an engine incident")
            metrics.set_gauge("public_api_durable_queued", counts["queued"],
                              "background durable /v1 runs queued as rows")
            metrics.set_gauge("public_api_durable_suspended", counts["suspended"],
                              "durable /v1 runs with no live lease")
            metrics.set_gauge("public_api_durable_pending_bytes", sum(r.pending_bytes for r in self.runs.values()),
                              "durable /v1 event bytes not yet committed in this process")
        with contextlib.suppress(Exception):
            out["blobs_reaped"] = await db.run_in_thread(self._blobs().reap, durable_store.referenced_blobs)
        return out

    async def _sweep_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(sweep_s())
                await self.sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.warning("durable sweep failed", exc_info=True)

    # -- cancel -----------------------------------------------------------

    def cancel_local(self, response_id: str) -> bool:
        run = self.runs.get(response_id)
        if run is None or run.terminal:
            return False
        self.stop_run(run, "cancel")
        return True

    async def cancel_after_claim(self, response_id: str) -> bool:
        """`cancel_local`, also for a run whose claim is in flight in this
        process right now (the dispatcher just picked the row). Such a claim
        may have read the row before the cancel flag was written; without
        this the cancel waited for the next lease tick (15 s), and meanwhile
        the run sat in its gate's line. Waits only for the claim itself."""
        if self.cancel_local(response_id):
            return True
        if response_id not in self._claiming:
            return False
        # The claim checks this set just before it starts the runner, so the
        # run is settled cancelled with no engine work at all.
        self._cancel_pending.add(response_id)
        try:
            while response_id in self._claiming:
                await asyncio.sleep(0.01)
        finally:
            self._cancel_pending.discard(response_id)
        return self.cancel_local(response_id)


class _RemoteFollower:
    __slots__ = ("response_id", "position", "queued", "queue", "reader")

    def __init__(self, response_id: str, position: int, reader: _Reader) -> None:
        self.response_id = response_id
        self.position = position
        self.queued = position
        self.queue: "asyncio.Queue[Any]" = asyncio.Queue()
        self.reader = reader


_END = object()
_EVICT = object()
_SWITCH = object()


def _wall_clock_finish() -> str:
    from .. import llm

    return getattr(llm, "WALL_CLOCK_FINISH", "wall_clock")


_CONNECTION_ERROR_NAMES = frozenset({
    "ModelUnavailable", "BreakerOpen", "QueuedForRecovery", "AdmissionRejected",
    "APIConnectionError", "ConnectError", "ConnectTimeout", "RemoteProtocolError", "ReadError",
    "WriteError", "NetworkError", "ReadTimeout", "APITimeoutError", "PoolTimeout", "TimeoutException",
    "ClientDisconnected", "IncompleteRead",
})


def _is_connection_error(exc: BaseException, engine: str) -> bool:
    """Errors that interrupt an attempt instead of failing it. An engine 4xx
    fails at once (the design: "An engine 4xx fails at once")."""
    if isinstance(exc, errors.ApiError):
        return exc.code in ("model_unavailable", "model_recovering") and exc.status >= 500
    if type(exc).__name__ in _CONNECTION_ERROR_NAMES or isinstance(exc, (ConnectionError, asyncio.TimeoutError)):
        return True
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    try:
        return status is not None and int(status) >= 500
    except (TypeError, ValueError):
        return False


def _ledger_fits(run: Run) -> bool:
    """T1's shared KV budget: `admission.kv_ledger.fits(tokens)` for this
    run's footprint. Monitoring-grade: a failure to read it is "fits" (the
    chat-LONG presence check still applies). Assembler, 2026-09-14: the
    getattr fallback for an admission.py without the ledger is gone."""
    try:
        from .. import admission

        footprint = int(run.spec.bounded_input_tokens or 0) + int(run.spec.planned)
        return bool(admission.kv_ledger.fits(footprint))
    except Exception:  # noqa: BLE001
        log.debug("kv ledger unreadable for %s", run.id, exc_info=True)
        return True


RUNTIME = Runtime()


# ------------------------------------------------ module-level interface --


def configure(**kwargs: Any) -> None:
    RUNTIME.configure(**kwargs)


async def start(**kwargs: Any) -> None:
    await RUNTIME.start(**kwargs)


async def stop() -> None:
    await RUNTIME.stop()


def request_suspend(reason: str = "restart") -> None:
    RUNTIME.request_suspend(reason)


async def suspend_all(reason: str = "restart") -> int:
    return await RUNTIME.suspend_all(reason)


async def launch(spec: streaming.GenerationSpec, **kwargs: Any) -> Handle:
    return await RUNTIME.launch(spec, **kwargs)


async def attach(response_id: str, **kwargs: Any) -> Handle:
    return await RUNTIME.attach(response_id, **kwargs)


async def attach_implicit(**kwargs: Any) -> Optional[Handle]:
    return await RUNTIME.attach_implicit(**kwargs)


def request_yield(response_id: str) -> bool:
    """T1 admission's yield callback target (also registered per attempt)."""
    return RUNTIME.request_yield(response_id)


def cancel(response_id: str) -> bool:
    return RUNTIME.cancel_local(response_id)


def is_active() -> bool:
    return RUNTIME.started and not RUNTIME.stopping


def reset_for_tests() -> Runtime:
    """A fresh runtime (tests only)."""
    global RUNTIME
    RUNTIME = Runtime()
    return RUNTIME


__all__ = [
    "AttachForbidden",
    "Caller",
    "RecorderRef",
    "register_recorder",
    "ChatRenderer",
    "DIALECT_CHAT",
    "DIALECT_RESPONSES",
    "FollowerAborted",
    "FollowerEvicted",
    "HEARTBEAT",
    "Handle",
    "NotStreamable",
    "OWNER",
    "RecordBuilder",
    "Run",
    "Runtime",
    "attach",
    "attach_implicit",
    "cancel",
    "caller_of",
    "configure",
    "is_active",
    "launch",
    "outcome_from_log",
    "public_data",
    "render_responses_frame",
    "sse_frames",
    "request_suspend",
    "request_yield",
    "reset_for_tests",
    "spec_from_json",
    "spec_to_json",
    "start",
    "stop",
    "suspend_all",
]
