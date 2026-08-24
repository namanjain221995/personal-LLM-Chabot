"""Markdown → PDF in REPORTS_DIR, using the toolchain reports already use.

The report engine (§8) renders with `pandoc --pdf-engine=weasyprint`: no
LaTeX, no headless browser, and both binaries are already in the orchestrator
image. This module is that same invocation, factored out so a caller which is
not the Salesforce report engine can produce a real PDF without duplicating
the subprocess handling — or growing a second PDF dependency.

Pure-ish: stdlib only, no app imports. The caller supplies the directory.
"""
from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path

from .exports import slugify


class ReportRenderError(RuntimeError):
    """Raised when pandoc could not produce the requested file."""


async def _run_pandoc(md_path: Path, out_path: Path, resource_dir: Path) -> None:
    cmd = [
        "pandoc",
        str(md_path),
        "--standalone",
        "--resource-path",
        str(resource_dir),
        "-o",
        str(out_path),
    ]
    if out_path.suffix.lower() == ".pdf":
        cmd.append("--pdf-engine=weasyprint")  # PDF without LaTeX
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except FileNotFoundError as exc:  # pandoc missing entirely
        raise ReportRenderError("pandoc is not installed") from exc
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise ReportRenderError(
            f"pandoc failed for {out_path.name}: "
            f"{stderr.decode(errors='replace')[:500]}"
        )


def timestamped_base(title: str, fallback: str = "report") -> str:
    """`<slug>-<YYYYmmdd-HHMMSS>` — the naming reports already use."""
    return f"{slugify(title, fallback=fallback)}-{time.strftime('%Y%m%d-%H%M%S')}"


async def render_markdown_pdf(
    markdown: str,
    reports_dir: str | Path,
    *,
    title: str,
    base_name: str | None = None,
) -> Path:
    """Write `markdown` out as a real PDF in `reports_dir`; return its path.

    Raises ReportRenderError if pandoc did not produce a non-empty file. The
    caller must NOT fall back to writing the markdown under a .pdf name — a
    file that is not a PDF is worse than an honest failure.
    """
    base = base_name or timestamped_base(title)
    directory = Path(reports_dir)
    directory.mkdir(parents=True, exist_ok=True)
    out_path = directory / f"{base}.pdf"

    with tempfile.TemporaryDirectory(prefix="dataset-report-") as tmp:
        tmp_dir = Path(tmp)
        md_path = tmp_dir / f"{base}.md"
        md_path.write_text(markdown, encoding="utf-8")
        await _run_pandoc(md_path, out_path, tmp_dir)

    if not out_path.is_file() or out_path.stat().st_size == 0:
        raise ReportRenderError("pandoc produced no output")
    return out_path


__all__ = [
    "ReportRenderError",
    "render_markdown_pdf",
    "timestamped_base",
]
