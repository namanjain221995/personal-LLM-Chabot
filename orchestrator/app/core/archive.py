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
import struct
import tarfile
import unicodedata
import zipfile
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from ..config import settings

_CHUNK = 64 * 1024
_MAX_NAME_CHARS = 200
_MAX_PATH_CHARS = 1024

#: Bytes of a zip's central directory parsed at most (2026-10-03). zipfile
#: reads the WHOLE directory and builds a ZipInfo for every entry (~500 B of
#: heap each): with no upload limit, listing a 2 GB zip of small files was
#: ~10 GB of orchestrator memory for the ARCHIVE_MAX_FILES entries ever used.
_MAX_LISTED_BYTES = 16 * 1024 * 1024
#: One central-directory file header (APPNOTE 4.3.12): 46 bytes, then the
#: name, the extra field and the comment, whose lengths are fields 12-14.
_CENTRAL = struct.Struct("<4s4B4HL2L5H2L")

# Readers that can execute code on load, or that we simply refuse to open.
REFUSED_SUFFIXES = {".pkl", ".pickle", ".pkl.gz", ".xlsm", ".xlsb", ".pyc", ".so"}

# Extensions treated as archives for the depth rule (listed, not opened).
NESTED_ARCHIVE_SUFFIXES = {
    ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar",
}


class ArchiveError(Exception):
    """Rejected input. The message is shown to the user, so keep it plain."""


class ArchiveTooLarge(ArchiveError):
    """Past a READING cap (entries, bytes, one member's expansion), not
    hostile in its structure: the file itself may be kept whole, only what
    is unpacked from it is bounded. `reason` says which, for a note."""

    def __init__(self, message: str, reason: str):
        super().__init__(message)
        self.reason = reason


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


def _unpacked_part(left: int, max_total: int, max_files: int, by_count: bool) -> Tuple[str, str]:
    """The `skipped` line for what a partial extraction left packed: the
    entries past the count cap (one line), or one member past the byte cap."""
    why = (
        f"not unpacked: only the first {max_files:,} entries are"
        if by_count
        else f"not unpacked: only {max_total // (1024 * 1024):,} MB of an archive are"
    )
    return (f"{left:,} more file(s)", why + " (the archive itself is kept whole)")


def _past_the_bytes(name: str, max_total: int) -> Tuple[str, str]:
    return (name, _unpacked_part(0, max_total, 0, False)[1])


class _FirstEntries:
    """A read-only view of a zip that ends after its first central-directory
    records: the file's first `cut` bytes, then `tail`, new end records that
    name only those. zipfile parses just them; members are read from the
    real file at their real offsets."""

    def __init__(self, fh, cut: int, tail: bytes):
        self._fh, self._cut, self._tail, self._pos = fh, cut, tail, 0

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = 0) -> int:
        base = (0, self._pos, self._cut + len(self._tail))[whence]
        if base + offset < 0:
            raise OSError("seek before the start of the archive")
        self._pos = base + offset
        return self._pos

    def read(self, n: int = -1) -> bytes:
        left = max(0, self._cut + len(self._tail) - self._pos)
        n = left if n is None or n < 0 else min(n, left)
        out = b""
        if n and self._pos < self._cut:
            self._fh.seek(self._pos)
            out = self._fh.read(min(n, self._cut - self._pos))
            self._pos += len(out)
            n -= len(out)
        if n and self._pos >= self._cut:
            start = self._pos - self._cut
            chunk = self._tail[start:start + n]
            out += chunk
            self._pos += len(chunk)
        return out

    def close(self) -> None:
        pass


def open_zip(fh, limit: int) -> zipfile.ZipFile:
    """The zip in the open file `fh` (which must outlive it), opened whole
    when its central directory holds at most `limit` entries in at most
    _MAX_LISTED_BYTES, else as its first entries within both
    (`_FirstEntries`): nothing past them is parsed. It carries
    `listed_whole` (every entry was parsed) and `listed_total` (the entries
    the archive holds, at least one more than parsed when not whole)."""
    end = zipfile._EndRecData(fh)  # the stdlib's own end-record reader
    if not end:
        raise zipfile.BadZipFile("File is not a zip file")
    entries = end[zipfile._ECD_ENTRIES_TOTAL]
    size = end[zipfile._ECD_SIZE]
    recorded = end[zipfile._ECD_OFFSET]
    location = end[zipfile._ECD_LOCATION]
    if end[zipfile._ECD_SIGNATURE] == zipfile.stringEndArchive64:
        # Since the 2025 zip64 hardening LOCATION is the zip64 record's
        # place; before it, the classic record's, 76 bytes further on.
        fh.seek(location)
        if fh.read(4) != zipfile.stringEndArchive64:
            location -= zipfile.sizeEndCentDir64 + zipfile.sizeEndCentDir64Locator
    start = location - size  # where the directory really is
    if start < 0:
        raise zipfile.BadZipFile("Bad offset for central directory")
    fh.seek(start)
    count = used = 0
    while count < limit and used < size:
        head = fh.read(_CENTRAL.size)
        if len(head) != _CENTRAL.size or head[:4] != zipfile.stringCentralDir:
            raise zipfile.BadZipFile("Bad magic number for central directory")
        record = _CENTRAL.size + sum(_CENTRAL.unpack(head)[12:15])
        if used + record > _MAX_LISTED_BYTES:
            break
        fh.seek(record - _CENTRAL.size, 1)
        count += 1
        used += record
    fh.seek(0)
    if used >= size:
        zf = zipfile.ZipFile(fh)
        zf.listed_whole, zf.listed_total = True, len(zf.filelist)
        return zf
    # New end records naming the first `count` entries, always zip64 (valid
    # whatever the offsets), consistent for every zipfile version's checks.
    concat = start - recorded  # bytes before the archive proper (an sfx stub)
    tail = (
        struct.pack(
            zipfile.structEndArchive64, zipfile.stringEndArchive64,
            zipfile.sizeEndCentDir64 - 12, 45, 45, 0, 0, count, count, used, recorded,
        )
        + struct.pack(
            zipfile.structEndArchive64Locator, zipfile.stringEndArchive64Locator,
            0, start + used - concat, 1,
        )
        + struct.pack(
            zipfile.structEndArchive, zipfile.stringEndArchive,
            0, 0, 0xFFFF, 0xFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0,
        )
    )
    zf = zipfile.ZipFile(_FirstEntries(fh, start + used, tail))
    zf.listed_whole, zf.listed_total = False, max(entries, count + 1)
    return zf


