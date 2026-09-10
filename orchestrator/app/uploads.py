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

import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import re
import shutil
import time
import uuid
from typing import Optional

from fastapi import (APIRouter, Depends, File, Form, HTTPException, Request,
                     UploadFile)
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.requests import ClientDisconnect

from . import db
from .auth import UserRow, require_user
from .config import settings
from .core import archive, profile as profiler
from .core.upload_paths import UploadPathError, resolve_upload_file

router = APIRouter(prefix="/uploads", tags=["uploads"])

log = logging.getLogger(__name__)

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


async def require_attachments(request: Request) -> None:
    """Refuse the upload when this account may not attach files (V17).

    THIS is the real gate for attachments — bytes land here, not in /chat —
    so it is a hard 403 rather than the downgrade /chat performs on inline
    base64. The composer hides the picker for these accounts, so reaching
    this is a stale tab or a direct API call.
    """
    from .authn import features as feature_access
    from .authn.principal import require_principal

    principal = await require_principal(request)
    if not feature_access.allowed(principal.features, feature_access.Feature.ATTACHMENTS):
        raise HTTPException(
            status_code=403,
            detail="File uploads are turned off for your account. Ask an administrator.",
        )


@router.post("")
async def create_upload(
    request: Request,
    file: UploadFile = File(...),
    conversation_id: str = Form(...),
    # "dataset" (extract + profile, the original is dropped), "document"
    # (keep the original byte-for-byte; PDFs/DOCX are not datasets and must
    # not be profiled as one -- and the chat engine needs the actual bytes)
    # or "video" (2026-09-09: keep the original, hash it, start the analysis
    # job behind the response — see app/video/api.attach_upload).
    purpose: str = Form("dataset"),
    user: UserRow = Depends(require_user),
    _attachments: None = Depends(require_attachments),
) -> dict:
    if not settings.dataset_uploads_enabled:
        raise HTTPException(status_code=404, detail="dataset uploads are disabled")
    if purpose == "video":
        # Its own gate (403 for the member, 404 for the deployment), checked
        # BEFORE any byte lands so a refused upload leaves nothing behind.
        from .video.api import require_video

        await require_video(request)

    # Same ownership rule as every other per-conversation store — and the
    # same claim-on-first-touch as /chat (see _own).
    await _own(conversation_id, user)

    upload_id = uuid.uuid4().hex
    root = upload_root(conversation_id, upload_id)
    filename = os.path.basename(file.filename or "upload.bin")
    raw_path = os.path.join(root, "_original", filename)

    try:
        from .core.repo import enforce_quota_and_ttl

        enforce_quota_and_ttl()
    except Exception:
        pass  # housekeeping only; never blocks an upload
    await _sweep_quietly()  # the chunked rail's expiry rides the same hook

    size = await _stream_to_disk(file, raw_path)
    if purpose == "document":
        return await _finalise_document(conversation_id, upload_id, filename, size)
    if purpose == "video":
        return await _finalise_video(conversation_id, upload_id, filename, raw_path, size, user)
    if purpose != "dataset":
        shutil.rmtree(root, ignore_errors=True)
        raise HTTPException(status_code=400, detail="unknown upload purpose")
    return await _finalise_dataset(conversation_id, upload_id, filename, raw_path, size)


#: Strong references to upload-time extraction tasks — asyncio keeps only
#: weak refs, and an unreferenced task can be collected mid-flight.
_PREWARM_TASKS: set = set()


async def _prewarm_document(conversation_id: str, upload_id: str, filename: str) -> None:
    """Extract a just-uploaded document in the background and cache it.

    The send that follows finds `extracted/document.json` and reads it
    instead of extracting on the answer's critical path (text layer, page
    renders, OCR of scanned pages — seconds to tens of seconds for a scan).
    Best-effort: any failure leaves no cache, and the chat path extracts
    exactly as it did before.
    """
    root = upload_root(conversation_id, upload_id)
    raw_path = os.path.join(root, "_original", filename)
    try:
        from .engines.document import extract_document, write_document_cache

        def _read() -> bytes:
            with open(raw_path, "rb") as fh:
                return fh.read()

        raw = await asyncio.to_thread(_read)
        doc, _err = await extract_document(filename, raw, effort="think", question="")
        if doc is None:
            return
        await asyncio.to_thread(write_document_cache, root, doc)
        log.info(
            "prewarmed %s (%s pages, %s OCR'd) for %s", filename, doc.total, doc.ocred, upload_id
        )
    except Exception:  # noqa: BLE001 — the answer path does not depend on this
        log.debug("document prewarm skipped for %s", upload_id, exc_info=True)


