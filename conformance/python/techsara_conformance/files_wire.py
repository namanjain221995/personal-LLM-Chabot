"""The Files / Uploads wire, asserted one way (2026-09-13).

WHAT THIS PINS. The objects and error codes as BUILT in
`orchestrator/app/publicapi/files/wire.py` (File, Upload, UploadPart, the
seven new codes), which follow the Files API design's route table and wire
sections. `asserts.STATUS_FOR` is CONTRACT-3's closed table as it stands; the
Files codes are not in it yet, so they live here until CONTRACT.md lists them.

WHAT THE SUITES EXERCISE ON THE WIRE (2026-09-13). `file_not_found`,
`upload_not_found`, `upload_state_conflict` and `checksum_mismatch` are
asserted deterministically. `file_not_ready` is asserted only when a content
read races assembly, which small parts usually win. `incomplete_body` (408)
and `storage_unavailable` (503) are tabled here but never provoked: a client
cannot make a server see a cut body deterministically (uvicorn drops the
buffered bytes on disconnect), nor fill its disk.

WHY `x-should-retry` IS ASSERTED. Both official SDKs retry a 408, 409 and
503 by default. An upload in the wrong state stays in the wrong state, so the
server says `x-should-retry: false` there, and `true` where a retry is the
cure (a body cut off mid-transfer, a file still processing). Without the
header an SDK hammers a permanent refusal with retries.
"""
from __future__ import annotations

import os
import re
from typing import Any, Mapping, Optional

from techsara_conformance import asserts

