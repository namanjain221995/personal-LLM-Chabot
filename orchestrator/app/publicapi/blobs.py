"""Content-addressed image blobs for durable `/v1` specs (2026-09-13).

WHY. A durable generation stores its request spec so another process can
resume it after a deploy (durable.py). A spec can carry base64 images — up to
the request body cap — and a JSONB row is the wrong place for megabytes that
are only read again on a resume: TOAST churn on `api_response_requests`, and a
copy in every WAL segment. So image data URIs are written ONCE per distinct
content to `PUBLIC_API_BLOB_DIR` (/data/publicapi/blobs), the spec keeps a
`blob:sha256:<hex>` reference, and `api_response_blobs` records which response
references which blob. Identical images across requests share one file.

PERMISSIONS: 0700 directories, 0600 files, written to a temp name and renamed,
so a reader never sees a torn blob and no other local user can read a
customer's image.

THE REAPER deletes a file only when NO row references it and the file is older
than `min_age_s` — the age floor closes the race where a blob is written just
before the row that references it is inserted (the insert runs after the put).

DISK GUARD. `ensure_free_disk` refuses a launch with 503 Retry-After 60 when
the volume has less than PUBLIC_API_MIN_FREE_DISK_BYTES (20 GiB) free, before
any header is sent — /data and pgdata share one device (design, operator
check), so a full disk would take Postgres down with it.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from . import errors, registry

log = logging.getLogger(__name__)

BLOB_SCHEME = "blob:sha256:"
_SHA = re.compile(r"^[0-9a-f]{64}$")
_DATA_URI = re.compile(r"^data:([a-zA-Z0-9.+/-]+);base64,(.*)$", re.DOTALL)


def blob_dir() -> Path:
    """PUBLIC_API_BLOB_DIR (/data/publicapi/blobs)."""
    from ..config import settings

    value = getattr(settings, "public_api_blob_dir", None) or os.environ.get(
        "PUBLIC_API_BLOB_DIR", ""
    )
    return Path(str(value or "/data/publicapi/blobs"))


def min_free_disk_bytes() -> int:
    """PUBLIC_API_MIN_FREE_DISK_BYTES (20 GiB)."""
    return max(0, registry.setting_int("PUBLIC_API_MIN_FREE_DISK_BYTES", 20 * 1024**3))


class BlobStore:
    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root) if root is not None else blob_dir()

    def _ensure_dir(self, path: Path) -> None:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass

    def path(self, sha256: str) -> Path:
        if not _SHA.match(sha256):
            raise ValueError("not a sha256 digest")
        return self.root / sha256[:2] / sha256

    def put(self, data: bytes) -> Tuple[str, int]:
        """Write `data` once; returns (sha256, size). Idempotent."""
        digest = hashlib.sha256(data).hexdigest()
        target = self.path(digest)
        if target.exists():
            try:
                os.utime(target)  # refresh the reaper's age floor
            except OSError:
                pass
            return digest, len(data)
        self._ensure_dir(self.root)
        self._ensure_dir(target.parent)
        fd, tmp = tempfile.mkstemp(prefix=".blob-", dir=str(target.parent))
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return digest, len(data)

    def get(self, sha256: str) -> bytes:
        return self.path(sha256).read_bytes()

    def exists(self, sha256: str) -> bool:
        try:
            return self.path(sha256).exists()
        except ValueError:
            return False

    def delete(self, sha256: str) -> bool:
        try:
            self.path(sha256).unlink()
            return True
        except (FileNotFoundError, ValueError):
            return False

    def iter_blobs(self) -> Iterable[Tuple[str, float]]:
        if not self.root.exists():
            return
        for sub in self.root.iterdir():
            if not sub.is_dir():
                continue
            for entry in sub.iterdir():
                if _SHA.match(entry.name):
                    try:
                        yield entry.name, entry.stat().st_mtime
                    except FileNotFoundError:
                        continue

    def reap(
        self,
        referenced: Callable[[Sequence[str]], Set[str]],
        *,
        min_age_s: float = 3600.0,
        batch: int = 500,
        now: Optional[float] = None,
    ) -> int:
        """Delete unreferenced blobs older than `min_age_s`. Returns how many."""
        moment = time.time() if now is None else now
        candidates: List[str] = []
        removed = 0
        for sha, mtime in list(self.iter_blobs()):
            if moment - mtime < min_age_s:
                continue
            candidates.append(sha)
            if len(candidates) >= batch:
                removed += self._reap_batch(candidates, referenced)
                candidates = []
        if candidates:
            removed += self._reap_batch(candidates, referenced)
        return removed

    def _reap_batch(self, shas: List[str], referenced: Callable[[Sequence[str]], Set[str]]) -> int:
        keep = referenced(shas)
        return sum(1 for sha in shas if sha not in keep and self.delete(sha))


# ------------------------------------------------------ spec images --


def externalize_images(
    messages: Sequence[Dict[str, Any]], store: BlobStore
) -> Tuple[List[Dict[str, Any]], List[Tuple[str, int]]]:
    """Replace every base64 `image_url` data URI with a blob reference.

    Returns (messages, [(sha256, bytes)]). Non-data URLs are left alone (the
    planner refuses remote URLs today; a reference is still not our data)."""
    blobs: Dict[str, int] = {}
    out: List[Dict[str, Any]] = []
    for message in messages:
        copied = dict(message)
        content = message.get("content")
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    image = part.get("image_url")
                    url = image.get("url") if isinstance(image, dict) else image
                    match = _DATA_URI.match(str(url or ""))
                    if match:
                        try:
                            raw = base64.b64decode(match.group(2), validate=False)
                        except (binascii.Error, ValueError):
                            parts.append(part)
                            continue
                        sha, size = store.put(raw)
                        blobs[sha] = size
                        new_image = dict(image) if isinstance(image, dict) else {}
                        new_image["url"] = f"{BLOB_SCHEME}{sha}"
                        new_image["mime"] = match.group(1)
                        parts.append({**part, "image_url": new_image})
                        continue
                parts.append(part)
            copied["content"] = parts
        out.append(copied)
    return out, sorted(blobs.items())


def internalize_images(messages: Sequence[Dict[str, Any]], store: BlobStore) -> List[Dict[str, Any]]:
    """The inverse of `externalize_images`, at dispatch time. A missing blob
    raises FileNotFoundError: the run cannot be resumed faithfully and fails
    retryably rather than generating from a different prompt."""
    out: List[Dict[str, Any]] = []
    for message in messages:
        copied = dict(message)
        content = message.get("content")
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    image = part.get("image_url")
                    url = str((image or {}).get("url") or "") if isinstance(image, dict) else ""
                    if url.startswith(BLOB_SCHEME):
                        sha = url[len(BLOB_SCHEME):]
                        raw = store.get(sha)
                        mime = str(image.get("mime") or "image/png")
                        new_image = {k: v for k, v in image.items() if k != "mime"}
                        new_image["url"] = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
                        parts.append({**part, "image_url": new_image})
                        continue
                parts.append(part)
            copied["content"] = parts
        out.append(copied)
    return out


def free_disk_bytes(path: Optional[Path] = None) -> Optional[int]:
    target = Path(path) if path is not None else blob_dir()
    probe = target
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        stats = os.statvfs(str(probe))
    except OSError:
        return None
    return int(stats.f_bavail) * int(stats.f_frsize)


def ensure_free_disk(path: Optional[Path] = None, *, minimum: Optional[int] = None) -> None:
    """The launch disk guard: 503 `model_unavailable`, Retry-After 60, before
    headers. An unreadable volume is not refused (the guard is physical, and
    a statvfs failure is not evidence of a full disk)."""
    free = free_disk_bytes(path)
    floor = min_free_disk_bytes() if minimum is None else int(minimum)
    if free is not None and free < floor:
        raise errors.model_unavailable(retry_after=60)


__all__ = [
    "BLOB_SCHEME",
    "BlobStore",
    "blob_dir",
    "ensure_free_disk",
    "externalize_images",
    "free_disk_bytes",
    "internalize_images",
    "min_free_disk_bytes",
]