def _schedule_prewarm(conversation_id: str, upload_id: str, filename: str, size: int) -> None:
    if not settings.document_prewarm_enabled:
        return
    if size > settings.document_prewarm_max_mb * 1024 * 1024:
        return
    lower = filename.lower()
    if lower.endswith((".zip", ".tar", ".tar.gz", ".tgz")):
        return  # archives are expanded per question by the chat path
    try:
        task = asyncio.create_task(_prewarm_document(conversation_id, upload_id, filename))
    except RuntimeError:  # pragma: no cover — no running loop
        return
    _PREWARM_TASKS.add(task)
    task.add_done_callback(_PREWARM_TASKS.discard)


async def _finalise_document(
    conversation_id: str, upload_id: str, filename: str, size: int
) -> dict:
    """A document keeps its original bytes; extraction is prewarmed behind
    the response (2026-09-03) so the next send reads a cache."""
    await db.run_in_thread(
        db.save_upload,
        upload_id, conversation_id, filename, size, "ready", None, "document",
    )
    _schedule_prewarm(conversation_id, upload_id, filename, size)
    return {
        "upload_id": upload_id,
        "filename": filename,
        "bytes": size,
        "files": 1,
        "notes": [],
        "profile": [],
    }


async def _finalise_video(
    conversation_id: str, upload_id: str, filename: str, raw_path: str, size: int, user: UserRow
) -> dict:
    """A video keeps its bytes AND starts its analysis job; the response
    returns while the job runs (app/video/api.attach_upload)."""
    from .video.api import attach_upload

    try:
        return await attach_upload(
            conversation_id=conversation_id,
            upload_id=upload_id,
            filename=filename,
            raw_path=raw_path,
            size=size,
            user_id=int(user["id"]),
        )
    except HTTPException:
        shutil.rmtree(upload_root(conversation_id, upload_id), ignore_errors=True)
        raise


async def _finalise_dataset(
    conversation_id: str, upload_id: str, filename: str, raw_path: str, size: int
) -> dict:
    root = upload_root(conversation_id, upload_id)
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
        # DOCUMENTS keep their bytes in `_original`, not `extracted` — the
        # dataset rail's shape. Without this fallback every document download
        # answered 410 "expired" while the file sat on disk (owner report,
        # 2026-09-02, minutes after document cards became openable at all).
        original = resolve_upload_file(
            settings.workspace_dir, conversation_id, upload_id, filename,
            subdir="_original",
        )
        if original.is_file():
            path = original

    if not path.is_file():
        # Two ways to get here, and the user can act on both the same way.
        # Either the workspace TTL swept the files, or this upload was a
        # DATASET ARCHIVE: the dataset finaliser deletes `_original` once it
        # has extracted the members, so the .zip the user chose is not kept.
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


# --------------------------------------------------------------- chunked
# Cloudflare's edge caps a single request body at 100 MB on this plan, so a
# 512 MB document cannot arrive in one POST over the public hostname however
# generous every server-side limit is. The client slices big files into parts
# under _PART_CAP and the pieces are reassembled here; each call carries the
# same session and passes the same ownership check as everything else in this
# file. LAN uploads may still use the single-shot endpoint above.
#
# V29 (2026-09-10, docs/upload-reliability/API.md): the session is a DATABASE
# ROW, not a marker file. Until then the rail kept parts on disk and nothing
# else, so a reload could not ask what had arrived, `complete` was not
# idempotent, and a part whose body was cut short was accepted with whatever
# bytes had landed. Now `upload_sessions` records owner, expectation, the
# parts that are durable on disk and, once finalised, the exact response — the
# disk holds bytes, the row holds truth about them, and every route reads the
# row first. The pre-V29 marker is gone: a session that was mid-flight across
# the deploy answers 404 and the browser starts it again, which is what a
# deploy mid-upload already meant.

_HEX32 = re.compile(r"^[0-9a-f]{32}$")
#: Comfortably under the 100 MB edge wall, with room for multipart overhead.
_PART_CAP = 90 * 1024 * 1024
#: 128 x 90 MiB = 11 GiB of headroom on the server side; the client's
#: 64 MiB parts make a 4 GB video 60 parts (2026-09-09).
_MAX_PARTS = 128
#: Where a session's parts live under its upload root. Each accepted part is
#: the file `<index>`; a part still streaming is `<index>.<nonce>.tmp`.
_PARTS_DIR = "_parts"
#: How long a `complete` that lost the race waits for the winner's outcome.
#: A 4 GB assembly plus the video hash is tens of seconds on this disk; two
#: minutes covers it with margin, and a caller that outlives it is told to
#: ask again rather than left hanging on the edge's own timeout.
_FINALIZE_WAIT_S = 120.0
_FINALIZE_POLL_S = 0.25
#: The expiry sweep piggybacks on upload traffic (there is no scheduler in
#: this module); once a minute is plenty for a 24-hour TTL and keeps twenty
#: simultaneous `init`s from all querying the same work list.
_SWEEP_INTERVAL_S = 60.0
_last_sweep_at: float = 0.0


_CONVERSATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


async def _own(conversation_id: str, user: UserRow) -> None:
    """The uploader must own the conversation — and if nobody does yet, the
    uploader claims it NOW, exactly as /chat claims an id on its first
    message.

    Until 2026-09-03 an unowned id was merely tolerated here, so bytes and
    extracted text landed under an id that whoever sent the next /chat with
    it would inherit (pre-seeding). Claiming first closes that: after this
    returns, the id belongs to this user or the request is refused.
    """
    owner = await db.run_in_thread(db.conversation_owner, conversation_id)
    if owner is None:
        if not _CONVERSATION_ID_RE.match(conversation_id or ""):
            raise HTTPException(status_code=422, detail="invalid conversation id")
        try:
            await db.run_in_thread(
                db.create_conversation, int(user["id"]), conversation_id, "New chat"
            )
        except db.IntegrityError:
            pass  # raced another request for the same id — the recheck decides
        owner = await db.run_in_thread(db.conversation_owner, conversation_id)
    if owner != int(user["id"]):
        raise HTTPException(status_code=404, detail="conversation not found")


async def _owned(conversation_id: str, user: UserRow) -> None:
    """`_own` without the claim, for the routes that only READ or CANCEL.

    A discovery GET on an id nobody owns must not mint a "New chat" row in
    the caller's sidebar as a side effect; init already claimed the
    conversation before any session could exist under it, so an unowned id
    has no session to show and is refused exactly like someone else's.
    """
    owner = await db.run_in_thread(db.conversation_owner, conversation_id)
    if owner is None or owner != int(user["id"]):
        raise HTTPException(status_code=404, detail="upload not found")


def _cap_total(purpose: str) -> int:
    """The running-total ceiling for ONE session, by purpose.

    PURPOSE-AWARE (2026-09-10): a video may be `VIDEO_MAX_UPLOAD_MB` (4096)
    while documents and datasets stop at `UPLOAD_MAX_MB` (200). The rail used
    to apply the smaller limit to everything, and yesterday's 400 MB video
    only passed because the deployed UPLOAD_MAX_MB had been raised for it.
    The same number is applied at all three points where bytes are counted —
    the declared `size` at init, accepted-plus-streaming at every part, and
    the assembled total at complete — so a session can never be refused later
    for a total it was told was fine earlier.
    """
    mb = settings.video_max_upload_mb if purpose == "video" else settings.upload_max_mb
    return int(mb) * 1024 * 1024


def _too_large(purpose: str) -> HTTPException:
    if purpose == "video":
        return HTTPException(
            status_code=413,
            detail=f"That video is larger than {settings.video_max_upload_mb} MB.",
        )
    return HTTPException(
        status_code=413, detail=f"That file is larger than {settings.upload_max_mb} MB."
    )


def _part_path(root: str, index: int) -> str:
    return os.path.join(root, _PARTS_DIR, str(int(index)))


def _uploads_base() -> str:
    return os.path.join(settings.workspace_dir, "uploads")


def _inside_uploads(path: str) -> bool:
    """Only ever delete under `<workspace>/uploads`. The ids come from our own
    rows, but a delete deserves its own fence regardless of who minted the
    name."""
    base = os.path.realpath(_uploads_base())
    target = os.path.realpath(path)
    return target != base and target.startswith(base + os.sep)


def remove_session_parts(conversation_id: str, upload_id: str) -> None:
    """Drop ONE session's parts directory and nothing else.

    THE LAYOUT every caller relies on (the workspace sweep in core/repo.py
    must learn it to spare live sessions):

        <WORKSPACE_DIR>/uploads/<conversation_id>/<upload_id>/_parts/<index>
                                                             /_parts/<index>.<nonce>.tmp
                                                             /_original/<filename>

    i.e. `upload_root(conversation_id, upload_id)/_parts`, with accepted parts
    named by their bare index and in-flight ones carrying a nonce and `.tmp`.
    This removes that `_parts` directory, and the session root only when it
    is left empty. NEVER `_original`: for a completed upload that is the
    file the conversation now refers to, and it has its own lifetime.
    """
    root = upload_root(conversation_id, upload_id)
    if not _HEX32.fullmatch(upload_id or "") or not _inside_uploads(root):
        return
    shutil.rmtree(os.path.join(root, _PARTS_DIR), ignore_errors=True)
    try:
        if os.path.isdir(root) and not any(os.scandir(root)):
            os.rmdir(root)
    except OSError:
        pass