FILE_ID_RE = re.compile(r"^file-[0-9a-f]{24}$")
UPLOAD_ID_RE = re.compile(r"^upload_[0-9a-f]{24}$")
PART_ID_RE = re.compile(r"^part_[0-9a-f]{24}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

#: `status` stays inside the SDK literal set so wait_for_processing works.
FILE_STATUSES = {"uploaded", "processed", "error"}
PROCESSING_STATES = {"queued", "processing", "processed", "failed"}
STAGE_STATUSES = {"pending", "running", "done", "skipped", "failed"}
UPLOAD_STATUSES = {"pending", "finalizing", "completed", "cancelled", "expired", "failed"}

#: code -> (HTTP status, error type).
FILE_CODES = {
    "file_not_found": (404, "invalid_request_error"),
    "upload_not_found": (404, "invalid_request_error"),
    "file_not_ready": (409, "invalid_request_error"),
    "upload_state_conflict": (409, "invalid_request_error"),
    "checksum_mismatch": (400, "invalid_request_error"),
    "incomplete_body": (408, "invalid_request_error"),
    "storage_unavailable": (503, "api_error"),
}
#: `invalid_request_error` also answers 411 (a raw part without
#: Content-Length) and 416 (an unsatisfiable Range) on the file routes.
INVALID_REQUEST_STATUSES = {400, 411, 416}


def envelope(
    status: int,
    body: Any,
    headers: Mapping[str, str],
    *,
    code: str,
    param: Optional[str] = "any",
    should_retry: Optional[bool] = None,
) -> Mapping[str, Any]:
    """The CONTRACT-3 §9 envelope for a Files code (or any closed-table code)."""
    assert isinstance(body, Mapping) and set(body) == {"error"}, f"not the §9 envelope: {body!r}"
    error = body["error"]
    assert set(error) == asserts.ENVELOPE_KEYS, f"envelope keys {sorted(error)} != {sorted(asserts.ENVELOPE_KEYS)}"
    assert error["code"] == code, f"code {error['code']!r} (HTTP {status}, message {error['message']!r}), expected {code!r}"
    if code in FILE_CODES:
        want_status, want_type = FILE_CODES[code]
        assert status == want_status, f"HTTP {status} for {code}, the Files API answers {want_status}"
        assert error["type"] == want_type, f"type {error['type']!r} for {code}, expected {want_type!r}"
    elif code == "invalid_request_error":
        assert status in INVALID_REQUEST_STATUSES, f"HTTP {status} for invalid_request_error"
    else:
        assert status == asserts.STATUS_FOR[code], f"HTTP {status} for {code}, contract says {asserts.STATUS_FOR[code]}"
    assert isinstance(error["message"], str) and error["message"], "empty message"
    request_id = headers.get("x-request-id")
    assert error["request_id"] and error["request_id"] == request_id, (
        f"request_id {error['request_id']!r} != X-Request-Id {request_id!r}"
    )
    if param != "any":
        assert error["param"] == param, f"param {error['param']!r}, expected {param!r} (message {error['message']!r})"
    for leak in asserts._LEAKS:
        assert leak not in error["message"], f"internal detail {leak!r} in an error message: {error['message']!r}"
    if should_retry is not None:
        wanted = "true" if should_retry else "false"
        assert headers.get("x-should-retry") == wanted, (
            f"x-should-retry {headers.get('x-should-retry')!r} on {code}, expected {wanted!r}: both SDKs retry "
            "408/409/503 by default, and only this header stops (or licenses) that"
        )
    return error


def sdk_error(exc: Any, *, code: str, param: Optional[str] = "any", should_retry: Optional[bool] = None) -> Mapping[str, Any]:
    """`envelope` for an `openai.APIStatusError`."""
    response = exc.response
    try:
        body = response.json()
    except ValueError:  # pragma: no cover - a non-JSON error is itself the failure
        raise AssertionError(f"HTTP {response.status_code} error body is not JSON: {response.text[:200]!r}")
    error = envelope(response.status_code, body, response.headers, code=code, param=param, should_retry=should_retry)
    assert exc.code == code, f"the SDK read code {exc.code!r} from the envelope, expected {code!r}"
    return error


#: Which form a refusal made before any body byte is read (411, 413) must take
#: on this run: `edge` (through the v1-gateway), `origin` (the orchestrator
#: reached directly), or unset — either is accepted and the test's failure
#: message names the one it saw. Set it whenever the path is known: a run that
#: pins it catches either side drifting toward the other's form (2026-09-13,
#: review finding).
_EDGE_REFUSALS_VAR = "TECHSARA_FILES_EDGE_REFUSALS"


def expected_refusal_form() -> Optional[str]:
    value = (os.environ.get(_EDGE_REFUSALS_VAR) or "").strip().lower() or None
    assert value in (None, "edge", "origin"), f"{_EDGE_REFUSALS_VAR}={value!r}: use edge, origin or leave it unset"
    return value


def pre_body_refusal(
    status: int,
    body: Any,
    headers: Mapping[str, str],
    *,
    want_status: int,
    code: str,
    origin_param: Optional[str],
    mentions: Optional[str] = None,
) -> str:
    """A refusal given before the body is read, in one of the two forms the
    /v1 surface is built to give. Returns which.

    * `origin` — the orchestrator's handler: the §9 envelope with `param`
      `origin_param` and a `request_id` equal to X-Request-Id.
    * `edge` — the v1-gateway refuses it before any byte reaches the
      orchestrator (`gateway/lib/headers.cjs` `edgeError`): `param: null`,
      `request_id: null` and NO X-Request-Id, the position route.ts's
      `edgeError` documents for every edge refusal (an id that exists in no
      other system sends a support conversation looking for a request that was
      never recorded).

    WHY TWO (2026-09-13): production /v1 traffic reaches the orchestrator
    through the gateway, a direct run does not; which one a deployment must
    give is pinned per run with TECHSARA_FILES_EDGE_REFUSALS."""
    assert status == want_status, f"HTTP {status}, expected {want_status} (body {body!r})"
    assert isinstance(body, Mapping) and set(body) == {"error"}, f"not the §9 envelope: {body!r}"
    error = body["error"]
    assert set(error) == asserts.ENVELOPE_KEYS, f"envelope keys {sorted(error)} != {sorted(asserts.ENVELOPE_KEYS)}"
    assert error["code"] == code and error["type"] == "invalid_request_error", error
    assert isinstance(error["message"], str) and error["message"], error
    if mentions is not None:
        assert mentions in error["message"], error["message"]
    form = "edge" if error["request_id"] is None else "origin"
    wanted = expected_refusal_form()
    assert wanted in (None, form), (
        f"the {want_status} came in the {form} form, but {_EDGE_REFUSALS_VAR}={wanted} "
        f"(param {error['param']!r}, request_id {error['request_id']!r}, X-Request-Id {headers.get('x-request-id')!r})"
    )
    if form == "edge":
        assert error["param"] is None and not headers.get("x-request-id"), (
            f"an edge refusal carries no request id anywhere: param {error['param']!r}, "
            f"X-Request-Id {headers.get('x-request-id')!r}"
        )
    else:
        envelope(status, body, headers, code=code, param=origin_param)
    return form


def length_required(status: int, body: Any, headers: Mapping[str, str]) -> str:
    """A raw part PUT without Content-Length: 411 `invalid_request_error`
    (`wire.length_required` at the origin names `Content-Length`)."""
    return pre_body_refusal(status, body, headers, want_status=411, code="invalid_request_error",
                            origin_param="Content-Length", mentions="Content-Length")


def part_too_large(status: int, body: Any, headers: Mapping[str, str]) -> str:
    """A raw part PUT declaring more than a part may hold: 413
    `request_too_large` (`wire.request_too_large` at the origin, `param` null)."""
    return pre_body_refusal(status, body, headers, want_status=413, code="request_too_large", origin_param=None)


def insufficient_scope(exc: Any, scope: str) -> Mapping[str, Any]:
    """403 `insufficient_scope` whose sentence names the ONE scope the route
    needs (CONTRACT-3 §7: each route requires exactly its one scope)."""
    error = sdk_error(exc, code="insufficient_scope")
    assert exc.status_code == 403, exc.status_code
    assert f"`{scope}`" in error["message"], f"the refusal should name `{scope}`: {error['message']!r}"
    return error


def comparable(error: Mapping[str, Any]) -> dict:
    """An error minus its request_id: what must be identical for a foreign id
    and an id that never existed (no existence oracle)."""
    return {k: v for k, v in error.items() if k != "request_id"}


def as_dict(obj: Any) -> dict:
    """An SDK model (with its extra keys) or a raw dict, as a dict."""
    if isinstance(obj, Mapping):
        return dict(obj)
    dump = getattr(obj, "model_dump", None)
    if dump is None:
        raise AssertionError(f"not an object: {obj!r}")
    return dump()


def assert_file(
    obj: Any,
    *,
    bytes: Optional[int] = None,
    filename: Optional[str] = None,
    purpose: Optional[str] = None,
    sha256: Optional[str] = None,
) -> dict:
    """A File object (the design's File object section)."""
    body = as_dict(obj)
    assert FILE_ID_RE.fullmatch(str(body.get("id"))), f"file id {body.get('id')!r} is not file-<24 hex>"
    assert body.get("object") == "file", body
    assert isinstance(body.get("bytes"), int) and isinstance(body.get("created_at"), int), body
    assert body.get("status") in FILE_STATUSES, f"status {body.get('status')!r} is outside the SDK literal set"
    assert "status_details" in body and "expires_at" in body, f"missing parity keys: {sorted(body)}"
    processing = body.get("processing")
    assert isinstance(processing, Mapping), f"no processing object: {body}"
    assert processing.get("state") in PROCESSING_STATES, processing
    stages = processing.get("stages")
    assert isinstance(stages, list) and stages, processing
    for stage in stages:
        assert isinstance(stage.get("name"), str) and stage.get("status") in STAGE_STATUSES, stage
    if body["status"] == "processed":
        assert processing["state"] == "processed", processing
    if body["status"] == "error":
        assert processing["state"] == "failed" and isinstance(body.get("status_details"), str), body
    if bytes is not None:
        assert body["bytes"] == bytes, f"bytes {body['bytes']} != {bytes}"
    if filename is not None:
        assert body.get("filename") == filename, body.get("filename")
    if purpose is not None:
        assert body.get("purpose") == purpose, body.get("purpose")
    if sha256 is not None:
        assert body.get("sha256") == sha256, f"sha256 {body.get('sha256')!r} != {sha256}"
    elif body.get("sha256") is not None:
        assert SHA256_RE.fullmatch(body["sha256"]), body["sha256"]
    return body


def assert_upload(obj: Any, *, status: Optional[str] = None, bytes: Optional[int] = None) -> dict:
    """An Upload object (and, with `parts`, the resume view)."""
    body = as_dict(obj)
    assert UPLOAD_ID_RE.fullmatch(str(body.get("id"))), f"upload id {body.get('id')!r} is not upload_<24 hex>"
    assert body.get("object") == "upload", body
    assert body.get("status") in UPLOAD_STATUSES, body.get("status")
    assert isinstance(body.get("created_at"), int), body
    assert "file" in body, f"no file key: {sorted(body)}"
    if status is not None:
        assert body["status"] == status, f"upload status {body['status']!r}, expected {status!r}"
    if bytes is not None:
        assert body.get("bytes") == bytes, f"upload bytes {body.get('bytes')} != {bytes}"
    return body


def assert_part(
    obj: Any,
    *,
    upload_id: str,
    part_number: Optional[int] = None,
    bytes: Optional[int] = None,
    sha256: Optional[str] = None,
) -> dict:
    """An `upload.part` object. `part_number` is 0-based."""
    body = as_dict(obj)
    assert PART_ID_RE.fullmatch(str(body.get("id"))), f"part id {body.get('id')!r} is not part_<24 hex>"
    assert body.get("object") == "upload.part", body
    assert body.get("upload_id") == upload_id, body
    assert isinstance(body.get("part_number"), int) and body["part_number"] >= 0, body
    if part_number is not None:
        assert body["part_number"] == part_number, f"part_number {body['part_number']} != {part_number}"
    if bytes is not None:
        assert body.get("bytes") == bytes, f"part bytes {body.get('bytes')} != {bytes}"
    if sha256 is not None:
        assert body.get("sha256") == sha256, f"part sha256 {body.get('sha256')!r} != {sha256}"
    return body


def text_payload(size: int, tag: str) -> bytes:
    """Exactly `size` bytes of readable text. Text, not random bytes: random
    bytes sniff as an unsupported kind and end processing in `error`, which
    would make every wait_for_processing assertion about something else."""
    out = bytearray()
    line = 0
    while len(out) < size:
        out += f"conformance {tag} line {line:08d}: the quick brown fox jumps over the lazy dog.\n".encode()
        line += 1
    return bytes(out[:size])
