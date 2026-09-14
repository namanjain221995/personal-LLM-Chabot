"""The public Files API's server side: bytes, rows and the jobs that read them.

ADDED 2026-09-13 (owner request: an OpenAI-compatible Files/Uploads API whose
documents, images, audio and video every public model can use). The binding
design is the Files API design of 2026-09-13; this package is its storage and
upload half. The HTTP surface is `publicapi/files/`.

WHAT LIVES WHERE.

* `ids`            — `file-`, `upload_`, `part_`, `blob_` ids and the regexes
                     every path segment is checked against before a join.
* `limits`         — every technical ceiling, read at call time from settings
                     with the environment as the fallback.
* `storage`        — the on-disk layout under PUBLIC_API_FILES_DIR, the
                     realpath fence, and the free-space watermark.
* `schema`         — the tables (DDL + an idempotent `ensure_schema`) and the
                     project-scoped accessors every other module goes through.
* `sniff`          — what the bytes ARE (magic numbers, zip members), never
                     what the client said they were.
* `queue`          — leases: the processing claim for blobs and the
                     `assemble` stage that turns upload parts into a blob.
* `accounting`     — storage bytes per project, the processing usage row,
                     and the in-process gauges.
* `uploads_sweep`  — expiry, stale-finalize reset, crash leftovers.

A BLOB is one project's copy of some bytes (unique per project + sha256, never
global: a global key would tell one tenant that another uploaded the same
bytes). A FILE is a named reference to a blob; the bytes go when the last live
file does.
"""
from __future__ import annotations

__all__ = [
    "accounting",
    "ids",
    "limits",
    "queue",
    "schema",
    "sniff",
    "storage",
    "uploads_sweep",
]
