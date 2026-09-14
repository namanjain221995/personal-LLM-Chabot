"""Where the Files API's bytes live on disk, and the two rules that guard it.

LAYOUT (design §7.1), under PUBLIC_API_FILES_DIR (default /data/api-files):

    _single/<nonce>.tmp                               POST /v1/files in flight
    _uploads/<upload_id>/parts/<n>                    accepted parts
    _uploads/<upload_id>/parts/<n>.<nonce>.tmp        a raw PUT part in flight
    _uploads/<upload_id>/parts/incoming.<nonce>.tmp   a multipart part before its number
    _uploads/<upload_id>/assembled.tmp(.assembling)   the `assemble` stage's output
    _inline/<request_id>/                             file_data for one response
    _trash/<blob_id>.<nonce>/                         a purged blob's directory, moved aside
    <project_id>/<sha256>/original                    the bytes (0640)
    <project_id>/<sha256>/derived/…                   processing output (team B)

RULE 1 — THE CLIENT'S FILENAME IS NEVER A PATH. Every segment joined here is an
id or a digest that matched its regex in `ids.py` first; anything else raises
`ValueError` before `os.path.join` runs.

RULE 2 — NOTHING IS DELETED OUTSIDE THE TWO TREES. `remove_tree` resolves the
real path and refuses anything that is not STRICTLY below PUBLIC_API_FILES_DIR
or VIDEO_DATA_DIR (the root itself included), so a symlink planted in a derived
directory, or a row that somehow names `/`, cannot turn a purge into `rm -rf`.

THE WATERMARK (design §7.4). New bytes are refused while free space minus the
bytes about to be written would fall under PUBLIC_API_FILES_MIN_FREE_GIB. That
disk also holds Postgres, LanceDB (119 G) and the model cache (68 G); the API is
the tenant that must give way.
"""
from __future__ import annotations

import errno
import hashlib
import logging
import os
import shutil
import uuid
from typing import Optional

from . import ids, limits

log = logging.getLogger(__name__)

SINGLE_DIR = "_single"
UPLOADS_DIR = "_uploads"
INLINE_DIR = "_inline"
TRASH_DIR = "_trash"
PARTS_DIR = "parts"
ORIGINAL_NAME = "original"
DERIVED_NAME = "derived"

#: 0750 for directories, 0640 for the original: the orchestrator's own user and
#: group read them; nothing else on the host does (design §7.1).
DIR_MODE = 0o750
FILE_MODE = 0o640

#: Retry-After on `storage_unavailable`. Capped at 60 because openai-python
#: ignores a Retry-After above 120 s and openai-node waits the FULL value
#: (finding #12); `x-should-retry: false` then decides that neither retries.
STORAGE_RETRY_AFTER_S = 60


class StorageUnavailable(Exception):
    """Free space is below the watermark. The HTTP layer renders it as
    `503 storage_unavailable` with `Retry-After: 60` and `x-should-retry:
    false`; the processing runner defers the blob instead of writing under the
    floor (finding #17)."""

    def __init__(self, free_bytes: int, needed_bytes: int, floor_bytes: int) -> None:
        super().__init__("free space is below the files watermark")
        self.free_bytes = int(free_bytes)
        self.needed_bytes = int(needed_bytes)
        self.floor_bytes = int(floor_bytes)
        self.retry_after = STORAGE_RETRY_AFTER_S


# ------------------------------------------------------------ the layout --


def root() -> str:
    return os.path.abspath(limits.files_dir())


def _require(check, value: str, what: str) -> str:
    if not check(value):
        raise ValueError(f"not a valid {what}")
    return value


def blob_dir(project_id: str, sha256: str) -> str:
    _require(ids.is_project_id, project_id, "project id")
    _require(ids.is_sha256, sha256, "sha256")
    return os.path.join(root(), project_id, sha256)


def original_path(project_id: str, sha256: str) -> str:
    return os.path.join(blob_dir(project_id, sha256), ORIGINAL_NAME)


def derived_dir(project_id: str, sha256: str) -> str:
    return os.path.join(blob_dir(project_id, sha256), DERIVED_NAME)


def upload_dir(upload_id: str) -> str:
    _require(ids.is_upload_id, upload_id, "upload id")
    return os.path.join(root(), UPLOADS_DIR, upload_id)


def parts_dir(upload_id: str) -> str:
    return os.path.join(upload_dir(upload_id), PARTS_DIR)


def part_path(upload_id: str, part_number: int) -> str:
    number = int(part_number)
    if number < 0 or number >= 1_000_000:
        raise ValueError("not a valid part number")
    return os.path.join(parts_dir(upload_id), str(number))


def nonce() -> str:
    return uuid.uuid4().hex[:16]


def part_tmp_path(upload_id: str, part_number: int) -> str:
    """`parts/<n>.<nonce>.tmp`. A nonce, not `<n>.tmp`: a retried part can
    arrive while the server still drains the connection the client gave up on,
    and two writers on one temporary file would interleave (V29 lesson)."""
    return f"{part_path(upload_id, part_number)}.{nonce()}.tmp"


