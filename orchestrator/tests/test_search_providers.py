"""Search provider parsing + factory selection (Phase 1). Mocked HTTP only."""
import asyncio

import httpx
import pytest

from app.config import settings
from app.search import base
from app.search.brave import BraveProvider
from app.search.searxng import SearxngProvider
from app.search.tavily import TavilyProvider


def _mock_client(monkeypatch, handler):
    real = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda *a, **k: real(*a, **{**k, "transport": transport})
    )


def test_searxng_parses_results(monkeypatch):
    _mock_client(
        monkeypatch,
        lambda req: httpx.Response(
            200,
            json={
                "results": [
                    {"url": "https://a.com", "title": "A", "content": "snip a"},
                    {"url": "https://b.com", "title": "B", "content": "snip b"},
                    {"title": "no url"},  # skipped
                ]
            },
        ),
    )
    res = asyncio.run(SearxngProvider("http://searxng:8080").search("q", 5))
    assert [r.url for r in res] == ["https://a.com", "https://b.com"]
    assert res[0].snippet == "snip a"


def test_searxng_respects_max_results(monkeypatch):
    _mock_client(
        monkeypatch,
        lambda req: httpx.Response(
            200,
            json={"results": [{"url": f"https://{i}.com", "title": str(i)} for i in range(10)]},
        ),
    )
    res = asyncio.run(SearxngProvider("http://s:8080").search("q", 3))
    assert len(res) == 3


def test_searxng_error_raises_unavailable(monkeypatch):
    _mock_client(monkeypatch, lambda req: httpx.Response(502))
    with pytest.raises(base.SearchUnavailableError):
        asyncio.run(SearxngProvider("http://s:8080").search("q", 3))


def test_tavily_parses(monkeypatch):
    _mock_client(
        monkeypatch,
        lambda req: httpx.Response(
            200, json={"results": [{"url": "https://t.com", "title": "T", "content": "c"}]}
        ),
    )
    res = asyncio.run(TavilyProvider("key").search("q", 5))
    assert res[0].url == "https://t.com" and res[0].snippet == "c"


def test_brave_parses(monkeypatch):
    _mock_client(
        monkeypatch,
        lambda req: httpx.Response(
            200,
            json={"web": {"results": [{"url": "https://x.com", "title": "X", "description": "d"}]}},
        ),
    )
    res = asyncio.run(BraveProvider("key").search("q", 5))
    assert res[0].url == "https://x.com" and res[0].snippet == "d"


def test_factory_selects_and_requires_config(monkeypatch):
    monkeypatch.setattr(settings, "search_provider", "searxng")
    monkeypatch.setattr(settings, "searxng_url", "")
    with pytest.raises(base.SearchUnavailableError):
        base.get_provider()
    monkeypatch.setattr(settings, "searxng_url", "http://searxng:8080")
    assert isinstance(base.get_provider(), SearxngProvider)

    monkeypatch.setattr(settings, "search_provider", "tavily")
    monkeypatch.setattr(settings, "tavily_api_key", "")
    with pytest.raises(base.SearchUnavailableError):
        base.get_provider()
    monkeypatch.setattr(settings, "tavily_api_key", "k")
    assert isinstance(base.get_provider(), TavilyProvider)

    monkeypatch.setattr(settings, "search_provider", "nonsense")
    with pytest.raises(base.SearchUnavailableError):
        base.get_provider()
