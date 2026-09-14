"""The wire shapes of the Files API and its error codes (design §2.2, §2.17).

PARITY FIRST. `status` stays inside the SDK literal set (`uploaded`,
`processed`, `error`) so `client.files.wait_for_processing()` works unchanged;
everything TechSara adds (`sha256`, `mime_type`, `processing`) is an extra key
both SDKs parse leniently (verified by the parity research, 2026-09-13).

THE SEVEN FILES CODES are rows of the closed table in `publicapi/errors.py`
(CONTRACT §9) since 2026-09-14, so the OpenAPI `code` enum and the docs list
them. `FilesApiError` takes each code's status and wire type from that table
alone; `FILE_CODES` below restates them only beside the `x-should-retry`
default, which is the one thing the table does not hold, and
`tests/test_publicapi_files_publication.py` fails if the two ever disagree.
`status=` still overrides the table's status for the two `invalid_request_error`
answers that are not a 400 (411 without Content-Length, 416 for a range).

`x-should-retry` MATTERS. Both SDKs retry a 409 and a 503 by default. An upload
in the wrong state will be in the wrong state on the retry too, and a full disk
does not empty in the SDK's backoff, so those carry `x-should-retry: false`;
`incomplete_body` and a busy `complete` carry `true`.
"""
from __future__ import annotations

import posixpath
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional

from ...apifiles import limits, sniff
from .. import errors

# ------------------------------------------------------------------ errors --

#: code → (HTTP status, wire type, x-should-retry). Design §2.17.
FILE_CODES: Dict[str, tuple] = {
    "file_not_found": (404, "invalid_request_error", False),
    "upload_not_found": (404, "invalid_request_error", False),
    "file_not_ready": (409, "invalid_request_error", True),
    "upload_state_conflict": (409, "invalid_request_error", False),
    "checksum_mismatch": (400, "invalid_request_error", False),
    "incomplete_body": (408, "invalid_request_error", True),
    "storage_unavailable": (503, "api_error", False),
}


def _echoable(value: Optional[str]) -> Optional[str]:
    checker = getattr(errors, "_echoable", None)
    if checker is not None:
        return checker(value)
    value = (value or "").strip()  # pragma: no cover - errors always has it
    return value if re.match(r"^[A-Za-z0-9][A-Za-z0-9._:\-\[\]]{0,63}$", value) else None


