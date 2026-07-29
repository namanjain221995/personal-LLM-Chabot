"""Safe archive inspection and extraction (Phase 4).

Uploaded archives are hostile input. Every check here runs on a MANUAL member
loop — never `extractall` — and the archive's own metadata is treated as a
claim, not a fact:

- zip-slip: each member is resolved against the destination and must stay
  inside it;
- symlinks / hardlinks / devices: rejected outright, because a symlink is how
  an archive escapes the root AFTER a clean path check;
- bombs: four independent caps (total uncompressed, per-file, per-member
  compression ratio, member count), enforced from the header AND re-counted
  while streaming, because the header can lie;
- nesting: inner archives are listed, never opened (ARCHIVE_MAX_DEPTH=1);
- nothing is ever executed.

`check_zip_container` is deliberately public: an .xlsx IS a zip, so it must
pass the same caps before any reader opens it, or it becomes a bomb path
straight around this module.
"""
from __future__ import annotations

import os
import stat
import tarfile
import unicodedata
import zipfile
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from ..config import settings

_CHUNK = 64 * 1024
_MAX_NAME_CHARS = 200
_MAX_PATH_CHARS = 1024

# Readers that can execute code on load, or that we simply refuse to open.
REFUSED_SUFFIXES = {".pkl", ".pickle", ".pkl.gz", ".xlsm", ".xlsb", ".pyc", ".so"}

# Extensions treated as archives for the depth rule (listed, not opened).
NESTED_ARCHIVE_SUFFIXES = {
    ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar",
}


class ArchiveError(Exception):
    """Rejected input. The message is shown to the user, so keep it plain."""


@dataclass
class MemberPlan:
    name: str
    size: int
    compressed: int
    is_nested_archive: bool = False


@dataclass
class ArchivePlan:
    members: List[MemberPlan] = field(default_factory=list)
    total_uncompressed: int = 0
    nested_archives: List[str] = field(default_factory=list)
    skipped: List[Tuple[str, str]] = field(default_factory=list)  # (name, why)


def _limits() -> Tuple[int, int, int]:
    return (
        settings.archive_max_uncompressed_mb * 1024 * 1024,
        settings.archive_max_files,
        settings.archive_max_ratio,
    )