def _discard(tmp: str) -> None:
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    except OSError:
        log.debug("could not remove %s", tmp, exc_info=True)


def _keep_uploads_warm() -> None:
    """Refresh the mtime of the top-level `uploads` directory.

    core/repo.enforce_quota_and_ttl deletes whole TOP-LEVEL workspace
    directories by age, and `uploads/` is one of them — its mtime moves only
    when a conversation directory is created or removed directly inside it.
    A quiet deployment whose last new conversation was a day ago would lose
    every live session's parts to the next single-shot upload's housekeeping.
    Touching the directory as parts arrive keeps it younger than the TTL for
    exactly as long as something in it is still being uploaded.
    """
    try:
        os.utime(_uploads_base(), None)
    except OSError:
        pass


def _present_parts(session: dict, root: str) -> dict:
    """index -> bytes for the parts the row lists AND the disk still holds at
    that size. The row is written only after the file is durable, so the two
    agree unless something outside this module removed the file (the
    workspace sweep, an operator); then the disk wins, the part is reported
    as not there, and the client sends it again."""
    out: dict = {}
    for key, entry in (session.get("accepted_parts") or {}).items():
        try:
            index = int(key)
            nbytes = int((entry or {}).get("bytes") or 0)
            if os.path.getsize(_part_path(root, index)) == nbytes:
                out[index] = nbytes
        except (OSError, TypeError, ValueError):
            continue
    return out


def _accepted(session: dict, root: str) -> tuple:
    """(sorted indexes, total bytes) as the client should see them. A
    completed session's parts are gone by design, so its row is the record;
    every other status is checked against the disk."""
    if session.get("status") == "complete":
        parts = session.get("accepted_parts") or {}
        indexes = sorted(int(k) for k in parts)
        return indexes, int(session.get("bytes_received") or 0)
    present = _present_parts(session, root)
    return sorted(present), sum(present.values())


async def _load_session(conversation_id: str, upload_id: str, user: UserRow) -> dict:
    """The row, or 404. Unknown, malformed, someone else's, or a session that
    belongs to a different conversation are all the same answer: a flat id
    space must not work as an oracle, and 403 would confirm the id exists."""
    if not _HEX32.fullmatch(upload_id or ""):
        raise HTTPException(status_code=404, detail="upload not found")
    session = await db.run_in_thread(db.get_upload_session, upload_id)
    if (
        session is None
        or int(session["user_id"]) != int(user["id"])
        or session["conversation_id"] != conversation_id
    ):
        raise HTTPException(status_code=404, detail="upload not found")
    return session


def _require_uploading(session: Optional[dict]) -> None:
    """Parts land only on an `uploading` session: 409 while it is being (or
    has been) finalised — the bytes belong to the conversation then — and
    404 for every terminal state, where there is nothing left to add to."""
    status = (session or {}).get("status")
    if status == "uploading":
        return
    if status in ("finalizing", "complete"):
        raise HTTPException(
            status_code=409,
            detail=(
                "This upload is being finalised."
                if status == "finalizing"
                else "This upload is already complete."
            ),
        )
    raise HTTPException(status_code=404, detail="upload not found")


def _declared_sha256(request: Request) -> Optional[str]:
    raw = (request.headers.get("x-part-sha256") or "").strip().lower()
    if not raw:
        return None
    if not re.fullmatch(r"[0-9a-f]{64}", raw):
        raise HTTPException(
            status_code=422, detail="X-Part-SHA256 is not a hex-encoded SHA-256 digest"
        )
    return raw


def _session_view(session: dict, root: str) -> dict:
    accepted, received = _accepted(session, root)
    return {
        "upload_id": session["id"],
        "status": session["status"],
        "filename": session["filename"],
        "purpose": session["purpose"],
        "expected_bytes": session.get("expected_bytes"),
        "expected_parts": session.get("expected_parts"),
        "part_size": session.get("part_size"),
        "accepted_parts": accepted,
        "bytes_received": received,
        "expires_at": session.get("expires_at"),
        "result": session.get("result") if session["status"] == "complete" else None,
        # Additive: the sentence a rejected session was refused with, so a
        # browser that reloads can still show why instead of a bare status.
        "error": session.get("error") or None,
    }


def _count_session(purpose: str, result: str) -> None:
    from . import metrics

    metrics.inc(
        "upload_session_total", "Chunked upload sessions by final outcome.",
        purpose=purpose, result=result,
    )