class FilesApiError(errors.ApiError):
    """An `ApiError` whose code may be one of design §2.17's, with an optional
    `x-should-retry` and extra headers (`Content-Range` on a 416)."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: Optional[int] = None,
        param: Optional[str] = None,
        retry_after: Optional[float] = None,
        should_retry: Optional[bool] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        # The closed table is the authority for status and type (it raises
        # for a code it does not hold); FILE_CODES adds the retry default.
        spec_status, spec_type = errors.status_for(code), errors.type_for(code)
        default_retry = FILE_CODES[code][2] if code in FILE_CODES else None
        Exception.__init__(self, message)
        self.code = code
        self.status = int(status if status is not None else spec_status)
        self.type = spec_type
        self.message = str(message)
        self.param = _echoable(param)
        self.retry_after = None if retry_after is None else max(errors.MIN_RETRY_AFTER, int(-(-float(retry_after) // 1)))
        if self.status in (429, 503) and self.retry_after is None:
            raise ValueError("a 429/503 must carry Retry-After (CONTRACT §9)")
        self.should_retry = default_retry if should_retry is None else bool(should_retry)
        self.extra_headers = dict(extra_headers or {})

    def headers(self) -> Dict[str, str]:
        out = dict(super().headers())
        if self.should_retry is not None:
            out["x-should-retry"] = "true" if self.should_retry else "false"
        out.update(self.extra_headers)
        return out


def invalid_request(message: str, *, param: Optional[str] = None, status: int = 400) -> FilesApiError:
    return FilesApiError("invalid_request_error", message, param=param, status=status)


def file_not_found(param: Optional[str] = None) -> FilesApiError:
    """ONE sentence for absent, malformed, deleted, expired and another
    project's (design §2.5, §7.2 rule 2). The id is NOT echoed: echoing it
    would make the body for a foreign id differ from the body for a random
    one, which is exactly the comparison acceptance A-4 makes."""
    return FilesApiError("file_not_found", "No file with that id was found for this project.", param=param)


def upload_not_found() -> FilesApiError:
    return FilesApiError("upload_not_found", "No upload with that id was found for this project.")


def file_not_ready(retry_after: float = 5, message: Optional[str] = None) -> FilesApiError:
    return FilesApiError(
        "file_not_ready",
        message or "This file is still being processed. Retry after the Retry-After interval.",
        retry_after=retry_after,
    )


def upload_state_conflict(message: str, *, retry_after: Optional[float] = None, should_retry: bool = False) -> FilesApiError:
    return FilesApiError("upload_state_conflict", message, retry_after=retry_after, should_retry=should_retry)


def checksum_mismatch(param: str, message: Optional[str] = None) -> FilesApiError:
    return FilesApiError(
        "checksum_mismatch",
        message or f"The bytes received do not match the {param} you supplied. Send them again.",
        param=param,
    )


def incomplete_body() -> FilesApiError:
    return FilesApiError(
        "incomplete_body",
        "The connection closed before the request body was complete. Nothing was recorded; send it again.",
    )


def storage_unavailable(retry_after: float = 60) -> FilesApiError:
    return FilesApiError(
        "storage_unavailable",
        "The service cannot accept new file bytes right now. Try again later.",
        retry_after=min(60.0, float(retry_after)),
    )


def storage_busy(retry_after: float = 2) -> FilesApiError:
    """The project's previous copy of these exact bytes is still being purged
    (a DELETE moments ago). Transient, unlike a full disk: the same 503 code
    but `x-should-retry: true`, so both SDKs retry it by themselves (review
    finding, 2026-09-13 — it used to say `false`)."""
    return FilesApiError(
        "storage_unavailable",
        "A previous copy of this file is still being removed. Retry after the Retry-After interval.",
        retry_after=min(60.0, float(retry_after)),
        should_retry=True,
    )


def request_too_large(limit_bytes: int, message: Optional[str] = None) -> FilesApiError:
    return FilesApiError(
        "request_too_large",
        message or f"The request body is larger than the {int(limit_bytes)} byte limit.",
    )


def length_required() -> FilesApiError:
    return invalid_request(
        "This endpoint requires a Content-Length header.", param="Content-Length", status=411
    )


def range_not_satisfiable(size: int) -> FilesApiError:
    return FilesApiError(
        "invalid_request_error",
        "The requested range is not satisfiable.",
        status=416,
        param="Range",
        extra_headers={"Content-Range": f"bytes */{int(size)}"},
    )


# ------------------------------------------------------------------- input --

PURPOSES = ("user_data", "assistants", "vision")
#: Real OpenAI purposes this deployment has no product for — named so the 400
#: can say so instead of "invalid".
UNSUPPORTED_PURPOSES = ("batch", "batch_output", "fine-tune", "fine-tune-results", "evals", "assistants_output")

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff]")


def is_encodable(text: str) -> bool:
    """False for a string UTF-8 cannot encode — a lone surrogate, which JSON's
    `\\ud800` escape produces. Such a string passed every check and then
    failed inside psycopg as a 500 (review finding, 2026-09-13)."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


_ASCII_DIGITS_RE = re.compile(r"[0-9]{1,19}")


def ascii_int(text: Optional[str]) -> Optional[int]:
    """A non-negative integer written with ASCII digits only, else None.
    `str.isdigit()` accepts '²' and '٣', which `int()` then rejects or
    accepts inconsistently — a 500 on a caller's input (review, 2026-09-13)."""
    if text is None:
        return None
    value = str(text).strip()
    if not _ASCII_DIGITS_RE.fullmatch(value):
        return None
    return int(value)


def normalize_filename(raw: Optional[str]) -> str:
    """NFC, control and bidi-override characters stripped, basename only (both
    separators), at most 255 characters keeping the extension, blank → `upload`
    (design §2.3). The result is metadata: it is never a path on disk."""
    if not is_encodable(str(raw or "")):
        raise invalid_request("filename must be valid Unicode text.", param="filename")
    text = unicodedata.normalize("NFC", str(raw or ""))
    text = _CONTROL_RE.sub("", text)
    text = text.replace("\\", "/")
    text = posixpath.basename(text).strip()
    if text in ("", ".", ".."):
        return "upload"
    if len(text) > 255:
        stem, dot, ext = text.rpartition(".")
        if dot and 0 < len(ext) <= 16 and stem:
            text = stem[: 255 - len(ext) - 1] + "." + ext
        else:
            text = text[:255]
    return text


def parse_purpose(value: Any, *, param: str = "purpose") -> str:
    if not isinstance(value, str) or not value:
        raise invalid_request("purpose is required and must be user_data.", param=param)
    if value in PURPOSES:
        return value
    if value in UNSUPPORTED_PURPOSES:
        raise invalid_request(
            "This deployment has no batch, fine-tuning or evals product; use `user_data`.", param=param
        )
    raise invalid_request("purpose must be one of user_data, assistants or vision.", param=param)


