"""Disk that public work is about to spend, reserved before it is spent.

WHY A LEDGER AND NOT A `statvfs` CHECK (no-timeout design, 2026-09-13). The
public API has no usage limits, so nothing bounds how many transcriptions are
in flight except the machine. A two-hour recording decodes to 230 MB of 16 kHz
mono PCM (32,000 B/s), and `/data` is the same device as PostgreSQL's data
directory, LanceDB and the model cache. Checking free space once, before
decoding, lets twenty jobs that each see "plenty free" start together and
spend it twenty times. Here every job RESERVES what it will write before it
writes it, and every later check subtracts what is reserved and not yet
written. The check is against `statvfs` free MINUS a floor
(PUBLIC_API_MIN_FREE_DISK_BYTES, 20 GiB), so public work is refused while the
database still has room to write.

ONE WATERMARK ON A SHARED DEVICE (review finding, 2026-09-13). The Files API
refuses new bytes below its own watermark, PUBLIC_API_FILES_MIN_FREE_GIB
(250 GiB, Files design §7.4), and the default PUBLIC_API_FILES_DIR
(/data/api-files) is on the same ext4 device as PUBLIC_API_ASR_CACHE_DIR
(/data/publicapi/asr). With two floors, unlimited transcription could draw the
device from 250 GiB free down to 20 GiB while every upload, part and complete
was refused 503 — and the Files design's promise that Postgres and chat are
never the writers to fail would stop holding. So a ledger whose root shares
`st_dev` with the Files store uses the LARGER of the two floors
(`floor_for`); on another device the 20 GiB floor stands. When the device
cannot be read, or the Files watermark cannot be parsed, the ledger assumes
the shared device and the Files default (fail closed: the larger floor).
The Files store's own check reads raw `statvfs`, so it cannot see bytes this
ledger has promised and not yet written; its integration seam is to subtract
`ledger_for(files_dir).outstanding_bytes()` (the same ledger object on one
device).

WRITTEN BYTES STOP COUNTING TWICE. Once bytes land on disk `statvfs` already
shows them as used, so a reservation's outstanding amount is
`reserved - written`, never the whole reservation for the file's life
(`Reservation.note_written`).

ONE BUDGET PER DEVICE. Two roots on the same filesystem (the ASR cache and the
files store) share one ledger, keyed by `st_dev`: they spend the same free
space.

A refusal is `DiskFull`, which a route turns into a 503 with Retry-After 60
BEFORE it writes a byte (`DiskFull.api_error`). Sixty seconds is the design's
figure: both SDKs honour it, and a disk that is short now is rarely long
enough to wait for inside one call.

`DiskSink` is the one streaming writer for request bodies that land here: a
0600 temporary file in a 0700 directory, sha256 computed on the way in, the
byte cap enforced while reading (never trusting Content-Length), writes and
hashing off the event loop in 1 MiB batches, fsync, and an atomic rename.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import secrets
import threading
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from ..config import _int, settings
from . import errors

log = logging.getLogger(__name__)

GIB = 1024 ** 3
MIB = 1024 ** 2

#: The floor below which public work may not push the device. 20 GiB is the
#: no-timeout design's launch disk guard: /data and pgdata share one NVMe
#: device, and Postgres must never be the writer that finds it full.
DEFAULT_MIN_FREE_BYTES = 20 * GIB

#: The Files API's watermark default (PUBLIC_API_FILES_MIN_FREE_GIB = 250,
#: Files design §7.4), used only when `apifiles.limits` cannot answer. One
#: maximum 100 GiB upload assembles at 2x; /data/lancedb 119 G, Prometheus
#: 20 GiB and the models 68 G share the device (measured 2026-09-13).
DEFAULT_FILES_MIN_FREE_BYTES = 250 * GIB

#: PUBLIC_API_FILES_DIR's default, used only when `apifiles.limits` cannot answer.
DEFAULT_FILES_DIR = "/data/api-files"

#: Retry-After on a refusal (design: "503 Retry-After 60").
RETRY_AFTER_S = 60

#: Bytes per write batch in `DiskSink`. 1 MiB keeps each off-loop hop cheap
#: (sha256 of 1 MiB is ~0.4 ms on one core here: 2,365 MiB/s measured
#: 2026-09-13) while never holding more than one batch per sink in memory.
SINK_BATCH_BYTES = 1 * MIB


def min_free_bytes() -> int:
    """PUBLIC_API_MIN_FREE_DISK_BYTES (21,474,836,480): read at call time.

    `settings.public_api_min_free_disk_bytes` when config.py defines it, else
    the environment parsed by config.py's own `_int` (blank means default).
    """
    value = getattr(settings, "public_api_min_free_disk_bytes", None)
    if value is not None:
        return max(0, int(value))
    return max(0, int(_int("PUBLIC_API_MIN_FREE_DISK_BYTES", DEFAULT_MIN_FREE_BYTES)))


def files_store_dir() -> str:
    """PUBLIC_API_FILES_DIR as the Files API reads it (`apifiles.limits`)."""
    try:
        from ..apifiles import limits as files_limits

        return str(files_limits.files_dir())
    except ImportError:
        value = getattr(settings, "public_api_files_dir", None)
        raw = value if isinstance(value, str) and value.strip() else os.environ.get("PUBLIC_API_FILES_DIR", "")
        return raw.strip() or DEFAULT_FILES_DIR


def files_store_floor_bytes() -> int:
    """PUBLIC_API_FILES_MIN_FREE_GIB in bytes, as the Files API reads it.

    A value the Files API itself cannot parse fails CLOSED here to the 250 GiB
    default (a 500 from a reservation would be worse than a conservative
    floor, and the Files routes surface the misconfiguration anyway)."""
    try:
        from ..apifiles import limits as files_limits

        return max(0, int(files_limits.min_free_bytes()))
    except ImportError:
        pass
    except (TypeError, ValueError):
        log.warning("PUBLIC_API_FILES_MIN_FREE_GIB is not a number; using the 250 GiB default")
        return DEFAULT_FILES_MIN_FREE_BYTES
    try:
        value = getattr(settings, "public_api_files_min_free_gib", None)
        if value is None or value == "":
            raw = os.environ.get("PUBLIC_API_FILES_MIN_FREE_GIB", "")
            value = raw.strip() or None
        if value is None:
            return DEFAULT_FILES_MIN_FREE_BYTES
        return max(0, int(float(value) * GIB))
    except (TypeError, ValueError):
        log.warning("PUBLIC_API_FILES_MIN_FREE_GIB is not a number; using the 250 GiB default")
        return DEFAULT_FILES_MIN_FREE_BYTES


def nearest_existing(path: str) -> str:
    """`path`, or its nearest existing ancestor (a cache dir may not exist yet)."""
    path = os.path.abspath(path)
    while not os.path.exists(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return path


def device_of(path: str) -> int:
    """`st_dev` of the filesystem that holds (or will hold) `path`."""
    return os.stat(nearest_existing(path)).st_dev


def shares_files_store_device(root: str) -> bool:
    """True when `root` is on the Files store's filesystem. An unreadable
    device counts as shared: the answer only ever raises the floor."""
    try:
        return device_of(root) == device_of(files_store_dir())
    except OSError:
        return True


def floor_for(root: str) -> int:
    """The floor a ledger for `root` enforces: PUBLIC_API_MIN_FREE_DISK_BYTES,
    raised to the Files API watermark when `root` shares its device. Read per
    call (cheap: two `stat`s; both settings may change under a test or an
    operator's .env)."""
    floor = min_free_bytes()
    if shares_files_store_device(root):
        floor = max(floor, files_store_floor_bytes())
    return floor


class DiskFull(Exception):
    """Refused: the bytes asked for would take the device under its floor."""

    retry_after = RETRY_AFTER_S

    def __init__(self, needed_bytes: int, available_bytes: int, purpose: str) -> None:
        super().__init__(
            f"{purpose}: {needed_bytes} bytes needed, {max(0, available_bytes)} available above the floor"
        )
        self.needed_bytes = int(needed_bytes)
        self.available_bytes = int(available_bytes)
        self.purpose = purpose

    def api_error(self) -> errors.ApiError:
        """The wire refusal. `storage_unavailable` when errors.py carries it
        (Files API, Team A adds the code), else `model_unavailable`: both are
        503 with Retry-After 60, and neither names a path or a byte count."""
        if "storage_unavailable" in errors.error_codes():
            return errors.ApiError(
                "storage_unavailable",
                "The service is short of storage at the moment. Try again shortly.",
                retry_after=self.retry_after,
            )
        return errors.model_unavailable(retry_after=self.retry_after)


class CapExceeded(Exception):
    """A streamed body went past its byte cap while it was being read."""

    def __init__(self, cap_bytes: int) -> None:
        super().__init__(f"more than {cap_bytes} bytes")
        self.cap_bytes = int(cap_bytes)


class Reservation:
    """Bytes promised to one writer. Thread-safe; release is idempotent."""

    __slots__ = ("_ledger", "purpose", "reserved", "written", "released")

    def __init__(self, ledger: "DiskLedger", reserved: int, purpose: str) -> None:
        self._ledger = ledger
        self.purpose = purpose
        self.reserved = int(reserved)
        self.written = 0
        self.released = False

    @property
    def outstanding(self) -> int:
        """Reserved and not yet visible to statvfs."""
        if self.released:
            return 0
        return max(0, self.reserved - self.written)

    def note_written(self, total_written: int) -> None:
        """The writer's running total of bytes on disk (monotonic)."""
        with self._ledger._lock:
            self.written = max(self.written, int(total_written))

    def forget_written(self) -> None:
        """The writer deleted what it wrote (a failed attempt it will retry):
        those bytes are free again, so the whole reservation counts again."""
        with self._ledger._lock:
            self.written = 0

    def ensure_covers(self, total_bytes: int, *, step_bytes: int = 64 * MIB) -> None:
        """Grow the reservation so it covers `total_bytes`, in steps, or raise
        `DiskFull`. A decoder whose estimate was low calls this as output grows."""
        with self._ledger._lock:
            if self.released or total_bytes <= self.reserved:
                return
            extra = max(int(total_bytes) - self.reserved, int(step_bytes))
            available = self._ledger._available_locked()
            if extra > available:
                raise DiskFull(extra, available, self.purpose)
            self.reserved += extra

    def release(self) -> None:
        with self._ledger._lock:
            if self.released:
                return
            self.released = True
            self._ledger._reservations.discard(self)

    def __enter__(self) -> "Reservation":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


class DiskLedger:
    """Free space on one device, net of promises."""

    def __init__(
        self,
        root: str,
        *,
        statvfs: Optional[Callable[[str], os.statvfs_result]] = None,
        floor_bytes: Optional[Callable[[], int]] = None,
    ) -> None:
        self.root = root
        self._statvfs = statvfs or os.statvfs
        self._floor = floor_bytes or (lambda: floor_for(self.root))
        self._lock = threading.Lock()
        self._reservations: "set[Reservation]" = set()

    def _probe_path(self) -> str:
        """The nearest existing ancestor of `root` (the cache dir may not exist yet)."""
        return nearest_existing(self.root)

    def floor_bytes(self) -> int:
        """The floor this ledger enforces right now (see `floor_for`)."""
        return max(0, int(self._floor()))

    def raw_free_bytes(self) -> int:
        st = self._statvfs(self._probe_path())
        return int(st.f_bavail) * int(st.f_frsize)

    def outstanding_bytes(self) -> int:
        with self._lock:
            return sum(r.outstanding for r in self._reservations)

    def _available_locked(self) -> int:
        outstanding = sum(r.outstanding for r in self._reservations)
        return self.raw_free_bytes() - self.floor_bytes() - outstanding

    def available_bytes(self) -> int:
        """What a new reservation may take right now (can be negative)."""
        with self._lock:
            return self._available_locked()

    def check(self, nbytes: int, *, purpose: str = "check") -> None:
        """Raise `DiskFull` if `nbytes` would not fit; reserve nothing."""
        with self._lock:
            available = self._available_locked()
        if int(nbytes) > available:
            raise DiskFull(int(nbytes), available, purpose)

    def reserve(self, nbytes: int, *, purpose: str) -> Reservation:
        """Promise `nbytes` to one writer, or raise `DiskFull`."""
        nbytes = max(0, int(nbytes))
        with self._lock:
            available = self._available_locked()
            if nbytes > available:
                raise DiskFull(nbytes, available, purpose)
            reservation = Reservation(self, nbytes, purpose)
            self._reservations.add(reservation)
        return reservation

    def snapshot(self) -> Dict[str, int]:
        """For health payloads and tests. Never names the root path."""
        with self._lock:
            outstanding = sum(r.outstanding for r in self._reservations)
            count = len(self._reservations)
        return {
            "free_bytes": self.raw_free_bytes(),
            "floor_bytes": self.floor_bytes(),
            "outstanding_bytes": outstanding,
            "reservations": count,
        }


_ledgers: Dict[int, DiskLedger] = {}
_ledgers_lock = threading.Lock()


def ledger_for(root: str) -> DiskLedger:
    """The process-wide ledger of the device that holds `root`. The Files
    store and the ASR cache on one device get the SAME object, so either can
    see what the other has promised (`outstanding_bytes`)."""
    device = device_of(root)
    with _ledgers_lock:
        ledger = _ledgers.get(device)
        if ledger is None:
            ledger = DiskLedger(root)
            _ledgers[device] = ledger
        return ledger


def reset_for_tests() -> None:
    with _ledgers_lock:
        _ledgers.clear()


# ------------------------------------------------------------ the sink --


@dataclass(frozen=True)
class Stored:
    path: str
    bytes: int
    sha256: str


def ensure_private_dir(path: str) -> str:
    """mkdir -p with 0700 on every directory this module creates."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:  # pragma: no cover - a directory we do not own
        pass
    return path


class DiskSink:
    """Stream bytes to a private temporary file under a reservation.

        async with DiskSink(directory, cap_bytes=..., ledger=...) as sink:
            async for chunk in body:
                await sink.write(chunk)
            stored = await sink.commit(final_path)

    Leaving the block without `commit` (an exception, a disconnect, a cap
    refusal) removes the temporary file and releases the reservation.
    """

    def __init__(
        self,
        directory: str,
        *,
        cap_bytes: int,
        ledger: DiskLedger,
        purpose: str = "ingest",
        batch_bytes: int = SINK_BATCH_BYTES,
    ) -> None:
        self.directory = directory
        self.cap_bytes = int(cap_bytes)
        self.ledger = ledger
        self.purpose = purpose
        self.batch_bytes = max(1, int(batch_bytes))
        self.bytes = 0
        self.tmp_path = ""
        self._fd: Optional[int] = None
        self._hash = hashlib.sha256()
        self._pending: List[bytes] = []
        self._pending_bytes = 0
        self._reservation: Optional[Reservation] = None
        self._done = False

    async def __aenter__(self) -> "DiskSink":
        # The whole cap is reserved up front: a body is refused before its
        # first byte is written rather than half-way through.
        self._reservation = self.ledger.reserve(self.cap_bytes, purpose=self.purpose)
        try:
            await asyncio.to_thread(self._open)
        except BaseException:
            self._reservation.release()
            raise
        return self

    def _open(self) -> None:
        ensure_private_dir(self.directory)
        self.tmp_path = os.path.join(self.directory, f".incoming.{secrets.token_hex(8)}.tmp")
        self._fd = os.open(self.tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if not self._done:
            await self.abort()

    async def write(self, chunk: bytes) -> None:
        if not chunk:
            return
        if self.bytes + self._pending_bytes + len(chunk) > self.cap_bytes:
            await self.abort()
            raise CapExceeded(self.cap_bytes)
        self._pending.append(bytes(chunk))
        self._pending_bytes += len(chunk)
        if self._pending_bytes >= self.batch_bytes:
            await self._flush()

    async def _flush(self) -> None:
        if not self._pending:
            return
        data = b"".join(self._pending)
        self._pending.clear()
        self._pending_bytes = 0
        await asyncio.to_thread(self._write_batch, data)

    def _write_batch(self, data: bytes) -> None:
        assert self._fd is not None
        view = memoryview(data)
        while view:
            written = os.write(self._fd, view)
            view = view[written:]
        self._hash.update(data)
        self.bytes += len(data)
        if self._reservation is not None:
            self._reservation.note_written(self.bytes)

    async def commit(self, final_path: str) -> Stored:
        await self._flush()
        digest = self._hash.hexdigest()
        await asyncio.to_thread(self._commit, final_path)
        self._done = True
        if self._reservation is not None:
            self._reservation.release()
        return Stored(final_path, self.bytes, digest)

    def _commit(self, final_path: str) -> None:
        assert self._fd is not None
        os.fsync(self._fd)
        os.close(self._fd)
        self._fd = None
        ensure_private_dir(os.path.dirname(final_path))
        os.replace(self.tmp_path, final_path)
        dir_fd = os.open(os.path.dirname(final_path), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    async def abort(self) -> None:
        if self._done:
            return
        self._done = True
        self._pending.clear()
        self._pending_bytes = 0
        await asyncio.to_thread(self._remove)
        if self._reservation is not None:
            self._reservation.release()

    def _remove(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        if self.tmp_path:
            try:
                os.unlink(self.tmp_path)
            except FileNotFoundError:
                pass


__all__ = [
    "CapExceeded",
    "DEFAULT_FILES_DIR",
    "DEFAULT_FILES_MIN_FREE_BYTES",
    "DEFAULT_MIN_FREE_BYTES",
    "DiskFull",
    "DiskLedger",
    "DiskSink",
    "RETRY_AFTER_S",
    "Reservation",
    "Stored",
    "device_of",
    "ensure_private_dir",
    "files_store_dir",
    "files_store_floor_bytes",
    "floor_for",
    "ledger_for",
    "min_free_bytes",
    "nearest_existing",
    "reset_for_tests",
    "shares_files_store_device",
]
