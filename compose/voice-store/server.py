"""The voice archive store: finished recordings kept on the worker's disk.

WHY IT EXISTS (owner, 2026-09-30: "improve the storage of that audio" ->
"Move to worker's big disk"). Every stored dictation lives in
/data/voice/<user>/<session>/ on the HEAD's root filesystem, the same NVMe as
the OS, all of /var/lib/docker and production Postgres, one copy and no
backup. The worker's disk is 3.7 TB and 17% used. 99.3% of a recording's
bytes are its source.<ext> (measured on the head, 2026-09-30), so this store
takes exactly that file once a recording is FINISHED, and nothing else:
recording, decoding, transcripts, previews and the list stay on the head and
never wait for the worker (app/voice_archive.py is the other half).

WHAT IT IS. A small authenticated HTTP object store for one kind of object,
    PUT    /v1/recordings/<user>/<session>/source.<ext>   store, verified
    GET    /v1/recordings/<user>/<session>/source.<ext>   read (Range, If-Range)
    HEAD   same path                                      size, sha256
    DELETE /v1/recordings/<user>/<session>                always 204
    GET    /v1/inventory?after=<user>/<session>&limit=N   what is stored
    GET    /health, /metrics                              no token (guarded port)
Everything under /v1 needs `Authorization: Bearer <token>`, compared in
constant time against VOICE_STORE_TOKENS (a comma-separated list, so a token
can be rotated without a window where nothing is accepted).

WHAT A 201 PROMISES. The body was streamed into a private `.incoming-<uuid>`
file (O_EXCL, 0600) and hashed as it arrived; its length equals the declared
Content-Length and its sha256 equals the declared X-Content-SHA256 (else 422
and the temporary file is gone); it was fsynced; it was LINKED into place
(os.link fails rather than replace, so an object is never overwritten) and
the directory fsynced; and manifest.json (ids, type, bytes, sha256, times)
was written the same way. The same object sent again is 200; different bytes
under the same name are 409 and the stored file is left alone.

WHAT IT REFUSES BEFORE READING A BYTE of the body: a missing token (401), a
malformed id or name (400), a chunked upload (411: the free-space check needs
the length), an object over VOICE_STORE_MAX_OBJECT_BYTES (413), and any upload
that would leave the disk with less than VOICE_STORE_MIN_FREE_BYTES free once
every upload in flight is counted (507). That floor protects the worker's
Docker, model caches, OCR and speech engines and another tenant's Postgres.
At most VOICE_STORE_MAX_CONCURRENT_PUTS (2) uploads run at once (503 busy).

ONE COPY. This is tiering, not a backup: once the orchestrator has read an
object back and checked its sha256 it deletes the head's copy. A weekly scrub
re-hashes every object at VOICE_STORE_SCRUB_BYTES_PER_S and reports a
mismatch (voice_store_scrub_mismatches); with a single copy it cannot repair
one. docs/voice-archive.md says what that means for the owner.

WHAT IT NEVER DOES: bind a wildcard (VOICE_STORE_BIND is required, and 0.0.0.0
or :: is refused), log a token, log or return any audio, write outside
VOICE_STORE_ROOT, or follow a path it did not build from validated ids.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from starlette.applications import Starlette
from starlette.requests import ClientDisconnect, Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

log = logging.getLogger("voice-store")

VERSION = "1"

#: The recording extensions the orchestrator stores: the values of
#: orchestrator/app/dictation.py `_EXTENSIONS`. Kept as a literal because this
#: image does not carry the orchestrator; tests/test_voice_archive.py reads
#: this line and fails when the two lists disagree.
EXTENSIONS = frozenset({"webm", "mp4", "m4a", "ogg", "opus", "wav", "mp3", "flac", "aac", "3gp"})

#: A user id is a positive integer; 0 is reserved for scripts/voice-store.sh
#: verify, which no real account can own.
USER_ID = re.compile(r"^(0|[1-9][0-9]{0,9})$")
SESSION_ID = re.compile(r"^[0-9a-f]{32}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SOURCE_NAME = re.compile(r"^source\.([a-z0-9]{2,5})$")
MIME = re.compile(r"^[a-z0-9][a-z0-9.+-]{0,40}/[a-z0-9][a-z0-9.+-]{0,60}$")
TIMESTAMP = re.compile(r"^[0-9T:.+\- ]{10,40}$")

INCOMING_PREFIX = ".incoming-"
MANIFEST_PREFIX = ".manifest-"
MANIFEST = "manifest.json"
GIB = 1024 ** 3
MIB = 1024 ** 2

#: Written to disk in batches of this, off the event loop.
WRITE_BATCH = 1 * MIB

#: uvicorn closes a kept-alive connection idle this long. The orchestrator's
#: pooled client gives an idle connection up well before this
#: (voice_archive._KEEPALIVE_EXPIRY_S; a test compares the two), so it never
#: reuses one the store is closing at that moment.
KEEP_ALIVE_TIMEOUT_S = 5

#: The label values /metrics may carry; anything else is "other".
OPS = ("put", "get", "head", "delete", "inventory")
CODES = ("200", "201", "204", "206", "400", "401", "404", "408", "409", "411", "413", "416", "422", "500", "503", "507")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------- settings --


@dataclass(frozen=True)
class Settings:
    root: str = "/store"
    tokens: Tuple[str, ...] = ()
    min_free_bytes: int = 250 * GIB
    max_object_bytes: int = 64 * GIB
    max_concurrent_puts: int = 2
    #: An upload that sends nothing for this long is abandoned (408).
    read_idle_timeout_s: float = 60.0
    #: Temporary files older than this, and not part of an upload in flight,
    #: are removed by the sweep (an upload that died with its client).
    incoming_max_age_s: float = 3600.0
    sweep_interval_s: float = 600.0
    scrub_interval_s: float = 7 * 86400.0
    scrub_first_after_s: float = 3600.0
    scrub_bytes_per_s: int = 50 * 1000 * 1000
    inventory_max: int = 1000

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Settings":
        """The container's settings; SystemExit with the reason when one is wrong."""
        tokens = tuple(t.strip() for t in (env.get("VOICE_STORE_TOKENS") or "").split(",") if t.strip())
        if not tokens:
            raise SystemExit("VOICE_STORE_TOKENS is empty: the store refuses to start without a token")
        if any(len(t) < 32 for t in tokens):
            raise SystemExit("every token in VOICE_STORE_TOKENS must be at least 32 characters")

        def number(key: str, default: float, minimum: float) -> float:
            raw = (env.get(key) or "").strip()
            if not raw:
                return default
            try:
                value = float(raw)
            except ValueError:
                raise SystemExit(f"{key} must be a number") from None
            if value < minimum:
                raise SystemExit(f"{key} must be at least {minimum:g}")
            return value

        return cls(
            root=(env.get("VOICE_STORE_ROOT") or "/store").strip(),
            tokens=tokens,
            min_free_bytes=int(number("VOICE_STORE_MIN_FREE_BYTES", 250 * GIB, 0)),
            max_object_bytes=int(number("VOICE_STORE_MAX_OBJECT_BYTES", 64 * GIB, 1)),
            max_concurrent_puts=int(number("VOICE_STORE_MAX_CONCURRENT_PUTS", 2, 1)),
            read_idle_timeout_s=number("VOICE_STORE_READ_IDLE_TIMEOUT_S", 60.0, 1.0),
            incoming_max_age_s=number("VOICE_STORE_INCOMING_MAX_AGE_S", 3600.0, 60.0),
            sweep_interval_s=number("VOICE_STORE_SWEEP_INTERVAL_S", 600.0, 10.0),
            scrub_interval_s=number("VOICE_STORE_SCRUB_INTERVAL_S", 7 * 86400.0, 60.0),
            scrub_first_after_s=number("VOICE_STORE_SCRUB_FIRST_AFTER_S", 3600.0, 0.0),
            scrub_bytes_per_s=int(number("VOICE_STORE_SCRUB_BYTES_PER_S", 50 * 1000 * 1000, 1024)),
        )