def incoming_part_tmp_path(upload_id: str) -> str:
    """A multipart part's body lands here before its number is known (the
    `part_number` field may follow the `data` field: openai-node sends extra
    fields after the file)."""
    return os.path.join(parts_dir(upload_id), f"incoming.{nonce()}.tmp")


def assembled_tmp_path(upload_id: str, attempt_nonce: Optional[str] = None) -> str:
    """`assembled.<nonce>.tmp`: a per-attempt name, so an assembler whose lease
    lapsed during a stalled copy can never write into the file a newer
    claimant is producing."""
    suffix = f".{attempt_nonce}" if attempt_nonce else ""
    if attempt_nonce is not None and not (attempt_nonce.isalnum() and len(attempt_nonce) <= 32):
        raise ValueError("not a valid attempt nonce")
    return os.path.join(upload_dir(upload_id), f"assembled{suffix}.tmp")


def inline_dir(request_id: str) -> str:
    _require(ids.is_request_id, request_id, "request id")
    return os.path.join(root(), INLINE_DIR, request_id)


def single_tmp() -> str:
    return os.path.join(root(), SINGLE_DIR, f"{nonce()}.tmp")


def api_video_hash(project_id: str, sha256: str) -> str:
    """The `video_analyses.content_hash` for an API audio/video blob.

    `sha256("api-files\\0" + project_id + "\\0" + sha256)`: the chat app's
    global `content_hash` dedupe (`video/api.py:67-114`, which answers
    `reused: true`) can then never match across projects, or between chat and
    the API — it would otherwise be a presence oracle for another tenant's
    bytes (design §7.2 rule 3). Not a secret: nothing depends on it being
    unguessable, only on it being different per project."""
    _require(ids.is_project_id, project_id, "project id")
    _require(ids.is_sha256, sha256, "sha256")
    return hashlib.sha256(f"api-files\0{project_id}\0{sha256}".encode("ascii")).hexdigest()


def ensure_dirs() -> None:
    """Create the root and its three working directories, mode 0750.
    Called at startup (integration: `main.py`) and by the routes lazily."""
    for path in (root(), os.path.join(root(), SINGLE_DIR), os.path.join(root(), UPLOADS_DIR), os.path.join(root(), INLINE_DIR)):
        os.makedirs(path, mode=DIR_MODE, exist_ok=True)


def makedirs(path: str) -> None:
    os.makedirs(path, mode=DIR_MODE, exist_ok=True)


def fsync_dir(path: str) -> None:
    """fsync a directory so a rename inside it survives power loss. Best
    effort: some filesystems refuse O_RDONLY directory fsync (EINVAL)."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


# ------------------------------------------------------------- the fence --


def _fence_roots() -> tuple:
    roots = []
    for candidate in (limits.files_dir(), limits.video_data_dir()):
        try:
            roots.append(os.path.realpath(candidate))
        except OSError:
            continue
    return tuple(roots)


def is_fenced(path: str) -> bool:
    """True when `path`'s real path is strictly below one of the two roots."""
    real = os.path.realpath(path)
    for fence in _fence_roots():
        if real != fence and real.startswith(fence.rstrip(os.sep) + os.sep):
            return True
    return False


def remove_tree(path: str) -> bool:
    """rmtree `path` if it is strictly under a fence root; True when something
    was removed. A missing path is not an error. Refusal raises ValueError —
    loudly, because a caller that computed a path outside the trees is a bug
    that must not be retried into a success."""
    if not is_fenced(path):
        raise ValueError("refusing to remove a path outside the files and video trees")
    real = os.path.realpath(path)
    if os.path.islink(path):
        # The link itself is inside the fence (checked via realpath of its
        # parent chain above only when it resolves inside); remove the link,
        # never what it points at.
        os.unlink(path)
        return True
    if not os.path.lexists(real):
        return False
    if os.path.isdir(real):
        shutil.rmtree(real, ignore_errors=False)
    else:
        os.unlink(real)
    return True


def trash_root() -> str:
    return os.path.join(root(), TRASH_DIR)


def move_blob_dir_aside(blob: dict) -> Optional[str]:
    """Rename a blob's directory to `_trash/<blob_id>.<nonce>` and return the
    new path (None when the directory is already gone).

    WHY A RENAME AND NOT AN rmtree (review, 2026-09-13). `<project>/<sha256>/`
    is keyed by CONTENT, not by blob id: once a purged blob's row is deleted,
    the same bytes uploaded again get a NEW blob at the SAME path. An rmtree
    that runs after that — a second purge of the same blob, or a purge whose
    job cancellation took seconds — deleted the new file's bytes (reproduced:
    GET file 200, GET content 404). The caller (`schema.retire_deleting_blob`)
    renames while it holds the old row `FOR UPDATE`; while that row exists the
    `(project_id, sha256)` unique index guarantees no other blob owns the path,
    so the rename only ever takes the old blob's bytes. The slow rmtree then
    runs on the trash path, which nothing else can name. Same filesystem, so
    the rename is O(1) whatever the directory holds."""
    blob_id = _require(ids.is_blob_id, str(blob.get("id") or ""), "blob id")
    source = blob_dir(str(blob.get("project_id") or ""), str(blob.get("sha256") or ""))
    if not os.path.lexists(source):
        return None
    if not is_fenced(os.path.dirname(source)):
        # `<root>/<project_id>` resolving outside the tree (a planted symlink).
        raise ValueError("refusing to move a path outside the files tree")
    makedirs(trash_root())
    target = os.path.join(trash_root(), f"{blob_id}.{nonce()}")
    try:
        os.replace(source, target)
    except FileNotFoundError:
        return None
    fsync_dir(os.path.dirname(source))
    return target


