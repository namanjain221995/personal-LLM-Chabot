"""SAFE resolution of a stored upload's bytes (Phase 3).

The sibling of core/report_paths: a client names a conversation, an upload and
nothing else, and this turns that into ONE path inside the upload workspace or
refuses. The client never supplies a path, a directory or an extension.

Two ids and one filename reach the filesystem here, and each is dangerous in a
different way:

  * `conversation_id` is client-chosen (the frontend mints it), so it is
    filtered to the same alphabet uploads.upload_root already uses;
  * `upload_id` is SERVER-minted (`uuid.uuid4().hex`) and is therefore required
    to look exactly like one — 32 lowercase hex characters. That single check
    removes `..`, separators, absolute paths and NUL in one statement, and it
    is the reason a caller cannot walk out of the workspace with it;
  * `filename` comes from the uploads ROW, not from the request, but it was
    originally chosen by whoever uploaded the file, so it is treated as hostile
    all the same: basename only, no separators, no dot-segments.

The resolved path is then re-checked against the resolved root, which closes
symlink escape — resolve() follows links, so a link planted inside the
workspace that points outside it lands outside `root` and is rejected.

Pure module: stdlib only, no FastAPI, no settings import. That is deliberate —
it keeps this runnable (and testable) without the service's dependency stack.
"""
from __future__ import annotations

import re
from pathlib import Path

#: uuid4().hex, which is what uploads.create_upload mints.
UPLOAD_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class UploadPathError(ValueError):
    """Raised for an unsafe or malformed conversation/upload/filename."""


def safe_conversation_segment(conversation_id: str) -> str:
    """The directory name uploads.upload_root uses for a conversation.

    Kept byte-identical to that function on purpose: if the two ever disagreed,
    a file would be written under one name and looked up under another.
    """
    return "".join(c for c in conversation_id if c.isalnum() or c in "-_")[:64]


def resolve_upload_file(
    workspace_dir: str | Path,
    conversation_id: str,
    upload_id: str,
    filename: str,
) -> Path:
    """Resolve the extracted copy of `filename` for one upload, or raise.

    Existence is NOT checked — the caller distinguishes "swept by the TTL"
    (410) from "never yours" (404), and those are different answers.
    """
    if not UPLOAD_ID_RE.fullmatch(upload_id or ""):
        raise UploadPathError("malformed upload id")

    conv = safe_conversation_segment(conversation_id or "")
    if not conv:
        raise UploadPathError("malformed conversation id")

    name = (filename or "").strip()
    if not name or name in {".", ".."} or ".." in name:
        raise UploadPathError("path traversal is not allowed")
    if "/" in name or "\\" in name or "\x00" in name:
        raise UploadPathError("nested or absolute paths are not allowed")
    if name.startswith("."):
        raise UploadPathError("hidden files are not allowed")
    if Path(name).is_absolute():
        raise UploadPathError("absolute paths are not allowed")

    root = (Path(workspace_dir) / "uploads" / conv / upload_id).resolve()
    resolved = (root / "extracted" / name).resolve()
    if not resolved.is_relative_to(root):
        raise UploadPathError("path escapes the upload directory")
    return resolved
