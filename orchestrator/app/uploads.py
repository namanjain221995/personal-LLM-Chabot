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

import mimetypes
import os
import shutil
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from . import db
from .auth import UserRow, require_user
from .config import settings
from .core import archive, profile as profiler
from .core.upload_paths import UploadPathError, resolve_upload_file

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


@router.get("/{conversation_id}/{upload_id}/file")
async def download_upload(
    conversation_id: str,
    upload_id: str,
    user: UserRow = Depends(require_user),
) -> FileResponse:
    """The stored bytes of ONE upload — the owner's only (Phase 3).

    The bytes were always here (uploads stream to the workspace and stay until
    the TTL sweeps them); what was missing was any way to ask for them, so a
    browser that had lost its in-memory copy of a CSV reported the file gone
    while the server was still answering questions about it.

    Modelled on GET /reports/{filename}, which is this project's established
    shape for "owner-checked file on disk": STRICT 404 for anything that is not
    yours — a conversation with no row, someone else's conversation, and an
    upload id that belongs to a different conversation are all indistinguishable
    from never having existed, so the flat id space cannot be used as an oracle.

    Expiry is its own answer. A row whose files the TTL has swept is 410 Gone,
    not 404: the client can tell "you may not have this" from "this is no longer
    here", and only the second is worth telling the user to re-attach for.
    """
    # Ownership first, and STRICTLY: `is None` counts as refused, exactly as in
    # list_uploads. An unowned conversation must not be readable by anyone.
    owner = await db.run_in_thread(db.conversation_owner, conversation_id)
    if owner is None or owner != int(user["id"]):
        raise HTTPException(status_code=404, detail="upload not found")

    # Scoped by BOTH ids: an upload id alone names nothing here.
    uploads = await db.run_in_thread(db.get_uploads, conversation_id)
    row = next((u for u in uploads if u["id"] == upload_id), None)
    if row is None:
        raise HTTPException(status_code=404, detail="upload not found")

    filename = str(row.get("filename") or "")
    try:
        path = resolve_upload_file(
            settings.workspace_dir, conversation_id, upload_id, filename
        )
    except UploadPathError:
        # A malformed id cannot name a real row, so this is effectively
        # unreachable — and it stays a 404 rather than leaking the distinction.
        raise HTTPException(status_code=404, detail="upload not found")

    if not path.is_file():
        # Two ways to get here, and the user can act on both the same way.
        # Either the workspace TTL swept the extracted files, or this upload was
        # an ARCHIVE: create_upload deletes `_original` once it has extracted
        # the members, so the .zip/.tar the user chose is genuinely not kept.
        raise HTTPException(
            status_code=410,
            detail=(
                "This upload has expired and is no longer stored. "
                "Attach the file again to use it."
            ),
        )

    # NEVER the type the browser declared at upload time — that value is
    # attacker-chosen. Guessed from the stored name, defaulting to a type no
    # browser will render, and served as an attachment (nosniff is set by the
    # frontend for every response it proxies).
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return FileResponse(path, filename=filename, media_type=media_type)


#: How much extracted document text a preview may pull. A 300-page PDF's text
#: is megabytes; one screen of it is what a preview needs, and the dialog says
#: when it has been cut.
DOCUMENT_PREVIEW_CHARS = 200_000


@router.get("/{conversation_id}/document")
async def document_text(
    conversation_id: str,
    name: str,
    user: UserRow = Depends(require_user),
) -> dict:
    """The extracted TEXT of a document attached to this conversation (4C).

    DOCX is a zip of XML: a browser cannot open one without a parser, and this
    project ships none. It does not need one — engines/document.py already
    extracted the text with the standard library when the file was sent, and
    stored it here. So the preview reads what the model read.

    TEXT, deliberately, never markup. There is no HTML anywhere on this path:
    not from the extractor (core/docx.py returns paragraphs and tab-separated
    table rows), not from this endpoint, and not in the dialog that renders it —
    which means the document's own content can never become DOM. A .docx is an
    untrusted file, and the safest renderer for one is a <pre>.

    The filename is a QUERY parameter rather than a path segment: it never
    touches the filesystem here (this is a database lookup keyed by exact
    name), and keeping it out of the path keeps it out of route matching too.
    """
    owner = await db.run_in_thread(db.conversation_owner, conversation_id)
    if owner is None or owner != int(user["id"]):
        raise HTTPException(status_code=404, detail="document not found")

    documents = await db.run_in_thread(db.get_documents, conversation_id)
    row = next((d for d in documents if d.get("filename") == name), None)
    if row is None:
        raise HTTPException(status_code=404, detail="document not found")

    text = str(row.get("text") or "")
    return {
        "filename": row.get("filename"),
        "total_pages": row.get("total_pages") or 0,
        "text": text[:DOCUMENT_PREVIEW_CHARS],
        "truncated": len(text) > DOCUMENT_PREVIEW_CHARS,
    }


@router.get("/{conversation_id}")
def list_uploads(
    conversation_id: str, user: UserRow = Depends(require_user)
) -> dict:
    # STRICT: a conversation with no row has no uploads to list, and one
    # owned by someone else is indistinguishable from that.
    owner = db.conversation_owner(conversation_id)
    if owner is None or owner != int(user["id"]):
        raise HTTPException(status_code=404, detail="conversation not found")
    uploads = db.get_uploads(conversation_id)
    # Report expiry rather than pretending the bytes are still there.
    for up in uploads:
        if up["status"] == "ready" and not bytes_available(conversation_id, up["id"]):
            up["status"] = "expired"
    return {"uploads": uploads}
