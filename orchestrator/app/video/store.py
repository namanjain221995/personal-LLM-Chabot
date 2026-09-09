"""Where an analysis lives on disk, and the file-level primitives.

ONE DIRECTORY PER VIDEO, KEYED BY THE HASH OF ITS BYTES:

    <VIDEO_DATA_DIR>/<sha256>/
        source.<ext>        the upload (hard-linked when the volume allows;
                            copied otherwise) — the workspace copy is swept
                            after 24 h and a resumable job must outlive that
        probe.json          what ffprobe found
        audio.wav           16 kHz mono PCM, once
        transcript.json     Whisper segments in video time
        frames/f_*.jpg      the frames worth reading (deduped set only)
        frames.json         which frames, with spans
        screen.json         OCR text + captions per kept frame
        understanding.json  the fused understanding
        artifacts/          transcript.txt/.srt/.vtt/.json, screen_text.*,
                            summary.md

Every stage writes its output file atomically (tmp + rename) and the row in
PostgreSQL records the stage as done only after the file is durable, so
"done in the database" always implies "readable on disk". The reverse can
happen (file present, row not updated, after a crash between the two) and
costs a re-run of that one stage, which is the cheap direction.

This directory is NOT under WORKSPACE_DIR on purpose: `core/repo
.enforce_quota_and_ttl` deletes whole top-level workspace directories by
age, and a pipeline that can take an hour must not lose its inputs to a
sweep triggered by somebody else's upload.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from typing import Any, Optional

from ..config import settings

_HASH_CHUNK = 4 * 1024 * 1024


def hash_file(path: str) -> str:
    """sha256 of the bytes, streamed — a 4 GB file never sits in memory."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_HASH_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def analysis_dir(content_hash: str) -> str:
    if not content_hash or len(content_hash) != 64 or not all(c in "0123456789abcdef" for c in content_hash):
        raise ValueError("content hash must be 64 hex characters")
    return os.path.join(settings.video_data_dir, content_hash)


def stage_path(content_hash: str, name: str) -> str:
    return os.path.join(analysis_dir(content_hash), name)


def frames_dir(content_hash: str) -> str:
    return os.path.join(analysis_dir(content_hash), "frames")


def artifacts_dir(content_hash: str) -> str:
    return os.path.join(analysis_dir(content_hash), "artifacts")


def source_path(content_hash: str) -> Optional[str]:
    """The stored source file, whatever its extension, or None."""
    root = analysis_dir(content_hash)
    try:
        for entry in os.scandir(root):
            if entry.is_file() and entry.name.startswith("source."):
                return entry.path
    except OSError:
        return None
    return None


def adopt_source(content_hash: str, path: str, filename: str) -> str:
    """Put the uploaded bytes under the analysis directory.

    A hard link when both sit on the same filesystem (the `data` volume holds
    both the workspace and this directory, so that is the normal case) —
    free, instant, and the workspace sweep removing its link leaves ours.
    A copy otherwise. Idempotent: an existing source is left alone, which is
    what "the same video uploaded twice is analysed once" rests on.
    """
    existing = source_path(content_hash)
    if existing:
        return existing
    root = analysis_dir(content_hash)
    os.makedirs(root, exist_ok=True)
    ext = os.path.splitext(filename or "")[1].lower()
    if not ext or len(ext) > 8 or not ext[1:].isalnum():
        ext = ".bin"
    dest = os.path.join(root, "source" + ext)
    tmp = dest + ".part"
    try:
        os.link(path, tmp)
    except OSError:
        shutil.copyfile(path, tmp)
    os.replace(tmp, dest)
    return dest


def read_json(path: str) -> Optional[Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def write_json(path: str, payload: Any) -> None:
    """Replace `path` with `payload`, or leave whatever was there intact."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".stage-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def remove_analysis(content_hash: str) -> None:
    shutil.rmtree(analysis_dir(content_hash), ignore_errors=True)


def dir_bytes(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total