def _count_part_bytes(purpose: str, nbytes: int) -> None:
    """upload_part_bytes_total{purpose} += nbytes.

    metrics.inc adds exactly one, and there is no add-by-N in app/metrics
    yet; this walks the same registry the same way `inc` does (declare,
    clean the labels, take the lock) so the exposition is a plain counter
    under the name API.md promises. Never raises, like everything there.
    """
    from . import metrics

    try:
        metrics._declare(
            "upload_part_bytes_total", "counter", "Bytes accepted into chunked upload parts."
        )
        key = metrics._clean({"purpose": purpose})
        with metrics._lock:
            series = metrics._counters.setdefault("upload_part_bytes_total", {})
            series[key] = series.get(key, 0.0) + float(nbytes)
    except Exception:  # noqa: BLE001 — a metric must never break a request
        pass


async def _sweep_quietly() -> None:
    """Run the expiry sweep behind an upload, at most once a minute, and
    never let it touch the response: housekeeping is not the caller's
    problem."""
    global _last_sweep_at
    now = time.monotonic()
    if now - _last_sweep_at < _SWEEP_INTERVAL_S:
        return
    _last_sweep_at = now
    try:
        swept = await asyncio.to_thread(sweep_expired_upload_sessions)
        if swept:
            log.info("swept %d expired upload session(s)", swept)
    except Exception:  # noqa: BLE001
        log.debug("upload session sweep skipped", exc_info=True)


@router.post("/chunked/init")
async def chunked_init(
    request: Request,
    conversation_id: str = Form(...),
    filename: str = Form(...),
    purpose: str = Form("document"),
    # Optional expectation (V29). A client that declares them lets `complete`
    # tell a missing FINAL part from a finished file; one that does not gets
    # the old behaviour, where the last part present is taken as the last.
    size: Optional[int] = Form(None),
    parts: Optional[int] = Form(None),
    part_size: Optional[int] = Form(None),
    user: UserRow = Depends(require_user),
    _attachments: None = Depends(require_attachments),
) -> dict:
    if not settings.dataset_uploads_enabled:
        raise HTTPException(status_code=404, detail="uploads are disabled")
    if purpose not in ("dataset", "document", "video"):
        raise HTTPException(status_code=400, detail="unknown upload purpose")
    if purpose == "video":
        from .video.api import require_video

        await require_video(request)
    # Everything the declaration makes impossible is refused HERE, before a
    # single byte moves: a file above the cap, more parts than the rail can
    # take, a part size no part could carry, or three numbers that disagree.
    if size is not None:
        if size < 0:
            raise HTTPException(status_code=400, detail="size cannot be negative")
        if size > _cap_total(purpose):
            raise _too_large(purpose)
    if parts is not None and not 1 <= parts <= _MAX_PARTS:
        raise HTTPException(
            status_code=400, detail=f"parts must be between 1 and {_MAX_PARTS}"
        )
    if part_size is not None:
        if part_size < 1:
            raise HTTPException(status_code=400, detail="part_size must be positive")
        if part_size > _PART_CAP:
            raise HTTPException(
                status_code=413, detail=f"part_size exceeds {_PART_CAP // (1024 * 1024)} MB"
            )
    if size is not None and parts is not None and part_size is not None:
        if not (parts - 1) * part_size < max(size, 1) <= parts * part_size:
            raise HTTPException(
                status_code=400, detail="size, parts and part_size do not agree"
            )
    await _own(conversation_id, user)
    await _sweep_quietly()

    upload_id = uuid.uuid4().hex
    session = await db.run_in_thread(
        db.create_upload_session,
        upload_id, int(user["id"]), conversation_id,
        os.path.basename(filename or "upload.bin"), purpose,
        expected_bytes=size, expected_parts=parts, part_size=part_size,
        ttl_hours=settings.upload_session_ttl_hours,
    )
    root = upload_root(conversation_id, upload_id)
    os.makedirs(os.path.join(root, _PARTS_DIR), exist_ok=True)
    _keep_uploads_warm()
    return {
        "upload_id": upload_id,
        "part_limit_bytes": _PART_CAP,
        "max_parts": _MAX_PARTS,
        "expires_at": session["expires_at"],
        "accepted_parts": [],
        "bytes_received": 0,
    }


