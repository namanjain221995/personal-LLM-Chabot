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

    async def search(self, query: str, max_results: int) -> List[SearchResult]:
        params = {"q": query, "format": "json", "safesearch": "1"}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
                resp = await client.get(f"{self.base_url}/search", params=params)
                resp.raise_for_status()
                data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SearchUnavailableError(f"SearXNG error: {exc}") from exc

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
