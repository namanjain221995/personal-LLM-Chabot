"""Pluggable web-search provider interface (Phase 1).

A provider turns a query string into a list of SearchResult (title, url,
snippet). The concrete provider is chosen by SEARCH_PROVIDER; the result pages
are fetched+extracted later through the SSRF-safe path, not here.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import List

from ..config import settings


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str


class SearchProvider(abc.ABC):
    name: str = "base"

    @abc.abstractmethod
    async def search(self, query: str, max_results: int) -> List[SearchResult]:
        ...


class SearchUnavailableError(RuntimeError):
    """Provider not configured or unreachable — the caller falls back to the
    model's own knowledge with a visible notice."""


def get_provider() -> SearchProvider:
    """Build the configured provider, or raise SearchUnavailableError when the
    required settings/keys are missing."""
    provider = (settings.search_provider or "searxng").lower()
    if provider == "searxng":
        from .searxng import SearxngProvider

        if not settings.searxng_url:
            raise SearchUnavailableError("SEARXNG_URL is not set")
        return SearxngProvider(settings.searxng_url)
    if provider == "tavily":
        from .tavily import TavilyProvider

        if not settings.tavily_api_key:
            raise SearchUnavailableError("TAVILY_API_KEY is not set")
        return TavilyProvider(settings.tavily_api_key)
    if provider == "brave":
        from .brave import BraveProvider

        if not settings.brave_api_key:
            raise SearchUnavailableError("BRAVE_API_KEY is not set")
        return BraveProvider(settings.brave_api_key)
    raise SearchUnavailableError(f"unknown SEARCH_PROVIDER {provider!r}")