@router.put("/chunked/{conversation_id}/{upload_id}/part/{index}")
async def chunked_part(
    conversation_id: str,
    upload_id: str,
    index: int,
    request: Request,
    user: UserRow = Depends(require_user),
) -> dict:
    """One part: streamed to a temporary file, hashed on the way, renamed
    into place only once the body ended cleanly.

    A body cut short — the reload that produced yesterday's
    `ClientDisconnect` traceback — leaves NO accepted part: the temporary
    file goes, nothing is recorded, and the answer is quiet. Before, the
    partial file kept the part's name and `complete` would have stitched it
    in as if it were whole.
    """
    await _own(conversation_id, user)
    session = await _load_session(conversation_id, upload_id, user)
    _require_uploading(session)
    if not 0 <= index < _MAX_PARTS:
        raise HTTPException(status_code=400, detail="part index out of range")
    expected_parts = session.get("expected_parts")
    if expected_parts is not None and index >= int(expected_parts):
        raise HTTPException(status_code=400, detail="part index out of range")
    declared = _declared_sha256(request)
    purpose = session["purpose"]
    root = upload_root(conversation_id, upload_id)
    # The quota is what the SESSION has accepted, not what the directory
    # holds: an index being replaced counts once, and a stray temporary file
    # from an interrupted attempt counts for nothing.
    already = sum(n for i, n in _present_parts(session, root).items() if i != index)
    cap_total = _cap_total(purpose)

    final = _part_path(root, index)
    # A nonce, not a bare `<index>.tmp`: a resumed upload can re-send a part
    # while the server is still draining the connection the browser gave up
    # on, and two writers on one temporary file would interleave.
    tmp = f"{final}.{uuid.uuid4().hex[:8]}.tmp"
    os.makedirs(os.path.dirname(final), exist_ok=True)
    written = 0
    digest = hashlib.sha256()
    try:
        with open(tmp, "wb") as out:
            async for chunk in request.stream():
                if not chunk:
                    continue
                written += len(chunk)
                if written > _PART_CAP:
                    raise HTTPException(
                        status_code=413,
                        detail=f"part exceeds {_PART_CAP // (1024 * 1024)} MB",
                    )
                if already + written > cap_total:
                    raise _too_large(purpose)
                digest.update(chunk)
                out.write(chunk)
            out.flush()
            await asyncio.to_thread(os.fsync, out.fileno())
    except ClientDisconnect:
        _discard(tmp)
        log.info(
            "chunked part %d of %s: client went away after %d bytes; nothing recorded",
            index, upload_id, written,
        )
        raise HTTPException(
            status_code=408,
            detail="The connection closed before the part was complete. Send it again.",
        )
    except BaseException:
        _discard(tmp)
        raise

    actual = digest.hexdigest()
    if declared is not None and actual != declared:
        _discard(tmp)
        raise HTTPException(
            status_code=422,
            detail="The part's bytes do not match X-Part-SHA256. Send it again.",
        )
    # The status is re-read AFTER the body: a `complete` can win the row while
    # a slow part is still streaming, and a part renamed in under an assembly
    # would land in a file that is already being read.
    fresh = await db.run_in_thread(db.get_upload_session, upload_id)
    if fresh is None or fresh.get("status") != "uploading":
        _discard(tmp)
        _require_uploading(fresh)
    os.replace(tmp, final)
    _keep_uploads_warm()
    row = await db.run_in_thread(db.record_upload_part, upload_id, index, written, actual)
    if row is None:  # the row vanished between the rename and the record
        _discard(final)
        raise HTTPException(status_code=404, detail="upload not found")
    _count_part_bytes(purpose, written)
    accepted, received = _accepted(row, root)
    return {"received": written, "accepted_parts": accepted, "bytes_received": received}


@router.get("/chunked/{conversation_id}/{upload_id}")
async def chunked_status(
    conversation_id: str,
    upload_id: str,
    user: UserRow = Depends(require_user),
) -> dict:
    """What the server already has — the resume call after a reload."""
    await _owned(conversation_id, user)
    session = await _load_session(conversation_id, upload_id, user)
    if session["status"] == "expired":
        # Swept: the parts are gone and the row is only a tombstone. The
        # contract says 404 so the browser treats it exactly like an id it
        # never had, and starts over rather than trying to resume nothing.
        raise HTTPException(status_code=404, detail="upload not found")
    return _session_view(session, upload_root(conversation_id, upload_id))


def _missing_parts(session: dict, present: dict) -> list:
    """The indexes the client must (re)send before the file can be assembled.

    With `parts` declared the range is exact. Without it the last index
    present is taken as the last there is — the only reading available —
    and a declared `part_size` then checks the shape: every part but the
    last must be exactly that size, the last at most that size. A part that
    fails the shape check is listed as missing, since sending it again is
    the fix. THE LIMIT: a client that declares neither `parts` nor `size`
    cannot be told apart from one whose final part has not arrived when the
    last present part is exactly `part_size` long — `complete` then trusts
    the caller, as the rail always did for such clients.
    """
    expected_parts = session.get("expected_parts")
    part_size = session.get("part_size")
    if expected_parts:
        count = int(expected_parts)
    else:
        count = (max(present) + 1) if present else 0
    missing = {i for i in range(count) if i not in present}
    if part_size:
        size = int(part_size)
        last = count - 1
        for index, nbytes in present.items():
            if index < last and nbytes != size:
                missing.add(index)
            elif index == last and nbytes > size:
                missing.add(index)
    return sorted(missing)


