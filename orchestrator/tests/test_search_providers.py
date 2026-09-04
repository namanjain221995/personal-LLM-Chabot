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


# ---------------------------------------------------------------------------
# The engine pool itself
#
# SearXNG's config is not code, so nothing else in this suite would notice it
# drifting — but it is the single biggest lever on both search latency and
# candidate quality, and it has now been wrong in both directions.
# ---------------------------------------------------------------------------

import pathlib
import re

import yaml

SETTINGS = pathlib.Path(__file__).resolve().parents[2] / "searxng" / "settings.yml"


def _engines() -> dict[str, bool]:
    """name -> enabled, from the real config file."""
    doc = yaml.safe_load(SETTINGS.read_text())
    return {e["name"]: not e.get("disabled", False) for e in doc.get("engines", [])}


def test_the_config_is_valid_yaml_and_lists_engines():
    assert _engines(), "no engines block — SearXNG would fall back to defaults"


def test_the_engines_that_never_answer_here_are_off():
    """Measured 2026-09-04 over eight varied queries: google cse 0/8,
    startpage 0/8, brave 1/8. A dead engine is WAITED ON, not skipped, and the
    three of them cost 533 ms — 34% — of every search."""
    engines = _engines()
    for name in ("google cse", "startpage", "brave"):
        assert engines.get(name) is False, f"{name} must be disabled: it never answers"


def test_dead_engines_are_disabled_rather_than_removed():
    """`remove` looks tidier and takes the whole instance down.

    Other engines declare `network: brave`, and SearXNG resolves those names at
    boot — removing brave raises KeyError('brave') inside network.initialize
    and every worker exits, which is exactly what happened when this was tried.
    """
    doc = yaml.safe_load(SETTINGS.read_text())
    removed = doc.get("use_default_settings", {}).get("engines", {}).get("remove", [])
    assert "brave" not in removed, "removing brave crashes SearXNG at boot"


def test_the_personal_site_indexes_are_off():
    """They index, by design, the part of the web this workspace never asks
    about. For "who is the CEO of OpenAI" they returned personal blogs and a
    2016 OpenAI Gym post — 97 results competing for candidate slots."""
    engines = _engines()
    for name in ("wiby", "searchmysite", "mwmbl"):
        assert engines.get(name) is False, f"{name} floods the pool with noise"


def test_the_general_engines_that_do_answer_are_on():
    """The pool must not be narrowed to nothing: these four answered 8/8."""
    engines = _engines()
    for name in ("bing", "duckduckgo web", "yandex", "yahoo"):
        assert engines.get(name) is True, f"{name} answers reliably and must stay"


def test_no_second_timeout_is_imposed_on_the_engines():
    """A 3.0s cap was tried and made things worse: bing went from 8/8 answering
    to 8/8 timing out and every search pinned to the cap. Measured per engine,
    bing is the FASTEST at 335 ms — nothing here is slow enough to cut off."""
    doc = yaml.safe_load(SETTINGS.read_text())
    assert "request_timeout" not in (doc.get("outgoing") or {}), (
        "an outgoing request_timeout here removes good results, not slow ones"
    )


def test_json_output_stays_enabled():
    """SearxngProvider consumes JSON; without this format the app silently
    loses web search entirely."""
    doc = yaml.safe_load(SETTINGS.read_text())
    assert "json" in doc["search"]["formats"]
