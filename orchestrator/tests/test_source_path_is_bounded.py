"""Nothing on the source path may hold a turn open for ever.

MEASURED 2026-09-28 on a Fast turn whose pre-pass went to the network. Two
stages sit in front of the first token with NO BOUND OF ANY KIND — no env
var, no asyncio timeout, nothing — in either the Fast lookup or the full
search engine:

  extraction (trafilatura/lxml, single-worker pool)
      p50 262 ms, p95 2,386 ms, max 3,510 ms
  index_pending (embed + LanceDB write, deliberately synchronous)
      p50 114 ms, p95 865 ms, max 4,305 ms — inside one 8,432 ms pre-pass

`grep -n "asyncio.timeout\\|wait_for" app/engines/search.py` returned NOTHING
before this change: the whole module was unbounded, and the Fast path was
bounded only by its CALLER's 8 s, with the full search engine bounded by
nothing at all.

The defaults are deliberately ABOVE the measured maxima. The point is to stop
a pathological page holding a turn open for ever, not to drop pages that work
today — so these tests drive the timeout by SETTING IT SMALL, never by
asserting a wall-clock number.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import settings
from app.engines import search

SEARCH_PY = Path(__file__).resolve().parents[1] / "app" / "engines" / "search.py"


class _Fetched(SimpleNamespace):
    pass


def _page() -> _Fetched:
    return _Fetched(content_type="text/html", body=b"<html><body>hi</body></html>",
                    url="https://example.org/a", headers={})


def test_both_stages_have_a_configurable_bound():
    assert settings.extract_timeout_ms > 0
    assert settings.index_pending_timeout_ms > 0
    # Above the measured maxima: a bound that cuts working pages is a
    # regression wearing a fix's clothes.
    assert settings.extract_timeout_ms >= 3510, "below the measured extraction maximum"
    assert settings.index_pending_timeout_ms >= 4305, "below the measured index maximum"


def test_the_search_module_is_no_longer_unbounded():
    """The grep that found this: the module had no timeout anywhere."""
    source = SEARCH_PY.read_text(encoding="utf-8")
    assert re.search(r"asyncio\.wait_for|asyncio\.timeout", source), (
        "app/engines/search.py has no timeout at all again"
    )


def test_a_slow_extraction_raises_instead_of_holding_the_turn(monkeypatch):
    """The whole point, driven by making the budget small rather than the page
    slow — a test that waits for a real 5 s is a test nobody runs."""
    monkeypatch.setattr(settings, "extract_timeout_ms", 30, raising=False)

    def never(*_a, **_k):
        import time as _t
        _t.sleep(2.0)
        return (SimpleNamespace(text="x", title="t"), [])

    monkeypatch.setattr(search, "_call_extract", never)

    async def go():
        with pytest.raises(asyncio.TimeoutError):
            await search._extract_bounded(_page(), {}, where="test")

    asyncio.run(go())


def test_a_fast_extraction_is_untouched(monkeypatch):
    monkeypatch.setattr(settings, "extract_timeout_ms", 5000, raising=False)
    monkeypatch.setattr(
        search, "_call_extract",
        lambda *_a, **_k: (SimpleNamespace(text="body", title="Title"), ["https://x/y"]),
    )

    async def go():
        ext, links = await search._extract_bounded(_page(), {}, where="test")
        assert ext.text == "body" and links == ["https://x/y"]

    asyncio.run(go())


def test_the_timeout_is_counted_so_it_cannot_be_silent(monkeypatch):
    """A bound that fires invisibly is how a source path loses pages without
    anyone noticing."""
    seen = []
    monkeypatch.setattr(settings, "extract_timeout_ms", 20, raising=False)
    monkeypatch.setattr(search.metrics, "inc", lambda name, **kw: seen.append((name, kw)))

    def never(*_a, **_k):
        import time as _t
        _t.sleep(2.0)
        return (SimpleNamespace(text="x", title="t"), [])

    monkeypatch.setattr(search, "_call_extract", never)

    async def go():
        with pytest.raises(asyncio.TimeoutError):
            await search._extract_bounded(_page(), {}, where="fetch_source")

    asyncio.run(go())
    assert ("search_stage_timeout_total", {"stage": "extract", "where": "fetch_source"}) in seen


def test_a_page_that_times_out_is_still_cited_from_its_snippet():
    """`_fetch_source` catches every failure and falls back to the provider's
    blurb, labelled `from_snippet`. A TimeoutError has to land there too —
    that is why the helper raises rather than returning a sentinel."""
    source = SEARCH_PY.read_text(encoding="utf-8")
    body = source[source.index("async def _fetch_source("):]
    body = body[: body.index("\nasync def ", 1)]
    assert "_extract_bounded" in body
    assert "except Exception:" in body
    assert "from_snippet=True" in body


def test_index_pending_is_bounded_and_named_in_the_metric():
    source = SEARCH_PY.read_text(encoding="utf-8")
    # Since 2026-09-29 the bound is `asyncio.timeout` (Python 3.11 CI rule:
    # `wait_for` can swallow a same-pass cancel) and it is also cut to what is
    # left of the Fast lookup's budget (tests/test_fast_lookup_budget.py).
    body = source[source.index("async def fetch_for_freshness("):]
    body = body[: body.index("\ndef ", 1)]
    at = body.index("await web_index.index_pending(repair_stale_chunks=False)")
    block = body[at - 500 : at + 500]
    assert "index_pending_timeout_ms" in block
    assert "async with asyncio.timeout(index_budget):" in block
    assert "asyncio.wait_for" not in block
    assert 'stage="index_pending"' in block
    # The pages are already stored: a timeout must NOT be treated as an error
    # that loses them.
    assert "except asyncio.TimeoutError:" in block