def _not_ready(session: dict, present: dict) -> Optional[JSONResponse]:
    """The refusal that sends the client back to uploading, or None when the
    parts on disk are a whole file by every declaration the client made.

    A JSONResponse rather than an HTTPException: the contract's 409 body is
    FLAT (`detail`, `missing_parts`, `accepted_parts` side by side) so the
    browser reads the list without unwrapping, and FastAPI would nest a
    dict `detail` one level down.
    """
    accepted = sorted(present)
    if not present:
        raise HTTPException(status_code=400, detail="no parts were uploaded")
    missing = _missing_parts(session, present)
    if missing:
        return JSONResponse(
            status_code=409,
            content={
                "detail": "parts are missing",
                "missing_parts": missing,
                "accepted_parts": accepted,
            },
        )
    expected_bytes = session.get("expected_bytes")
    total = sum(present.values())
    if expected_bytes is not None and total != int(expected_bytes):
        return JSONResponse(
            status_code=409,
            content={
                "detail": f"received {total} bytes, expected {int(expected_bytes)}",
                "missing_parts": [],
                "accepted_parts": accepted,
                "bytes_received": total,
            },
        )
    return None


def _assemble(root: str, present: dict, raw_path: str) -> int:
    """Concatenate the parts in index order into `_original` — a streaming
    copy through a 1 MiB buffer, written to a sibling temporary file and
    renamed so a crash mid-way never leaves a short `_original` that a later
    reader could mistake for the file.

    Blocking by design and therefore ALWAYS called through asyncio.to_thread:
    the pre-V29 `complete` did this concatenation on the event loop, and a
    400 MB video stalled every other request for the seconds it took
    (review F-02). The video finaliser's sha256 of the result is off-loop
    too (video/api.attach_upload runs store.hash_file in a thread).
    """
    os.makedirs(os.path.dirname(raw_path), exist_ok=True)
    tmp = raw_path + ".assembling"
    size = 0
    with open(tmp, "wb") as out:
        for index in sorted(present):
            with open(_part_path(root, index), "rb") as fh:
                while True:
                    chunk = fh.read(_CHUNK)
                    if not chunk:
                        break
                    size += len(chunk)
                    out.write(chunk)
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, raw_path)
    return size


def _stored_rejection(session: dict) -> HTTPException:
    """Replay the refusal the finaliser recorded, code and sentence alike."""
    stored = session.get("result") if isinstance(session.get("result"), dict) else {}
    code = int(stored.get("status_code") or 409)
    detail = stored.get("detail") or session.get("error") or "This upload was rejected."
    return HTTPException(status_code=code, detail=detail)


async def _finalise_session(conversation_id: str, upload_id: str, user: UserRow):
    """The winner's path: the row is `finalizing` and no part can land now.

    Three outcomes. Not ready (a hole, a wrong total, nothing at all): the row
    goes back to `uploading` and the client is told what to send. Rejected by
    a finaliser (too big for its purpose, not a dataset): the row is terminal
    and remembers the refusal so a retry replays it. Complete: the finaliser's
    response is stored on the row FIRST, then the parts are dropped — a crash
    between the two costs disk until the workspace TTL, never the answer.
    """
    from . import metrics

    session = await db.run_in_thread(db.get_upload_session, upload_id)
    if session is None:
        raise HTTPException(status_code=404, detail="upload not found")
    purpose = session["purpose"]
    filename = os.path.basename(session["filename"] or "upload.bin")
    root = upload_root(conversation_id, upload_id)
    present = await asyncio.to_thread(_present_parts, session, root)

    try:
        problem = _not_ready(session, present)
    except HTTPException:
        await db.run_in_thread(db.set_upload_session_status, upload_id, "uploading")
        raise
    if problem is not None:
        await db.run_in_thread(db.set_upload_session_status, upload_id, "uploading")
        return problem

    started = time.monotonic()
    raw_path = os.path.join(root, "_original", filename)
    try:
        total = sum(present.values())
        if total > _cap_total(purpose):
            raise _too_large(purpose)
        size = await asyncio.to_thread(_assemble, root, present, raw_path)
        if purpose == "document":
            result = await _finalise_document(conversation_id, upload_id, filename, size)
        elif purpose == "video":
            result = await _finalise_video(
                conversation_id, upload_id, filename, raw_path, size, user
            )
        else:
            result = await _finalise_dataset(conversation_id, upload_id, filename, raw_path, size)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else json.dumps(exc.detail)
        await asyncio.to_thread(remove_session_parts, conversation_id, upload_id)
        await db.run_in_thread(
            db.set_upload_session_status, upload_id, "rejected",
            error=detail, result={"status_code": int(exc.status_code), "detail": exc.detail},
        )
        _count_session(purpose, "rejected")
        raise
    except Exception:
        # Not a verdict on the file — a full disk, a database hiccup. The
        # parts are intact, so the row goes back to `uploading` and the
        # client may call `complete` again rather than upload everything twice.
        log.exception("chunked upload %s could not be finalised", upload_id)
        await db.run_in_thread(db.set_upload_session_status, upload_id, "uploading")
        raise HTTPException(
            status_code=500, detail="The upload could not be finalised. Try again."
        )

    await db.run_in_thread(
        db.set_upload_session_status, upload_id, "complete", result=result
    )
    await asyncio.to_thread(remove_session_parts, conversation_id, upload_id)
    metrics.observe(
        "upload_finalize_seconds", time.monotonic() - started,
        "Time from winning the finalisation to the stored result.", purpose=purpose,
    )
    _count_session(purpose, "complete")
    return result


