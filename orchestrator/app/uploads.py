"""Dataset upload endpoint (Phase 4).

The body is STREAMED to disk in chunks — a 200 MB archive must never be held
in memory (and never base64-encoded through the chat body, which is how images
and PDFs travel). The file is then extracted under the per-conversation
workspace, profiled, and the PROFILE is stored in SQLite.

Bytes and profile have different lifetimes on purpose: the workspace TTL
sweeps the extracted files after 24 h, while the profile keeps answering
questions. Anything that needs actual bytes reports the dataset as expired and
asks for a re-upload — never a 500.
"""
from __future__ import annotations

import os
import shutil
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from . import db
from .auth import UserRow, require_user
from .config import settings
from .core import archive, profile as profiler

router = APIRouter(prefix="/uploads", tags=["uploads"])

_CHUNK = 1024 * 1024


def upload_root(conversation_id: str, upload_id: str) -> str:
    safe_conv = "".join(c for c in conversation_id if c.isalnum() or c in "-_")[:64]
    return os.path.join(settings.workspace_dir, "uploads", safe_conv, upload_id)


def bytes_available(conversation_id: str, upload_id: str) -> bool:
    """True while the extracted files still exist (TTL has not swept them)."""
    root = upload_root(conversation_id, upload_id)
    return os.path.isdir(root) and any(os.scandir(root))


async def _stream_to_disk(upload: UploadFile, dest: str) -> int:
    """Write the request body out in chunks, enforcing the size cap live."""
    cap = settings.upload_max_mb * 1024 * 1024
    written = 0
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as out:
        while True:
            chunk = await upload.read(_CHUNK)
            if not chunk:
                break
            written += len(chunk)
            if written > cap:
                out.close()
                os.unlink(dest)
                raise HTTPException(
                    status_code=413,
                    detail=f"That file is larger than {settings.upload_max_mb} MB.",
                )
            out.write(chunk)
    return written


@router.post("")
async def create_upload(
    file: UploadFile = File(...),
    conversation_id: str = Form(...),
    user: UserRow = Depends(require_user),
) -> dict:
    if not settings.dataset_uploads_enabled:
        raise HTTPException(status_code=404, detail="dataset uploads are disabled")

    # Same ownership rule as every other per-conversation store.
    owner = await db.run_in_thread(db.conversation_owner, conversation_id)
    if owner is not None and owner != int(user["id"]):
        raise HTTPException(status_code=404, detail="conversation not found")

    upload_id = uuid.uuid4().hex
    root = upload_root(conversation_id, upload_id)
    filename = os.path.basename(file.filename or "upload.bin")
    raw_path = os.path.join(root, "_original", filename)

    try:
        from .core.repo import enforce_quota_and_ttl

        enforce_quota_and_ttl()
    except Exception:
        pass  # housekeeping only; never blocks an upload

    size = await _stream_to_disk(file, raw_path)
    extract_dir = os.path.join(root, "extracted")
    notes: list = []

    try:
        lower = filename.lower()
        if archive.is_zip_container(raw_path) and not lower.endswith(".xlsx"):
            plan = archive.extract(raw_path, extract_dir)
        elif lower.endswith((".tar", ".tar.gz", ".tgz")) or (
            archive.sniff_format(raw_path) == "gzip"
        ):
            plan = archive.extract(raw_path, extract_dir)
        else:
            # A single data file. An .xlsx IS a zip container, so it faces the
            # same bomb/member caps HERE — before it is stored or read — and a
            # hostile one is rejected outright rather than quietly skipped
            # during profiling.
            if archive.is_zip_container(raw_path):
                archive.check_zip_container(raw_path, label="spreadsheet")
            os.makedirs(extract_dir, exist_ok=True)
            shutil.copy2(raw_path, os.path.join(extract_dir, filename))
            plan = None

        if plan is not None:
            for name, why in plan.skipped:
                notes.append(f"skipped {name}: {why}")
            for name in plan.nested_archives:
                notes.append(f"nested archive listed but not opened: {name}")

        profiles = profiler.profile_directory(extract_dir)
    except archive.ArchiveError as exc:
        shutil.rmtree(root, ignore_errors=True)
        await db.run_in_thread(
            db.save_upload,
            upload_id, conversation_id, filename, size, "rejected", None, str(exc),
        )
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        shutil.rmtree(root, ignore_errors=True)
        await db.run_in_thread(
            db.save_upload,
            upload_id, conversation_id, filename, size, "failed", None,
            f"{type(exc).__name__}",
        )
        raise HTTPException(
            status_code=400, detail="That file could not be read as a dataset."
        )

    # The original archive is not needed once extracted; drop it to save quota.
    shutil.rmtree(os.path.join(root, "_original"), ignore_errors=True)

    await db.run_in_thread(
        db.save_upload,
        upload_id,
        conversation_id,
        filename,
        size,
        "ready",
        profiler.profile_json(profiles),
        "; ".join(notes[:20]) or None,
    )
    return {
        "upload_id": upload_id,
        "filename": filename,
        "bytes": size,
        "files": len(profiles),
        "notes": notes[:20],
        "profile": profiles,
    }


@router.get("/{conversation_id}")
def list_uploads(
    conversation_id: str, user: UserRow = Depends(require_user)
) -> dict:
    owner = db.conversation_owner(conversation_id)
    if owner is not None and owner != int(user["id"]):
        raise HTTPException(status_code=404, detail="conversation not found")
    uploads = db.get_uploads(conversation_id)
    # Report expiry rather than pretending the bytes are still there.
    for up in uploads:
        if up["status"] == "ready" and not bytes_available(conversation_id, up["id"]):
            up["status"] = "expired"
    return {"uploads": uploads}