# ------------------------------------------------------------ filesystem --


def _fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _private_dir(path: str) -> None:
    """`path` exists as a 0700 directory, whatever the umask."""
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    os.chmod(path, 0o700)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def _sha256_file(path: str, *, rate: int = 0, stop: Optional[threading.Event] = None) -> Tuple[str, int]:
    """(sha256, bytes) of a file, read at no more than `rate` bytes a second
    when a rate is given, and dropped from the page cache as it goes."""
    digest = hashlib.sha256()
    total = 0
    started = time.monotonic()
    fd = os.open(path, os.O_RDONLY)
    try:
        while True:
            chunk = os.read(fd, 4 * MIB)
            if not chunk:
                break
            digest.update(chunk)
            with contextlib.suppress(AttributeError, OSError):
                os.posix_fadvise(fd, total, len(chunk), os.POSIX_FADV_DONTNEED)
            total += len(chunk)
            if rate > 0:
                ahead = total / rate - (time.monotonic() - started)
                if ahead > 0:
                    if stop is not None:
                        if stop.wait(ahead):
                            break
                    else:
                        time.sleep(ahead)
    finally:
        os.close(fd)
    return digest.hexdigest(), total


def _write_json_atomic(directory: str, name: str, payload: Dict[str, Any]) -> None:
    tmp = os.path.join(directory, f"{MANIFEST_PREFIX}{uuid.uuid4().hex}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        _write_all(fd, json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(tmp, os.path.join(directory, name))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    _fsync_dir(directory)


@dataclass
class Stored:
    name: str
    bytes: int
    sha256: Optional[str]
    manifest: Optional[Dict[str, Any]]


class Refusal(Exception):
    def __init__(self, status: int, reason: str, detail: str, **headers: str) -> None:
        super().__init__(detail)
        self.status = status
        self.reason = reason
        self.detail = detail
        self.headers = headers

    def response(self) -> JSONResponse:
        return JSONResponse(
            {"reason": self.reason, "detail": self.detail},
            status_code=self.status,
            headers={"cache-control": "no-store", **self.headers},
        )


# ------------------------------------------------------------------ store --


class Store:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root = os.path.abspath(settings.root)
        self._lock = threading.Lock()
        #: (user, session) -> the temporary file of the upload in flight.
        self._in_flight: Dict[Tuple[str, str], str] = {}
        self._reserved = 0
        self.objects: Optional[int] = None
        self.bytes: Optional[int] = None
        self.requests: Dict[Tuple[str, str], int] = {}
        self.put_bytes_total = 0
        self.swept_total = 0
        self.scrub: Dict[str, Any] = {
            "last_started_at": None, "last_completed_at": None, "checked": 0, "mismatches": 0,
        }
        self._mismatched: List[str] = []
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []

    # -- bookkeeping ---------------------------------------------------------

    def count(self, op: str, status: int) -> None:
        code = str(status) if str(status) in CODES else "other"
        key = (op if op in OPS else "other", code)
        with self._lock:
            self.requests[key] = self.requests.get(key, 0) + 1

    def _adjust(self, objects: int, size: int) -> None:
        with self._lock:
            if self.objects is not None:
                self.objects = max(0, self.objects + objects)
            if self.bytes is not None:
                self.bytes = max(0, self.bytes + size)

    def free_bytes(self) -> int:
        st = os.statvfs(self.root)
        return int(st.f_bavail) * int(st.f_frsize)

    # -- paths ---------------------------------------------------------------

    @staticmethod
    def key(user: str, session: str) -> Tuple[str, str]:
        if not USER_ID.match(user or "") or not SESSION_ID.match(session or ""):
            raise Refusal(400, "bad_request", "The user id must be digits and the session id 32 hex characters.")
        return user, session

    def session_dir(self, user: str, session: str) -> str:
        return os.path.join(self.root, user, session)

    @staticmethod
    def source_name(name: str) -> str:
        match = SOURCE_NAME.match(name or "")
        if match is None or match.group(1) not in EXTENSIONS:
            raise Refusal(400, "bad_request", "The object must be named source.<a recording extension>.")
        return name

    def authorized(self, request: Request) -> bool:
        header = request.headers.get("authorization") or ""
        scheme, _, presented = header.partition(" ")
        if scheme.lower() != "bearer" or not presented.strip():
            return False
        given = presented.strip().encode("utf-8")
        ok = False
        for token in self.settings.tokens:
            # Every token is compared, whatever matched first.
            ok |= hmac.compare_digest(given, token.encode("utf-8"))
        return ok

    def require_token(self, request: Request) -> None:
        if not self.authorized(request):
            raise Refusal(401, "unauthorized", "A valid bearer token is required.", **{"www-authenticate": "Bearer"})

    # -- reading what is there ------------------------------------------------

    @staticmethod
    def read_manifest(directory: str) -> Optional[Dict[str, Any]]:
        try:
            with open(os.path.join(directory, MANIFEST), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def stored(self, directory: str) -> Optional[Stored]:
        """The object in `directory`, or None. Blocking."""
        try:
            names = [n for n in os.listdir(directory) if SOURCE_NAME.match(n)]
        except OSError:
            return None
        if not names:
            return None
        name = sorted(names)[0]
        path = os.path.join(directory, name)
        try:
            size = os.stat(path).st_size
        except OSError:
            return None
        manifest = self.read_manifest(directory)
        sha = manifest.get("sha256") if manifest and manifest.get("name") == name else None
        if not (isinstance(sha, str) and SHA256.match(sha)):
            sha = None
        return Stored(name=name, bytes=size, sha256=sha, manifest=manifest)

    def _stored_with_sha(self, directory: str) -> Optional[Stored]:
        """`stored`, with a missing manifest rebuilt from the file itself (a
        crash between the link and the manifest). Blocking."""
        found = self.stored(directory)
        if found is None or found.sha256 is not None:
            return found
        sha, size = _sha256_file(os.path.join(directory, found.name))
        user, session = os.path.split(os.path.relpath(directory, self.root))
        manifest = {
            "user_id": int(user), "session_id": session, "name": found.name,
            "ext": found.name.split(".", 1)[1], "mime": "application/octet-stream",
            "bytes": size, "sha256": sha, "stored_at": _now_iso(), "rebuilt": True,
        }
        _write_json_atomic(directory, MANIFEST, manifest)
        return Stored(name=found.name, bytes=size, sha256=sha, manifest=manifest)

    # -- admission -------------------------------------------------------------

    def _admit(self, key: Tuple[str, str], length: int, temp: str) -> None:
        with self._lock:
            if key in self._in_flight:
                raise Refusal(503, "in_progress", "This recording is being stored right now.", **{"retry-after": "5"})
            if len(self._in_flight) >= self.settings.max_concurrent_puts:
                raise Refusal(503, "busy", "The store is taking other recordings; try again shortly.", **{"retry-after": "5"})
            free = self.free_bytes()
            if free - self._reserved - length < self.settings.min_free_bytes:
                raise Refusal(507, "storage_full", "The archive disk has no room for this recording.")
            self._in_flight[key] = temp
            self._reserved += length

    def _release(self, key: Tuple[str, str], length: int) -> None:
        with self._lock:
            if self._in_flight.pop(key, None) is not None:
                self._reserved = max(0, self._reserved - length)

    def in_flight_temps(self) -> List[str]:
        with self._lock:
            return list(self._in_flight.values())

    # -- the handlers ------------------------------------------------------------

    async def put(self, request: Request) -> Response:
        self.require_token(request)
        user, session = self.key(request.path_params["user"], request.path_params["session"])
        name = self.source_name(request.path_params["name"])
        if "chunked" in (request.headers.get("transfer-encoding") or "").lower():
            raise Refusal(411, "length_required", "Send the recording with a Content-Length, not chunked.")
        raw_length = request.headers.get("content-length")
        if raw_length is None:
            raise Refusal(411, "length_required", "Content-Length is required.")
        try:
            length = int(raw_length)
        except ValueError:
            raise Refusal(400, "bad_request", "Content-Length must be an integer.") from None
        if length <= 0:
            raise Refusal(400, "bad_request", "An empty recording is not stored.")
        if length > self.settings.max_object_bytes:
            raise Refusal(413, "too_large", f"An object may be at most {self.settings.max_object_bytes} bytes.")
        want = (request.headers.get("x-content-sha256") or "").strip().lower()
        if not SHA256.match(want):
            raise Refusal(400, "bad_request", "X-Content-SHA256 must be the lowercase hex sha256 of the body.")
        mime = (request.headers.get("x-recording-type") or "").split(";")[0].strip().lower()
        if not MIME.match(mime):
            mime = "application/octet-stream"
        meta = {}
        for header, field in (("x-recording-created-at", "created_at"), ("x-recording-finished-at", "finished_at")):
            value = (request.headers.get(header) or "").strip()
            if value and TIMESTAMP.match(value):
                meta[field] = value

        directory = self.session_dir(user, session)
        existing = await asyncio.to_thread(self._stored_with_sha, directory)
        if existing is not None:
            return self._answer_existing(existing, name, length, want)

        temp = os.path.join(directory, f"{INCOMING_PREFIX}{uuid.uuid4().hex}")
        key = (user, session)
        self._admit(key, length, temp)
        try:
            return await self._receive(request, user, session, name, length, want, mime, meta, temp)
        finally:
            self._release(key, length)

    @staticmethod
    def _answer_existing(existing: Stored, name: str, length: int, want: str) -> Response:
        same = existing.name == name and existing.sha256 == want and existing.bytes == length
        if same:
            return JSONResponse(
                {"stored": "already", "sha256": existing.sha256, "bytes": existing.bytes},
                status_code=200, headers={"cache-control": "no-store"},
            )
        raise Refusal(409, "conflict", "A different recording is already stored under this name; it was not replaced.")

    async def _receive(
        self, request: Request, user: str, session: str, name: str, length: int, want: str,
        mime: str, meta: Dict[str, str], temp: str,
    ) -> Response:
        directory = self.session_dir(user, session)

        def prepare() -> int:
            for attempt in (1, 2):
                _private_dir(self.root)
                _private_dir(os.path.join(self.root, user))
                _private_dir(directory)
                try:
                    return os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except FileNotFoundError:
                    # The sweep removed an empty folder between the mkdir
                    # and the open; make it again, once.
                    if attempt == 2:
                        raise
            raise AssertionError("unreachable")

        fd = await asyncio.to_thread(prepare)
        digest = hashlib.sha256()
        received = 0
        pending = bytearray()
        try:
            stream = request.stream().__aiter__()
            while True:
                try:
                    async with asyncio.timeout(self.settings.read_idle_timeout_s):
                        chunk = await stream.__anext__()
                except StopAsyncIteration:
                    break
                if not chunk:
                    continue
                received += len(chunk)
                if received > length:
                    raise Refusal(422, "length_mismatch", "The body is longer than its Content-Length.")
                digest.update(chunk)
                pending += chunk
                if len(pending) >= WRITE_BATCH:
                    data, pending = bytes(pending), bytearray()
                    await asyncio.to_thread(_write_all, fd, data)
            if pending:
                await asyncio.to_thread(_write_all, fd, bytes(pending))
            if received != length:
                raise Refusal(422, "length_mismatch", f"{received} bytes arrived, {length} were declared.")
            got = digest.hexdigest()
            if got != want:
                raise Refusal(422, "sha_mismatch", "The body's sha256 is not the one declared.")
            await asyncio.to_thread(os.fsync, fd)
            os.close(fd)
            fd = -1
            final = os.path.join(directory, name)
            try:
                await asyncio.to_thread(os.link, temp, final)
            except FileExistsError:
                existing = await asyncio.to_thread(self._stored_with_sha, directory)
                if existing is None:
                    raise Refusal(409, "conflict", "The recording changed while it was being stored.") from None
                return self._answer_existing(existing, name, length, want)
            except FileNotFoundError:
                raise Refusal(409, "deleted", "The recording was deleted while it was being stored.") from None
            manifest = {
                "user_id": int(user), "session_id": session, "name": name, "ext": name.split(".", 1)[1],
                "mime": mime, "bytes": length, "sha256": got, "stored_at": _now_iso(), **meta,
            }

            def settle() -> None:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temp)
                _fsync_dir(directory)
                _write_json_atomic(directory, MANIFEST, manifest)

            try:
                await asyncio.to_thread(settle)
            except FileNotFoundError:
                raise Refusal(409, "deleted", "The recording was deleted while it was being stored.") from None
            self._adjust(1, length)
            with self._lock:
                self.put_bytes_total += length
            log.info("stored %s/%s %s (%d bytes)", user, session, name, length)
            return JSONResponse(
                {"stored": "new", "sha256": got, "bytes": length, "stored_at": manifest["stored_at"]},
                status_code=201, headers={"cache-control": "no-store"},
            )
        except TimeoutError:
            raise Refusal(408, "upload_stalled", "The upload sent nothing for too long.") from None
        except ClientDisconnect:
            raise Refusal(400, "upload_incomplete", "The upload was cut off.") from None
        finally:
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temp)

    async def get(self, request: Request) -> Response:
        self.require_token(request)
        user, session = self.key(request.path_params["user"], request.path_params["session"])
        name = self.source_name(request.path_params["name"])
        directory = self.session_dir(user, session)
        found = await asyncio.to_thread(self.stored, directory)
        if found is None or found.name != name:
            raise Refusal(404, "not_found", "No such recording in the store.")
        headers = {"cache-control": "no-store"}
        mime = "application/octet-stream"
        if found.sha256:
            headers["etag"] = f'"{found.sha256}"'
            headers["x-content-sha256"] = found.sha256
        if found.manifest and isinstance(found.manifest.get("mime"), str) and MIME.match(found.manifest["mime"]):
            mime = found.manifest["mime"]
        return FileResponse(os.path.join(directory, name), media_type=mime, headers=headers)

    async def delete(self, request: Request) -> Response:
        self.require_token(request)
        user, session = self.key(request.path_params["user"], request.path_params["session"])
        directory = self.session_dir(user, session)

        def remove() -> Optional[Stored]:
            found = self.stored(directory)
            shutil.rmtree(directory, ignore_errors=True)
            parent = os.path.join(self.root, user)
            with contextlib.suppress(OSError):
                os.rmdir(parent)  # only when empty
            with contextlib.suppress(OSError):
                _fsync_dir(parent if os.path.isdir(parent) else self.root)
            return found

        found = await asyncio.to_thread(remove)
        if found is not None:
            self._adjust(-1, -found.bytes)
            log.info("deleted %s/%s", user, session)
        return Response(status_code=204, headers={"cache-control": "no-store"})

    def inventory_page(self, after: str, limit: int) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """Objects in (user as a number, session) order after `after`. Blocking."""
        cursor: Optional[Tuple[int, str]] = None
        if after:
            user, _, session = after.partition("/")
            self.key(user, session)
            cursor = (int(user), session)
        try:
            users = sorted((int(e.name), e.name) for e in os.scandir(self.root) if e.is_dir() and USER_ID.match(e.name))
        except OSError:
            users = []
        out: List[Dict[str, Any]] = []
        more = False
        for number, user in users:
            if cursor is not None and number < cursor[0]:
                continue
            try:
                sessions = sorted(
                    e.name for e in os.scandir(os.path.join(self.root, user)) if e.is_dir() and SESSION_ID.match(e.name)
                )
            except OSError:
                continue
            for session in sessions:
                if cursor is not None and (number, session) <= cursor:
                    continue
                found = self.stored(self.session_dir(user, session))
                if found is None:
                    continue
                if len(out) >= limit:
                    more = True
                    break
                manifest = found.manifest or {}
                out.append({
                    "user_id": number, "session_id": session, "name": found.name, "bytes": found.bytes,
                    "sha256": found.sha256, "stored_at": manifest.get("stored_at"),
                })
            if more:
                break
        next_after = f"{out[-1]['user_id']}/{out[-1]['session_id']}" if more and out else None
        return out, next_after

    async def inventory(self, request: Request) -> Response:
        self.require_token(request)
        after = (request.query_params.get("after") or "").strip()
        try:
            limit = int(request.query_params.get("limit") or 500)
        except ValueError:
            raise Refusal(400, "bad_request", "limit must be an integer.") from None
        limit = max(1, min(self.settings.inventory_max, limit))
        objects, next_after = await asyncio.to_thread(self.inventory_page, after, limit)
        return JSONResponse({"objects": objects, "next_after": next_after}, headers={"cache-control": "no-store"})

    def health_body(self) -> Dict[str, Any]:
        try:
            free: Optional[int] = self.free_bytes()
            writable = os.path.isdir(self.root) and os.access(self.root, os.W_OK)
        except OSError:
            free, writable = None, False
        with self._lock:
            reserved = self._reserved
            puts = len(self._in_flight)
            objects, size = self.objects, self.bytes
            scrub = dict(self.scrub)
        return {
            "ready": bool(writable and self.settings.tokens and free is not None),
            "version": VERSION,
            "free_bytes": free,
            "min_free_bytes": self.settings.min_free_bytes,
            "reserved_bytes": reserved,
            "puts_in_flight": puts,
            "objects": objects,
            "bytes": size,
            "scrub": scrub,
        }

    def metrics_text(self) -> str:
        body = self.health_body()
        lines: List[str] = []

        def gauge(name: str, value: Any, help_text: str) -> None:
            if value is None:
                return  # never a made-up zero
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} gauge")
            lines.append(f"{name} {float(value):g}" if isinstance(value, float) else f"{name} {int(value)}")

        gauge("voice_store_up", 1 if body["ready"] else 0, "1 when the store can take recordings.")
        gauge("voice_store_free_bytes", body["free_bytes"], "Free bytes on the archive filesystem.")
        gauge("voice_store_min_free_bytes", body["min_free_bytes"], "The floor below which uploads are refused (507).")
        gauge("voice_store_reserved_bytes", body["reserved_bytes"], "Bytes reserved by uploads in flight.")
        gauge("voice_store_objects", body["objects"], "Recordings stored.")
        gauge("voice_store_bytes", body["bytes"], "Bytes of recordings stored.")
        gauge("voice_store_scrub_checked", body["scrub"]["checked"], "Objects re-hashed by the last scrub.")
        gauge("voice_store_scrub_mismatches", body["scrub"]["mismatches"], "Objects whose bytes no longer match their sha256 (last scrub).")
        completed = body["scrub"].get("last_completed_at")
        if completed:
            with contextlib.suppress(ValueError):
                gauge(
                    "voice_store_scrub_last_completed_timestamp_seconds",
                    int(datetime.fromisoformat(completed).timestamp()),
                    "When the last scrub finished.",
                )
        with self._lock:
            requests = dict(self.requests)
            put_bytes, swept = self.put_bytes_total, self.swept_total
        lines.append("# HELP voice_store_requests_total Requests answered, by operation and status.")
        lines.append("# TYPE voice_store_requests_total counter")
        for (op, code), n in sorted(requests.items()):
            lines.append(f'voice_store_requests_total{{op="{op}",code="{code}"}} {n}')
        lines.append("# HELP voice_store_put_bytes_total Bytes of recordings stored since start.")
        lines.append("# TYPE voice_store_put_bytes_total counter")
        lines.append(f"voice_store_put_bytes_total {put_bytes}")
        lines.append("# HELP voice_store_incoming_swept_total Abandoned temporary files removed.")
        lines.append("# TYPE voice_store_incoming_swept_total counter")
        lines.append(f"voice_store_incoming_swept_total {swept}")
        return "\n".join(lines) + "\n"

    # -- background work ---------------------------------------------------------

    def scan(self) -> None:
        """Count what is stored (once at start; kept current by PUT/DELETE)."""
        objects = size = 0
        try:
            users = [e for e in os.scandir(self.root) if e.is_dir() and USER_ID.match(e.name)]
        except OSError:
            users = []
        for user in users:
            try:
                sessions = [e for e in os.scandir(user.path) if e.is_dir() and SESSION_ID.match(e.name)]
            except OSError:
                continue
            for session in sessions:
                found = self.stored(session.path)
                if found is not None:
                    objects += 1
                    size += found.bytes
        with self._lock:
            self.objects, self.bytes = objects, size

    def sweep_once(self, *, now: Optional[float] = None) -> int:
        """Remove temporary files of uploads that died. Blocking."""
        now = time.time() if now is None else now
        live = set(self.in_flight_temps())
        removed = 0
        try:
            users = [e for e in os.scandir(self.root) if e.is_dir() and USER_ID.match(e.name)]
        except OSError:
            return 0
        for user in users:
            try:
                sessions = [e for e in os.scandir(user.path) if e.is_dir() and SESSION_ID.match(e.name)]
            except OSError:
                continue
            for session in sessions:
                try:
                    entries = list(os.scandir(session.path))
                except OSError:
                    continue
                for entry in entries:
                    if not entry.name.startswith((INCOMING_PREFIX, MANIFEST_PREFIX)) or entry.path in live:
                        continue
                    try:
                        age = now - entry.stat().st_mtime
                    except OSError:
                        continue
                    if age > self.settings.incoming_max_age_s:
                        with contextlib.suppress(OSError):
                            os.unlink(entry.path)
                            removed += 1
                # A session folder an interrupted first upload left empty.
                with contextlib.suppress(OSError):
                    if not os.listdir(session.path) and now - os.stat(session.path).st_mtime > self.settings.incoming_max_age_s:
                        os.rmdir(session.path)
        with self._lock:
            self.swept_total += removed
        return removed

    def scrub_once(self) -> Dict[str, Any]:
        """Re-hash every object against its manifest. Blocking, rate-limited."""
        started = _now_iso()
        with self._lock:
            self.scrub["last_started_at"] = started
        checked = mismatches = 0
        mismatched: List[str] = []
        try:
            users = sorted(e.name for e in os.scandir(self.root) if e.is_dir() and USER_ID.match(e.name))
        except OSError:
            users = []
        for user in users:
            if self._stop.is_set():
                return dict(self.scrub)
            try:
                sessions = sorted(
                    e.name for e in os.scandir(os.path.join(self.root, user)) if e.is_dir() and SESSION_ID.match(e.name)
                )
            except OSError:
                continue
            for session in sessions:
                if self._stop.is_set():
                    return dict(self.scrub)
                directory = self.session_dir(user, session)
                found = self.stored(directory)
                if found is None or found.sha256 is None:
                    continue
                try:
                    sha, size = _sha256_file(
                        os.path.join(directory, found.name), rate=self.settings.scrub_bytes_per_s, stop=self._stop
                    )
                except OSError:
                    continue
                if self._stop.is_set():
                    return dict(self.scrub)
                checked += 1
                if sha != found.sha256 or size != (found.manifest or {}).get("bytes", size):
                    mismatches += 1
                    mismatched.append(f"{user}/{session}")
                    log.error("scrub: %s/%s no longer matches its sha256", user, session)
        with self._lock:
            self.scrub.update(
                {"last_completed_at": _now_iso(), "checked": checked, "mismatches": mismatches}
            )
            self._mismatched = mismatched[:100]
            return dict(self.scrub)

    def start_background(self) -> None:
        def scanner() -> None:
            try:
                self.scan()
            except Exception:  # noqa: BLE001 - counts are informational
                log.exception("the start-up scan failed")

        def sweeper() -> None:
            while not self._stop.wait(self.settings.sweep_interval_s):
                try:
                    self.sweep_once()
                except Exception:  # noqa: BLE001
                    log.exception("the sweep failed; trying again later")

        def scrubber() -> None:
            wait = self.settings.scrub_first_after_s
            while not self._stop.wait(wait):
                try:
                    self.scrub_once()
                except Exception:  # noqa: BLE001
                    log.exception("the scrub failed; trying again later")
                wait = self.settings.scrub_interval_s

        for target, name in ((scanner, "scan"), (sweeper, "sweep"), (scrubber, "scrub")):
            thread = threading.Thread(target=target, name=f"voice-store-{name}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop_background(self) -> None:
        self._stop.set()


# -------------------------------------------------------------------- app --


def create_app(settings: Settings, *, background: bool = True) -> Starlette:
    store = Store(settings)

    def handler(op: str, fn: Any) -> Any:
        async def endpoint(request: Request) -> Response:
            actual = "head" if op == "get" and request.method == "HEAD" else op
            try:
                response = await fn(request)
            except Refusal as refusal:
                response = refusal.response()
            except Exception:  # noqa: BLE001 - one request, not the process
                log.exception("%s failed", actual)
                response = JSONResponse({"reason": "internal", "detail": "The store failed."}, status_code=500)
            store.count(actual, response.status_code)
            return response

        return endpoint

    async def health(_request: Request) -> Response:
        body = await asyncio.to_thread(store.health_body)
        return JSONResponse(body, headers={"cache-control": "no-store"})

    async def metrics(_request: Request) -> Response:
        text = await asyncio.to_thread(store.metrics_text)
        return PlainTextResponse(text, media_type="text/plain; version=0.0.4; charset=utf-8")

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette):
        if background:
            store.start_background()
        try:
            yield
        finally:
            store.stop_background()

    object_path = "/v1/recordings/{user}/{session}/{name}"
    routes = [
        Route("/health", health, methods=["GET"]),
        Route("/metrics", metrics, methods=["GET"]),
        Route(object_path, handler("put", store.put), methods=["PUT"]),
        Route(object_path, handler("get", store.get), methods=["GET", "HEAD"]),
        Route("/v1/recordings/{user}/{session}", handler("delete", store.delete), methods=["DELETE"]),
        Route("/v1/inventory", handler("inventory", store.inventory), methods=["GET"]),
    ]
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.store = store
    return app