def check_zip_container(path: str, *, label: str = "archive", partial: bool = False) -> ArchivePlan:
    """Apply the bomb/traversal caps to a zip WITHOUT extracting anything.

    Used for real archives and for .xlsx — an xlsx is a zip, so opening one
    with a spreadsheet reader before this check would bypass every cap.

    `partial` (an uploaded ARCHIVE, never an .xlsx): an archive with more
    entries or more bytes than the caps is not refused (no upload limit since
    2026-10-03, docs/chat-media/LIMITS.md): the first ones within the caps
    are planned and the rest listed in `skipped`. The caps still bound what
    is unpacked, and a bomb-shaped member or a lying header is still refused.
    Only the planned entries are ever parsed (`open_zip`).

    A cap (entries, bytes, a member's expansion) raises `ArchiveTooLarge`;
    a structure that is hostile or unreadable raises `ArchiveError`.
    """
    max_total, max_files, max_ratio = _limits()
    plan = ArchivePlan()
    try:
        with open(path, "rb") as fh, open_zip(fh, max_files) as zf:
            infos = zf.infolist()
            entries = zf.listed_total
            if not zf.listed_whole and not partial:
                # Its reader opens every entry, so every entry must have been
                # checked: one this large is not opened at all.
                over = (
                    f"it has {entries:,} parts, more than the {max_files:,} that are opened"
                    if entries > max_files
                    else f"its list of contents is over {_MAX_LISTED_BYTES // (1024 * 1024)} MB"
                )
                raise ArchiveTooLarge(
                    f"This {label} contains {entries:,} entries; the limit is {max_files:,}."
                    if entries > max_files
                    else f"This {label} is too large to check: {over}.",
                    over,
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
                        raise ArchiveTooLarge(
                            f"This {label} looks like a decompression bomb: "
                            f"'{safe}' expands {ratio:,.0f}x.",
                            f"'{safe}' expands {ratio:,.0f}x when unpacked",
                        )
                if partial and plan.total_uncompressed + info.file_size > max_total:
                    # Past the byte cap: this member stays packed, and a
                    # smaller one after it may still fit.
                    plan.skipped.append(_past_the_bytes(safe, max_total))
                    continue
                plan.total_uncompressed += info.file_size
                if plan.total_uncompressed > max_total:
                    raise ArchiveTooLarge(
                        f"This {label} expands to more than "
                        f"{settings.archive_max_uncompressed_mb} MB.",
                        f"it expands to more than {settings.archive_max_uncompressed_mb:,} MB",
                    )
                nested = any(
                    safe.lower().endswith(s) for s in NESTED_ARCHIVE_SUFFIXES
                )
                if nested:
                    plan.nested_archives.append(safe)
                plan.members.append(
                    MemberPlan(safe, info.file_size, info.compress_size, nested)
                )
            if not zf.listed_whole:
                plan.skipped.append(_unpacked_part(entries - len(infos), max_total, len(infos), True))
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


def extract_zip(path: str, dest: str, partial: bool = False) -> ArchivePlan:
    """Extract a zip after check_zip_container, streaming with a live budget."""
    plan = check_zip_container(path, partial=True) if partial else check_zip_container(path)
    os.makedirs(dest, exist_ok=True)
    budget = [settings.archive_max_uncompressed_mb * 1024 * 1024]
    extracted: List[MemberPlan] = []
    with open(path, "rb") as fh, open_zip(fh, settings.archive_max_files) as zf:
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


def extract_tar(path: str, dest: str, partial: bool = False) -> ArchivePlan:
    """Extract a tar/tar.gz with the same guarantees as extract_zip
    (`partial` as in `check_zip_container`)."""
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
                    if partial:
                        # A tar has no directory to count ahead: say "more".
                        plan.skipped.append(("more files", _unpacked_part(0, max_total, max_files, True)[1]))
                        break
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
                if partial and plan.total_uncompressed + member.size > max_total:
                    plan.skipped.append(_past_the_bytes(safe, max_total))
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
    """Extract any supported archive; raises ArchiveError on hostile input.

    An archive larger than the caps is unpacked up to them, never refused
    (`partial`): what was left packed is in `plan.skipped`."""
    fmt = sniff_format(path)
    if fmt == "zip":
        return extract_zip(path, dest, partial=True)
    if fmt == "gzip" or tarfile.is_tarfile(path):
        return extract_tar(path, dest, partial=True)
    raise ArchiveError("Unsupported archive format — upload a .zip or .tar.gz.")
