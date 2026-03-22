# 🔎 OpenClaw Search
Advanced web search tools with multiple engines, fallbacks, and content extraction.

## Tools
- **web_search.py** — Multi-layer web search with 4 fallback tiers, content extraction, query expansion
- **search_engine.py** — Advanced search engine CLI with LLM re-ranking and caching
- **search_health.py** — Cron health-check for search system monitoring
- **websearch.py** — Web search tool with SearXNG, ddgr, and DuckDuckGo fallbacks

## Features
- Multi-source search (SearXNG, Brave API, ddgr, DuckDuckGo)
- Parallel processing for speed
- Content extraction and summarization
- Query expansion with LLM
- Health monitoring and diagnostics
- Results caching and re-ranking

## Usage
```bash
python3 web_search.py "query" --fast               # parallel search
python3 web_search.py "query" --extract            # with content
python3 websearch.py "query" --num 5 --fetch       # fetch content
python3 search_health.py                           # health check
```

## Requirements
- Python 3.10+
- SearXNG instance (localhost:8888) recommended
- Optional: Brave API key (`BRAVE_API_KEY`) for enhanced results
- Optional: Tavily API key (`TAVILY_API_KEY`) for Tavily search source
- ddgr tool for fallback

## License
MIT