"""PDF → images + text (V8, 2026-07-23).

The multimodal main model can't read a PDF binary, so we render its pages to
PNG images (preserving tables/stamps/layout) AND pull the text layer when the
PDF has one. Both are fed to the model. Rendering uses pypdfium2, which ships a
self-contained arm64 wheel (no system libraries needed).

Pure/offline: imports pypdfium2 lazily so the app and tests load without it.
"""
from __future__ import annotations

import base64
import io
from typing import List, Tuple

# A handful of pages keeps the vision-token budget (and memory) sane; more than
# this is reported as truncated to the user.
MAX_PDF_PAGES = 6
RENDER_SCALE = 2.0  # ~144 DPI — legible for OCR without exploding token count
MAX_TEXT_CHARS = 24000


def _strip_data_url(b64: str) -> str:
    return b64.split(",", 1)[-1] if b64.startswith("data:") else b64


def render_pdf(
    pdf_base64: str, max_pages: int = MAX_PDF_PAGES
) -> Tuple[List[str], str, int]:
    """Render a base64 PDF to (page image data URLs, extracted text, total pages).

    Only the first ``max_pages`` pages are rendered; ``total`` is the real page
    count so the caller can tell the user when it was truncated.
    """
    import pypdfium2 as pdfium  # lazy: arm64 wheel, no system deps

    pdf_bytes = base64.b64decode(_strip_data_url(pdf_base64))
    pdf = pdfium.PdfDocument(pdf_bytes)
    try:
        total = len(pdf)
        n = min(total, max_pages)
        images: List[str] = []
        texts: List[str] = []
        for i in range(n):
            page = pdf[i]
            textpage = page.get_textpage()
            texts.append(textpage.get_text_range() or "")
            textpage.close()

            bitmap = page.render(scale=RENDER_SCALE)
            pil = bitmap.to_pil().convert("RGB")
            buf = io.BytesIO()
            pil.save(buf, format="PNG")
            images.append(
                "data:image/png;base64,"
                + base64.b64encode(buf.getvalue()).decode()
            )
            page.close()

        text = "\n\n".join(
            f"--- Page {i + 1} ---\n{t.strip()}"
            for i, t in enumerate(texts)
            if t.strip()
        )[:MAX_TEXT_CHARS]
        return images, text, total
    finally:
        pdf.close()