EXPIRES_MIN_S = 3600
EXPIRES_MAX_S = 2_592_000


def parse_expires_after(anchor: Any, seconds: Any, *, param: str = "expires_after") -> Optional[int]:
    """`{anchor: "created_at", seconds: 3600…2592000}` → seconds, or None when
    neither is given. One without the other is a 400."""
    if anchor is None and seconds is None:
        return None
    if anchor != "created_at":
        raise invalid_request("expires_after.anchor must be created_at.", param=f"{param}.anchor")
    try:
        if isinstance(seconds, bool):
            raise ValueError
        value = int(seconds) if not isinstance(seconds, str) else int(seconds.strip())
        if isinstance(seconds, float) and not float(seconds).is_integer():
            raise ValueError
    except (TypeError, ValueError):
        raise invalid_request("expires_after.seconds must be an integer.", param=f"{param}.seconds") from None
    if not EXPIRES_MIN_S <= value <= EXPIRES_MAX_S:
        raise invalid_request(
            "expires_after.seconds must be between 3600 and 2592000.", param=f"{param}.seconds"
        )
    return value


# ----------------------------------------------------------------- objects --


def epoch(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp())
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(datetime.fromisoformat(str(value)).timestamp())
    except ValueError:
        return None


#: design §4.6 — fixed sentences, never exception text or an engine name.
STATUS_DETAILS = {
    "unsupported_file": "This file type cannot be used as model input.",
    "file_corrupt": "The file could not be read; it may be damaged or encrypted.",
    "file_too_complex": "The file exceeds a processing ceiling.",
    "processing_unavailable": (
        "Processing could not reach a required service after several attempts. Upload the file again later."
    ),
    "internal_error": "Something went wrong while processing this file.",
    "checksum_mismatch": "The assembled bytes did not match the checksum you supplied.",
    "assembly_failed": "The uploaded parts could not be assembled. Upload the file again.",
}

_STAGE_STATUSES = ("pending", "running", "done", "skipped", "failed")


def _stage_list(kind: str, stages: Mapping[str, Any], current: str, state: str) -> List[dict]:
    names = sniff.STAGES_BY_KIND.get(kind) or sniff.STAGES_BY_KIND["unknown"]
    out = []
    for name in names:
        entry = stages.get(name) if isinstance(stages, Mapping) else None
        status = "pending"
        percent = None
        if isinstance(entry, Mapping):
            status = str(entry.get("status") or "pending")
            if entry.get("percent") is not None:
                percent = int(entry["percent"])
        elif name == current and state == "processing":
            status = "running"
        item: Dict[str, Any] = {"name": name, "status": status if status in _STAGE_STATUSES else "pending"}
        if percent is not None:
            item["percent"] = max(0, min(100, percent))
        out.append(item)
    return out


def default_processing_view(row: Mapping[str, Any]) -> Dict[str, Any]:
    """`{status, status_details, processing}` for a file row joined with its
    blob (`schema._FILE_SELECT`). Renders stage NAMES, statuses and percents
    only — never `detail` text, engine names or paths (design §10.1). Team B's
    `jobs.processing_view` replaces this through `FilesDependencies`."""
    file_error = row.get("error_code")
    if file_error:
        sentence = STATUS_DETAILS["checksum_mismatch"] if file_error == "checksum_mismatch" else STATUS_DETAILS["assembly_failed"]
        return {
            "status": "error",
            "status_details": sentence,
            "processing": {
                "state": "failed", "kind": "unknown", "stage": "assemble", "step": 0, "total_steps": None,
                "percent": None, "stages": [{"name": "assemble", "status": "failed"}], "queue_position": None,
                "waited_for_capacity_s": 0.0, "started_at": None, "finished_at": None,
                "error": {"code": file_error, "message": sentence}, "facts": {}, "derived": [],
            },
        }
    if row.get("blob_id") is None:
        total = int(row.get("bytes") or 0)
        done = int(row.get("assembly_bytes_done") or 0)
        percent = 100 if total == 0 else max(0, min(99, int(done * 100 / total)))
        return {
            "status": "uploaded",
            "status_details": None,
            "processing": {
                "state": "processing", "kind": "unknown", "stage": "assemble", "step": 0, "total_steps": None,
                "percent": percent, "stages": [{"name": "assemble", "status": "running", "percent": percent}],
                "queue_position": None, "waited_for_capacity_s": 0.0,
                "started_at": epoch(row.get("assembly_started_at")), "finished_at": None,
                "error": None, "facts": {}, "derived": [],
            },
        }
    kind = str(row.get("blob_kind") or "unknown")
    blob_status = str(row.get("blob_status") or "queued")
    state = {"queued": "queued", "processing": "processing", "processed": "processed", "failed": "failed"}.get(blob_status, "processing")
    stage = str(row.get("blob_stage") or "sniff")
    names = sniff.STAGES_BY_KIND.get(kind) or sniff.STAGES_BY_KIND["unknown"]
    step = names.index(stage) + 1 if stage in names else None
    progress = row.get("blob_progress") or {}
    percent = progress.get("percent") if isinstance(progress, Mapping) else None
    if state == "processed":
        percent = 100
    error_code = row.get("blob_error_code")
    status = {"processed": "processed", "failed": "error"}.get(state, "uploaded")
    details = STATUS_DETAILS.get(str(error_code)) if state == "failed" else None
    if state == "failed" and details is None:
        details = STATUS_DETAILS["internal_error"]
    facts = row.get("blob_facts") if isinstance(row.get("blob_facts"), Mapping) else {}
    return {
        "status": status,
        "status_details": details,
        "processing": {
            "state": state,
            "kind": kind,
            "stage": stage,
            "step": step,
            "total_steps": len(names),
            "percent": None if percent is None else max(0, min(100, int(percent))),
            "stages": _stage_list(kind, row.get("blob_stages") or {}, stage, state),
            "queue_position": None,
            "waited_for_capacity_s": float((progress or {}).get("waited_for_capacity_s") or 0.0) if isinstance(progress, Mapping) else 0.0,
            "started_at": epoch(row.get("blob_started_at")),
            "finished_at": epoch(row.get("blob_processed_at")),
            "error": None if state != "failed" else {"code": str(error_code or "internal_error"), "message": details},
            "facts": dict(facts),
            "derived": [],
        },
    }


