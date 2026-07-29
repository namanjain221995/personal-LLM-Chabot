"""REPORTS_DIR listing and SAFE filename resolution (spec §8).

resolve_report_file rejects:
  * empty / dot / hidden names
  * anything containing a path separator (no nested paths)
  * absolute paths
  * `..` traversal
  * symlink escape (the fully resolved path must stay inside REPORTS_DIR)

Pure module: stdlib only.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import List


class ReportPathError(ValueError):
    """Raised for unsafe or malformed report filenames."""


def resolve_report_file(reports_dir: str | Path, filename: str) -> Path:
    """Resolve `filename` inside `reports_dir` or raise ReportPathError.

    Existence is NOT checked here — callers decide between 404 and 400.
    """
    if not filename or not filename.strip():
        raise ReportPathError("empty filename")
    name = filename.strip()
    if name in {".", ".."} or ".." in name:
        raise ReportPathError("path traversal is not allowed")
    if "/" in name or "\\" in name:
        raise ReportPathError("nested or absolute paths are not allowed")
    if Path(name).is_absolute():
        raise ReportPathError("absolute paths are not allowed")
    if name.startswith("."):
        raise ReportPathError("hidden files are not allowed")
    if "\x00" in name:
        raise ReportPathError("invalid filename")

    base = Path(reports_dir).resolve()
    resolved = (base / name).resolve()
    # resolve() follows symlinks: a symlink pointing outside REPORTS_DIR lands
    # outside `base` and is rejected here.
    if not resolved.is_relative_to(base):
        raise ReportPathError("path escapes the reports directory")
    return resolved


def list_reports(reports_dir: str | Path) -> List[dict]:
    """List regular files in REPORTS_DIR, newest first. Missing dir → []."""
    base = Path(reports_dir)
    if not base.is_dir():
        return []
    items: List[dict] = []
    for p in sorted(base.iterdir()):
        if not p.is_file() or p.name.startswith("."):
            continue
        st = p.stat()
        items.append(
            {
                "filename": p.name,
                "size_bytes": st.st_size,
                "modified": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
            }
        )
    items.sort(key=lambda d: d["modified"], reverse=True)
    return items
