"""The model-input facade: what `/v1/responses` and `/v1/chat/completions`
call to turn file parts into model input (2026-09-13).

ONE ORDER, for both dialects (design §5.1–§5.7):

1. `lift_file_parts(payload, dialect=…)` — synchronous, before pydantic
   validation. Takes every file part out of the raw body (`input_file`,
   `input_image` with a `file_id`, `input_video`; Chat `file`, `input_audio`)
   and the request-level `file_context`, validates their SHAPE (user turns
   only, exactly one of file_id / file_data / file_url, `file_url` refused and
   never fetched, at most 20 file parts) and hands back a payload the existing
   validator accepts unchanged.
2. `required_scope(lifted)` — `files.read` when any `file_id` is present. The
   router checks it BEFORE resolution, so a key that may not read files learns
   nothing about which ids exist (403 before 404).
3. `prepare(…)` — resolves every id INSIDE THE CALLER'S PROJECT (one query,
   project in the WHERE clause), waits for readiness the way the delivery mode
   allows, refuses failed and unsupported files, and builds the `FileContext`.
4. `splice_messages(…)` — puts the rendered blocks where each file part was,
   and the citation rules into the leading system message.
5. After generation, `annotate(…)` — `file_citation` annotations for markers
   that name content the model was actually shown.

ISOLATION, THE RULE THIS FILE EXISTS TO KEEP. A file id of another project is
indistinguishable from one that never existed: the same single indexed query
runs either way, a miss is the same `404 file_not_found` with the same
sentence and the same `param`, and nothing about the id is echoed — so the
two bodies are byte-identical apart from `request_id`. A malformed id takes
the same path (it is looked up, and misses) rather than a faster 400.
A deleted, expired, or being-purged file is a miss too.

READINESS, PER DELIVERY (design §5.2):

* `sync` — wait up to PUBLIC_API_FILES_SYNC_READY_WAIT_S (30 s) before the
  status line, then `409 file_not_ready` with Retry-After 5 s (text kinds) or
  30 s (audio/video). WHY 30: together with the 30 s capacity gate the
  pre-header silence stays well under Cloudflare's 100 s origin timeout.
  When the no-timeout wave's committed JSON response lands (it answers 200
  and writes a space every 15 s), pass `sync_wait_s=NO_DEADLINE`.
* `stream` — no deadline. `on_progress` is called with a comment text
  (`file file-… transcript 40%`) whenever a stage or percent changes, for the
  router to write as an SSE comment: every client ignores comments, whereas
  an unknown event type can break a typed SDK stream parser.
* `background` — no deadline, `abandon` honoured (a cancelled job stops
  waiting at once).

A file that ends `failed` — or whose kind is `unsupported` — is
`400 invalid_request_error` naming the part, with the file's fixed sentence.

ONE DEADLINE FOR ALL OF A SYNC PREPARE (2026-09-13 review). Readiness is not
the only wait before a sync status line: each `input_audio` clip takes the
`asr` gate (30 s) and decodes (~43 s for 300 s of audio), the query embed and
the rerank each bring their own 30 s gate budget, and page renders run a
child. Budgeted separately, a retrieval request under load was 30+30+30+30 s
and three 300 s MP3 clips ~219 s — past Cloudflare's 100 s, and both SDKs
retry a 524, re-running the transcriptions. So a sync `prepare` has ONE
monotonic deadline, PUBLIC_API_FILES_SYNC_PREPARE_BUDGET_S (45 s), and every
step draws on what is left: readiness stops at it (409), the query embed and
the rerank are skipped when under MIN_OPTIONAL_S remain (lexical ranking,
`rerank: "skipped"`) and abandoned at it, renders send text only, a
transcription past it is a 503 that is safe to retry, and an inline
extraction past it is a 400 toward /v1/files or streaming. Before any clip
reaches whisper, a sync request whose `input_audio` could exceed one clip's
ceiling in total (a WAV's header duration, or the ceiling itself for an MP3,
whose duration is unknown before decoding) is refused toward stream or
background. `sync_wait_s=NO_DEADLINE` (the committed JSON response) lifts
this deadline too.
"""
from __future__ import annotations

import asyncio
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
)

from ..publicapi import errors
from . import citations as cite
from . import context as file_context
from . import inline as inline_files
from . import retrieval, vectors

DIALECT_RESPONSES = "responses"
DIALECT_CHAT = "chat"

DELIVERY_SYNC = "sync"
DELIVERY_STREAM = "stream"
DELIVERY_BACKGROUND = "background"

FILES_READ_SCOPE = "files.read"

