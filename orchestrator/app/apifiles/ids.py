"""Identifiers of the Files API, and the shapes a path segment must have.

WHY RANDOM, 96 BITS (design §10.1). These ids travel in customer code, request
logs and webhook payloads. A sequence would turn every `/v1/files/{id}` into an
enumeration oracle (OWASP API1); 24 hex characters from `secrets` are the same
96 bits `db._api_id` gives every V34 table.

WHY `file-` WITH A HYPHEN. It is OpenAI's spelling (`file-abc…`), and the SDKs
and developer tooling that pattern-match ids expect it. Uploads and parts use
the underscore spelling OpenAI uses for them (`upload_…`, `part_…`).

WHY EVERY SEGMENT IS CHECKED BY REGEX BEFORE A JOIN (design §7.1). An id is
caller text until it has matched; `os.path.join(root, "../../etc")` is how a
path parameter becomes a traversal. Nothing in `storage.py` joins a segment
that has not passed one of these.
"""
from __future__ import annotations

import re
import secrets
from typing import Any

FILE_PREFIX = "file-"
UPLOAD_PREFIX = "upload_"
PART_PREFIX = "part_"
BLOB_PREFIX = "blob_"
LIST_PREFIX = "lst_"

FILE_ID_RE = re.compile(r"^file-[0-9a-f]{24}$")
UPLOAD_ID_RE = re.compile(r"^upload_[0-9a-f]{24}$")
PART_ID_RE = re.compile(r"^part_[0-9a-f]{24}$")
BLOB_ID_RE = re.compile(r"^blob_[0-9a-f]{24}$")
#: `db._api_id("proj")` — the V34 project id shape.
PROJECT_ID_RE = re.compile(r"^proj_[0-9a-f]{24}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MD5_RE = re.compile(r"^[0-9a-f]{32}$")
#: `router._new_request_id()` spells `req_<32 hex>`; `_inline/<request_id>`
#: accepts that and nothing else.
REQUEST_ID_RE = re.compile(r"^req_[0-9a-f]{32}$")


def _token() -> str:
    return secrets.token_hex(12)


def new_file_id() -> str:
    return FILE_PREFIX + _token()


def new_upload_id() -> str:
    return UPLOAD_PREFIX + _token()


def new_part_id() -> str:
    return PART_PREFIX + _token()


def new_blob_id() -> str:
    return BLOB_PREFIX + _token()


def new_list_id() -> str:
    """`usage_events.generation_id` for `GET /v1/files` (design §11)."""
    return LIST_PREFIX + _token()


def _matches(pattern: "re.Pattern[str]", value: Any) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def is_file_id(value: Any) -> bool:
    return _matches(FILE_ID_RE, value)


def is_upload_id(value: Any) -> bool:
    return _matches(UPLOAD_ID_RE, value)


def is_part_id(value: Any) -> bool:
    return _matches(PART_ID_RE, value)


def is_blob_id(value: Any) -> bool:
    return _matches(BLOB_ID_RE, value)


def is_project_id(value: Any) -> bool:
    return _matches(PROJECT_ID_RE, value)


def is_sha256(value: Any) -> bool:
    return _matches(SHA256_RE, value)


def is_md5(value: Any) -> bool:
    return _matches(MD5_RE, value)


def is_request_id(value: Any) -> bool:
    return _matches(REQUEST_ID_RE, value)
