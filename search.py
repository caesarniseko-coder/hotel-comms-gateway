"""Web search — zero-auth DuckDuckGo backend.

Used by:
  • the Concierge agent (via a Hermes skill) when a guest asks something
    that needs fresh info (restaurants, events, transport, news)
  • the /search_web admin command for ad-hoc lookups

Pluggable: drop in Brave or Tavily by replacing _ddg_search with the right call
and setting BRAVE_API_KEY / TAVILY_API_KEY env vars.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any

log = logging.getLogger("search")

PROVIDER = os.environ.get("SEARCH_PROVIDER", "duckduckgo").lower()
BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY", "")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")


def _ddg_search(query: str, n: int = 5) -> list[dict[str, Any]]:
    """Synchronous DDG call — wrapped to_thread by the async wrapper below."""
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        log.error("duckduckgo_search not installed")
        return []
    out: list[dict[str, Any]] = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=n, region="wt-wt", safesearch="moderate"):
                out.append({
                    "title": r.get("title") or "",
                    "url": r.get("href") or r.get("url") or "",
                    "snippet": r.get("body") or r.get("snippet") or "",
                })
    except Exception as exc:
        log.warning("DDG search failed: %s", exc)
    return out


async def _brave_search(query: str, n: int = 5) -> list[dict[str, Any]]:
    if not BRAVE_API_KEY:
        return []
    import httpx
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={"X-Subscription-Token": BRAVE_API_KEY, "Accept": "application/json"},
            params={"q": query, "count": n},
        )
        r.raise_for_status()
        results = (r.json().get("web") or {}).get("results") or []
        return [
            {"title": x.get("title", ""), "url": x.get("url", ""), "snippet": x.get("description", "")}
            for x in results[:n]
        ]


async def _tavily_search(query: str, n: int = 5) -> list[dict[str, Any]]:
    if not TAVILY_API_KEY:
        return []
    import httpx
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            "https://api.tavily.com/search",
            json={"api_key": TAVILY_API_KEY, "query": query, "max_results": n},
        )
        r.raise_for_status()
        results = r.json().get("results") or []
        return [
            {"title": x.get("title", ""), "url": x.get("url", ""), "snippet": x.get("content", "")}
            for x in results[:n]
        ]


async def search(query: str, n: int = 5) -> list[dict[str, Any]]:
    """Run a web search using the configured provider. Returns [] on failure."""
    if not query:
        return []
    query = query.strip()[:200]

    if PROVIDER == "brave" and BRAVE_API_KEY:
        try:
            return await _brave_search(query, n)
        except Exception as exc:
            log.warning("brave search failed, falling back to ddg: %s", exc)
    if PROVIDER == "tavily" and TAVILY_API_KEY:
        try:
            return await _tavily_search(query, n)
        except Exception as exc:
            log.warning("tavily search failed, falling back to ddg: %s", exc)

    # default: DuckDuckGo
    return await asyncio.to_thread(_ddg_search, query, n)


def format_snippets(results: list[dict[str, Any]], limit: int = 5) -> str:
    """Compact text format suitable for feeding into an LLM context."""
    if not results:
        return "(no results)"
    lines = []
    for i, r in enumerate(results[:limit], 1):
        title = re.sub(r"\s+", " ", r.get("title", "")).strip()
        snippet = re.sub(r"\s+", " ", r.get("snippet", "")).strip()
        url = r.get("url", "")
        lines.append(f"{i}. {title}\n   {snippet[:280]}\n   {url}")
    return "\n".join(lines)