@router.post("/chunked/{conversation_id}/{upload_id}/complete")
async def chunked_complete(
    conversation_id: str,
    upload_id: str,
    user: UserRow = Depends(require_user),
) -> dict:
    """Idempotent: exactly one caller finalises; everyone else gets ITS answer.

    The row moves `uploading -> finalizing` under a row lock, so of two
    concurrent completes one assembles and the other waits and replays the
    stored outcome — same body, same status code. A `complete` retried after
    a lost acknowledgement finds the row already `complete` and answers
    from it without touching the disk.
    """
    await _own(conversation_id, user)
    await _load_session(conversation_id, upload_id, user)
    deadline = time.monotonic() + _FINALIZE_WAIT_S
    while True:
        before = await db.run_in_thread(db.try_begin_upload_finalize, upload_id)
        if before is None:
            raise HTTPException(status_code=404, detail="upload not found")
        if before == "uploading":
            return await _finalise_session(conversation_id, upload_id, user)
        if before == "finalizing":
            if time.monotonic() >= deadline:
                raise HTTPException(
                    status_code=409,
                    detail="This upload is still being finalised. Ask again shortly.",
                )
            await asyncio.sleep(_FINALIZE_POLL_S)
            continue
        session = await db.run_in_thread(db.get_upload_session, upload_id)
        if session is None:
            raise HTTPException(status_code=404, detail="upload not found")
        if before == "complete":
            return session.get("result") or {"upload_id": upload_id}
        if before == "rejected":
            raise _stored_rejection(session)
        # cancelled / expired: the parts are gone, there is nothing to finish.
        raise HTTPException(status_code=404, detail="upload not found")


@router.delete("/chunked/{conversation_id}/{upload_id}", status_code=204)
async def chunked_cancel(
    conversation_id: str,
    upload_id: str,
    user: UserRow = Depends(require_user),
) -> Response:
    """Cancel an `uploading` session and reclaim its parts. Idempotent for a
    session that is already over (cancelled, expired, rejected): the bytes
    are gone either way, and the person asked for exactly that."""
    await _owned(conversation_id, user)
    session = await _load_session(conversation_id, upload_id, user)
    if session["status"] in ("finalizing", "complete"):
        raise HTTPException(
            status_code=409,
            detail="This upload has been finalised and belongs to the conversation now.",
        )
    if session["status"] == "uploading":
        await asyncio.to_thread(remove_session_parts, conversation_id, upload_id)
        await db.run_in_thread(
            db.set_upload_session_status, upload_id, "cancelled",
            error="cancelled by the uploader",
        )
        _count_session(session["purpose"], "cancelled")
    return Response(status_code=204)


def sweep_expired_upload_sessions(limit: int = 50) -> int:
    """Reclaim the parts of open sessions past their TTL and mark them
    `expired`. Returns how many were swept.

    Only `uploading` rows: a `finalizing` one is somebody's assembly in
    progress and API.md says the sweep never touches it (the work list
    accessor returns both, so it is filtered here). `_original` is never
    removed — it is not a session's to remove — and nothing outside
    `<workspace>/uploads` can be reached from a row. Synchronous on purpose:
    the async callers run it in a thread, and tests call it directly.
    """
    swept = 0
    for session in db.expired_upload_sessions(limit):
        if session.get("status") != "uploading":
            continue
        upload_id = str(session["id"])
        if not _HEX32.fullmatch(upload_id):
            continue
        remove_session_parts(str(session["conversation_id"]), upload_id)
        db.set_upload_session_status(
            upload_id, "expired", error="the upload session expired before it was completed"
        )
        _count_session(str(session["purpose"]), "expired")
        swept += 1
    return swept
