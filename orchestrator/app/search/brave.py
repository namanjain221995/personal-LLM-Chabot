"""Brave Search API provider (hosted, privacy-oriented)."""
from __future__ import annotations

from typing import List

import httpx

from .base import SearchProvider, SearchResult, SearchUnavailableError

_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"


class BraveProvider(SearchProvider):
    name = "brave"

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    async def search(
        self, query: str, max_results: int, categories: str = ""
    ) -> List[SearchResult]:
        # `categories` is a SearXNG concept; this API has no equivalent.
        del categories
        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": self.api_key,
        }
        params = {"q": query, "count": max_results}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(12.0)) as client:
                resp = await client.get(_ENDPOINT, params=params, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SearchUnavailableError(f"Brave error: {exc}") from exc

        out: List[SearchResult] = []
        for item in (data.get("web", {}) or {}).get("results", [])[:max_results]:
            url = item.get("url")
            if not url:
                continue
            out.append(
                SearchResult(
                    title=item.get("title") or url,
                    url=url,
                    snippet=item.get("description") or "",
                )
            )
        return out