def remove_trash(path: Optional[str]) -> bool:
    """rmtree one `_trash/` entry; tolerant of a concurrent remover (the
    upload sweep clears leftovers of a crash between the rename and this)."""
    if not path:
        return False
    real_parent = os.path.realpath(os.path.dirname(path))
    if real_parent != os.path.realpath(trash_root()):
        raise ValueError("not a trash entry")
    for _ in range(3):
        try:
            return remove_tree(path)
        except FileNotFoundError:
            if not os.path.lexists(path):
                return False
    return remove_tree(path)


def remove_file(path: str) -> bool:
    """unlink one file under the fence; False when it was already gone."""
    if not is_fenced(path):
        raise ValueError("refusing to remove a path outside the files and video trees")
    try:
        os.unlink(path)
        return True
    except FileNotFoundError:
        return False


def discard(path: Optional[str]) -> None:
    """Remove a temporary file, never raising (the error path must not mask
    the error it is cleaning up after)."""
    if not path:
        return
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        log.warning("could not remove a temporary files-api file", exc_info=True)


def dir_bytes(path: str) -> int:
    """Bytes under `path`, following nothing (a symlink counts as its own
    size, never as its target's)."""
    total = 0
    for base, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                total += os.lstat(os.path.join(base, name)).st_size
            except OSError:
                pass
    return total


# --------------------------------------------------------- the watermark --


def _statvfs_target() -> str:
    """The nearest existing ancestor of the root: before `ensure_dirs` runs
    (a fresh volume) statvfs of the root itself would raise."""
    path = root()
    while path and not os.path.exists(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return path or os.sep


def free_bytes() -> int:
    stats = os.statvfs(_statvfs_target())
    return int(stats.f_bavail) * int(stats.f_frsize)


def require_free(extra_bytes: int) -> None:
    """Raise `StorageUnavailable` when writing `extra_bytes` would leave less
    than the watermark free. `extra_bytes` is what the caller is about to
    write: a part's declared length, twice an upload's size (parts plus the
    assembled copy), or a processing stage's projection."""
    floor = limits.min_free_bytes()
    free = free_bytes()
    needed = max(0, int(extra_bytes or 0))
    if free - needed < floor:
        log.warning(
            "files api refused new bytes: free=%d needed=%d floor=%d", free, needed, floor
        )
        raise StorageUnavailable(free, needed, floor)


_MIB = 1024 * 1024

#: Vector index ceiling per blob: 50,000 x 1,024 float32 = 195 MiB (measured).
_VECTORS_MAX = 195 * _MIB


def projected_derived_bytes(kind: str, blob_bytes: int) -> int:
    """An upper estimate of what processing `kind` will write under `derived/`
    (finding #17), used for `require_free` before each derived-writing stage.

    The factors, and where each comes from (2026-09-13):
    * text-bearing office/html/text: a fully-compressible DOCX expanded 34.8x
      to JSONL (measured) — 35x, plus chunks (~1x) and the vector index;
    * pdf: the text layer is at most the file's own size in practice; OCR
      renders are transient (one page at a time) — 2x plus vectors;
    * spreadsheet/tabular: per-sheet CSV + row-block text ≈ the XLSX's
      uncompressed XML, bounded by the zip ratio guard (200) — 8x plus vectors,
      an estimate, not a measurement;
    * image: three variants and a PNG of at most 2,560 px on the long edge,
      RGBA uncompressed 26 MiB each worst case — 4 x 26 MiB;
    * audio/video: `audio.wav` is 1.83 MB/min (measured) and frames/text are
      small beside it; duration is unknown before `probe`, so the blob's own
      size is used (a compressed stream decodes to more WAV than its bytes
      only below ~245 kbit/s, i.e. for audio-only files, hence 2x);
    * assemble / unknown: the assembled copy itself.
    """
    size = max(0, int(blob_bytes or 0))
    if kind in ("document", "presentation", "html", "text"):
        return size * 36 + _VECTORS_MAX
    if kind == "pdf":
        return size * 2 + _VECTORS_MAX
    if kind in ("spreadsheet", "tabular"):
        return size * 8 + _VECTORS_MAX
    if kind == "image":
        return 4 * 26 * _MIB
    if kind == "audio":
        return size * 2 + _VECTORS_MAX
    if kind == "video":
        return size + _VECTORS_MAX
    return size


def is_enospc(exc: BaseException) -> bool:
    return isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, errno.EDQUOT)