PROJECT_ID_RE = re.compile(r"^proj_[0-9a-f]{24}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

#: `processing.error.code` → the fixed sentence (design §4.6). Never an
#: exception text, engine name, URL or path.
FAILURE_SENTENCES = {
    "unsupported_file": "This file type cannot be used as model input.",
    "file_corrupt": "The file could not be read; it may be damaged or encrypted.",
    "file_too_complex": "The file exceeds a processing ceiling.",
    "processing_unavailable": (
        "Processing could not reach a required service after several attempts. "
        "Upload the file again later."
    ),
    "internal_error": "Something went wrong while processing this file.",
    "checksum_mismatch": "The assembled bytes did not match the checksum you supplied.",
}
NOT_FOUND_SENTENCE = "No such file."
NOT_READY_SENTENCE = "The file is still being processed. Retry after the Retry-After interval, or use stream or background."

#: The step names a progress comment may carry (design §4.3, plus `assemble`
#: and `queued`). Anything else is rendered as `processing`: a stage string
#: comes from a jsonb column, and the wire must never carry free text from it.
STAGE_NAMES = frozenset({
    "assemble", "queued", "sniff", "text", "ocr", "chunk", "index", "finalize", "sheets", "profile",
    "decode", "variants", "probe", "audio", "transcript", "frames", "vision", "fusion", "artifacts",
})

#: The design §2.17 rows for the two codes this facade raises that are not in
#: `publicapi.errors`' table until the ingest team adds them.
_PENDING_CODES = {
    "file_not_found": (404, "invalid_request_error"),
    "file_not_ready": (409, "invalid_request_error"),
}


# ---------------------------------------------------------------- settings --


def sync_ready_wait_s() -> float:
    from . import limits

    return max(0.0, limits.sync_ready_wait_s())


def sync_prepare_budget_s() -> float:
    """PUBLIC_API_FILES_SYNC_PREPARE_BUDGET_S (45 s): the whole of a sync
    `prepare` (module docstring, ONE DEADLINE). 45 = the 30 s readiness wait
    + 15 s for the engine steps after it; with the router's 30 s main gate
    that is 75 s of silence before generation, 25 s inside Cloudflare's 100 s.
    One idle 300 s clip measured ~43 s of decode, so it still fits alone."""
    from ..publicapi.registry import setting_float

    return max(0.0, setting_float("PUBLIC_API_FILES_SYNC_PREPARE_BUDGET_S", 45.0))


#: Optional engine work (query embed, rerank, page renders) is skipped when
#: less than this remains of a sync deadline: each is a gate wait plus a
#: sub-second call, and starting one that cannot finish only wastes the slot.
MIN_OPTIONAL_S = 2.0


class Deadline:
    """One monotonic deadline for a request's pre-header work, or none."""

    def __init__(self, seconds: Optional[float], *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self.end: Optional[float] = None if seconds is None else clock() + max(0.0, float(seconds))

    @property
    def bounded(self) -> bool:
        return self.end is not None

    def remaining(self) -> Optional[float]:
        if self.end is None:
            return None
        return max(0.0, self.end - self._clock())


def ready_poll_s() -> float:
    """PUBLIC_API_FILES_READY_POLL_S (1 s): how often readiness re-reads the
    rows. One indexed query per waiting request per second; the SSE events
    route polls every 5 s, but a sync wait has only 30 s to notice."""
    from ..publicapi.registry import setting_float

    return max(0.05, setting_float("PUBLIC_API_FILES_READY_POLL_S", 1.0))


def max_file_parts_per_request() -> int:
    """PUBLIC_API_FILES_MAX_PER_REQUEST (20): bounds context-build fan-out."""
    from . import limits

    return max(1, limits.max_file_parts_per_request())


def max_videos_per_request() -> int:
    """PUBLIC_API_FILES_VIDEOS_PER_REQUEST (3): the chat app's video limit."""
    from . import limits

    return max(0, limits.max_videos_per_request())


def retrieval_max_tokens() -> int:
    return file_context.retrieval_max_tokens()


# ------------------------------------------------------------------ errors --


class _PendingCodeError(errors.ApiError):
    """An `ApiError` for a design §2.17 code the closed table does not hold
    yet. Renders the identical envelope; exists only until integration adds
    the code to `publicapi.errors._CODES`, after which `api_error` never
    constructs one."""

    def __init__(self, code: str, message: str, *, param: Optional[str], retry_after: Optional[float]) -> None:
        status, wire_type = _PENDING_CODES[code]
        Exception.__init__(self, message)
        self.code = code
        self.status = status
        self.type = wire_type
        self.message = str(message)
        self.param = errors._echoable(param)
        self.retry_after = None if retry_after is None else max(errors.MIN_RETRY_AFTER, int(math.ceil(retry_after)))


def api_error(code: str, message: str, *, param: Optional[str] = None, retry_after: Optional[float] = None) -> errors.ApiError:
    try:
        return errors.ApiError(code, message, param=param, retry_after=retry_after)
    except ValueError:
        if code not in _PENDING_CODES:
            raise
        return _PendingCodeError(code, message, param=param, retry_after=retry_after)


def file_not_found(param: Optional[str]) -> errors.ApiError:
    return api_error("file_not_found", NOT_FOUND_SENTENCE, param=param)


def file_not_ready(param: Optional[str], retry_after: float) -> errors.ApiError:
    return api_error("file_not_ready", NOT_READY_SENTENCE, param=param, retry_after=retry_after)


# ------------------------------------------------------------ lifting parts --


@dataclass
class FilePartRef:
    """One file part taken out of the body."""

    message_index: int
    part_index: int
    param: str                     # e.g. input.0.content.1
    part_type: str                 # input_file | input_image | input_video | file | input_audio
    file_id: Optional[str] = None
    file_data: Optional[str] = None
    filename: Optional[str] = None
    detail: Optional[str] = None
    audio: Optional[Mapping[str, Any]] = None

    @property
    def id_param(self) -> str:
        return f"{self.param}.file_id" if self.part_type != DIALECT_CHAT_FILE else f"{self.param}.file.file_id"


DIALECT_CHAT_FILE = "file"


@dataclass
class FileContextOptions:
    mode: str = file_context.MODE_AUTO
    max_tokens: Optional[int] = None


@dataclass
class LiftedRequest:
    payload: Any
    dialect: str
    refs: List[FilePartRef] = field(default_factory=list)
    options: FileContextOptions = field(default_factory=FileContextOptions)
    #: message index → the raw content list as the caller sent it.
    original_content: Dict[int, List[Any]] = field(default_factory=dict)
    instructions_present: bool = False
    question: str = ""
    #: Did the caller type any text at all (instructions, a string input, a
    #: non-blank text part in any message)? The `(attached file)` placeholder
    #: the lift adds is not caller text: planning's "OCR" default prompt must
    #: still apply to a request that sent only an image file.
    caller_text: bool = False

    @property
    def has_files(self) -> bool:
        return bool(self.refs)

    @property
    def file_ids(self) -> List[str]:
        out: List[str] = []
        for ref in self.refs:
            if ref.file_id is not None and ref.file_id not in out:
                out.append(ref.file_id)
        return out


_PLACEHOLDER = "(attached file)"
_RESPONSES_KEYS = {
    "input_file": {"type", "file_id", "file_data", "file_url", "filename", "detail"},
    "input_image": {"type", "file_id", "detail"},
    "input_video": {"type", "file_id", "detail"},
}
_FILE_DETAILS = ("low", "auto", "high")
_IMAGE_DETAILS = ("low", "auto", "high", "original")


def _invalid(message: str, param: Optional[str]) -> errors.ApiError:
    return errors.invalid_request(message, param=param)


def _one_of(part: Mapping[str, Any], param: str, *, allow_url: bool) -> None:
    present = [k for k in ("file_id", "file_data", "file_url") if part.get(k) is not None]
    if len(present) != 1:
        names = "file_id, file_data or file_url" if allow_url else "file_id or file_data"
        raise _invalid(f"A file part needs exactly one of {names}.", param)
    if present[0] == "file_url":
        raise _invalid(
            "Files are not fetched from URLs; upload the file with /v1/files and pass its file_id.",
            f"{param}.file_url",
        )


def _string(value: Any, param: str, *, max_len: int = 255) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > max_len:
        raise _invalid(f"{param.rsplit('.', 1)[-1]} must be a string of at most {max_len} characters.", param)
    return value


def _detail(value: Any, param: str, allowed: Sequence[str]) -> Optional[str]:
    if value is None:
        return None
    if value not in allowed:
        raise _invalid(f"detail must be one of {', '.join(allowed)}.", param)
    return str(value)


def _file_id(value: Any, param: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid("file_id must be a string.", param)
    return value.strip()


def _lift_responses_part(part: Mapping[str, Any], param: str, mi: int, pi: int) -> Optional[FilePartRef]:
    kind = part.get("type")
    if kind == "input_image" and part.get("file_id") is None:
        return None  # an inline image_url part: the existing validator's
    if kind not in _RESPONSES_KEYS:
        return None
    unknown = sorted(k for k in part if k not in _RESPONSES_KEYS[kind])
    if kind == "input_image" and "image_url" in part:
        raise _invalid("An input_image part takes image_url or file_id, not both.", param)
    if unknown:
        raise _invalid(f"Unsupported field in a {kind} part: {unknown[0]}.", f"{param}.{unknown[0]}")
    if kind == "input_file":
        _one_of(part, param, allow_url=True)
        ref = FilePartRef(
            mi, pi, param, kind,
            file_id=_file_id(part["file_id"], f"{param}.file_id") if part.get("file_id") is not None else None,
            file_data=part.get("file_data"),
            filename=_string(part.get("filename"), f"{param}.filename"),
            detail=_detail(part.get("detail"), f"{param}.detail", _FILE_DETAILS),
        )
        if ref.file_data is not None and not ref.filename:
            ref.filename = "upload"
        return ref
    if part.get("file_id") is None:
        raise _invalid(f"An {kind} part needs a file_id.", f"{param}.file_id")
    allowed = _IMAGE_DETAILS if kind == "input_image" else _FILE_DETAILS
    return FilePartRef(
        mi, pi, param, kind,
        file_id=_file_id(part["file_id"], f"{param}.file_id"),
        detail=_detail(part.get("detail"), f"{param}.detail", allowed),
    )


def _lift_chat_part(part: Mapping[str, Any], param: str, mi: int, pi: int) -> Optional[FilePartRef]:
    kind = part.get("type")
    if kind == "file":
        unknown = sorted(k for k in part if k not in ("type", "file"))
        body = part.get("file")
        if unknown or not isinstance(body, Mapping):
            raise _invalid('A file part must be {"type": "file", "file": {"file_id": …}}.', param)
        extra = sorted(k for k in body if k not in ("file_id", "file_data", "filename"))
        if extra:
            raise _invalid(f"Unsupported field in file: {extra[0]}.", f"{param}.file.{extra[0]}")
        _one_of(body, f"{param}.file", allow_url=False)
        ref = FilePartRef(
            mi, pi, param, kind,
            file_id=_file_id(body["file_id"], f"{param}.file.file_id") if body.get("file_id") is not None else None,
            file_data=body.get("file_data"),
            filename=_string(body.get("filename"), f"{param}.file.filename"),
        )
        if ref.file_data is not None and not ref.filename:
            ref.filename = "upload"
        return ref
    if kind == "input_audio":
        unknown = sorted(k for k in part if k not in ("type", "input_audio"))
        if unknown:
            raise _invalid(f"Unsupported field in an input_audio part: {unknown[0]}.", f"{param}.{unknown[0]}")
        return FilePartRef(mi, pi, param, kind, audio=part.get("input_audio"))
    return None


def _text_of(part: Any) -> Optional[str]:
    if isinstance(part, Mapping) and part.get("type") in ("input_text", "text") and isinstance(part.get("text"), str):
        return part["text"]
    return None


def lift_file_parts(payload: Any, *, dialect: str) -> LiftedRequest:
    """Step 1 of the module docstring. A body with no file parts and no
    `file_context` comes back as the same object, untouched."""
    if dialect not in (DIALECT_RESPONSES, DIALECT_CHAT):
        raise ValueError(f"unknown dialect {dialect!r}")
    lifted = LiftedRequest(payload=payload, dialect=dialect)
    if not isinstance(payload, Mapping):
        return lifted
    container = "input" if dialect == DIALECT_RESPONSES else "messages"
    messages = payload.get(container)
    options_raw = payload.get("file_context")
    lifted.instructions_present = bool(dialect == DIALECT_RESPONSES and isinstance(payload.get("instructions"), str) and payload.get("instructions", "").strip())

    new_messages = messages
    if isinstance(messages, list):
        new_messages = []
        for mi, message in enumerate(messages):
            content = message.get("content") if isinstance(message, Mapping) else None
            if not isinstance(content, list):
                new_messages.append(message)
                continue
            kept: List[Any] = []
            found: List[FilePartRef] = []
            for pi, part in enumerate(content):
                param = f"{container}.{mi}.content.{pi}"
                ref = None
                if isinstance(part, Mapping):
                    ref = (_lift_responses_part if dialect == DIALECT_RESPONSES else _lift_chat_part)(part, param, mi, pi)
                if ref is None:
                    kept.append(part)
                else:
                    found.append(ref)
            if not found:
                new_messages.append(message)
                continue
            if message.get("role") != "user":
                raise _invalid("File parts are accepted only in user messages.", found[0].param)
            lifted.refs.extend(found)
            lifted.original_content[mi] = list(content)
            if not any(_text_of(p) is not None or (isinstance(p, Mapping) and p.get("type") in ("input_image", "image_url")) for p in kept):
                kept.append({"type": "input_text" if dialect == DIALECT_RESPONSES else "text", "text": _PLACEHOLDER})
            new_messages.append({**message, "content": kept})

    if len(lifted.refs) > max_file_parts_per_request():
        raise _invalid(
            f"A request may carry at most {max_file_parts_per_request()} file parts.",
            lifted.refs[max_file_parts_per_request()].param,
        )
    audio_parts = [r for r in lifted.refs if r.part_type in ("input_video", "input_audio")]
    if len(audio_parts) > max_videos_per_request():
        raise _invalid(
            f"A request may carry at most {max_videos_per_request()} audio or video files.",
            audio_parts[max_videos_per_request()].param,
        )
    if options_raw is not None:
        lifted.options = _parse_options(options_raw)

    if lifted.refs or "file_context" in payload:
        rebuilt = {k: v for k, v in payload.items() if k != "file_context"}
        if isinstance(messages, list):
            rebuilt[container] = new_messages
        lifted.payload = rebuilt
    lifted.question = _question(payload, dialect)
    lifted.caller_text = _has_caller_text(payload, dialect)
    return lifted


def _has_caller_text(payload: Mapping[str, Any], dialect: str) -> bool:
    if dialect == DIALECT_RESPONSES and isinstance(payload.get("instructions"), str) and payload["instructions"].strip():
        return True
    container = payload.get("input" if dialect == DIALECT_RESPONSES else "messages")
    if isinstance(container, str):
        return bool(container.strip())
    if not isinstance(container, list):
        return False
    for message in container:
        content = message.get("content") if isinstance(message, Mapping) else None
        if isinstance(content, str) and content.strip():
            return True
        if isinstance(content, list) and any((_text_of(p) or "").strip() for p in content):
            return True
    return False


def _parse_options(raw: Any) -> FileContextOptions:
    if not isinstance(raw, Mapping):
        raise _invalid("file_context must be an object.", "file_context")
    unknown = sorted(k for k in raw if k not in ("mode", "max_tokens"))
    if unknown:
        raise _invalid(f"Unsupported field in file_context: {unknown[0]}.", f"file_context.{unknown[0]}")
    mode = raw.get("mode", file_context.MODE_AUTO)
    if mode not in file_context.MODES:
        raise _invalid("file_context.mode must be auto, full or retrieval.", "file_context.mode")
    max_tokens = raw.get("max_tokens")
    if max_tokens is not None:
        ceiling = retrieval_max_tokens()
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or not 1 <= max_tokens <= ceiling:
            raise _invalid(f"file_context.max_tokens must be an integer between 1 and {ceiling}.", "file_context.max_tokens")
    return FileContextOptions(mode=str(mode), max_tokens=max_tokens)


def _question(payload: Mapping[str, Any], dialect: str) -> str:
    """The text of the LAST user message; else the instructions; else the
    generic "Describe the attached files." (summary retrieval)."""
    container = payload.get("input" if dialect == DIALECT_RESPONSES else "messages")
    if isinstance(container, str):
        return container.strip()
    if isinstance(container, list):
        for message in reversed(container):
            if not isinstance(message, Mapping) or message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                return content.strip()
            if isinstance(content, list):
                texts = [t for t in (_text_of(p) for p in content) if t and t.strip()]
                if texts:
                    return "\n".join(texts).strip()
            break
    instructions = payload.get("instructions") if dialect == DIALECT_RESPONSES else None
    if isinstance(instructions, str) and instructions.strip():
        return instructions.strip()
    return file_context.GENERIC_QUESTION


def required_scope(lifted: LiftedRequest) -> Optional[str]:
    """`files.read` when any part names a file id (design §2.1: "may use in a
    prompt" means "may read", since a model can be asked to repeat a file).
    Inline `file_data` needs no file scope: the caller already holds it."""
    return FILES_READ_SCOPE if lifted.file_ids else None


# ---------------------------------------------------------------- the store --


@dataclass(frozen=True)
class FileRecord:
    """A live file of the caller's project, as model input needs it."""

    file_id: str
    project_id: str
    filename: str
    bytes: int
    #: assembling | queued | processing | processed | failed
    state: str
    kind: str = "unknown"
    mime_type: str = "application/octet-stream"
    sha256: Optional[str] = None
    error_code: Optional[str] = None
    stage: Optional[str] = None
    percent: Optional[int] = None
    facts: Mapping[str, Any] = field(default_factory=dict)
    video_analysis_id: Optional[int] = None

    @property
    def terminal(self) -> bool:
        return self.state in ("processed", "failed")

    @property
    def media(self) -> bool:
        return self.kind in file_context.KINDS_MEDIA or self.mime_type.startswith(("audio/", "video/"))


class FileStore(Protocol):
    async def get_files(self, project_id: str, file_ids: Sequence[str]) -> Dict[str, FileRecord]:
        """Live files of `project_id` among `file_ids`; everything else absent."""


def _aware(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return None


def record_from_row(row: Mapping[str, Any], *, now: Optional[datetime] = None) -> Optional[FileRecord]:
    """A joined `api_files` + `api_file_blobs` row (blob columns prefixed
    `blob_`, the ingest team's `get_api_files` contract) → a record, or None
    when the file is not usable at all (deleted, expired, being purged)."""
    now = now or datetime.now(timezone.utc)
    if row.get("deleted_at") is not None:
        return None
    expires = _aware(row.get("expires_at"))
    if expires is not None and expires <= now:
        return None
    blob_status = row.get("blob_status")
    if row.get("blob_id") is not None and blob_status == "deleting":
        return None
    progress = row.get("blob_progress") if isinstance(row.get("blob_progress"), Mapping) else {}
    percent = progress.get("percent") if isinstance(progress, Mapping) else None
    kind = str(row.get("blob_kind") or "unknown")
    error_code = row.get("error_code") or row.get("blob_error_code")
    if row.get("error_code"):
        state = "failed"
    elif row.get("blob_id") is None:
        if row.get("assembling_upload_id") is None:
            return None
        state = "assembling"
    elif blob_status == "processed":
        state = "failed" if kind == "unsupported" else "processed"
        if kind == "unsupported":
            error_code = error_code or "unsupported_file"
    elif blob_status == "failed":
        state = "failed"
    elif blob_status in ("queued", "processing"):
        state = str(blob_status)
    else:
        return None
    return FileRecord(
        file_id=str(row["id"]),
        project_id=str(row["project_id"]),
        filename=str(row.get("filename") or "file"),
        bytes=int(row.get("bytes") or 0),
        state=state,
        kind=kind,
        mime_type=str(row.get("blob_mime_type") or "application/octet-stream"),
        sha256=(str(row["blob_sha256"]) if row.get("blob_sha256") else None),
        error_code=(str(error_code) if error_code else None),
        stage=("assemble" if state == "assembling" else (str(row.get("blob_stage")) if row.get("blob_stage") else None)),
        percent=(int(percent) if isinstance(percent, (int, float)) and not isinstance(percent, bool) else None),
        facts=dict(row.get("blob_facts") or {}) if isinstance(row.get("blob_facts"), Mapping) else {},
        video_analysis_id=(int(row["blob_video_analysis_id"]) if row.get("blob_video_analysis_id") is not None else None),
    )


#: One indexed lookup (the `api_files` primary key, filtered by project) with
#: the blob joined on id AND project: the join itself refuses a blob row of
#: another project even if an id were ever mis-pointed.
FILES_QUERY = """
SELECT f.id, f.project_id, f.blob_id, f.assembling_upload_id, f.error_code, f.filename, f.bytes,
       f.expires_at, f.deleted_at,
       b.sha256 AS blob_sha256, b.kind AS blob_kind, b.mime_type AS blob_mime_type,
       b.status AS blob_status, b.stage AS blob_stage, b.progress AS blob_progress,
       b.facts AS blob_facts, b.error_code AS blob_error_code,
       b.video_analysis_id AS blob_video_analysis_id
  FROM api_files f
  LEFT JOIN api_file_blobs b ON b.id = f.blob_id AND b.project_id = f.project_id
 WHERE f.project_id = %s AND f.id = ANY(%s) AND f.deleted_at IS NULL
"""


#: Real ids are `file-` + 24 hex (29 chars); 128 leaves room for any future
#: shape without letting a megabyte string reach the bind list.
MAX_FILE_ID_CHARS = 128


class SqlFileStore:
    """The production store over the V36 tables. `connect` is a context
    manager factory yielding a psycopg connection with dict rows —
    `db.connection` by default; tests pass one bound to a private schema."""

    def __init__(self, connect: Optional[Callable[[], Any]] = None) -> None:
        self._connect = connect

    def _query(self, project_id: str, file_ids: Sequence[str]) -> List[Mapping[str, Any]]:
        connect = self._connect
        if connect is None:
            from .. import db

            connect = db.connection
        # An id Postgres cannot even hold (a NUL raised psycopg's DataError, a
        # 500 anyone could trigger) or longer than any real id is a miss — but
        # the query still runs, once, so the path is the same as for a
        # well-formed id of another project.
        bindable = [i for i in file_ids if "\x00" not in i and len(i) <= MAX_FILE_ID_CHARS]
        with connect() as con:
            return list(con.execute(FILES_QUERY, (project_id, bindable)).fetchall())

    async def get_files(self, project_id: str, file_ids: Sequence[str]) -> Dict[str, FileRecord]:
        if not file_ids:
            return {}
        rows = await asyncio.to_thread(self._query, project_id, list(dict.fromkeys(file_ids)))
        now = datetime.now(timezone.utc)
        out: Dict[str, FileRecord] = {}
        for row in rows:
            record = record_from_row(row, now=now)
            if record is not None and record.project_id == project_id:
                out[record.file_id] = record
        return out


class MemoryFileStore:
    """Rows in memory, keyed (project_id, file_id): the reference behaviour
    the SQL store is tested against, and the store the facade tests use."""

    def __init__(self) -> None:
        self.rows: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.queries: List[Tuple[str, Tuple[str, ...]]] = []

    def put(self, row: Mapping[str, Any]) -> None:
        self.rows[(str(row["project_id"]), str(row["id"]))] = dict(row)

    def update(self, project_id: str, file_id: str, **fields: Any) -> None:
        self.rows[(project_id, file_id)].update(fields)

    async def get_files(self, project_id: str, file_ids: Sequence[str]) -> Dict[str, FileRecord]:
        self.queries.append((project_id, tuple(file_ids)))
        out: Dict[str, FileRecord] = {}
        for file_id in file_ids:
            row = self.rows.get((project_id, file_id))
            record = record_from_row(row) if row is not None else None
            if record is not None:
                out[file_id] = record
        return out


# --------------------------------------------------------------- readiness --


def progress_comment(record: FileRecord) -> str:
    """`file file-… transcript 40%` — stage names from a closed set only."""
    if record.state == "queued":
        # The column defaults to `sniff` before any work: a queued blob has
        # not started, and saying "sniff" would claim it had.
        stage = "queued"
    else:
        stage = record.stage if record.stage in STAGE_NAMES else "processing"
    text = f"file {record.file_id} {stage}"
    if record.percent is not None:
        text += f" {max(0, min(100, int(record.percent)))}%"
    return text


#: `sync_wait_s` values: the setting (default), or no deadline at all.
USE_SETTING: Any = object()
NO_DEADLINE = None


def retry_after_for(records: Iterable[FileRecord]) -> float:
    """5 s while only text kinds are pending, 30 s when any is audio/video."""
    return 30.0 if any(r.media for r in records) else 5.0


async def wait_until_ready(
    store: FileStore,
    project_id: str,
    lifted: LiftedRequest,
    *,
    delivery: str,
    sync_wait_s: Any = USE_SETTING,
    on_progress: Optional[Callable[[str], Awaitable[None]]] = None,
    abandon: Optional[asyncio.Event] = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
    deadline: Optional[Deadline] = None,
) -> Tuple[Dict[str, FileRecord], float]:
    """Resolve and wait (module docstring, READINESS). Returns the records and
    the seconds waited. Raises 404 / 409 / 400 as described. A sync wait also
    stops at `deadline`, the request's whole pre-header budget."""
    ids = lifted.file_ids
    if not ids:
        return {}, 0.0
    first_param = {}
    for ref in lifted.refs:
        if ref.file_id is not None:
            first_param.setdefault(ref.file_id, ref.id_param)
    started = clock()
    if delivery == DELIVERY_SYNC:
        wait = sync_ready_wait_s() if sync_wait_s is USE_SETTING else sync_wait_s
        ready_by = None if wait is None else started + max(0.0, float(wait))
        if deadline is not None and deadline.bounded:
            left = deadline.remaining() or 0.0
            ready_by = started + left if ready_by is None else min(ready_by, started + left)
    else:
        ready_by = None
    last_comments: Dict[str, str] = {}
    while True:
        records = await store.get_files(project_id, ids)
        for file_id in ids:
            if file_id not in records:
                raise file_not_found(first_param[file_id])
        for file_id in ids:
            record = records[file_id]
            if record.state == "failed":
                sentence = FAILURE_SENTENCES.get(record.error_code or "", FAILURE_SENTENCES["internal_error"])
                raise _invalid(sentence, first_param[file_id])
        pending = [records[i] for i in ids if not records[i].terminal]
        if not pending:
            return records, clock() - started
        if abandon is not None and abandon.is_set():
            raise asyncio.CancelledError()
        if ready_by is not None and clock() >= ready_by:
            raise file_not_ready(first_param[pending[0].file_id], retry_after_for(pending))
        if on_progress is not None:
            for record in pending:
                comment = progress_comment(record)
                if last_comments.get(record.file_id) != comment:
                    last_comments[record.file_id] = comment
                    await on_progress(comment)
        interval = ready_poll_s()
        if ready_by is not None:
            interval = max(0.0, min(interval, ready_by - clock()))
        if abandon is not None and sleep is asyncio.sleep:
            # A cancelled background job stops waiting now, not a poll later.
            try:
                async with asyncio.timeout(interval):
                    await abandon.wait()
            except asyncio.TimeoutError:
                pass
        else:
            await sleep(interval)


# ------------------------------------------------------------------ paths --


def derived_dir_for(project_id: str, sha256: str) -> str:
    """`<PUBLIC_API_FILES_DIR>/<project_id>/<sha256>/derived`, both segments
    validated by regex before the join (design §7.1) — the vector store of a
    project can only ever be opened under that project's directory."""
    if not PROJECT_ID_RE.match(project_id or "") or not SHA256_RE.match(sha256 or ""):
        raise ValueError("project id or content hash is not a valid path segment")
    from . import storage

    return storage.derived_dir(project_id, sha256)


AnalysisLoader = Callable[[int], Awaitable[Optional[Mapping[str, Any]]]]


async def db_analysis_loader(analysis_id: int) -> Optional[Mapping[str, Any]]:
    from .. import db

    return await asyncio.to_thread(db.get_video_analysis, int(analysis_id))


# --------------------------------------------------------------- the build --


@dataclass
class ModelInput:
    """Everything the generating route needs from files."""

    context: file_context.FileContext
    lifted: LiftedRequest
    files: Dict[str, FileRecord]
    audio_blocks: Dict[Tuple[int, int], Dict[str, Any]]
    audio_seconds: float
    files_wait_s: float
    workspace: Optional[inline_files.InlineWorkspace] = None
    caps: Optional[file_context.ModelCaps] = None

    @property
    def estimated_tokens(self) -> int:
        return self.context.estimated_tokens + sum(_estimate(b["text"]) for b in self.audio_blocks.values())

    @property
    def bounded_tokens(self) -> int:
        return self.context.bounded_tokens + sum(len(b["text"].encode("utf-8")) for b in self.audio_blocks.values())

    @property
    def image_count(self) -> int:
        return self.context.image_count

    def usage_meta(self) -> Dict[str, Any]:
        meta = dict(self.context.meta)
        meta["files_wait_s"] = round(float(self.files_wait_s), 3)
        if self.audio_blocks:
            meta["audio_seconds"] = round(float(self.audio_seconds), 3)
        return meta

    def cleanup(self) -> None:
        """Call at the response's terminal state (inline bytes go then)."""
        if self.workspace is not None:
            self.workspace.cleanup()


def _estimate(text: str) -> int:
    from .. import context as token_context

    return int(token_context.estimate_tokens(text))


@dataclass
class Engines:
    """The injectable engine seams for one request."""

    embed_query: Optional[vectors.QueryEmbedder] = None
    rerank: Optional[retrieval.Reranker] = None
    render_pages: Optional[file_context.PageRenderer] = None
    load_frame: Optional[file_context.FrameLoader] = None
    extractor: Optional[inline_files.InlineExtractor] = None
    transcriber: Optional[inline_files.Transcriber] = None
    analysis_loader: Optional[AnalysisLoader] = None

    @classmethod
    def production(cls, *, gate_wait_s: float) -> "Engines":
        return cls(
            embed_query=vectors.make_engine_query_embedder(gate_wait_s),
            rerank=retrieval.make_engine_reranker(),
            render_pages=file_context.make_engine_page_renderer(),
            load_frame=file_context.load_frame_896,
            extractor=inline_files.make_subprocess_extractor(),
            transcriber=inline_files.engine_transcriber,
            analysis_loader=db_analysis_loader,
        )

    def bounded(self, deadline: Deadline) -> "Engines":
        """These engines, each held to `deadline` (module docstring, ONE
        DEADLINE). An unbounded deadline returns `self` unchanged.

        `asyncio.wait_for` cancels the inner call at the deadline: a gate
        waiter is removed from its queue (`capacity.hold` handles the
        cancellation), an HTTP call is closed, and an extraction or render
        child is killed by `extract_worker.run`'s own cancellation path."""
        if not deadline.bounded:
            return self

        async def within(call: Awaitable[Any]) -> Any:
            left = deadline.remaining() or 0.0
            return await asyncio.wait_for(call, timeout=max(0.001, left))

        def optional_left() -> bool:
            return (deadline.remaining() or 0.0) >= MIN_OPTIONAL_S

        embed_query = None
        if self.embed_query is not None:
            inner_embed = self.embed_query

            async def embed_query(question: str) -> Optional[Sequence[float]]:
                if not optional_left():
                    return None
                try:
                    return await within(inner_embed(question))
                except asyncio.TimeoutError:
                    return None

        rerank = None
        if self.rerank is not None:
            inner_rerank = self.rerank

            async def rerank(question: str, documents: Sequence[str]) -> Optional[Sequence[float]]:
                if not optional_left():
                    return None
                try:
                    return await within(inner_rerank(question, documents))
                except asyncio.TimeoutError:
                    return None

        render_pages = None
        if self.render_pages is not None:
            inner_render = self.render_pages

            async def render_pages(file: file_context.ResolvedFile, pages: Sequence[int]) -> List[Tuple[int, str]]:
                if not optional_left():
                    return []
                try:
                    return await within(inner_render(file, pages))
                except asyncio.TimeoutError:
                    return []

        extractor = None
        if self.extractor is not None:
            inner_extract = self.extractor

            async def extractor(source: str, kind: str, derived: str, max_ocr: int, strict: bool) -> inline_files.ExtractOutcome:
                try:
                    return await within(inner_extract(source, kind, derived, max_ocr, strict))
                except asyncio.TimeoutError:
                    raise inline_files.InlineTooSlow() from None

        inner_transcribe = self.transcriber or inline_files.engine_transcriber

        async def transcriber(raw: bytes, content_type: str) -> Tuple[str, Optional[float]]:
            try:
                return await within(inner_transcribe(raw, content_type))
            except asyncio.TimeoutError:
                raise errors.model_at_capacity(TRANSCRIBE_RETRY_AFTER_S) from None

        return Engines(
            embed_query=embed_query,
            rerank=rerank,
            render_pages=render_pages,
            load_frame=self.load_frame,
            extractor=extractor,
            transcriber=transcriber,
            analysis_loader=self.analysis_loader,
        )


#: Retry-After for a sync transcription that ran out of the request deadline:
#: whisper decodes one clip at a time fleet-wide, so a queue that held a clip
#: past the budget is not gone in 5 s (the capacity gate's own figure is 30).
TRANSCRIBE_RETRY_AFTER_S = 30.0


async def prepare(
    lifted: LiftedRequest,
    *,
    project_id: str,
    store: FileStore,
    caps: file_context.ModelCaps,
    delivery: str,
    request_id: str,
    engines: Optional[Engines] = None,
    caller_text_tokens: int = 0,
    caller_images: int = 0,
    sync_wait_s: Any = USE_SETTING,
    on_progress: Optional[Callable[[str], Awaitable[None]]] = None,
    abandon: Optional[asyncio.Event] = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
    sync_budget_s: Any = USE_SETTING,
) -> ModelInput:
    """Steps 3 of the module docstring: resolve, wait, refuse, build.

    `sync_budget_s` is the ONE DEADLINE of a sync request (the setting by
    default; `NO_DEADLINE` for none). Passing `sync_wait_s=NO_DEADLINE`
    without a budget lifts the deadline too: that is the committed JSON
    response, which writes bytes while it waits."""
    engines = engines or Engines()
    if delivery != DELIVERY_SYNC:
        deadline = Deadline(None, clock=clock)
    elif sync_budget_s is USE_SETTING:
        deadline = Deadline(None if sync_wait_s is NO_DEADLINE else sync_prepare_budget_s(), clock=clock)
    else:
        deadline = Deadline(None if sync_budget_s is NO_DEADLINE else float(sync_budget_s), clock=clock)
    # Every refusal that needs no engine and no wait comes first: a bad clip
    # is a 400 now, not after 30 s of readiness.
    decoded_audio: Dict[Tuple[int, int], inline_files.DecodedAudio] = {}
    for ref in lifted.refs:
        if ref.part_type == "input_audio":
            decoded_audio[(ref.message_index, ref.part_index)] = inline_files.decode_input_audio(
                ref.audio, param=f"{ref.param}.input_audio"
            )
    if deadline.bounded and decoded_audio:
        ceiling = float(inline_files.audio_max_seconds())
        total = 0.0
        for audio in decoded_audio.values():
            total += audio.seconds if audio.seconds is not None else ceiling
            if total > ceiling + 0.5:
                raise _invalid(
                    f"A synchronous request may carry at most {int(ceiling)} seconds of input_audio in total "
                    "(an mp3 counts as the full limit, its length is unknown until it is decoded); "
                    "use stream: true or background for more.",
                    audio.param,
                )
    engines = engines.bounded(deadline)
    records, waited = await wait_until_ready(
        store, project_id, lifted, delivery=delivery, sync_wait_s=sync_wait_s,
        on_progress=on_progress, abandon=abandon, sleep=sleep, clock=clock, deadline=deadline,
    )
    media_files = {fid for fid, r in records.items() if r.kind in file_context.KINDS_MEDIA}
    media_refs = [r for r in lifted.refs if (r.file_id in media_files) or r.part_type == "input_audio"]
    if len(media_refs) > max_videos_per_request():
        raise _invalid(
            f"A request may carry at most {max_videos_per_request()} audio or video files.",
            media_refs[max_videos_per_request()].param,
        )

    resolved: List[file_context.ResolvedFile] = []
    workspace: Optional[inline_files.InlineWorkspace] = None
    audio_blocks: Dict[Tuple[int, int], Dict[str, Any]] = {}
    audio_seconds = 0.0
    try:
        for n, ref in enumerate(lifted.refs):
            if ref.part_type == "input_audio":
                transcript = await inline_files.transcribe_decoded(
                    decoded_audio[(ref.message_index, ref.part_index)], transcriber=engines.transcriber
                )
                audio_blocks[(ref.message_index, ref.part_index)] = transcript.block()
                audio_seconds += float(transcript.seconds or 0.0)
                continue
            if ref.file_data is not None:
                if workspace is None:
                    workspace = inline_files.InlineWorkspace(request_id)
                data_param = f"{ref.param}.file_data" if ref.part_type != "file" else f"{ref.param}.file.file_data"
                raw, _declared = inline_files.decode_file_data(ref.file_data, param=data_param)
                resolved.append(await inline_files.materialize_file_data(
                    raw=raw, filename=ref.filename or "upload", param=data_param, slot=n,
                    workspace=workspace, delivery=delivery, detail=ref.detail, extractor=engines.extractor,
                ))
                continue
            record = records[ref.file_id]
            _check_part_kind(ref, record)
            analysis = None
            if record.kind in file_context.KINDS_MEDIA and record.video_analysis_id is not None and engines.analysis_loader:
                analysis = await engines.analysis_loader(record.video_analysis_id)
            resolved.append(file_context.ResolvedFile(
                key=record.file_id,
                file_id=record.file_id,
                filename=ref.filename or record.filename,
                kind=record.kind,
                derived_dir=derived_dir_for(record.project_id, record.sha256) if record.sha256 else None,
                param=ref.id_param,
                detail=ref.detail,
                facts=record.facts,
                bytes=record.bytes,
                analysis=analysis,
            ))
        built = await file_context.build(
            resolved,
            caps=caps,
            mode=lifted.options.mode,
            max_tokens=lifted.options.max_tokens,
            question=lifted.question,
            caller_text_tokens=caller_text_tokens,
            caller_images=caller_images,
            deps=file_context.ContextDeps(
                embed_query=engines.embed_query,
                rerank=engines.rerank,
                render_pages=engines.render_pages,
                load_frame=engines.load_frame,
            ),
        )
    except BaseException:
        if workspace is not None:
            workspace.cleanup()
        raise
    return ModelInput(
        context=built, lifted=lifted, files=records, audio_blocks=audio_blocks,
        audio_seconds=audio_seconds, files_wait_s=waited, workspace=workspace, caps=caps,
    )


def _check_part_kind(ref: FilePartRef, record: FileRecord) -> None:
    """`input_image` must name an image; `input_video` audio or video."""
    if ref.part_type == "input_image" and record.kind != file_context.KIND_IMAGE:
        raise _invalid("input_image needs a file of kind image; use input_file for other files.", ref.id_param)
    if ref.part_type == "input_video" and record.kind not in file_context.KINDS_MEDIA:
        raise _invalid("input_video needs an audio or video file; use input_file for other files.", ref.id_param)


# ------------------------------------------------------------------ splicing --


def splice_messages(messages: Sequence[Mapping[str, Any]], model_input: ModelInput) -> List[Dict[str, Any]]:
    """The engine messages (`ResponsesRequest.chat_messages()` of the lifted
    payload) with each file part's blocks put back where the part was, and the
    citation rules appended to the LEADING system message — Qwen's template
    rejects a second or non-leading system turn."""
    lifted = model_input.lifted
    out = [dict(m) for m in messages]
    offset = 1 if lifted.instructions_present and lifted.dialect == DIALECT_RESPONSES else 0
    by_slot = {(r.message_index, r.part_index): (n, r) for n, r in enumerate(lifted.refs)}
    blocks_left = {key: list(parts) for key, parts in model_input.context.blocks.items()}
    for mi, original in lifted.original_content.items():
        target = mi + offset
        if target >= len(out):
            continue
        engine_content = out[target].get("content")
        images = [p for p in engine_content if isinstance(p, Mapping) and p.get("type") == "image_url"] if isinstance(engine_content, list) else []
        parts: List[Dict[str, Any]] = []
        for pi, part in enumerate(original):
            slot = by_slot.get((mi, pi))
            if slot is not None:
                n, ref = slot
                if ref.part_type == "input_audio":
                    block = model_input.audio_blocks.get((mi, pi))
                    if block:
                        parts.append(dict(block))
                    continue
                key = ref.file_id if ref.file_id is not None else f"inline:{n}"
                blocks = blocks_left.pop(key, None)
                if blocks is None and ref.file_id is not None:
                    parts.append({"type": "text", "text": f"(The file {ref.file_id} is attached above.)"})
                for block in blocks or []:
                    parts.append(dict(block))
                continue
            text = _text_of(part)
            if text is not None:
                parts.append({"type": "text", "text": text})
            elif isinstance(part, Mapping) and part.get("type") in ("input_image", "image_url") and images:
                parts.append(dict(images.pop(0)))
        if any(p.get("type") == "image_url" for p in parts):
            out[target] = {**out[target], "content": parts}
        else:
            out[target] = {**out[target], "content": "\n\n".join(p["text"] for p in parts)}
    caps = model_input.caps
    if caps is not None and caps.ocr and not lifted.caller_text:
        # planning's `_with_ocr_prompt` saw the lift's `(attached file)`
        # placeholder as caller text and skipped; the rebuilt turn has no
        # placeholder and no prompt. techsara-ocr loops garbage without the
        # "OCR" prompt (engines/ocr.py, 2026-09-11) and /health cannot see it.
        for i in range(len(out) - 1, -1, -1):
            content = out[i].get("content")
            if out[i].get("role") == "user" and isinstance(content, list) and any(
                isinstance(p, Mapping) and p.get("type") == "image_url" for p in content
            ):
                from ..publicapi import registry

                if not (content and isinstance(content[-1], Mapping) and content[-1].get("text") == registry.OCR_DEFAULT_PROMPT):
                    out[i] = {**out[i], "content": [*content, {"type": "text", "text": registry.OCR_DEFAULT_PROMPT}]}
                break
    addendum = model_input.context.system_addendum
    if addendum:
        if out and out[0].get("role") == "system" and isinstance(out[0].get("content"), str):
            out[0] = {**out[0], "content": f"{out[0]['content']}\n\n{addendum}"}
        else:
            out.insert(0, {"role": "system", "content": addendum})
    return out


# ----------------------------------------------------------------- output --


def annotate(text: str, model_input: ModelInput) -> cite.Annotated:
    return cite.annotate(text, model_input.context.citations)


@dataclass(frozen=True)
class PlanningInputs:
    """What `planning.plan_generation` adds for files."""

    file_tokens: int
    file_bounded_tokens: int
    file_images: int
    kinds: Tuple[str, ...]


def planning_inputs(model_input: Optional[ModelInput]) -> PlanningInputs:
    if model_input is None:
        return PlanningInputs(0, 0, 0, ())
    kinds = tuple(sorted({r.kind for r in model_input.files.values()}))
    return PlanningInputs(
        file_tokens=int(model_input.estimated_tokens),
        file_bounded_tokens=int(model_input.bounded_tokens),
        file_images=int(model_input.image_count),
        kinds=kinds,
    )