ProcessingView = Callable[[Mapping[str, Any]], Dict[str, Any]]


def file_object(row: Mapping[str, Any], *, processing_view: Optional[ProcessingView] = None) -> Dict[str, Any]:
    """The File object of design §2.2 from a joined file row."""
    view = (processing_view or default_processing_view)(row)
    mime = row.get("blob_mime_type")
    return {
        "id": row["id"],
        "object": "file",
        "bytes": int(row.get("bytes") or 0),
        "created_at": epoch(row.get("created_at")),
        "filename": row.get("filename") or "upload",
        "purpose": row.get("purpose") or "user_data",
        "status": view["status"],
        "status_details": errors.redact(view["status_details"]) if view.get("status_details") else None,
        "expires_at": epoch(row.get("expires_at")),
        "sha256": row.get("blob_sha256"),
        "mime_type": mime if row.get("blob_id") else None,
        "processing": view["processing"],
    }


def upload_status(row: Mapping[str, Any]) -> str:
    if row.get("status") == "pending" and row.get("lapsed"):
        return "expired"
    return str(row.get("status") or "pending")


def upload_object(
    row: Mapping[str, Any],
    *,
    file: Optional[Dict[str, Any]] = None,
    parts: Optional[List[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """The Upload object (§2.10); with `parts` it is the resume view (§2.13)."""
    body: Dict[str, Any] = {
        "id": row["id"],
        "object": "upload",
        "bytes": int(row.get("bytes") or 0),
        "created_at": epoch(row.get("created_at")),
        "expires_at": epoch(row.get("expires_at")),
        "filename": row.get("filename"),
        "purpose": row.get("purpose"),
        "status": upload_status(row),
        "file": file,
        "part_max_bytes": limits.part_max_bytes(),
        "max_parts": limits.max_parts(),
        "bytes_received": int(row.get("bytes_received") or 0),
    }
    if parts is not None:
        upload_id = str(row["id"])
        body["parts"] = [part_object(part, upload_id) for part in parts]
        body["part_mode"] = row.get("part_mode")
        body["error"] = (
            {"code": row.get("error_code"), "message": errors.redact(row.get("error_message") or "")}
            if row.get("error_code")
            else None
        )
    return body


def part_object(row: Mapping[str, Any], upload_id: str) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "object": "upload.part",
        "created_at": epoch(row.get("created_at")),
        "upload_id": upload_id,
        "part_number": int(row["part_number"]),
        "bytes": int(row["bytes"]),
        "sha256": row.get("sha256"),
    }


def list_object(data: List[Dict[str, Any]], has_more: bool) -> Dict[str, Any]:
    return {
        "object": "list",
        "data": data,
        "has_more": bool(has_more),
        "first_id": data[0]["id"] if data else None,
        "last_id": data[-1]["id"] if data else None,
    }


def deleted_object(file_id: str) -> Dict[str, Any]:
    return {"id": file_id, "object": "file", "deleted": True}
