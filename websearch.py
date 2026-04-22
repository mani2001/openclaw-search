#!/usr/bin/env python3
"""
websearch.py — Web search tool for OpenClaw agents.

Sources (priority order):
  1. SearXNG local instance (http://localhost:8888) — local, fast, unlimited
  2. ddgr via subprocess (/opt/homebrew/bin/ddgr --json)
  3. duckduckgo-search Python library (fallback)

Usage CLI:
  python websearch.py "query" --num 5
  python websearch.py "query" --num 5 --fetch       # fetch first result content
  python websearch.py "query" --num 5 --all          # search all sources, merge results
  python websearch.py "query" --source searxng       # force specific source
  python websearch.py "query" --source ddgr
  python websearch.py "query" --source duckduckgo

Usage module:
  from websearch import search, search_all, fetch_page
"""
__version__ = "1.0"

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
CACHE_DB = "/tmp/websearch_cache.db"
CACHE_TTL = 3600  # 1 hour

def _cache_init():
    conn = sqlite3.connect(CACHE_DB)
    conn.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, data TEXT, ts REAL)")
    conn.commit()
    return conn

def _cache_get(query: str, source: str) -> list[dict] | None:
    key = hashlib.md5(f"{source}:{query}".encode()).hexdigest()
    conn = _cache_init()
    row = conn.execute("SELECT data, ts FROM cache WHERE key=?", (key,)).fetchone()
    conn.close()
    if row and (time.time() - row[1]) < CACHE_TTL:
        return json.loads(row[0])
    return None

def _cache_set(query: str, source: str, results: list[dict]):
    key = hashlib.md5(f"{source}:{query}".encode()).hexdigest()
    conn = _cache_init()
    conn.execute("INSERT OR REPLACE INTO cache (key, data, ts) VALUES (?, ?, ?)",
                 (key, json.dumps(results), time.time()))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Search backends
# ---------------------------------------------------------------------------

