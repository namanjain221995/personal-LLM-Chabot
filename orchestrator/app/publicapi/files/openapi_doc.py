"""The Files API in the public OpenAPI document (files-hookup, 2026-09-13).

WHY THIS EXISTS. `tests/test_publicapi_surface.py` holds the line that the
router serves exactly the operations the document describes: an undocumented
route is attack surface nobody reviewed, a documented one the router lacks is a
promise that 404s. Mounting `/v1/files` and `/v1/uploads` therefore comes WITH
their description, and `openapi.public_openapi` adds it exactly when
`router.FILES_MOUNTED` is true.

WHAT IT DESCRIBES, from the code rather than from the design: the operation ids
`publicapi/files/routes.py` registers, the scopes `routes.SCOPE_READ` /
`SCOPE_WRITE` enforce with `scopes.SCOPE_DESCRIPTIONS`' sentences, the caps in
`apifiles/limits.py`, the error codes of `files/wire.FILE_CODES`, and the model
input parts `apifiles/service.lift_file_parts` accepts. It deliberately names
no engine, path or internal service.
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List, Tuple

from ...apiplatform.scopes import SCOPE_DESCRIPTIONS, Scope
from . import wire

_FILE_REF = "#/components/schemas/File"

TAGS: List[Dict[str, str]] = [
    {"name": "Files", "description": "Store a file once and use it as model input by id."},
    {"name": "Uploads", "description": "Upload a large file in resumable, checksummed parts."},
]


def _scope(scope: Scope) -> str:
    return f"Scope `{scope.value}` — {SCOPE_DESCRIPTIONS[scope]}"


def _errors(base: Any, *codes: str) -> Dict[str, Any]:
    """`openapi._error_responses` for `codes`, with the Files headers added.

    Every Files code is a row of the closed table since 2026-09-14, so the
    statuses come from `errors.status_for` like every other operation's. What
    the base helper does not know is `x-should-retry`: a Files 408, 409 or 503
    says through it whether the same request can succeed later (both SDKs
    obey it before their own status table), so those statuses document it,
    with `Retry-After` optional where the base made it required only for
    429/503."""
    out = base._error_responses(*base._always(), *codes)
    for code in codes:
        if code not in wire.FILE_CODES:
            continue
        entry = out[str(wire.FILE_CODES[code][0])]
        if code not in entry["description"]:
            entry["description"] = f"{entry['description']}; {code}"
        if str(wire.FILE_CODES[code][0]) in ("408", "409", "503"):
            headers = dict(entry.get("headers") or {})
            headers.setdefault("Retry-After", {
                "description": "Seconds to wait before retrying.", "required": False,
                "schema": {"type": "integer", "minimum": 1},
            })
            headers["x-should-retry"] = {
                "description": "`true` or `false`: whether sending the same request again can succeed.",
                "required": False, "schema": {"type": "string", "enum": ["true", "false"]},
            }
            entry["headers"] = headers
    return dict(sorted(out.items()))


def _ok(base: Any, ref: str, description: str = "OK") -> Dict[str, Any]:
    return {
        "description": description,
        "headers": base._request_id_header(),
        "content": {"application/json": {"schema": {"$ref": ref}}},
    }


def _path_param(name: str, example: str) -> Dict[str, Any]:
    return {"name": name, "in": "path", "required": True, "schema": {"type": "string"}, "example": example}


def _operation(base: Any, *, tag: str, operation_id: str, summary: str, scope: Scope, description: str,
               responses: Dict[str, Any], parameters: List[Dict[str, Any]] = (), request_body: Any = None) -> Dict[str, Any]:
    op: Dict[str, Any] = {
        "tags": [tag],
        "operationId": operation_id,
        "summary": summary,
        "description": f"{_scope(scope)}\n\n{description}",
        "security": [{"bearerAuth": []}],
        "responses": responses,
    }
    if parameters:
        op["parameters"] = list(parameters)
    if request_body is not None:
        op["requestBody"] = request_body
    return op


def paths(base: Any) -> Dict[str, Any]:
    from ...apifiles import limits

    part_max = limits.part_max_bytes()
    single_max = limits.single_max_bytes()
    file_id = _path_param("file_id", "file-6f1c2a9e0b7d4c3a8e5f1b2c")
    upload_id = _path_param("upload_id", "upload_8a0d3e5b7c9f1a2b4c6d8e0f")
    read, write = Scope.FILES_READ, Scope.FILES_WRITE
    not_found = "Absent, deleted, expired and another project's ids are the same 404 `file_not_found`."
    return {
        "/v1/files": {
            "post": _operation(
                base, tag="Files", operation_id="createFile", summary="Upload a file",
                scope=write,
                description=(
                    f"`multipart/form-data` with `file` and `purpose` (`user_data`, `assistants` or `vision`), "
                    f"optionally `expires_after[anchor]=created_at` and `expires_after[seconds]`. At most "
                    f"{single_max} bytes in one request; larger files use `/v1/uploads`. The file is processed "
                    "after it is stored: read `status` / `processing`, or follow `/v1/files/{file_id}/events`."
                ),
                request_body={"required": True, "content": {"multipart/form-data": {"schema": {"$ref": "#/components/schemas/FileCreateRequest"}}}},
                responses={"200": _ok(base, _FILE_REF), **_errors(base, "invalid_request_error", "request_too_large", "storage_unavailable", "incomplete_body")},
            ),
            "get": _operation(
                base, tag="Files", operation_id="listFiles", summary="List files", scope=read,
                description="Newest first by default. `after` is a file id from a previous page.",
                parameters=[
                    {"name": "after", "in": "query", "required": False, "schema": {"type": "string"}},
                    {"name": "limit", "in": "query", "required": False,
                     "schema": {"type": "integer", "minimum": 1, "maximum": limits.list_max_limit()}},
                    {"name": "order", "in": "query", "required": False, "schema": {"type": "string", "enum": ["asc", "desc"]}},
                    {"name": "purpose", "in": "query", "required": False, "schema": {"type": "string"}},
                ],
                responses={"200": _ok(base, "#/components/schemas/FileList"), **_errors(base, "invalid_request_error")},
            ),
        },
        "/v1/files/{file_id}": {
            "get": _operation(
                base, tag="Files", operation_id="retrieveFile", summary="Read a file", scope=read,
                description=not_found, parameters=[file_id],
                responses={"200": _ok(base, _FILE_REF), **_errors(base, "file_not_found")},
            ),
            "delete": _operation(
                base, tag="Files", operation_id="deleteFile", summary="Delete a file", scope=write,
                description="The bytes, derived data and indexes go at once; processing stops. " + not_found,
                parameters=[file_id],
                responses={"200": _ok(base, "#/components/schemas/FileDeleted"), **_errors(base, "file_not_found")},
            ),
        },
        "/v1/files/{file_id}/content": {
            "get": _operation(
                base, tag="Files", operation_id="downloadFile", summary="Download a file's bytes", scope=read,
                description=(
                    "Always `application/octet-stream` as an attachment. Honours `Range` (206, or 416) and "
                    "`If-None-Match` (304 against the `ETag`). " + not_found
                ),
                parameters=[file_id,
                            {"name": "Range", "in": "header", "required": False, "schema": {"type": "string"}},
                            {"name": "If-None-Match", "in": "header", "required": False, "schema": {"type": "string"}}],
                responses={
                    "200": {"description": "The bytes.", "headers": base._request_id_header(),
                            "content": {"application/octet-stream": {"schema": {"type": "string", "contentMediaType": "application/octet-stream"}}}},
                    "206": {"description": "The requested range.",
                            "content": {"application/octet-stream": {"schema": {"type": "string", "contentMediaType": "application/octet-stream"}}}},
                    "304": {"description": "Not modified."},
                    "416": {"description": "The range is not satisfiable.",
                            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorEnvelope"}}}},
                    **_errors(base, "file_not_found"),
                },
            ),
        },
        "/v1/files/{file_id}/events": {
            "get": _operation(
                base, tag="Files", operation_id="streamFileEvents", summary="Follow a file's processing", scope=read,
                description=(
                    "`text/event-stream`: numbered `file.processing` events, then exactly one of `file.processed` or "
                    "`file.failed`; a `: ping` comment at least every 15 seconds. " + not_found
                ),
                parameters=[file_id],
                responses={
                    "200": {"description": "The event stream.", "headers": base._request_id_header(),
                            "content": {"text/event-stream": {"schema": {"type": "string"}}}},
                    **_errors(base, "file_not_found"),
                },
            ),
        },
        "/v1/files/{file_id}/derived": {
            "get": _operation(
                base, tag="Files", operation_id="listFileDerived", summary="List a file's derived outputs", scope=read,
                description="Transcripts, extracted text and profiles, by name, once the file is `processed` (else `409 file_not_ready`).",
                parameters=[file_id],
                responses={"200": _ok(base, "#/components/schemas/DerivedList"), **_errors(base, "file_not_found", "file_not_ready")},
            ),
        },
        "/v1/files/{file_id}/derived/{name}": {
            "get": _operation(
                base, tag="Files", operation_id="downloadFileDerived", summary="Download a derived output", scope=read,
                description="A name from the derived list. `Range` is honoured.",
                parameters=[file_id, _path_param("name", "text.txt")],
                responses={
                    "200": {"description": "The bytes.", "headers": base._request_id_header(),
                            "content": {"application/octet-stream": {"schema": {"type": "string", "contentMediaType": "application/octet-stream"}}}},
                    "206": {"description": "The requested range."},
                    **_errors(base, "file_not_found", "file_not_ready"),
                },
            ),
        },
        "/v1/uploads": {
            "post": _operation(
                base, tag="Uploads", operation_id="createUpload", summary="Start a resumable upload", scope=write,
                description=(
                    f"Declare `bytes`, `filename`, `mime_type` and `purpose`; then send parts of at most {part_max} "
                    f"bytes, up to {limits.max_parts()} of them and {limits.upload_max_bytes()} bytes in all."
                ),
                request_body={"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/UploadCreateRequest"}}}},
                responses={"200": _ok(base, "#/components/schemas/Upload"), **_errors(base, "invalid_request_error", "storage_unavailable")},
            ),
        },
        "/v1/uploads/{upload_id}": {
            "get": _operation(
                base, tag="Uploads", operation_id="retrieveUpload", summary="Read an upload and its parts (resume)", scope=write,
                description="The parts the server holds, so a client that crashed resumes from the first missing one.",
                parameters=[upload_id],
                responses={"200": _ok(base, "#/components/schemas/Upload"), **_errors(base, "upload_not_found")},
            ),
        },
        "/v1/uploads/{upload_id}/parts": {
            "post": _operation(
                base, tag="Uploads", operation_id="addUploadPart", summary="Add a part", scope=write,
                description="`multipart/form-data` with `data`; optional `part_number` (0-based) and `sha256`.",
                parameters=[upload_id],
                request_body={"required": True, "content": {"multipart/form-data": {"schema": {"$ref": "#/components/schemas/UploadPartRequest"}}}},
                responses={"200": _ok(base, "#/components/schemas/UploadPart"),
                           **_errors(base, "invalid_request_error", "request_too_large", "upload_not_found",
                                     "upload_state_conflict", "checksum_mismatch", "incomplete_body", "storage_unavailable")},
            ),
        },
        "/v1/uploads/{upload_id}/parts/{part_number}": {
            "put": _operation(
                base, tag="Uploads", operation_id="putUploadPart", summary="Put a part as a raw body", scope=write,
                description=(
                    f"The raw bytes, with `Content-Length` (411 without). Idempotent by `part_number`. A "
                    f"`X-Part-SHA256` or `Content-Digest: sha-256=:…:` is checked. At most {part_max} bytes."
                ),
                parameters=[upload_id, _path_param("part_number", "0"),
                            {"name": "X-Part-SHA256", "in": "header", "required": False, "schema": {"type": "string"}}],
                request_body={"required": True, "content": {"application/octet-stream": {"schema": {"type": "string", "contentMediaType": "application/octet-stream"}}}},
                responses={"200": _ok(base, "#/components/schemas/UploadPart"),
                           # `wire.length_required`: the one invalid_request_error that is a 411.
                           "411": {"description": "invalid_request_error: `Content-Length` is required.",
                                   "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorEnvelope"}}}},
                           **_errors(base, "invalid_request_error", "request_too_large", "upload_not_found",
                                     "upload_state_conflict", "checksum_mismatch", "incomplete_body", "storage_unavailable")},
            ),
        },
        "/v1/uploads/{upload_id}/complete": {
            "post": _operation(
                base, tag="Uploads", operation_id="completeUpload", summary="Complete an upload", scope=write,
                description=(
                    "Always answers 200 with the Upload and its nested `file`; assembling and checking the "
                    "bytes is the file's first processing stage. Replaying a completed upload returns the same file."
                ),
                parameters=[upload_id],
                request_body={"required": False, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/UploadCompleteRequest"}}}},
                responses={"200": _ok(base, "#/components/schemas/Upload"),
                           **_errors(base, "invalid_request_error", "upload_not_found", "upload_state_conflict", "storage_unavailable")},
            ),
        },
        "/v1/uploads/{upload_id}/cancel": {
            "post": _operation(
                base, tag="Uploads", operation_id="cancelUpload", summary="Cancel an upload", scope=write,
                description="Idempotent. The parts are removed.",
                parameters=[upload_id],
                responses={"200": _ok(base, "#/components/schemas/Upload"), **_errors(base, "upload_not_found", "upload_state_conflict")},
            ),
        },
    }


def schemas() -> Dict[str, Any]:
    ts = {"type": ["integer", "null"], "description": "Unix seconds."}
    return {
        "File": {
            "type": "object",
            "required": ["id", "object", "bytes", "created_at", "filename", "purpose", "status"],
            "properties": {
                "id": {"type": "string", "example": "file-6f1c2a9e0b7d4c3a8e5f1b2c"},
                "object": {"type": "string", "enum": ["file"]},
                "bytes": {"type": "integer"},
                "created_at": {"type": "integer"},
                "expires_at": ts,
                "filename": {"type": "string"},
                "purpose": {"type": "string"},
                "status": {"type": "string", "enum": ["uploaded", "processed", "error"]},
                "status_details": {"type": ["string", "null"]},
                "sha256": {"type": ["string", "null"]},
                "mime_type": {"type": ["string", "null"]},
                "processing": {"$ref": "#/components/schemas/FileProcessing"},
            },
        },
        "FileProcessing": {
            "type": ["object", "null"],
            "properties": {
                "state": {"type": "string", "enum": ["queued", "processing", "processed", "failed"]},
                "kind": {"type": "string"},
                "stage": {"type": ["string", "null"]},
                "step": {"type": ["integer", "null"]},
                "total_steps": {"type": ["integer", "null"]},
                "percent": {"type": ["integer", "null"]},
                "stages": {"type": "array", "items": {"type": "object"}},
                "error": {"type": ["object", "null"]},
                "facts": {"type": "object"},
                "derived": {"type": "array", "items": {"type": "string"}},
            },
        },
        "FileList": {
            "type": "object",
            "required": ["object", "data", "has_more"],
            "properties": {
                "object": {"type": "string", "enum": ["list"]},
                "data": {"type": "array", "items": {"$ref": _FILE_REF}},
                "has_more": {"type": "boolean"},
                "first_id": {"type": ["string", "null"]},
                "last_id": {"type": ["string", "null"]},
            },
        },
        "FileDeleted": {
            "type": "object",
            "required": ["id", "object", "deleted"],
            "properties": {"id": {"type": "string"}, "object": {"type": "string", "enum": ["file"]}, "deleted": {"type": "boolean"}},
        },
        "FileCreateRequest": {
            "type": "object",
            "required": ["file", "purpose"],
            "properties": {
                "file": {"type": "string", "contentMediaType": "application/octet-stream"},
                "purpose": {"type": "string", "enum": list(wire.PURPOSES)},
            },
        },
        "DerivedList": {
            "type": "object",
            "properties": {
                "object": {"type": "string", "enum": ["list"]},
                "data": {"type": "array", "items": {"type": "object", "properties": {
                    "name": {"type": "string"}, "bytes": {"type": "integer"}, "content_type": {"type": "string"}}}},
            },
        },
        "Upload": {
            "type": "object",
            "required": ["id", "object", "bytes", "filename", "purpose", "status"],
            "properties": {
                "id": {"type": "string", "example": "upload_8a0d3e5b7c9f1a2b4c6d8e0f"},
                "object": {"type": "string", "enum": ["upload"]},
                "bytes": {"type": "integer"},
                "created_at": {"type": "integer"},
                "filename": {"type": "string"},
                "purpose": {"type": "string"},
                "status": {"type": "string"},
                "expires_at": ts,
                "file": {"oneOf": [{"$ref": _FILE_REF}, {"type": "null"}]},
                "parts": {"type": "array", "items": {"$ref": "#/components/schemas/UploadPart"}},
            },
        },
        "UploadPart": {
            "type": "object",
            "required": ["id", "object", "upload_id"],
            "properties": {
                "id": {"type": "string", "example": "part_3c5e7a9b1d2f4a6c8e0b2d4f"},
                "object": {"type": "string", "enum": ["upload.part"]},
                "upload_id": {"type": "string"},
                "part_number": {"type": "integer"},
                "bytes": {"type": "integer"},
                "sha256": {"type": ["string", "null"]},
                "created_at": {"type": "integer"},
            },
        },
        "UploadCreateRequest": {
            "type": "object",
            "additionalProperties": False,
            "required": ["bytes", "filename", "mime_type", "purpose"],
            "properties": {
                "bytes": {"type": "integer", "minimum": 0},
                "filename": {"type": "string"},
                "mime_type": {"type": "string"},
                "purpose": {"type": "string", "enum": list(wire.PURPOSES)},
                "expires_after": {"type": "object"},
            },
        },
        "UploadPartRequest": {
            "type": "object",
            "required": ["data"],
            "properties": {
                "data": {"type": "string", "contentMediaType": "application/octet-stream"},
                "part_number": {"type": "integer", "minimum": 0},
                "sha256": {"type": "string"},
            },
        },
        "UploadCompleteRequest": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "part_ids": {"type": "array", "items": {"type": "string"}},
                "md5": {"type": "string"},
                "sha256": {"type": "string"},
            },
        },
        # -- files as model input (apifiles/service.lift_file_parts) --
        "InputFile": {
            "type": "object",
            "additionalProperties": False,
            "required": ["type"],
            "description": (
                "A file as model input: exactly one of `file_id` (needs scope `files.read` as well as "
                "`responses.write`) or `file_data` (a base64 or data: URL; no file scope). `file_url` is refused."
            ),
            "properties": {
                "type": {"type": "string", "enum": ["input_file"]},
                "file_id": {"type": "string"},
                "file_data": {"type": "string"},
                "filename": {"type": "string", "maxLength": 255},
                "detail": {"type": "string", "enum": ["low", "auto", "high"]},
            },
        },
        "InputImageFile": {
            "type": "object",
            "additionalProperties": False,
            "required": ["type", "file_id"],
            "properties": {
                "type": {"type": "string", "enum": ["input_image"]},
                "file_id": {"type": "string"},
                "detail": {"type": "string", "enum": ["low", "auto", "high", "original"]},
            },
        },
        "InputVideo": {
            "type": "object",
            "additionalProperties": False,
            "required": ["type", "file_id"],
            "description": "A TechSara extension: an audio or video file as model input.",
            "properties": {
                "type": {"type": "string", "enum": ["input_video"]},
                "file_id": {"type": "string"},
                "detail": {"type": "string", "enum": ["low", "auto", "high"]},
            },
        },
        "ChatFilePart": {
            "type": "object",
            "additionalProperties": False,
            "required": ["type", "file"],
            "properties": {
                "type": {"type": "string", "enum": ["file"]},
                "file": {"type": "object", "additionalProperties": False, "properties": {
                    "file_id": {"type": "string"}, "file_data": {"type": "string"}, "filename": {"type": "string"}}},
            },
        },
        "ChatInputAudioPart": {
            "type": "object",
            "additionalProperties": False,
            "required": ["type", "input_audio"],
            "properties": {
                "type": {"type": "string", "enum": ["input_audio"]},
                "input_audio": {"type": "object", "required": ["data", "format"], "properties": {
                    "data": {"type": "string"}, "format": {"type": "string", "enum": ["wav", "mp3"]}}},
            },
        },
        "FileContext": {
            "type": "object",
            "additionalProperties": False,
            "description": (
                "How files become context. `auto` inlines whole files while they fit the inline budget and "
                "retrieves excerpts beyond it; `full` always inlines; `retrieval` always retrieves."
            ),
            "properties": {
                "mode": {"type": "string", "enum": ["auto", "full", "retrieval"]},
                "max_tokens": {"type": "integer", "minimum": 1},
            },
        },
        "FileCitation": {
            "type": "object",
            "required": ["type", "file_id", "filename", "index"],
            "properties": {
                "type": {"type": "string", "enum": ["file_citation"]},
                "file_id": {"type": "string"},
                "filename": {"type": "string"},
                "index": {"type": "integer", "description": "UTF-16 offset of the citation in the text."},
                "page": {"type": "integer"},
                "timestamp_s": {"type": "number"},
            },
        },
    }


def _append_items(schema: Dict[str, Any], refs: Tuple[str, ...]) -> None:
    for option in schema.get("oneOf") or []:
        if option.get("type") == "array":
            items = option.setdefault("items", {})
            choices = items.setdefault("oneOf", [])
            for ref in refs:
                entry = {"$ref": f"#/components/schemas/{ref}"}
                if entry not in choices:
                    choices.append(entry)


def extend(document: Dict[str, Any], base: Any) -> Dict[str, Any]:
    """The document with the Files API described. A copy; never mutates `base`."""
    out = copy.deepcopy(document)
    out["paths"].update(paths(base))
    components = out["components"]["schemas"]
    components.update(schemas())
    out["tags"] = list(out.get("tags") or []) + [tag for tag in TAGS if tag not in (out.get("tags") or [])]
    message = components.get("InputMessage", {}).get("properties", {}).get("content")
    if isinstance(message, dict):
        _append_items(message, ("InputFile", "InputImageFile", "InputVideo"))
    chat = components.get("ChatMessage", {}).get("properties", {}).get("content")
    if isinstance(chat, dict):
        _append_items(chat, ("ChatFilePart", "ChatInputAudioPart"))
    request = components.get("ResponsesRequest", {}).get("properties")
    if isinstance(request, dict):
        request.setdefault("file_context", {"$ref": "#/components/schemas/FileContext"})
    text = components.get("OutputText", {}).get("properties")
    if isinstance(text, dict):
        text.setdefault("annotations", {"type": "array", "items": {"$ref": "#/components/schemas/FileCitation"}})
    return out
