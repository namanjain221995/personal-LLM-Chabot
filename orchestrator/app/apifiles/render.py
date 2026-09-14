"""PDF page renders for OCR and for vision context (design §4.4, §5.1).

TWO HALVES, ONE FILE.

* `render_in_child(spec)` runs inside `extract_worker` — PDFium never runs in
  the orchestrator for API files. It renders the requested pages ONE AT A
  TIME at `core/pdf.RENDER_SCALE` (2.0, ~144 DPI, the chat app's OCR scale)
  and writes each PNG atomically, so a child killed mid-batch leaves whole
  renders or none.
* `render_pages(...)` is the parent's call: it spawns the child for a batch
  and returns the paths that exist afterwards.

WHY BATCHES, NOT ONE CHILD PER PAGE. A child costs an interpreter start plus
PDFium's document open. Measured on this box 2026-09-13 with the 1,000-page
test fixture (978 KB): a child that opens the document and renders nothing
takes 52 ms; 16 pages rendered one child each took 2.16 s (135 ms/page),
in batches of 8 0.99 s (62 ms/page). Batches stay small
(`ocr_pages.RENDER_BATCH`) so the renders on disk at once are bounded: an A4
text page at scale 2.0 measured 252-265 KB as PNG; a photographic scan is
larger.

WHY RENDERS ARE TRANSIENT. `derived/renders/<n>.png` is deleted as soon as its
page's OCR result is recorded (§7.3 "renders/ transient"); the retention sweep
removes any left by a crash after 24 h. Vision renders for model input
(team MODEL INPUT, §5.1 `detail`) use `render_pages(..., out_dir=<tmp>)` and
are never stored at all.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Sequence

from .extractors import Spec, atomic_write_bytes

RENDERS_DIR = "renders"
#: `core/pdf.RENDER_SCALE`; imported in the parent (where `app.core.pdf` is
#: already loaded) and passed to the child in the spec.
DEFAULT_SCALE = 2.0
#: Defaults when a caller passes no ceilings (review finding, 2026-09-13:
#: `render_pages(caps=None, wall_s=None)` ran the child with no CPU limit and
#: no deadline, so one PDF page that sends PDFium into a loop held the caller
#: forever). Measured 62 ms/page in a batch of 8 and 52 ms to start a child;
#: 15 s per page with a 60 s floor is > 200× that and stops only a hostile
#: page. The CPU ceiling is the same number: rendering is single-threaded.
RENDER_WALL_PER_PAGE_S = 15.0
RENDER_WALL_FLOOR_S = 60.0
#: A render at scale 2.0 of a normal page is ~1,190 × 1,684 px. A PDF can
#: declare a 200-inch page; this caps the long edge so one hostile page cannot
#: ask PDFium for a multi-gigabyte bitmap (RLIMIT_AS would stop it, but a
#: clean skip is better than a killed batch).
MAX_RENDER_EDGE_PX = 4096


def render_path(derived_dir: str, page: int) -> str:
    return os.path.join(derived_dir, RENDERS_DIR, f"{int(page)}.png")


def default_wall_s(pages: int) -> float:
    """The wall deadline for rendering `pages` pages in one child, never
    above the PDF kind's own wall ceiling."""
    from . import limits

    wall = max(RENDER_WALL_FLOOR_S, RENDER_WALL_PER_PAGE_S * max(1, int(pages)))
    kind_wall = limits.kind_caps("pdf").wall_s
    return float(min(wall, kind_wall) if kind_wall else wall)


def default_caps(source: str, pages: int) -> Dict[str, Any]:
    """The ceilings a render child gets when its caller names none: the PDF
    kind's byte and page ceilings, the extraction address-space ceiling, a
    CPU ceiling equal to the wall deadline, and the file-size ceiling."""
    from . import extract_worker, limits

    kind = limits.kind_caps("pdf")
    try:
        size = os.path.getsize(source)
    except OSError:
        size = 0
    return {
        "bytes": kind.bytes,
        "pages": kind.pages,
        "cpu_s": default_wall_s(pages),
        "rlimit_as_bytes": limits.extract_rlimit_as_bytes(),
        "fsize_bytes": extract_worker.fsize_limit(size),
    }


