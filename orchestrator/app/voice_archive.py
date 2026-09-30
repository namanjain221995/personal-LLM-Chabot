"""The voice archive: finished recordings' audio on the worker's disk (2026-09-30).

THE OWNER'S ASK, 2026-09-30: "improve the storage of that audio", answered
"Move to worker's big disk". Every stored dictation (app/dictation.py, V42)
lives in VOICE_DATA_DIR/<user>/<session>/ on the head's root NVMe, beside the
OS, /var/lib/docker and production Postgres: one copy, no backup, and a disk
the database needs. The worker's disk is 3.7 TB and 17% used.

WHAT MOVES, AND WHAT DOES NOT. Only source.<ext> of a FINISHED recording (done
or failed, not deleted, no retranscription running), VOICE_ARCHIVE_AFTER_S
after it finished: 99.3% of a recording's bytes (measured on the head). The
transcript, parts, plan and results stay here, so the list, the previews, the
session state and the admin transcript never wait for the worker. Recording,
decoding and transcription never touch it: the fsync, flock and
acknowledgement path of dictation.append_part is unchanged, and a worker that
is down stops nothing but playback of recordings that already moved.

THE STATES (V43, voice_sessions.archive_state):
    local     only the head has the audio;
    copied    the store holds a copy READ BACK AND HASHED, and the head file
              still exists (just copied, held, or brought back);
    archived  the store holds the only copy; the head file is gone.

ONE PASS OF THE MOVER (a daemon thread with its own event loop, every
VOICE_ARCHIVE_INTERVAL_S while VOICE_ARCHIVE_ENABLED):
    1. /health of the store; nothing more when it is down.
    2. PURGE: deleted recordings whose copy is still on the store.
    3. RELEASE copied recordings whose hold has passed.
    4. COPY up to VOICE_ARCHIVE_BATCH due recordings, oldest first, claimed
       with FOR UPDATE SKIP LOCKED (two processes during a rolling recreate
       never take the same one): re-hash the head file against
       source_sha256, PUT it at VOICE_ARCHIVE_RATE_BYTES_PER_S (the store
       checks length and sha256 and fsyncs before it answers), GET it back and
       hash it again, mark it copied on conditions that still hold, then
       release it.
    Hourly, the head sweep; daily, the reconcile against the store's
    inventory. Failures back off per recording (min(3600, 60 x 2^(n-1)) s,
    +/-20%), with a reason code in archive_error.

RELEASE is the one step that deletes the only local copy, so it is guarded
three ways: it runs under flock(<session>/.archive.lock), the lock every
restore takes too; its UPDATE requires the row still copied, finished, not
deleted, not being retranscribed, past any hold, and not an earlier part of a
continuation that is still recording or finishing (walked recursively, since
a continuation decodes every recording it continues); and the file is
unlinked only after that UPDATE changed the row.

RESTORE (`ensure_local`), for a retranscription and for a continuation whose
earlier recording moved, takes the same lock, re-reads the row, and for an
archived one downloads into .restore-<uuid>, checks length and sha256,
fsyncs, renames into place and marks the row copied with a hold of
VOICE_ARCHIVE_HOLD_S. For any other row it only sets the hold, which is what
stops a recording being released between a retranscription's checks and its
own UPDATE. It never creates a session folder: a discarded recording must not
come back.

READS. `recording_response` serves the head file when it exists (every local
or copied recording), and otherwise streams the store's copy, forwarding
Range and If-Range and passing 200/206/416 and their headers back. Store down:
503 archive_unavailable with Retry-After. Store has no copy: 410
audio_missing, counted, and flagged on the row for the reconcile.

DELETES keep dictation's order (row cancelled, then the folder), then ask the
store to delete its copy at once without waiting; the purge step retries what
that misses, and the reconcile removes anything left.

ROLLBACK. VOICE_ARCHIVE_ENABLED=false, then `python -m app.voice_archive
recall-all` (inside the orchestrator container) BEFORE any code rollback: it
brings every recording back with sha256 checks and marks it local again. Code
that predates V43 cannot see the store and answers 410 for anything archived.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import concurrent.futures
import contextlib
import fcntl
import hashlib
import json
import logging
import os
import ssl
import sys
import threading
import time
import uuid
import weakref
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence, Tuple

import httpx
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse

from . import db, dictation, metrics
from .config import settings

log = logging.getLogger(__name__)

LOCAL = "local"
COPIED = "copied"
ARCHIVED = "archived"

#: Why a store call or a copy failed: archive_error on the row, and the
#: `reason` of voice_archive_errors_total. Closed (metrics.py lists them).
ERROR_REASONS = (
    "unreachable", "timeout", "tls", "auth", "storage_full", "busy", "conflict",
    "remote_sha_mismatch", "missing", "http_4xx", "http_5xx",
    "local_missing", "local_size_mismatch", "local_sha_mismatch", "remote_missing",
)
#: A failure of the STORE (not of one recording): the rest of the pass waits.
STOP_REASONS = frozenset({"unreachable", "timeout", "tls", "auth", "storage_full", "busy", "http_5xx"})
PROXY_RESULTS = ("ok", "partial", "not_satisfiable", "unavailable", "missing")
RESTORE_RESULTS = ("restored", "held", "unavailable", "missing", "mismatch", "no_space", "deleted")
RECONCILE_RESULTS = ("orphan_deleted", "deleted_row_purged", "repaired", "remote_missing", "foreign")

UNAVAILABLE_DETAIL = (
    "This recording is kept on the archive server, which isn't answering right now. "
    "Nothing is lost; try again in a few minutes."
)
MISSING_DETAIL = "This recording's audio could not be found on the archive server."

#: Per-request limits for the store. Connect is short: the store is one LAN
#: hop away, and a dead one must answer 503 to a person promptly.
_CONNECT_TIMEOUT_S = 2.0
_READ_TIMEOUT_S = 30.0
#: An idle pooled connection is dropped after this, well before the store
#: closes it (compose/voice-store/server.py KEEP_ALIVE_TIMEOUT_S, 5 s). With
#: httpx's default of 5 s the two were equal, and a request reusing a
#: connection the store was closing at that moment failed with nothing
#: wrong: 503 archive_unavailable to someone seeking in a moved recording.
_KEEPALIVE_EXPIRY_S = 2.0
_DELETE_TIMEOUT_S = 2.0
_HEALTH_TIMEOUT_S = 5.0
#: Download and hashing chunks; the proxy streams 64 KiB pieces.
_FILE_CHUNK = 1024 * 1024
_PROXY_CHUNK = 64 * 1024
#: The PACED copy and read-back move in steps this small. Every step leaves
#: at line rate (1 GbE), so the step size is the burst other traffic on the
#: management LAN queues behind: 1 MiB steps at a 20 MiB/s average put ping
#: at 1.69 ms average / 4.26 ms max (candidate test, 2026-09-30), against
#: 0.27 ms idle.
_PACE_CHUNK = 64 * 1024
_BACKOFF_CAP_S = 3600.0
_CLAIM_LEASE_S = 600
_RECONCILE_EVERY_S = 86400.0
_HEAD_SWEEP_EVERY_S = 3600.0
#: An object with no row at all is deleted only after this long: a row is
#: created long before its first byte, so this covers only a users row an
#: operator deleted by hand (the V42 cascade) and a verify run that died.
_ORPHAN_GRACE_S = 86400.0
_TEMP_MAX_AGE_S = 3600.0
_LOCK_NAME = ".archive.lock"
_RESTORE_PREFIX = ".restore-"
#: How long a request waits for another process's release or restore of the
#: same recording (the flock) before it gives up.
_LOCK_WAIT_S = 120.0


class StoreError(Exception):
    """A store call that did not do what was asked; `reason` is one of ERROR_REASONS."""

    def __init__(self, reason: str, detail: str = "", status: Optional[int] = None) -> None:
        super().__init__(detail or reason)
        self.reason = reason if reason in ERROR_REASONS else "http_4xx"
        self.detail = detail
        self.status = status


def configured() -> bool:
    """URL and token: enough to READ the archive (playback, restore, deletes)."""
    return bool(settings.voice_archive_url and settings.voice_archive_token)


def enabled() -> bool:
    """...and the switch that starts the mover."""
    return bool(settings.voice_archive_enabled) and configured()


def _unavailable() -> dictation.SessionError:
    return dictation.SessionError(503, "archive_unavailable", UNAVAILABLE_DETAIL, retry_after_s=30)


def _missing() -> dictation.SessionError:
    return dictation.SessionError(410, "audio_missing", MISSING_DETAIL)


def _deleted() -> dictation.SessionError:
    return dictation.SessionError(410, "audio_deleted", "This recording's audio has been deleted.")


def _error(reason: str) -> None:
    metrics.inc("voice_archive_errors_total", "voice archive failures, by reason", reason=reason)


# ------------------------------------------------------------ the client --


_SSL_LOCK = threading.Lock()
_SSL_CONTEXTS: Dict[str, ssl.SSLContext] = {}


def _pinned_context() -> ssl.SSLContext:
    """TLS that trusts exactly the store's own certificate (and no CA bundle).

    The store's key is generated on the worker and never leaves it; the
    public certificate comes from scripts/voice-store.sh as base64 PEM, with
    the store's address as its IP subjectAltName, so the hostname check still
    applies."""
    b64 = settings.voice_archive_tls_cert_b64
    if not b64:
        raise StoreError(
            "tls", "VOICE_ARCHIVE_URL is https:// but VOICE_ARCHIVE_TLS_CERT_B64 is empty: the store's certificate is pinned"
        )
    with _SSL_LOCK:
        context = _SSL_CONTEXTS.get(b64)
        if context is None:
            try:
                pem = base64.b64decode(b64, validate=True).decode("ascii")
                context = ssl.create_default_context(cadata=pem)
            except (binascii.Error, UnicodeDecodeError, ssl.SSLError, ValueError) as exc:
                raise StoreError("tls", f"VOICE_ARCHIVE_TLS_CERT_B64 is not a base64 PEM certificate ({type(exc).__name__})") from None
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            _SSL_CONTEXTS.clear()
            _SSL_CONTEXTS[b64] = context
        return context


#: loop id -> {"loop": weakref, "factory", "key", "client", "lock"}: one
#: pooled client per event loop (the mover's, the request loop's), rebuilt
#: when the store's address or certificate changes. rerank.py's pattern.
_CLIENTS: Dict[int, Dict[str, Any]] = {}
_CLIENTS_MAX = 8


async def _client() -> httpx.AsyncClient:
    if not configured():
        raise StoreError("unreachable", "the voice archive is not configured (VOICE_ARCHIVE_URL, VOICE_ARCHIVE_TOKEN)")
    loop = asyncio.get_running_loop()
    url = settings.voice_archive_url
    verify: Any = True
    if url.lower().startswith("https://"):
        verify = _pinned_context()
    factory = httpx.AsyncClient
    key = (url, settings.voice_archive_tls_cert_b64)
    state = _CLIENTS.get(id(loop))
    if state is None or state["loop"]() is not loop or state["factory"] is not factory or state["key"] != key:
        state = {"loop": weakref.ref(loop), "factory": factory, "key": key, "client": None, "lock": asyncio.Lock()}
        _CLIENTS[id(loop)] = state
        while len(_CLIENTS) > _CLIENTS_MAX:
            oldest = next(iter(_CLIENTS))
            if oldest == id(loop):
                break
            _CLIENTS.pop(oldest, None)  # dropped, not closed: aclose must run on its own loop
    client = state["client"]
    if client is not None and not getattr(client, "is_closed", False):
        return client
    async with state["lock"]:
        client = state["client"]
        if client is None or getattr(client, "is_closed", False):
            client = await asyncio.to_thread(
                lambda: factory(
                    verify=verify,
                    timeout=httpx.Timeout(_READ_TIMEOUT_S, connect=_CONNECT_TIMEOUT_S),
                    limits=httpx.Limits(
                        max_connections=8, max_keepalive_connections=4, keepalive_expiry=_KEEPALIVE_EXPIRY_S,
                    ),
                    follow_redirects=False,
                    # The store is a LAN address: an HTTP(S)_PROXY in the
                    # environment must never carry recordings elsewhere.
                    trust_env=False,
                )
            )
            state["client"] = client
        return client


async def close_client() -> None:
    """Close this loop's pooled client (application shutdown). Never raises."""
    loop = asyncio.get_running_loop()
    state = _CLIENTS.pop(id(loop), None)
    if state is None or state["loop"]() is not loop or state.get("client") is None:
        return
    with contextlib.suppress(Exception):
        await state["client"].aclose()


