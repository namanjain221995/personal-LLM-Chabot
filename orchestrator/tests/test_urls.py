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


# ── A paste is not a list of links ───────────────────────────────────────────
# Owner report 2026-08-11: a 30,599-character paste (831 lines of content to
# analyse) happened to contain URLs. They were extracted, the request was routed
# to the URL engine, every fetch failed, and the whole answer was "I couldn't
# read any of those links." The paste was never looked at.

from app.core.urls import links_are_the_request


def test_a_bare_link_is_a_request_to_read_it():
    url = "https://example.com/pricing"
    assert links_are_the_request(url, [url]) is True


def test_a_link_with_a_short_instruction_is_still_a_request():
    text = "Please read these and compare them against our pricing:\nhttps://a.com https://b.com"
    assert links_are_the_request(text, ["https://a.com", "https://b.com"]) is True


def test_a_large_paste_containing_a_url_is_CONTENT_not_a_request():
    """The exact shape that broke: hundreds of lines, a URL somewhere inside."""
    paste = "\n".join(f"2026-08-11 12:0{i%10} service log line {i}" for i in range(831))
    text = f"{paste}\nsee https://internal.example.com/runbook for context"
    assert links_are_the_request(text, ["https://internal.example.com/runbook"]) is False


def test_a_long_prose_message_mentioning_a_link_is_not_a_request():
    text = ("Here is the full incident writeup. " * 40) + " https://example.com/x"
    assert links_are_the_request(text, ["https://example.com/x"]) is False


def test_many_lines_alone_are_enough_to_disqualify():
    text = "\n".join(["short"] * 40) + "\nhttps://example.com"
    assert links_are_the_request(text, ["https://example.com"]) is False


def test_no_urls_is_never_a_link_request():
    assert links_are_the_request("just a question", []) is False


def test_the_url_route_is_gated_on_it():
    """The gate has to be WIRED, not merely available."""
    import inspect

    from app import main

    source = inspect.getsource(main)
    assert "links_are_the_request(request.text, url_list)" in source
    # …and the repo route too: a GitHub link inside a paste must not clone.
    assert "github_ref is not None and not links_are_the_request" in source


def test_the_url_engine_no_longer_dead_ends_on_an_unfetchable_link():
    """Even when the links genuinely are the request and none can be read, the
    rest of the message must still be answered rather than discarded."""
    import inspect

    from app.engines import url as url_engine

    source = inspect.getsource(url_engine.run_url_engine)
    assert "stream_chat_events" in source
    assert "fetch_failed" in source