# ------------------------------------------------------------- the child --


def render_in_child(spec: Spec) -> Dict[str, Any]:
    """Render `args.pages` (1-based) of `spec.source` into `args.out_dir`."""
    pages = [int(p) for p in (spec.args.get("pages") or [])]
    out_dir = str(spec.args.get("out_dir") or os.path.join(spec.derived_dir, RENDERS_DIR))
    scale = float(spec.args.get("scale") or DEFAULT_SCALE)
    try:
        # mkdir, not makedirs: the parent directory must already exist, so a
        # render racing a DELETE's rmtree cannot resurrect the blob directory.
        os.mkdir(out_dir, 0o750)
    except FileExistsError:
        pass
    from .extractors import pdf as pdf_extractor

    pdf = pdf_extractor.open_document(spec.source, spec)  # byte ceiling first, then FileCorrupt
    rendered: List[int] = []
    skipped: List[int] = []
    try:
        total = len(pdf)
        for number in pages:
            if number < 1 or number > total:
                skipped.append(number)
                continue
            try:
                page = pdf[number - 1]
                width, height = page.get_size()
                edge = max(width, height) * scale
                page_scale = scale if edge <= MAX_RENDER_EDGE_PX else scale * MAX_RENDER_EDGE_PX / edge
                bitmap = page.render(scale=page_scale)
                image = bitmap.to_pil().convert("RGB")
                page.close()
            except Exception:  # noqa: BLE001 — one bad page does not sink the batch
                skipped.append(number)
                continue
            import io

            buf = io.BytesIO()
            image.save(buf, format="PNG")
            atomic_write_bytes(os.path.join(out_dir, f"{number}.png"), buf.getvalue())
            rendered.append(number)
    finally:
        pdf.close()
    return {"rendered": rendered, "skipped": skipped}


# ------------------------------------------------------------ the parent --


async def render_pages(
    derived_dir: str,
    source: str,
    pages: Sequence[int],
    *,
    out_dir: Optional[str] = None,
    scale: Optional[float] = None,
    caps: Optional[Dict[str, Any]] = None,
    wall_s: Optional[float] = None,
) -> Dict[int, str]:
    """Render `pages` in a child; {page: png path} for every page rendered.

    Raises `extractors.ExtractError` when the child reports the document
    unreadable (or was killed by a ceiling). `caps` are merged OVER
    `default_caps` and `wall_s` None means `default_wall_s`: no caller gets
    a render without a CPU ceiling and a deadline."""
    from . import extract_worker

    if not pages:
        return {}
    merged = default_caps(source, len(pages))
    merged.update({k: v for k, v in (caps or {}).items() if v is not None})
    if wall_s is None or wall_s <= 0:
        wall_s = default_wall_s(len(pages))
    if scale is None:
        try:
            from ..core.pdf import RENDER_SCALE

            scale = float(RENDER_SCALE)
        except Exception:  # noqa: BLE001
            scale = DEFAULT_SCALE
    target = out_dir or os.path.join(derived_dir, RENDERS_DIR)
    if out_dir:
        # The sandbox grants the child write access to an EXISTING out_dir
        # only; mkdir (not makedirs), so a vanished parent is not recreated.
        try:
            os.mkdir(out_dir, 0o750)
        except FileExistsError:
            pass
    result = await extract_worker.run(
        Spec(
            op="render",
            kind="pdf",
            derived_dir=derived_dir,
            source=source,
            caps=merged,
            args={"pages": [int(p) for p in pages], "out_dir": target, "scale": scale},
        ),
        wall_s=wall_s,
    )
    out: Dict[int, str] = {}
    for number in result.get("rendered") or []:
        path = os.path.join(target, f"{int(number)}.png")
        if os.path.exists(path):
            out[int(number)] = path
    return out