def _url(path: str) -> str:
    return settings.voice_archive_url.rstrip("/") + path


def object_path(user_id: int, session_id: str, ext: str) -> str:
    if not dictation._SESSION_ID.match(session_id or "") or ext not in set(dictation._EXTENSIONS.values()):
        raise ValueError("not a stored recording")
    return f"/v1/recordings/{int(user_id)}/{session_id}/source.{ext}"


def _object_url(row: Dict[str, Any]) -> str:
    return _url(object_path(int(row["user_id"]), row["id"], row["ext"]))


def _auth() -> Dict[str, str]:
    return {"Authorization": f"Bearer {settings.voice_archive_token}"}


def _transport_reason(exc: BaseException) -> str:
    text = str(exc)
    if "CERTIFICATE_VERIFY_FAILED" in text or "certificate" in text.lower() or isinstance(exc, ssl.SSLError):
        return "tls"
    if isinstance(exc, httpx.ConnectTimeout):
        return "unreachable"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    return "unreachable"


def _status_reason(status: int) -> str:
    if status in (401, 403):
        return "auth"
    if status == 404:
        return "missing"
    if status == 409:
        return "conflict"
    if status == 422:
        return "remote_sha_mismatch"
    if status == 503:
        return "busy"
    if status == 507:
        return "storage_full"
    if status >= 500:
        return "http_5xx"
    return "http_4xx"


def _timeout(read: float = _READ_TIMEOUT_S) -> httpx.Timeout:
    return httpx.Timeout(read, connect=_CONNECT_TIMEOUT_S)


async def _pace(started: float, done: int, rate: int) -> None:
    if rate > 0:
        ahead = done / rate - (time.monotonic() - started)
        if ahead > 0:
            await asyncio.sleep(ahead)