def _bind_address(env: Mapping[str, str]) -> str:
    bind = (env.get("VOICE_STORE_BIND") or "").strip()
    if not bind:
        raise SystemExit("VOICE_STORE_BIND is required: scripts/voice-store.sh derives it from the worker's management interface")
    if bind in ("0.0.0.0", "::", "[::]", "*"):
        raise SystemExit(f"VOICE_STORE_BIND={bind} is a wildcard; the store binds one address only")
    return bind


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("VOICE_STORE_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = Settings.from_env(os.environ)
    bind = _bind_address(os.environ)
    port = int(os.environ.get("VOICE_STORE_PORT") or 30011)
    cert = (os.environ.get("VOICE_STORE_TLS_CERT") or "").strip()
    key = (os.environ.get("VOICE_STORE_TLS_KEY") or "").strip()
    if bool(cert) != bool(key):
        raise SystemExit("VOICE_STORE_TLS_CERT and VOICE_STORE_TLS_KEY go together")
    if not os.path.isdir(settings.root) or not os.access(settings.root, os.W_OK):
        raise SystemExit(f"{settings.root} is not a writable directory")
    import uvicorn

    log.info("voice store on %s:%d (%s), root %s", bind, port, "tls" if cert else "plain http", settings.root)
    uvicorn.run(
        create_app(settings),
        host=bind,
        port=port,
        ssl_certfile=cert or None,
        ssl_keyfile=key or None,
        http="h11",
        loop="asyncio",
        lifespan="on",
        access_log=False,
        log_level="warning",
        server_header=False,
        proxy_headers=False,
        timeout_keep_alive=KEEP_ALIVE_TIMEOUT_S,
        limit_concurrency=32,
    )


if __name__ == "__main__":
    main()
