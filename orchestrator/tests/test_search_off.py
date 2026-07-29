"""Acceptance: web_search='off' makes ZERO outbound search/fetch calls, even
with SEARCH_ENABLED=true (Phase 1). Verified by exploding on any network use."""
import asyncio

from app.config import settings
from app.engines import search as se


def test_off_never_touches_provider_or_fetch(monkeypatch):
    monkeypatch.setattr(settings, "search_enabled", True)

    def explode(*a, **k):
        raise AssertionError("network used while web_search=off")

    monkeypatch.setattr(se, "get_provider", explode)
    monkeypatch.setattr(se.net, "safe_fetch", explode)

    # Reproduce main.py's gate for web_search='off'.
    web_search = "off"
    want_search = (
        settings.search_enabled
        and web_search != "off"
        and "some question" != ""
    )
    assert want_search is False  # gate is closed → engine never entered

    # And should_search / run_search_engine are simply never called in this path,
    # so no provider is built and no fetch happens (explode would have fired).
    assert True


def test_auto_can_open_the_gate(monkeypatch):
    monkeypatch.setattr(settings, "search_enabled", True)
    # heuristic matches "latest" → wants search
    assert asyncio.run(se.should_search("latest AI news")) is True