def _read_dropping(fd: int, offset: int, size: int) -> bytes:
    """pread, then drop what was read from the page cache: copying an hour
    of audio must not grow the head's cache by 58 MB."""
    data = os.pread(fd, size, offset)
    if data:
        with contextlib.suppress(AttributeError, OSError):
            os.posix_fadvise(fd, offset, len(data), os.POSIX_FADV_DONTNEED)
    return data


async def _paced_file(path: str, rate: int) -> AsyncIterator[bytes]:
    fd = await asyncio.to_thread(os.open, path, os.O_RDONLY)
    try:
        started = time.monotonic()
        sent = 0
        while True:
            chunk = await asyncio.to_thread(_read_dropping, fd, sent, _PACE_CHUNK)
            if not chunk:
                return
            sent += len(chunk)
            yield chunk
            await _pace(started, sent, rate)
    finally:
        os.close(fd)


def _hash_file(path: str) -> Tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    fd = os.open(path, os.O_RDONLY)
    try:
        while True:
            chunk = _read_dropping(fd, total, 4 * _FILE_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
    finally:
        os.close(fd)
    return digest.hexdigest(), total


def _fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# ------------------------------------------------------ the store calls --


async def store_health() -> Dict[str, Any]:
    client = await _client()
    try:
        response = await client.get(_url("/health"), timeout=_timeout(_HEALTH_TIMEOUT_S))
    except httpx.HTTPError as exc:
        raise StoreError(_transport_reason(exc), str(exc)[:200]) from None
    if response.status_code != 200:
        raise StoreError(_status_reason(response.status_code), status=response.status_code)
    try:
        body = response.json()
    except ValueError:
        raise StoreError("http_5xx", "the store's /health is not JSON") from None
    return body if isinstance(body, dict) else {}


async def put_object(row: Dict[str, Any], path: str, size: int, sha: str) -> str:
    """PUT the head file; "new" or "already" (the same bytes were there)."""
    client = await _client()
    headers = {
        **_auth(),
        # With a Content-Length httpx sends the body as it is read, never chunked.
        "Content-Length": str(size),
        "X-Content-SHA256": sha,
        "X-Recording-Type": str(row.get("mime_type") or ""),
    }
    for name, key in (("X-Recording-Created-At", "created_at"), ("X-Recording-Finished-At", "finished_at")):
        if isinstance(row.get(key), datetime):
            headers[name] = row[key].isoformat()
    try:
        response = await client.put(
            _object_url(row), content=_paced_file(path, settings.voice_archive_rate_bytes_per_s),
            headers=headers, timeout=_timeout(),
        )
    except httpx.HTTPError as exc:
        raise StoreError(_transport_reason(exc), str(exc)[:200]) from None
    if response.status_code == 201:
        return "new"
    if response.status_code == 200:
        return "already"
    raise StoreError(_status_reason(response.status_code), _refusal_reason(response), status=response.status_code)


def _refusal_reason(response: httpx.Response) -> str:
    with contextlib.suppress(Exception):
        body = response.json()
        if isinstance(body, dict):
            return str(body.get("reason") or "")[:60]
    return ""


async def read_back(row: Dict[str, Any], size: int, sha: str) -> None:
    """GET the stored copy and hash it: the head's copy is deleted next, so
    the store's word that it wrote the right bytes is not enough."""
    client = await _client()
    digest = hashlib.sha256()
    got = 0
    started = time.monotonic()
    try:
        async with client.stream("GET", _object_url(row), headers=_auth(), timeout=_timeout()) as response:
            if response.status_code != 200:
                raise StoreError(_status_reason(response.status_code), status=response.status_code)
            # Read in small paced steps: the receive window then opens a
            # step at a time, so the store's sends are paced too.
            async for chunk in response.aiter_bytes(_PACE_CHUNK):
                digest.update(chunk)
                got += len(chunk)
                await _pace(started, got, settings.voice_archive_rate_bytes_per_s)
    except httpx.HTTPError as exc:
        raise StoreError(_transport_reason(exc), str(exc)[:200]) from None
    if got != size or digest.hexdigest() != sha:
        raise StoreError("remote_sha_mismatch", f"read back {got} bytes that do not hash to the recording's sha256")


async def delete_object(user_id: int, session_id: str, *, timeout: float = _READ_TIMEOUT_S) -> None:
    client = await _client()
    try:
        response = await client.delete(
            _url(f"/v1/recordings/{int(user_id)}/{session_id}"), headers=_auth(), timeout=_timeout(timeout),
        )
    except httpx.HTTPError as exc:
        raise StoreError(_transport_reason(exc), str(exc)[:200]) from None
    if response.status_code not in (200, 204):
        raise StoreError(_status_reason(response.status_code), status=response.status_code)


async def inventory_page(after: str, limit: int = 500) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    client = await _client()
    try:
        response = await client.get(
            _url("/v1/inventory"), params={"after": after, "limit": limit}, headers=_auth(), timeout=_timeout(),
        )
    except httpx.HTTPError as exc:
        raise StoreError(_transport_reason(exc), str(exc)[:200]) from None
    if response.status_code != 200:
        raise StoreError(_status_reason(response.status_code), status=response.status_code)
    body = response.json()
    objects = [o for o in body.get("objects") or [] if isinstance(o, dict)]
    return objects, body.get("next_after") or None


async def download_to(row: Dict[str, Any], dest: str, size: int, sha: Optional[str]) -> None:
    """Stream the stored copy into `dest` (created, 0600), check it, fsync it."""
    client = await _client()
    digest = hashlib.sha256()
    got = 0
    fd = await asyncio.to_thread(os.open, dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        pending = bytearray()
        try:
            async with client.stream("GET", _object_url(row), headers=_auth(), timeout=_timeout()) as response:
                if response.status_code != 200:
                    raise StoreError(_status_reason(response.status_code), status=response.status_code)
                async for chunk in response.aiter_bytes(_FILE_CHUNK):
                    digest.update(chunk)
                    got += len(chunk)
                    pending += chunk
                    if len(pending) >= _FILE_CHUNK:
                        data, pending = bytes(pending), bytearray()
                        await asyncio.to_thread(_write_all, fd, data)
        except httpx.HTTPError as exc:
            raise StoreError(_transport_reason(exc), str(exc)[:200]) from None
        if pending:
            await asyncio.to_thread(_write_all, fd, bytes(pending))
        if got != size or (sha is not None and digest.hexdigest() != sha):
            raise StoreError("remote_sha_mismatch", f"downloaded {got} bytes that are not this recording")
        await asyncio.to_thread(os.fsync, fd)
    finally:
        os.close(fd)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


# ------------------------------------------------------------ the rows --


def _columns(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return ", ".join(prefix + name.strip() for name in dictation._COLUMNS.split(","))


def _claim(limit: int, after_s: float) -> List[Dict[str, Any]]:
    """Due recordings, oldest first, taken for _CLAIM_LEASE_S so another
    process's pass skips them (SKIP LOCKED while this runs, the lease after)."""
    with db.connection() as con:
        rows = con.execute(
            f"""WITH due AS (
                    SELECT id FROM voice_sessions
                    WHERE archive_state = 'local' AND status IN ('done', 'failed')
                      AND audio_deleted_at IS NULL AND retranscribe IS NULL AND bytes_stored > 0
                      AND finished_at < now() - make_interval(secs => %s)
                      AND (archive_next_at IS NULL OR archive_next_at <= now())
                    ORDER BY finished_at
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED)
                UPDATE voice_sessions v SET archive_next_at = now() + make_interval(secs => %s)
                FROM due WHERE v.id = due.id
                RETURNING {_columns('v')}""",
            (float(after_s), int(limit), _CLAIM_LEASE_S),
        ).fetchall()
    return sorted((dict(r) for r in rows), key=lambda r: (r.get("finished_at") or datetime.min.replace(tzinfo=timezone.utc)))


def _set_sha(session_id: str, sha: str) -> None:
    with db.connection() as con:
        con.execute(
            "UPDATE voice_sessions SET source_sha256 = %s WHERE id = %s AND source_sha256 IS NULL", (sha, session_id)
        )


def _fail(session_id: str, reason: str) -> None:
    """Back off this recording: min(3600, 60 x 2^(n-1)) s, +/-20%."""
    with db.connection() as con:
        con.execute(
            """UPDATE voice_sessions SET archive_attempts = archive_attempts + 1, archive_error = %s,
                   archive_next_at = now() + make_interval(
                       secs => LEAST(%s, 60 * power(2, LEAST(archive_attempts, 20))) * (0.8 + random() * 0.4))
               WHERE id = %s""",
            (reason, _BACKOFF_CAP_S, session_id),
        )


def _mark_copied(session_id: str, sha: str) -> Optional[Dict[str, Any]]:
    with db.connection() as con:
        row = con.execute(
            f"""UPDATE voice_sessions SET archive_state = 'copied', archived_at = now(), archive_attempts = 0,
                    archive_error = NULL, archive_next_at = NULL
                WHERE id = %s AND archive_state = 'local' AND status IN ('done', 'failed')
                  AND audio_deleted_at IS NULL AND retranscribe IS NULL AND source_sha256 = %s
                RETURNING {dictation._COLUMNS}""",
            (session_id, sha),
        ).fetchone()
    return dict(row) if row else None


#: The recordings a live continuation still decodes: every predecessor of a
#: session that is recording or finishing, however deep the chain.
_LIVE_CHAIN = """
    WITH RECURSIVE chain(id) AS (
        SELECT continues_session_id FROM voice_sessions
        WHERE status IN ('recording', 'finishing') AND continues_session_id IS NOT NULL
      UNION
        SELECT s.continues_session_id FROM voice_sessions s JOIN chain ON s.id = chain.id
        WHERE s.continues_session_id IS NOT NULL
    ) SELECT id FROM chain WHERE id IS NOT NULL"""


def _mark_released(session_id: str) -> Optional[Dict[str, Any]]:
    with db.connection() as con:
        row = con.execute(
            f"""UPDATE voice_sessions v SET archive_state = 'archived', head_released_at = now()
                WHERE v.id = %s AND v.archive_state = 'copied' AND v.status IN ('done', 'failed')
                  AND v.retranscribe IS NULL AND v.audio_deleted_at IS NULL
                  AND (v.head_hold_until IS NULL OR v.head_hold_until <= now())
                  AND v.id NOT IN ({_LIVE_CHAIN})
                RETURNING {_columns('v')}""",
            (session_id,),
        ).fetchone()
    return dict(row) if row else None


def _releasable(limit: int) -> List[Dict[str, Any]]:
    with db.connection() as con:
        rows = con.execute(
            f"""SELECT {dictation._COLUMNS} FROM voice_sessions
                WHERE archive_state = 'copied' AND status IN ('done', 'failed') AND retranscribe IS NULL
                  AND audio_deleted_at IS NULL AND (head_hold_until IS NULL OR head_hold_until <= now())
                ORDER BY archived_at LIMIT %s""",
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]


def _purge_due(limit: int) -> List[Dict[str, Any]]:
    with db.connection() as con:
        rows = con.execute(
            """SELECT id, user_id FROM voice_sessions
               WHERE audio_deleted_at IS NOT NULL AND archive_state <> 'local' AND remote_purged_at IS NULL
               ORDER BY audio_deleted_at LIMIT %s""",
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]


def _mark_purged(session_id: str) -> None:
    with db.connection() as con:
        con.execute(
            "UPDATE voice_sessions SET remote_purged_at = COALESCE(remote_purged_at, now()) WHERE id = %s",
            (session_id,),
        )


def _set_hold(session_id: str, hold_s: float) -> Optional[Dict[str, Any]]:
    with db.connection() as con:
        row = con.execute(
            f"""UPDATE voice_sessions
                SET head_hold_until = GREATEST(COALESCE(head_hold_until, now()), now() + make_interval(secs => %s))
                WHERE id = %s RETURNING {dictation._COLUMNS}""",
            (float(hold_s), session_id),
        ).fetchone()
    return dict(row) if row else None


def _mark_restored(session_id: str, hold_s: float) -> Optional[Dict[str, Any]]:
    with db.connection() as con:
        row = con.execute(
            f"""UPDATE voice_sessions SET archive_state = 'copied', head_released_at = NULL, archive_error = NULL,
                    head_hold_until = now() + make_interval(secs => %s)
                WHERE id = %s AND archive_state = 'archived' RETURNING {dictation._COLUMNS}""",
            (float(hold_s), session_id),
        ).fetchone()
    return dict(row) if row else None


def _flag(session_id: str, reason: str) -> None:
    with db.connection() as con:
        con.execute("UPDATE voice_sessions SET archive_error = %s WHERE id = %s", (reason, session_id))


def _rows_by_ids(ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    if not ids:
        return {}
    with db.connection() as con:
        rows = con.execute(
            f"SELECT {dictation._COLUMNS} FROM voice_sessions WHERE id = ANY(%s)", (list(ids),)
        ).fetchall()
    return {r["id"]: dict(r) for r in rows}


def _archived_rows() -> List[Dict[str, Any]]:
    with db.connection() as con:
        rows = con.execute(
            f"SELECT {dictation._COLUMNS} FROM voice_sessions WHERE archive_state = 'archived' AND audio_deleted_at IS NULL"
        ).fetchall()
    return [dict(r) for r in rows]


def _db_now() -> datetime:
    with db.connection() as con:
        return con.execute("SELECT now() AS now").fetchone()["now"]


def _repair(session_id: str) -> bool:
    with db.connection() as con:
        row = con.execute(
            """UPDATE voice_sessions SET archive_state = 'archived', archived_at = COALESCE(archived_at, now()),
                   head_released_at = COALESCE(head_released_at, now()), archive_error = NULL, archive_next_at = NULL
               WHERE id = %s AND archive_state IN ('local', 'copied') AND audio_deleted_at IS NULL
               RETURNING id""",
            (session_id,),
        ).fetchone()
    return row is not None


def _mark_local(session_id: str) -> Optional[Dict[str, Any]]:
    with db.connection() as con:
        row = con.execute(
            f"""UPDATE voice_sessions SET archive_state = 'local', archived_at = NULL, head_released_at = NULL,
                    head_hold_until = NULL, archive_attempts = 0, archive_next_at = NULL, archive_error = NULL
                WHERE id = %s AND archive_state = 'copied' RETURNING {dictation._COLUMNS}""",
            (session_id,),
        ).fetchone()
    return dict(row) if row else None


def _not_local_rows() -> List[Dict[str, Any]]:
    with db.connection() as con:
        rows = con.execute(
            f"""SELECT {dictation._COLUMNS} FROM voice_sessions
                WHERE archive_state <> 'local' AND audio_deleted_at IS NULL AND status <> 'cancelled'
                ORDER BY created_at"""
        ).fetchall()
    return [dict(r) for r in rows]


def _gauges() -> Dict[str, Any]:
    with db.connection() as con:
        row = con.execute(
            """SELECT
                count(*) FILTER (WHERE backlog) AS backlog_sessions,
                COALESCE(sum(bytes_stored) FILTER (WHERE backlog), 0) AS backlog_bytes,
                count(*) FILTER (WHERE backlog AND finished_at < now() - make_interval(secs => %s)) AS overdue_sessions,
                COALESCE(EXTRACT(EPOCH FROM now() - min(finished_at) FILTER (
                    WHERE backlog AND finished_at < now() - make_interval(secs => %s))), 0) AS overdue_oldest_s,
                count(*) FILTER (WHERE archive_state = 'copied' AND audio_deleted_at IS NULL) AS copied_sessions,
                count(*) FILTER (WHERE archive_state = 'archived' AND audio_deleted_at IS NULL) AS archived_sessions,
                COALESCE(sum(bytes_stored) FILTER (
                    WHERE archive_state = 'archived' AND audio_deleted_at IS NULL), 0) AS archived_bytes,
                count(*) FILTER (WHERE audio_deleted_at IS NOT NULL AND archive_state <> 'local'
                                   AND remote_purged_at IS NULL) AS purge_pending,
                count(*) FILTER (WHERE archive_error = 'remote_missing' AND archive_state = 'archived'
                                   AND audio_deleted_at IS NULL) AS remote_missing
               FROM (SELECT *, (archive_state = 'local' AND status IN ('done', 'failed') AND audio_deleted_at IS NULL
                                AND bytes_stored > 0) AS backlog
                     FROM voice_sessions) s""",
            (float(settings.voice_archive_after_s), float(settings.voice_archive_after_s)),
        ).fetchone()
    return dict(row) if row else {}


def _publish_gauges(values: Dict[str, Any]) -> None:
    helps = {
        "backlog_sessions": "finished recordings whose audio is still only on the head",
        "backlog_bytes": "bytes of those recordings",
        "overdue_sessions": "backlog recordings past VOICE_ARCHIVE_AFTER_S (should be 0)",
        "overdue_oldest_s": "seconds the oldest overdue recording has waited past its grace",
        "copied_sessions": "recordings on both the head and the store",
        "archived_sessions": "recordings whose only copy is on the store",
        "archived_bytes": "bytes of those recordings",
        "purge_pending": "deleted recordings whose copy on the store is still to be deleted",
        "remote_missing": "archived recordings the store no longer has (the reconcile)",
    }
    names = {"overdue_oldest_s": "voice_archive_overdue_oldest_seconds"}
    for key, text in helps.items():
        metrics.set_gauge(names.get(key, f"voice_archive_{key}"), float(values.get(key) or 0), text)


def _publish_store(health: Optional[Dict[str, Any]]) -> None:
    metrics.set_gauge("voice_archive_store_up", 1.0 if health and health.get("ready") else 0.0, "1 when the voice archive store answers ready")
    if not health:
        return
    for key, name, text in (
        ("free_bytes", "voice_archive_store_free_bytes", "free bytes on the archive disk (worker)"),
        ("min_free_bytes", "voice_archive_store_min_free_bytes", "the archive's free-space floor"),
        ("objects", "voice_archive_store_objects", "recordings on the archive store"),
        ("bytes", "voice_archive_store_bytes", "bytes on the archive store"),
    ):
        if isinstance(health.get(key), (int, float)):
            metrics.set_gauge(name, float(health[key]), text)
    scrub = health.get("scrub") if isinstance(health.get("scrub"), dict) else {}
    if isinstance(scrub.get("mismatches"), (int, float)):
        metrics.set_gauge(
            "voice_archive_store_scrub_mismatches", float(scrub["mismatches"]),
            "stored recordings whose bytes no longer match their sha256 (the store's last scrub)",
        )


# ------------------------------------------------------------- the lock --


@contextlib.asynccontextmanager
async def _session_flock(directory: str, *, timeout: float = _LOCK_WAIT_S):
    """flock(<session>/.archive.lock), polled so a cancelled caller never
    leaves a thread blocked on it. FileNotFoundError when the folder is gone:
    the lock file is created inside an existing folder only."""
    fd = await asyncio.to_thread(os.open, os.path.join(directory, _LOCK_NAME), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"{directory} stayed locked for {timeout:.0f}s") from None
                await asyncio.sleep(0.05)
        yield
    finally:
        os.close(fd)


def _unlink_and_sync(path: str, directory: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)
    with contextlib.suppress(OSError):
        _fsync_dir(directory)


# ------------------------------------------------------------ the mover --


async def release(row: Dict[str, Any]) -> bool:
    """Delete the head's copy of a copied recording, if everything still allows it."""
    directory = dictation.session_dir(row["user_id"], row["id"])
    try:
        async with _session_flock(directory):
            released = await db.run_in_thread(_mark_released, row["id"])
            if released is None:
                return False
            await asyncio.to_thread(_unlink_and_sync, dictation.source_path(released), directory)
    except FileNotFoundError:
        return False  # the folder is gone: discarded, or swept
    except TimeoutError:
        return False  # a restore holds it; the next pass tries again
    metrics.inc("voice_archive_released_total", "head copies released after a verified archive copy")
    return True


async def copy_one(row: Dict[str, Any], *, room: Optional[int] = None) -> str:
    """Copy one claimed recording to the store and release it. Returns what
    happened; raises StoreError when the STORE failed (the pass then stops)."""
    session_id = row["id"]
    path = dictation.source_path(row)
    size = int(row.get("bytes_stored") or 0)
    if room is not None and size > room:
        await db.run_in_thread(_fail, session_id, "storage_full")
        _error("storage_full")
        raise StoreError("storage_full", "the store's free space is under its floor")
    if not await asyncio.to_thread(os.path.exists, path):
        await db.run_in_thread(_fail, session_id, "local_missing")
        _error("local_missing")
        return "local_missing"
    sha, actual = await asyncio.to_thread(_hash_file, path)
    if actual != size:
        await db.run_in_thread(_fail, session_id, "local_size_mismatch")
        _error("local_size_mismatch")
        return "local_size_mismatch"
    known = row.get("source_sha256")
    if known is None:
        await db.run_in_thread(_set_sha, session_id, sha)
    elif known != sha:
        await db.run_in_thread(_fail, session_id, "local_sha_mismatch")
        _error("local_sha_mismatch")
        log.error("voice archive: %s no longer matches its sha256 on the head; not copied", session_id)
        return "local_sha_mismatch"
    try:
        await put_object(row, path, size, sha)
        await read_back(row, size, sha)
    except StoreError as exc:
        await db.run_in_thread(_fail, session_id, exc.reason)
        _error(exc.reason)
        raise
    metrics.inc("voice_archive_copied_total", "recordings copied to the store and read back")
    copied = await db.run_in_thread(_mark_copied, session_id, sha)
    if copied is None:
        current = await db.run_in_thread(dictation._row, session_id)
        if current is None or current["status"] == dictation.STATUS_CANCELLED or current.get("audio_deleted_at") is not None:
            # Deleted while it was copied: the copy goes too.
            with contextlib.suppress(StoreError):
                await delete_object(row["user_id"], session_id)
                if current is not None:
                    await db.run_in_thread(_mark_purged, session_id)
            return "deleted_meanwhile"
        # A retranscription started: the stored bytes are the same bytes, so
        # the copy stays and the next pass finds it already there.
        return "busy_meanwhile"
    return "archived" if await release(copied) else "copied"


async def _purge_pass(stats: Dict[str, int]) -> None:
    for row in await db.run_in_thread(_purge_due, 200):
        try:
            await delete_object(row["user_id"], row["id"])
        except StoreError as exc:
            _error(exc.reason)
            stats["errors"] = stats.get("errors", 0) + 1
            if exc.reason in STOP_REASONS:
                return
            continue
        await db.run_in_thread(_mark_purged, row["id"])
        metrics.inc("voice_archive_purged_total", "store copies of deleted recordings deleted")
        stats["purged"] = stats.get("purged", 0) + 1


async def archive_once() -> Dict[str, int]:
    """One pass of the mover. Returns counts (tests, the CLI)."""
    stats: Dict[str, int] = {}
    if not configured():
        return stats
    try:
        health = await store_health()
    except StoreError as exc:
        _error(exc.reason)
        _publish_store(None)
        stats["store_down"] = 1
        await _refresh_gauges()
        return stats
    _publish_store(health)
    await _purge_pass(stats)
    if not health.get("ready"):
        stats["store_down"] = 1
        await _refresh_gauges()
        return stats
    for row in await db.run_in_thread(_releasable, 200):
        if await release(row):
            stats["released"] = stats.get("released", 0) + 1
    room: Optional[int] = None
    if isinstance(health.get("free_bytes"), (int, float)) and isinstance(health.get("min_free_bytes"), (int, float)):
        room = int(health["free_bytes"]) - int(health["min_free_bytes"]) - int(health.get("reserved_bytes") or 0)
    for row in await db.run_in_thread(_claim, settings.voice_archive_batch, settings.voice_archive_after_s):
        try:
            outcome = await copy_one(row, room=room)
        except StoreError as exc:
            stats["errors"] = stats.get("errors", 0) + 1
            if exc.reason in STOP_REASONS:
                log.warning("voice archive: the store failed (%s); the rest waits for the next pass", exc.reason)
                break
            continue
        stats[outcome] = stats.get(outcome, 0) + 1
        if room is not None and outcome in ("archived", "copied"):
            room -= int(row.get("bytes_stored") or 0)
    await _refresh_gauges()
    return stats


async def _refresh_gauges() -> None:
    """The backlog gauges, and the time of this pass (whatever it found:
    a pass that met a down store still ran)."""
    metrics.set_gauge("voice_archive_last_pass_timestamp_seconds", time.time(), "when the voice archive's last pass ended")
    with contextlib.suppress(Exception):
        _publish_gauges(await db.run_in_thread(_gauges))


# ------------------------------------------------------------ restoring --


async def _restore_file(row: Dict[str, Any], directory: str) -> None:
    """The store's copy into place at source.<ext>, checked. SessionError when it cannot."""
    size = int(row.get("bytes_stored") or 0)
    sha = row.get("source_sha256")
    source = dictation.source_path(row)
    if await asyncio.to_thread(os.path.exists, source):
        # A head file left behind (a release that died between its UPDATE and
        # its unlink): the same bytes, if they hash right.
        have, length = await asyncio.to_thread(_hash_file, source)
        if length == size and (sha is None or have == sha):
            return
    free = dictation.free_bytes(directory)
    if free is not None and free - size < settings.voice_min_free_bytes:
        metrics.inc("voice_archive_restored_total", "recordings brought back to the head", result="no_space")
        raise dictation.SessionError(507, "storage_full", "The server has no room to bring this recording back right now.")
    temp = os.path.join(directory, f"{_RESTORE_PREFIX}{uuid.uuid4().hex}")
    try:
        try:
            await download_to(row, temp, size, sha)
        except StoreError as exc:
            _error(exc.reason)
            if exc.reason == "missing":
                await db.run_in_thread(_flag, row["id"], "remote_missing")
                metrics.inc("voice_archive_restored_total", "recordings brought back to the head", result="missing")
                raise _missing() from None
            if exc.reason == "remote_sha_mismatch":
                await db.run_in_thread(_flag, row["id"], "remote_sha_mismatch")
                metrics.inc("voice_archive_restored_total", "recordings brought back to the head", result="mismatch")
                log.error("voice archive: the stored copy of %s does not hash to its sha256", row["id"])
                raise _missing() from None
            metrics.inc("voice_archive_restored_total", "recordings brought back to the head", result="unavailable")
            raise _unavailable() from None

        def place() -> None:
            os.replace(temp, source)
            _fsync_dir(directory)

        try:
            await asyncio.to_thread(place)
        except FileNotFoundError:
            raise _deleted() from None  # the folder went while it downloaded
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)


async def ensure_local(row: Dict[str, Any], *, hold_s: Optional[float] = None) -> Dict[str, Any]:
    """The head holds this recording's audio, and keeps it for `hold_s`.

    Returns the fresh row. dictation.SessionError: 410 audio_deleted (the
    recording or its folder is gone), 503 archive_unavailable (the store is
    not answering), 410 audio_missing (the store has no good copy), 507
    storage_full (no room on the head)."""
    hold = float(settings.voice_archive_hold_s if hold_s is None else hold_s)
    user_id, session_id = int(row["user_id"]), row["id"]
    directory = dictation.session_dir(user_id, session_id)
    try:
        async with _session_flock(directory):
            current = await db.run_in_thread(dictation._row, session_id)
            if (
                current is None or current["status"] == dictation.STATUS_CANCELLED
                or current.get("audio_deleted_at") is not None
            ):
                metrics.inc("voice_archive_restored_total", "recordings brought back to the head", result="deleted")
                raise _deleted()
            if (current.get("archive_state") or LOCAL) != ARCHIVED:
                held = await db.run_in_thread(_set_hold, session_id, hold)
                metrics.inc("voice_archive_restored_total", "recordings brought back to the head", result="held")
                return held or current
            if not configured():
                raise _unavailable()
            await _restore_file(current, directory)
            restored = await db.run_in_thread(_mark_restored, session_id, hold)
            metrics.inc("voice_archive_restored_total", "recordings brought back to the head", result="restored")
            log.info("voice archive: %s brought back to the head", session_id)
            return restored or (await db.run_in_thread(dictation._row, session_id)) or current
    except FileNotFoundError:
        raise _deleted() from None
    except TimeoutError:
        raise _unavailable() from None


def ensure_local_sync(row: Dict[str, Any], *, hold_s: Optional[float] = None, timeout: float = 900.0) -> Dict[str, Any]:
    """`ensure_local` from a worker thread (dictation.retranscribe), run on
    the archive's own loop."""
    future = ARCHIVER.submit(ensure_local(row, hold_s=hold_s))
    try:
        return future.result(timeout)
    except concurrent.futures.TimeoutError:
        future.cancel()
        raise _unavailable() from None


# ---------------------------------------------------------------- reads --


def _unavailable_response() -> JSONResponse:
    error = _unavailable()
    return JSONResponse(
        status_code=error.status, content=error.body(), headers={"Retry-After": "30", "Cache-Control": "no-store"}
    )


async def recording_response(row: Dict[str, Any], request: Request) -> Response:
    """The stored recording for a player or a download: the head file when
    there is one, else the store's copy streamed through. SessionError 410
    audio_deleted as before for a recording that is gone."""
    if row.get("audio_deleted_at") is not None:
        raise _deleted()
    stamp = row["created_at"].strftime("%Y%m%d-%H%M") if row.get("created_at") else "recording"
    filename = f"recording-{stamp}.{row['ext']}"
    headers = {
        "Cache-Control": "no-store",
        "X-Recording-Complete": "false" if row["status"] == dictation.STATUS_RECORDING else "true",
    }
    path = dictation.source_path(row)
    if await asyncio.to_thread(os.path.exists, path):
        return FileResponse(path, media_type=row["mime_type"], filename=filename, headers=headers)
    if (row.get("archive_state") or LOCAL) == LOCAL:
        raise _deleted()
    return await _proxy(row, request, filename, headers)


async def _proxy(row: Dict[str, Any], request: Request, filename: str, headers: Dict[str, str]) -> Response:
    def counted(result: str) -> None:
        metrics.inc("voice_archive_proxy_total", "archived recordings served from the store", result=result)

    upstream = _auth()
    for name in ("range", "if-range"):
        value = request.headers.get(name)
        if value:
            upstream[name] = value
    try:
        client = await _client()
        response = await client.send(
            client.build_request("GET", _object_url(row), headers=upstream, timeout=_timeout()), stream=True,
        )
    except (httpx.HTTPError, StoreError) as exc:
        _error(exc.reason if isinstance(exc, StoreError) else _transport_reason(exc))
        counted("unavailable")
        return _unavailable_response()
    status = response.status_code
    if status in (200, 206):
        out = dict(headers)
        for name in ("content-length", "content-range", "accept-ranges"):
            if response.headers.get(name):
                out[name] = response.headers[name]
        out["content-disposition"] = f'attachment; filename="{filename}"'

        async def body() -> AsyncIterator[bytes]:
            try:
                async for chunk in response.aiter_raw(_PROXY_CHUNK):
                    yield chunk
            finally:
                await response.aclose()

        counted("ok" if status == 200 else "partial")
        # The background close also runs when the listener left before the
        # first byte, so the pooled connection is never held by a dead stream.
        return StreamingResponse(
            body(), status_code=status, headers=out, media_type=row["mime_type"],
            background=BackgroundTask(response.aclose),
        )
    await response.aclose()
    if status == 416:
        counted("not_satisfiable")
        return Response(
            status_code=416,
            headers={"Content-Range": response.headers.get("content-range", ""), "Cache-Control": "no-store"},
        )
    if status == 404:
        counted("missing")
        _error("missing")
        log.error("voice archive: the store has no copy of archived recording %s", row["id"])
        with contextlib.suppress(Exception):
            await db.run_in_thread(_flag, row["id"], "remote_missing")
        error = _missing()
        return JSONResponse(status_code=error.status, content=error.body(), headers={"Cache-Control": "no-store"})
    _error(_status_reason(status))
    counted("unavailable")
    return _unavailable_response()


# -------------------------------------------------------------- deletes --


async def _forget(user_id: int, session_id: str) -> bool:
    try:
        await delete_object(user_id, session_id, timeout=_DELETE_TIMEOUT_S)
    except StoreError as exc:
        _error(exc.reason)
        return False
    await db.run_in_thread(_mark_purged, session_id)
    metrics.inc("voice_archive_purged_total", "store copies of deleted recordings deleted")
    return True


def forget_remote(user_id: int, session_id: str) -> Optional[concurrent.futures.Future]:
    """Ask the store to delete its copy of a recording that was just deleted,
    without waiting for the answer. The purge step retries what this misses."""
    if not configured():
        return None
    try:
        return ARCHIVER.submit(_forget(int(user_id), session_id))
    except Exception:  # noqa: BLE001 - the purge pass is the guarantee
        log.warning("voice archive: could not ask the store to delete %s now; the purge will", session_id)
        return None


# -------------------------------------------------- sweep and reconcile --


async def head_sweep_once(*, now: Optional[float] = None) -> int:
    """Head files and restore leftovers of archived recordings (a release or a
    restore that died half way). Hourly."""
    now = time.time() if now is None else now
    removed = 0
    for row in await db.run_in_thread(_archived_rows):
        directory = dictation.session_dir(row["user_id"], row["id"])
        try:
            names = await asyncio.to_thread(os.listdir, directory)
        except OSError:
            continue
        for name in names:
            if name.startswith(_RESTORE_PREFIX):
                path = os.path.join(directory, name)
                with contextlib.suppress(OSError):
                    if now - os.stat(path).st_mtime > _TEMP_MAX_AGE_S:
                        os.unlink(path)
                        removed += 1
        source = dictation.source_path(row)
        if os.path.basename(source) not in names:
            continue
        try:
            async with _session_flock(directory):
                current = await db.run_in_thread(dictation._row, row["id"])
                if current is None or current.get("archive_state") != ARCHIVED or current.get("audio_deleted_at") is not None:
                    continue
                live = await db.run_in_thread(_in_live_chain, row["id"])
                if live:
                    continue
                await asyncio.to_thread(_unlink_and_sync, source, directory)
                removed += 1
        except (FileNotFoundError, TimeoutError):
            continue
    return removed


def _in_live_chain(session_id: str) -> bool:
    with db.connection() as con:
        row = con.execute(f"SELECT %s IN ({_LIVE_CHAIN}) AS live", (session_id,)).fetchone()
    return bool(row and row["live"])


def _parse_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    with contextlib.suppress(ValueError):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


async def reconcile_once() -> Dict[str, int]:
    """The store's inventory against the rows. Deletes what nobody owns any
    more, flags archived recordings the store lost, and repairs rows that
    say local or copied when only the store has the audio."""
    stats = {name: 0 for name in RECONCILE_RESULTS}
    started = await db.run_in_thread(_db_now)
    seen: set = set()
    after = ""
    while True:
        objects, next_after = await inventory_page(after, 500)
        rows = await db.run_in_thread(_rows_by_ids, [str(o.get("session_id") or "") for o in objects])
        for item in objects:
            session_id = str(item.get("session_id") or "")
            if not dictation._SESSION_ID.match(session_id):
                continue
            user_id = int(item.get("user_id") or 0)
            seen.add(session_id)
            row = rows.get(session_id)
            if row is None:
                stored_at = _parse_time(item.get("stored_at"))
                if stored_at is None or (started - stored_at).total_seconds() > _ORPHAN_GRACE_S:
                    with contextlib.suppress(StoreError):
                        await delete_object(user_id, session_id)
                        stats["orphan_deleted"] += 1
                continue
            if int(row["user_id"]) != user_id:
                stats["foreign"] += 1
                log.error("voice archive: the store holds %s under user %d, the row says %d", session_id, user_id, int(row["user_id"]))
                continue
            if row["status"] == dictation.STATUS_CANCELLED or row.get("audio_deleted_at") is not None:
                with contextlib.suppress(StoreError):
                    await delete_object(user_id, session_id)
                    await db.run_in_thread(_mark_purged, session_id)
                    stats["deleted_row_purged"] += 1
                continue
            state = row.get("archive_state") or LOCAL
            if (
                state in (LOCAL, COPIED)
                and item.get("sha256") and item.get("sha256") == row.get("source_sha256")
                and int(item.get("bytes") or -1) == int(row.get("bytes_stored") or 0)
                and not await asyncio.to_thread(os.path.exists, dictation.source_path(row))
                and row["status"] in (dictation.STATUS_DONE, dictation.STATUS_FAILED)
            ):
                if await db.run_in_thread(_repair, session_id):
                    stats["repaired"] += 1
            elif state == ARCHIVED and row.get("archive_error") == "remote_missing":
                await db.run_in_thread(_flag, session_id, None)
        if not next_after:
            break
        after = next_after
    for row in await db.run_in_thread(_archived_rows):
        archived_at = row.get("archived_at")
        if row["id"] in seen or (isinstance(archived_at, datetime) and archived_at >= started):
            continue
        stats["remote_missing"] += 1
        _error("remote_missing")
        log.error("voice archive: archived recording %s is not on the store", row["id"])
        await db.run_in_thread(_flag, row["id"], "remote_missing")
    for name, count in stats.items():
        for _ in range(count):
            metrics.inc("voice_archive_reconcile_total", "voice archive reconcile outcomes", result=name)
    await _refresh_gauges()
    return stats


# --------------------------------------------------------------- rollback --


async def recall_all(*, keep_remote: bool = False) -> Dict[str, int]:
    """Every recording back on the head, marked local (the rollback)."""
    stats = {"recalled": 0, "failed": 0, "remote_deleted": 0}
    for row in await db.run_in_thread(_not_local_rows):
        directory = dictation.session_dir(row["user_id"], row["id"])
        try:
            current = await ensure_local(row, hold_s=3600.0)
            async with _session_flock(directory):
                sha, size = await asyncio.to_thread(_hash_file, dictation.source_path(current))
                if size != int(current.get("bytes_stored") or 0) or (
                    current.get("source_sha256") and sha != current["source_sha256"]
                ):
                    raise StoreError("local_sha_mismatch", "the head copy does not match; the store's is kept")
                local = await db.run_in_thread(_mark_local, row["id"])
            if local is None:
                raise StoreError("conflict", "the row changed while it was recalled")
        except (dictation.SessionError, StoreError, OSError) as exc:
            stats["failed"] += 1
            log.error("voice archive: %s was not recalled: %s", row["id"], exc)
            continue
        stats["recalled"] += 1
        if not keep_remote:
            with contextlib.suppress(StoreError):
                await delete_object(row["user_id"], row["id"])
                stats["remote_deleted"] += 1
    return stats


# ------------------------------------------------------------ the thread --


class _Archiver:
    """One daemon thread and event loop for the archive: the mover's passes,
    and every store call made from a worker thread (a restore for a
    retranscription, a delete)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._task: Optional[concurrent.futures.Future] = None

    def loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is not None and self._thread is not None and self._thread.is_alive():
                return self._loop
            ready = threading.Event()
            holder: Dict[str, Any] = {}

            def run() -> None:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                holder["loop"] = loop
                ready.set()
                loop.run_forever()

            thread = threading.Thread(target=run, name="voice-archive", daemon=True)
            thread.start()
            ready.wait()
            self._loop, self._thread = holder["loop"], thread
            return self._loop

    def submit(self, coro: Any) -> concurrent.futures.Future:
        return asyncio.run_coroutine_threadsafe(coro, self.loop())

    def running(self) -> bool:
        with self._lock:
            return self._task is not None and not self._task.done()

    def start(self) -> bool:
        if not enabled():
            if settings.voice_archive_enabled:
                log.warning("VOICE_ARCHIVE_ENABLED is set but VOICE_ARCHIVE_URL or VOICE_ARCHIVE_TOKEN is not; the archive stays off")
            return False
        loop = self.loop()
        with self._lock:
            if self._task is not None and not self._task.done():
                return True
            self._task = asyncio.run_coroutine_threadsafe(_run_forever(), loop)
        metrics.set_gauge("voice_archive_enabled", 1.0, "1 while this process moves finished recordings to the store")
        return True

    def stop(self, timeout: float = 10.0) -> None:
        with self._lock:
            task, self._task = self._task, None
        if task is not None:
            task.cancel()
        if self._loop is not None and self._thread is not None and self._thread.is_alive():
            with contextlib.suppress(Exception):
                self.submit(close_client()).result(timeout)
        metrics.set_gauge("voice_archive_enabled", 0.0, "1 while this process moves finished recordings to the store")


ARCHIVER = _Archiver()


async def _run_forever() -> None:
    log.info(
        "voice archive on: %s, finished recordings move after %.0f s at up to %d bytes/s",
        settings.voice_archive_url, settings.voice_archive_after_s, settings.voice_archive_rate_bytes_per_s,
    )
    last_sweep = time.monotonic()
    last_reconcile = time.monotonic() - _RECONCILE_EVERY_S + 600.0  # the first one ten minutes in
    while True:
        try:
            await archive_once()
            if time.monotonic() - last_sweep >= _HEAD_SWEEP_EVERY_S:
                last_sweep = time.monotonic()
                await head_sweep_once()
            if time.monotonic() - last_reconcile >= _RECONCILE_EVERY_S:
                last_reconcile = time.monotonic()
                result = await reconcile_once()
                if any(result.values()):
                    log.info("voice archive reconcile: %s", result)
        except asyncio.CancelledError:
            raise
        except StoreError as exc:
            log.warning("voice archive: the store failed (%s); trying again next pass", exc.reason)
        except Exception:  # noqa: BLE001 - the loop must outlive any one pass
            log.exception("voice archive pass failed; trying again")
        await asyncio.sleep(float(settings.voice_archive_interval_s))


def start() -> bool:
    return ARCHIVER.start()


def stop() -> None:
    ARCHIVER.stop()


# ------------------------------------------------------------------- CLI --


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.voice_archive", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    recall = sub.add_parser("recall-all", help="bring every recording back to the head and mark it local (rollback)")
    recall.add_argument("--keep-remote", action="store_true", help="leave the store's copies in place")
    sub.add_parser("status", help="counts by state, and the store's /health")
    sub.add_parser("reconcile", help="run the reconcile once")
    sub.add_parser("pass", help="run one pass of the mover once")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if not configured():
        print("VOICE_ARCHIVE_URL and VOICE_ARCHIVE_TOKEN must be set", file=sys.stderr)
        return 2

    async def run() -> Any:
        try:
            if args.command == "recall-all":
                return await recall_all(keep_remote=args.keep_remote)
            if args.command == "reconcile":
                return await reconcile_once()
            if args.command == "pass":
                return await archive_once()
            counts = await db.run_in_thread(_gauges)
            try:
                health: Any = await store_health()
            except StoreError as exc:
                health = {"error": exc.reason}
            return {"rows": counts, "store": health}
        finally:
            await close_client()

    result = asyncio.run(run())
    print(json.dumps(result, default=str, indent=2, sort_keys=True))
    return 1 if isinstance(result, dict) and result.get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
