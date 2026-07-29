"""Tavily provider (hosted search API for LLMs)."""
from __future__ import annotations

from typing import List

import httpx

from .base import SearchProvider, SearchResult, SearchUnavailableError

_ENDPOINT = "https://api.tavily.com/search"


class TavilyProvider(SearchProvider):
    name = "tavily"

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    async def search(self, query: str, max_results: int) -> List[SearchResult]:
        payload = {
            "api_key": self.api_key,
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
        }
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(12.0)) as client:
                resp = await client.post(_ENDPOINT, json=payload)
                resp.raise_for_status()
                data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SearchUnavailableError(f"Tavily error: {exc}") from exc

        out: List[SearchResult] = []
        for item in data.get("results", [])[:max_results]:
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
        return out
