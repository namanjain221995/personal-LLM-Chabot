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
import re
import tempfile
import time
from pathlib import Path

from .exports import slugify


# ---------------------------------------------------------------------------
# Nothing the renderer can fetch
#
# WeasyPrint resolves every reference in the HTML pandoc hands it, and it does
# so with its own HTTP stack — not through core.net.safe_fetch, which guards
# every other outbound request in this application. PROVEN on 2026-09-04: a
# report containing three references to a loopback address produced three GET
# requests from inside the container:
#
#     ![probe](http://127.0.0.1:PORT/INTERNAL-SSRF)   -> GET /INTERNAL-SSRF
#     ![probe2](http://127.0.0.1:PORT/second)         -> GET /second
#     <img src="http://127.0.0.1:PORT/rawhtml">       -> GET /rawhtml
#
# `file:///etc/passwd` and `http://169.254.169.254/latest/meta-data/` are the
# same code path. It matters because report markdown is not ours alone: a Deep
# Research report is written from web pages, and a page that gets a marker
# into the text gets a fetch out of the exporter.
#
# The ONE legitimate case is a chart this application just wrote next to the
# markdown — `report.py` emits `![title](chart-abc.png)`, a bare filename
# resolved through pandoc's --resource-path. So the rule is simply: a
# reference may be a plain local filename and nothing else.
# ---------------------------------------------------------------------------

#: `![alt](target)` — the target is what the renderer would fetch.
_MD_IMAGE = re.compile(r"!\[([^\]]*)\]\(\s*([^)\s]+)[^)]*\)")

#: Raw HTML that pulls a resource. Pandoc passes HTML through untouched.
#: Raw HTML that pulls a resource. Pandoc passes HTML through untouched.
#:
#: The tag-name boundary is a LOOKAHEAD, not \b. Written as \b through a
#: shell heredoc it became a literal backspace (0x08) in the pattern, so the
#: expression required a control character after the tag name and matched
#: nothing at all — the guard was there, imported, called, and silently
#: inert, which a passing test on markdown images would never have caught.
_HTML_FETCHERS = re.compile(
    r"<\s*/?\s*(?:img|link|style|script|iframe|object|embed|video|audio|source)"
    r"(?=[\s/>])[^>]*>",
    re.IGNORECASE,
)

#: `<style>` and `<script>` must go with their CONTENTS, not just their tags:
#: the fetch lives in the body — `@import url("http://…")`, or a script that
#: loads anything it likes — so removing the wrapper alone leaves the URL in
#: the document for the renderer to resolve.
_HTML_ELEMENTS_WITH_BODY = re.compile(
    r"<\s*(style|script)\b[^>]*>.*?<\s*/\s*\1\s*>",
    re.IGNORECASE | re.DOTALL,
)

def _is_local_asset(target: str) -> bool:
    """A bare filename beside the markdown, and nothing more.

    Rejects a scheme (`http:`, `file:`, `data:`), a protocol-relative `//`,
    an absolute path, any directory component, and traversal. Those are the
    only shapes that can leave the resource directory.
    """
    t = target.strip().strip("'\"")
    if not t or ":" in t or t.startswith(("/", "//", "#", "?")):
        return False
    if "/" in t or "\\" in t or ".." in t:
        return False
    return True


def sanitise_for_render(markdown: str) -> str:
    """Remove every reference the PDF renderer could fetch.

    Links are LEFT ALONE: `[text](https://example.com)` is not fetched by
    WeasyPrint, and in a research report those links are the citations — the
    whole point of the document. Only things that are *loaded* are removed.
    """

    def _image(m: re.Match) -> str:
        alt, target = m.group(1), m.group(2)
        if _is_local_asset(target):
            return m.group(0)
        # Keep the alt text so the reader sees that something was referenced,
        # and keep it as text so nothing resolves it.
        return f"[image omitted: {alt}]" if alt else "[image omitted]"

    without_bodies = _HTML_ELEMENTS_WITH_BODY.sub("", markdown)
    return _HTML_FETCHERS.sub("", _MD_IMAGE.sub(_image, without_bodies))


class ReportRenderError(RuntimeError):
    """Raised when pandoc could not produce the requested file."""


async def _run_pandoc(md_path: Path, out_path: Path, resource_dir: Path) -> None:
    # Sanitise HERE rather than at the call sites: this function is the one
    # thing every PDF passes through, and a guard a caller can forget is not a
    # guard.
    try:
        original = md_path.read_text(encoding="utf-8")
        cleaned = sanitise_for_render(original)
        if cleaned != original:
            md_path.write_text(cleaned, encoding="utf-8")
    except OSError:
        pass  # unreadable markdown fails in pandoc below, with its own message
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