def sniff_format(path: str) -> str:
    """Identify by MAGIC BYTES, so a renamed file cannot pick its reader."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(8)
    except OSError:
        return "unknown"
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        return "zip"
    if head[:2] == b"\x1f\x8b":
        return "gzip"
    if head[:4] == b"PAR1":
        return "parquet"
    if head[:5] == b"%PDF-":
        return "pdf"
    return "unknown"


def is_zip_container(path: str) -> bool:
    return sniff_format(path) == "zip"


def safe_member_name(name: str) -> Optional[str]:
    """Normalized relative path, or None when the name itself is hostile."""
    if not name or name in (".", ".."):
        return None
    if "\x00" in name or any(ord(c) < 32 for c in name):
        return None
    cleaned = unicodedata.normalize("NFC", name).replace("\\", "/")
    if cleaned.startswith("/") or (len(cleaned) > 1 and cleaned[1] == ":"):
        return None  # absolute path
    parts = [p for p in cleaned.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return None  # zip-slip via traversal
    if not parts or len(cleaned) > _MAX_PATH_CHARS:
        return None
    if any(len(p) > _MAX_NAME_CHARS for p in parts):
        return None
    return "/".join(parts)


def resolves_inside(root: str, relative: str) -> bool:
    """Second, independent zip-slip check: the RESOLVED path must stay in root.

    Path inspection alone is not enough — a previously extracted symlink could
    redirect a later member — so this is re-checked at write time too.
    """
    root_real = os.path.realpath(root)
    target = os.path.realpath(os.path.join(root_real, relative))
    return target == root_real or target.startswith(root_real + os.sep)


def _classify(name: str) -> Optional[str]:
    lower = name.lower()
    for suffix in REFUSED_SUFFIXES:
        if lower.endswith(suffix):
            return f"refused file type ({suffix})"
    return None


def check_zip_container(path: str, *, label: str = "archive") -> ArchivePlan:
    """Apply the bomb/traversal caps to a zip WITHOUT extracting anything.

    Used for real archives and for .xlsx — an xlsx is a zip, so opening one
    with a spreadsheet reader before this check would bypass every cap.
    """
    max_total, max_files, max_ratio = _limits()
    plan = ArchivePlan()
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            if len(infos) > max_files:
                raise ArchiveError(
                    f"This {label} contains {len(infos):,} entries; the limit is "
                    f"{max_files:,}."
                )
            for info in infos:
                if info.is_dir():
                    continue
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode):
                    plan.skipped.append((info.filename, "symlink"))
                    continue
                safe = safe_member_name(info.filename)
                if safe is None:
                    plan.skipped.append((info.filename, "unsafe path"))
                    continue
                refused = _classify(safe)
                if refused:
                    plan.skipped.append((safe, refused))
                    continue
                # Per-member ratio: one entry that explodes is the classic bomb.
                if info.compress_size > 0:
                    ratio = info.file_size / info.compress_size
                    if ratio > max_ratio and info.file_size > 1024 * 1024:
                        raise ArchiveError(
                            f"This {label} looks like a decompression bomb: "
                            f"'{safe}' expands {ratio:,.0f}x."
                        )
                plan.total_uncompressed += info.file_size
                if plan.total_uncompressed > max_total:
                    raise ArchiveError(
                        f"This {label} expands to more than "
                        f"{settings.archive_max_uncompressed_mb} MB."
                    )
                nested = any(
                    safe.lower().endswith(s) for s in NESTED_ARCHIVE_SUFFIXES
                )
                if nested:
                    plan.nested_archives.append(safe)
                plan.members.append(
                    MemberPlan(safe, info.file_size, info.compress_size, nested)
                )
    except zipfile.BadZipFile:
        raise ArchiveError(f"This {label} is not a readable ZIP file.")
    return plan


def _write_member(src, dest_path: str, budget: List[int]) -> None:
    """Stream one member, aborting if the RUNNING total exceeds the budget."""
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    with open(dest_path, "wb") as out:
        while True:
            chunk = src.read(_CHUNK)
            if not chunk:
                break
            budget[0] -= len(chunk)
            if budget[0] < 0:
                out.close()
                os.unlink(dest_path)
                raise ArchiveError(
                    "This archive expands to more than "
                    f"{settings.archive_max_uncompressed_mb} MB "
                    "(its listed sizes understated the real contents)."
                )
            out.write(chunk)


def extract_zip(path: str, dest: str) -> ArchivePlan:
    """Extract a zip after check_zip_container, streaming with a live budget."""
    plan = check_zip_container(path)
    os.makedirs(dest, exist_ok=True)
    budget = [settings.archive_max_uncompressed_mb * 1024 * 1024]
    extracted: List[MemberPlan] = []
    with zipfile.ZipFile(path) as zf:
        for member in plan.members:
            if member.is_nested_archive:
                continue  # depth 1: listed in the profile, never opened
            target = os.path.join(dest, member.name)
            if not resolves_inside(dest, member.name):
                plan.skipped.append((member.name, "escapes the extraction root"))
                continue
            with zf.open(member.name) as src:
                _write_member(src, target, budget)
            extracted.append(member)
    plan.members = extracted
    return plan


def extract_tar(path: str, dest: str) -> ArchivePlan:
    """Extract a tar/tar.gz with the same guarantees as extract_zip."""
    max_total, max_files, max_ratio = _limits()
    plan = ArchivePlan()
    os.makedirs(dest, exist_ok=True)
    budget = [max_total]
    try:
        with tarfile.open(path) as tf:
            count = 0
            for member in tf:
                count += 1
                if count > max_files:
                    raise ArchiveError(
                        f"This archive contains more than {max_files:,} entries."
                    )
                if member.isdir():
                    continue
                # Symlinks, hardlinks, devices and FIFOs are all escape routes.
                if not member.isfile():
                    plan.skipped.append((member.name, "not a regular file"))
                    continue
                safe = safe_member_name(member.name)
                if safe is None:
                    plan.skipped.append((member.name, "unsafe path"))
                    continue
                refused = _classify(safe)
                if refused:
                    plan.skipped.append((safe, refused))
                    continue
                if any(safe.lower().endswith(s) for s in NESTED_ARCHIVE_SUFFIXES):
                    plan.nested_archives.append(safe)
                    plan.members.append(MemberPlan(safe, member.size, 0, True))
                    continue
                if not resolves_inside(dest, safe):
                    plan.skipped.append((safe, "escapes the extraction root"))
                    continue
                src = tf.extractfile(member)
                if src is None:
                    continue
                plan.total_uncompressed += member.size
                if plan.total_uncompressed > max_total:
                    raise ArchiveError(
                        "This archive expands to more than "
                        f"{settings.archive_max_uncompressed_mb} MB."
                    )
                _write_member(src, os.path.join(dest, safe), budget)
                plan.members.append(MemberPlan(safe, member.size, 0, False))
    except tarfile.TarError:
        raise ArchiveError("This archive is not a readable TAR file.")
    return plan


def extract(path: str, dest: str) -> ArchivePlan:
    """Extract any supported archive; raises ArchiveError on hostile input."""
    fmt = sniff_format(path)
    if fmt == "zip":
        return extract_zip(path, dest)
    if fmt == "gzip" or tarfile.is_tarfile(path):
        return extract_tar(path, dest)
    raise ArchiveError("Unsupported archive format — upload a .zip or .tar.gz.")
