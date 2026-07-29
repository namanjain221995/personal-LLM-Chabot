"""URL detection + relevance chunking (Phase 2)."""
from app.core import urls


def test_extract_urls_finds_and_dedupes():
    text = "See https://a.com/x and http://b.org/y, plus (https://a.com/x) again."
    assert urls.extract_urls(text) == ["https://a.com/x", "http://b.org/y"]


def test_extract_urls_strips_trailing_punctuation():
    assert urls.extract_urls("check https://ex.com/page).") == ["https://ex.com/page"]


def test_extract_urls_none():
    assert urls.extract_urls("no links here") == []
    assert urls.extract_urls("ftp://x.com not http") == []


def test_extract_urls_limit():
    text = " ".join(f"https://s{i}.com" for i in range(10))
    assert len(urls.extract_urls(text, limit=3)) == 3


def test_chunk_text_overlap():
    text = "word " * 1000  # 5000 chars
    chunks = urls.chunk_text(text, chunk_chars=800, overlap=100)
    assert len(chunks) > 1
    assert all(len(c) <= 820 for c in chunks)


def test_chunk_small_text_single():
    assert urls.chunk_text("short") == ["short"]
    assert urls.chunk_text("") == []


def test_select_relevant_keeps_pertinent_chunks():
    filler = "the cat sat on the mat. " * 200
    pricing = " The Pro plan costs $49 per month with unlimited seats. "
    text = filler + pricing + filler
    out = urls.select_relevant(text, "what is the pricing per month?", 400)
    assert "$49" in out
    assert len(out) <= 420


def test_select_relevant_small_text_passthrough():
    assert urls.select_relevant("tiny", "q", 100) == "tiny"
