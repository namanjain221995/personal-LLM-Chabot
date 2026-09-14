"""`/v1/files` and `/v1/uploads` — the OpenAI-compatible Files API surface.

ADDED 2026-09-13 (Files API design). `routes.create_files_router(deps)` builds
an `APIRouter`; `routes.register(router, deps)` adds the same routes to an
existing router (the public `/v1` router, so every route is a `PublicRoute`).
Authentication, scope, admission and usage recording are PARAMETERS
(`routes.FilesDependencies`), so integration only registers.

* `wire`            — the File / Upload / UploadPart objects and the seven new
                      error codes of design §2.17.
* `multipart_disk`  — a streaming multipart reader that writes the file part to
                      disk (never `request.form()`, which spools to /tmp).
* `content`         — byte-range downloads with the safe header set.
* `routes`          — the handlers, in CONTRACT §4's order.
"""
from __future__ import annotations

__all__ = ["content", "multipart_disk", "routes", "wire"]
