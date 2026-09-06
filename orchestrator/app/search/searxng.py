"""SearXNG provider (Phase 1 default) — a self-hosted metasearch engine.

Queries the operator-configured SearXNG JSON API. SEARXNG_URL is trusted
infrastructure (set by the operator, not user input), so it is not routed
through the SSRF guard; the RESULT pages are.
"""
from __future__ import annotations

from typing import List

import httpx

from .base import SearchProvider, SearchResult, SearchUnavailableError


class SearxngProvider(SearchProvider):
    name = "searxng"

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        #: {query: [engine, …]} for the engines that did NOT answer, read back
        #: by engines/search.py after the fan-out. SearXNG has always reported
        #: this and nothing read it (finding S6): the last real query before
        #: the audit ran with wikipedia, duckduckgo and yandex all timed out
        #: against a 3.0 s ceiling, and the application recorded none of it —
        #: a thin result set that looked exactly like a thorough one, cached
        #: as authoritative for 900 s.
        #: One instance serves one request's fan-out, and each concurrent
        #: query writes its own key, so this needs no lock and does not grow.
        self.unresponsive: dict = {}

    async def search(
        self, query: str, max_results: int, categories: str = ""
    ) -> List[SearchResult]:
        # Category routing (measured 2026-08-30): a plain general query only
        # ever reaches google cse / bing / mwmbl / yahoo here, while
        # `categories=it` (github, stackoverflow, mdn, docker hub) returned 60
        # results with ZERO unresponsive engines and `categories=science`
        # (arxiv, pubmed) returned full 1100-1900 character abstracts instead
        # of 135-character snippets. Those pools have no rate-limit problem
        # because nothing else on this host queries them.
        params = {"q": query, "format": "json", "safesearch": "1"}
        if categories:
            params["categories"] = categories
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
                resp = await client.get(f"{self.base_url}/search", params=params)
                resp.raise_for_status()
                data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SearchUnavailableError(f"SearXNG error: {exc}") from exc

        # `unresponsive_engines` is a list of [engine, reason] pairs (older
        # builds send bare strings). Only the ENGINE NAME is kept: the reason
        # string is free text from an upstream and the query must not be
        # carried alongside it anywhere it could be logged.
        down: List[str] = []
        for item in data.get("unresponsive_engines") or []:
            name = item[0] if isinstance(item, (list, tuple)) and item else item
            if isinstance(name, str) and name.strip():
                down.append(name.strip())
        if down:
            self.unresponsive[query] = sorted(set(down))

        out: List[SearchResult] = []
        for item in data.get("results", [])[: max_results * 2]:
            url = item.get("url")
            if not url:
                continue
            out.append(
                SearchResult(
                    title=item.get("title") or url,
                    url=url,
                    snippet=item.get("content") or "",
                )
            )
            if len(out) >= max_results:
                break
        return out
