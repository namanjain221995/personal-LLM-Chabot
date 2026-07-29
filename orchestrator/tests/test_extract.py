"""Readable-text extraction tests (Phase 1). No network; trafilatura optional
(the tag-stripping fallback is always exercised)."""
import pytest

from app.core import extract

HTML = (
    "<html><head><title>  Pricing &amp; Plans </title></head>"
    "<body><nav>menu junk</nav>"
    "<main><h1>Our Pricing</h1><p>The Pro plan is $49 per month.</p></main>"
    "<script>tracker()</script></body></html>"
).encode()


def test_html_title_and_text():
    out = extract.extract_readable("text/html; charset=utf-8", HTML, "https://x.com/p")
    assert "Pricing" in out.title
    assert "$49" in out.text
    assert "tracker" not in out.text  # script dropped


def test_plain_text_passthrough():
    out = extract.extract_readable("text/plain", b"just some text", "https://x.com/f")
    assert out.text == "just some text"
    assert out.title == "x.com"


def test_unsupported_type_raises():
    with pytest.raises(extract.UnsupportedContentError):
        extract.extract_readable("image/png", b"\x89PNG", "https://x.com/i.png")


def test_empty_content_type_defaults_to_html():
    out = extract.extract_readable("", HTML, "https://x.com/p")
    assert "$49" in out.text


def test_truncate_on_boundary():
    text = "word " * 100  # 500 chars
    out = extract.truncate_chars(text, 40)
    assert out.endswith("…")
    assert len(out) <= 44
    assert extract.truncate_chars("short", 40) == "short"


def test_pdf_url_dispatches_to_pdf(monkeypatch):
    called = {}

    def fake_render(b64, max_pages=10):
        called["hit"] = True
        return [], "PDF TEXT HERE", 1

    monkeypatch.setitem(__import__("sys").modules, "app.core.pdf", type("m", (), {"render_pdf": staticmethod(fake_render)}))
    # patch the lazily-imported name
    import app.core.pdf as pdfmod

    monkeypatch.setattr(pdfmod, "render_pdf", fake_render, raising=False)
    out = extract.extract_readable("application/pdf", b"%PDF-1.4", "https://x.com/doc")
    assert out.text == "PDF TEXT HERE"