def _search_searxng(query: str, num: int = 5) -> list[dict]:
    """PRIMARY: Search via local SearXNG instance."""
    resp = requests.get(
        "http://localhost:8888/search",
        params={"q": query, "format": "json", "pageno": 1},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    results = []
    for r in data.get("results", [])[:num]:
        results.append({
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": r.get("content", ""),
            "source": "searxng",
        })
    return results


def _search_ddgr(query: str, num: int = 5) -> list[dict]:
    """SECONDARY: Search via ddgr CLI tool."""
    proc = subprocess.run(
        ["/opt/homebrew/bin/ddgr", "--noprompt", "--num", str(num), "--json", query],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ddgr exited {proc.returncode}: {proc.stderr.strip()}")
    data = json.loads(proc.stdout)
    results = []
    for r in data:
        results.append({
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": r.get("abstract", ""),
            "source": "ddgr",
        })
    return results


def _search_duckduckgo(query: str, num: int = 5) -> list[dict]:
    """TERTIARY: Search via duckduckgo-search Python library."""
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS
    results = []
    with DDGS() as ddgs:
        for r in ddgs.text(query, max_results=num):
            results.append({
                "title": r.get("title", ""),
                "url": r.get("href", ""),
                "snippet": r.get("body", ""),
                "source": "duckduckgo",
            })
    return results


def _search_tavily(query: str, num: int = 5) -> list[dict]:
    """Search via Tavily API (cloud search for LLMs)."""
    tavily_api_key = os.environ.get("TAVILY_API_KEY", "")
    if not tavily_api_key:
        return []
    from tavily import TavilyClient
    client = TavilyClient(api_key=tavily_api_key)
    response = client.search(
        query=query,
        max_results=min(num, 20),
        search_depth="basic",
        topic="general",
    )
    results = []
    for r in response.get("results", [])[:num]:
        results.append({
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": r.get("content", ""),
            "source": "tavily",
        })
    return results


_BACKENDS = [
    ("searxng", _search_searxng),
    ("tavily", _search_tavily),
    ("ddgr", _search_ddgr),
    ("duckduckgo", _search_duckduckgo),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _archive_to_knowledge(query: str, results: list[dict]):
    """Archive search results to knowledge base (best effort)."""
    try:
        from knowledge import KnowledgeBase
        kb = KnowledgeBase()
        kb.archive_search(query, results)
        kb.close()
    except Exception:
        pass  # Don't fail if knowledge base is unavailable


def search_local(query: str, num: int = 5) -> list[dict]:
    """Search the local knowledge base first."""
    try:
        from knowledge import KnowledgeBase
        kb = KnowledgeBase()
        hits = kb.search(query, num)
        kb.close()
        # Flatten search results from archived searches
        results = []
        for h in hits:
            if h["type"] == "search":
                for r in h["results"][:num]:
                    r["source"] = "knowledge"
                    results.append(r)
            elif h["type"] == "page":
                results.append({
                    "title": h["title"],
                    "url": h["url"],
                    "snippet": h["snippet"],
                    "source": "knowledge",
                })
        return results[:num]
    except Exception:
        return []


def search(query: str, num: int = 5, source: str | None = None) -> list[dict]:
    """
    Search the web. Tries local knowledge base first, then web sources.
    Returns list of {"title", "url", "snippet", "source"}.
    """
    # Try local knowledge base first (unless specific source requested)
    if not source:
        local = search_local(query, num)
        if local:
            print(f"[websearch] found {len(local)} results in knowledge base", file=sys.stderr)
            return local

    if source:
        backends = [(n, fn) for n, fn in _BACKENDS if n == source]
        if not backends:
            raise ValueError(f"Unknown source: {source}. Available: {[n for n,_ in _BACKENDS]}")
    else:
        backends = _BACKENDS

    for name, fn in backends:
        cached = _cache_get(query, name)
        if cached:
            return cached
        try:
            results = fn(query, num)
            if results:
                _cache_set(query, name, results)
                _archive_to_knowledge(query, results)  # Persist to knowledge base
                return results
        except Exception as e:
            print(f"[websearch] {name} failed: {e}", file=sys.stderr)

    return []


def search_all(query: str, num: int = 5) -> list[dict]:
    """
    Search ALL sources and merge results (deduped by URL).
    Returns combined list with source attribution.
    """
    seen_urls = set()
    merged = []

    for name, fn in _BACKENDS:
        try:
            results = fn(query, num)
            for r in results:
                url = r.get("url", "")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    merged.append(r)
        except Exception as e:
            print(f"[websearch] {name} failed: {e}", file=sys.stderr)

    return merged


def fetch_page(url: str, max_chars: int = 8000) -> str:
    """Fetch a URL and return cleaned text content."""
    resp = requests.get(url, timeout=15, headers={
        "User-Agent": "Mozilla/5.0 (compatible; websearch/1.0)"
    })
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    # Remove script/style/nav
    for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
        tag.decompose()

    text = soup.get_text(separator="\n", strip=True)
    lines = [l for l in text.splitlines() if l.strip()]
    return "\n".join(lines)[:max_chars]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Web search (SearXNG → ddgr → duckduckgo-search)")
    parser.add_argument("query", help="Search query")
    parser.add_argument("--num", type=int, default=5, help="Number of results (default 5)")
    parser.add_argument("--fetch", action="store_true", help="Fetch full content of first result")
    parser.add_argument("--deep", type=int, metavar="N", help="Fetch top N results content")
    parser.add_argument("--summarize", action="store_true", help="Use TaskForge to summarize fetched content in parallel")
    parser.add_argument("--all", action="store_true", dest="search_all", help="Search all sources and merge results")
    parser.add_argument("--source", choices=["searxng", "tavily", "ddgr", "duckduckgo"], help="Force a specific search source")
    parser.add_argument("--format", choices=["json", "markdown", "text"], default="json", dest="fmt", help="Output format")
    args = parser.parse_args()

    if args.search_all:
        results = search_all(args.query, args.num)
    else:
        results = search(args.query, args.num, source=args.source)

    if args.fetch and results:
        try:
            content = fetch_page(results[0]["url"])
            results[0]["content"] = content
        except Exception as e:
            results[0]["content"] = f"[fetch error: {e}]"

    if args.deep and results:
        for r in results[:args.deep]:
            try:
                r["content"] = fetch_page(r["url"], max_chars=3000)
            except Exception as e:
                r["content"] = f"[fetch error: {e}]"

    if args.summarize and results:
        import asyncio as _aio
        import aiohttp as _aioh

        contents = [r for r in results if r.get("content") and not r["content"].startswith("[fetch")]
        if contents:
            WORKERS = [
                ("brain", "http://localhost:8001/v1/chat/completions", "mlx-community/Qwen3-30B-A3B-4bit"),
                ("m2", "http://192.168.68.66:8000/v1/chat/completions", "mlx-community/Qwen3-30B-A3B-4bit"),
                ("glm", "http://192.168.68.66:8002/v1/chat/completions", "mlx-community/GLM-4.7-Flash-4bit"),
            ]

            async def _summarize_one(session, worker, r):
                name, url, model = worker
                prompt = f"/no_think\nSummarize this web page in 2-3 sentences. Be factual and concise.\n\nTitle: {r['title']}\nURL: {r['url']}\nContent:\n{r['content'][:2000]}"
                payload = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 200}
                try:
                    async with session.post(url, json=payload, timeout=_aioh.ClientTimeout(total=30)) as resp:
                        data = await resp.json()
                        content = data["choices"][0]["message"]["content"]
                        # Strip thinking from any model
                        if "</think>" in content:
                            content = content[content.index("</think>") + 8:]
                        # Strip numbered reasoning steps (GLM pattern)
                        import re
                        content = re.sub(r'^\d+\.\s+\*\*.*?\*\*.*?\n', '', content, flags=re.MULTILINE)
                        content = re.sub(r'^\s*\*\s+\*\*.*?\*\*.*?\n', '', content, flags=re.MULTILINE)
                        # Take just the first 2-3 sentences
                        sentences = [s.strip() for s in content.split('.') if s.strip()]
                        if len(sentences) > 4:
                            content = '. '.join(sentences[:3]) + '.'
                        return content.strip()
                except Exception:
                    return None

            async def _summarize_all():
                async with _aioh.ClientSession() as session:
                    tasks = []
                    for i, r in enumerate(contents):
                        worker = WORKERS[i % len(WORKERS)]
                        tasks.append(_summarize_one(session, worker, r))
                    return await _aio.gather(*tasks)

            summaries = _aio.run(_summarize_all())
            for r, s in zip(contents, summaries):
                if s:
                    r["summary"] = s

    # Output formatting
    if args.fmt == "json":
        print(json.dumps(results, indent=2, ensure_ascii=False))
    elif args.fmt == "markdown":
        for i, r in enumerate(results, 1):
            print(f"### {i}. [{r['title']}]({r['url']})")
            print(f"> {r['snippet']}\n")
            if "summary" in r:
                print(f"**Summary:** {r['summary']}\n")
            elif "content" in r:
                print(f"{r['content'][:500]}...\n")
    elif args.fmt == "text":
        for i, r in enumerate(results, 1):
            print(f"{i}. {r['title']}")
            print(f"   {r['url']}")
            print(f"   {r['snippet']}")
            if "summary" in r:
                print(f"   📝 {r['summary']}")
            elif "content" in r:
                print(f"   {r['content'][:300]}...")
            print()


if __name__ == "__main__":
    main()


def _track_tokens(tool, worker, tokens, model=""):
    """Log to TokenTracker (best effort)."""
    try:
        from token_tracker import log_usage
        log_usage(tool, worker, tokens, model)
    except Exception:
        pass
