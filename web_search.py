#!/usr/bin/env python3
"""
web_search.py v4.0 — Recherche web multi-couche ultime
========================================================
4 couches de fallback + extraction contenu + re-ranking + query expansion.

Usage:
  python3 scripts/web_search.py "query"                    # standard
  python3 scripts/web_search.py "query" --fast              # parallèle
  python3 scripts/web_search.py "query" --deep              # toutes couches, merge
  python3 scripts/web_search.py "query" --extract           # fetch contenu top 3
  python3 scripts/web_search.py "query" --extract --num 5   # fetch contenu top 5
  python3 scripts/web_search.py "query" --expand            # query expansion LLM
  python3 scripts/web_search.py "query" --news              # news only
  python3 scripts/web_search.py --health                    # diagnostic
  python3 scripts/web_search.py --metrics                   # stats 24h

Couches:
  1. SearXNG (localhost:8888) — 10+ engines weighted
  2. Brave API directe — 2000/mois free (si BRAVE_API_KEY set)
  3. DDGS Python — DuckDuckGo illimité
  4. ddgr CLI — DuckDuckGo CLI illimité

v4.0 — 2026-03-19
v4.1 — 2026-03-19 — Query quality validator (strict by default, --no-validate to bypass)
v5.0 — 2026-03-19 — Knowledge Store: persistent knowledge base with temporal decay + auto-save
"""

__version__ = "5.0"

import json, sys, subprocess, urllib.request, urllib.parse, urllib.error
import socket, time, hashlib, os, re, sqlite3
from pathlib import Path
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# === CONFIG ===
SEARXNG_URL = "http://localhost:8888/search"
SEARXNG_TIMEOUT = 10
DDGS_TIMEOUT = 10
DDGR_TIMEOUT = 10
BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY", "")
BRAVE_API_URL = "https://api.search.brave.com/res/v1/web/search"
CACHE_DIR = Path.home() / ".cache" / "web_search"
METRICS_DB = CACHE_DIR / "metrics.db"
DEFAULT_NUM = 8
EXTRACT_NUM = 3  # default pages to extract content from
EXTRACT_MAX_CHARS = 2000  # max chars per extracted page

# === QUERY QUALITY VALIDATOR ===
_DIMENSIONS = {
    "techno": re.compile(r'\b(python|rust|go|golang|java|javascript|typescript|node|nodejs|react|vue|angular|svelte|docker|kubernetes|k8s|nginx|postgres|postgresql|mysql|redis|mongodb|sqlite|flask|django|fastapi|express|nextjs|tailwind|css|html|swift|kotlin|ruby|rails|php|laravel|c\+\+|cmake|git|npm|pip|cargo|homebrew|brew|apt|systemd|ssh|curl|wget|ffmpeg|ollama|mlx|pytorch|tensorflow|vllm|comfyui|ansible|terraform|grafana|prometheus)\b', re.I),
    "version": re.compile(r'\b(v?\d+\.\d+[\.\d]*|python\s*3|node\s*\d+|version\s*\d+|latest|lts|stable|beta|alpha|nightly|canary)\b', re.I),
    "context": re.compile(r'\b(error|bug|fix|debug|crash|fail|timeout|refused|denied|permission|install|setup|config|configure|deploy|migration|upgrade|downgrade|build|compile|link|import|require|dependency|authentication|authorization|ssl|tls|certificate|cors|proxy|firewall|dns|port|socket|memory|leak|cpu|performance|slow|optimize|cache)\b', re.I),
    "temporal": re.compile(r'\b(202[4-9]|2030|latest|recent|new|current|today|since|after|before|deprecated|removed|breaking\s*change|changelog|release\s*notes?)\b', re.I),
    "platform": re.compile(r'\b(macos|mac\s*os|linux|ubuntu|debian|fedora|centos|arch|windows|win11|arm64|aarch64|x86_64|amd64|apple\s*silicon|m[1-5]|raspberry\s*pi|docker|wsl|ios|android)\b', re.I),
    "scope": re.compile(r'\b(how\s*to|tutorial|guide|example|best\s*practice|vs\b|versus|alternative|comparison|benchmark|documentation|docs|api\s*reference|troubleshoot|workaround|solution|difference\s*between|pros?\s*and\s*cons?|recommend)\b', re.I),
}

_DIMENSION_LABELS = {
    "techno": "techno/langage",
    "version": "version (3.12? v2? latest?)",
    "context": "contexte (error? setup? deploy?)",
    "temporal": "année/période (2026? recent?)",
    "platform": "OS/plateforme (macos? linux? arm64?)",
    "scope": "type (how to? tutorial? vs?)",
}

def validate_query(query: str) -> dict:
    """Check query has enough specificity dimensions. Returns analysis dict."""
    detected = []
    for dim, pattern in _DIMENSIONS.items():
        if pattern.search(query):
            detected.append(dim)
    missing = [label for dim, label in _DIMENSION_LABELS.items() if dim not in detected]
    warning = None
    if len(detected) < 2:
        missing_str = ", ".join(missing[:4])
        warning = (f"⚠️  Query pauvre ({len(detected)}/6 dimensions): \"{query}\"\n"
                   f"   Manque: {missing_str}\n"
                   f"   Tip: ajouter version, OS, lib, année, type de problème\n"
                   f"   Ex: \"python 3.12 asyncio timeout macos 2026\"")
    return {"dimensions": len(detected), "detected": detected,
            "missing_suggestions": missing, "warning": warning}

# === SMART CACHE ===
def _cache_ttl(query: str, news: bool = False) -> int:
    q = query.lower()
    if news or any(w in q for w in ["price", "prix", "today", "aujourd'hui", "latest", "news", "live", "current", "now"]):
        return 30
    if any(w in q for w in ["how to", "tutorial", "documentation", "guide", "setup", "install", "comment", "configur"]):
        return 1440
    return 120

def _cache_key(query: str, source: str) -> Path:
    h = hashlib.md5(f"{query}:{source}".encode()).hexdigest()[:12]
    return CACHE_DIR / f"{h}.json"

def _cache_get(query: str, source: str, news: bool = False) -> list | None:
    path = _cache_key(query, source)
    if not path.exists(): return None
    try:
        data = json.loads(path.read_text())
        if datetime.now() - datetime.fromisoformat(data["cached_at"]) > timedelta(minutes=_cache_ttl(query, news)):
            path.unlink(missing_ok=True); return None
        return data["results"]
    except: path.unlink(missing_ok=True); return None

def _cache_set(query: str, source: str, results: list):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cache_key(query, source).write_text(json.dumps({
        "query": query, "source": source,
        "cached_at": datetime.now().isoformat(), "results": results
    }, ensure_ascii=False, indent=2))

# === KNOWLEDGE STORE ===
KNOWLEDGE_DIR = CACHE_DIR / "knowledge"
KNOWLEDGE_INDEX = KNOWLEDGE_DIR / "index.json"

# TTL categories: how long knowledge stays confident (in hours)
_KNOWLEDGE_TTL = {
    "news":     24,      # 1 day — news, prices, live events
    "tech":     168,     # 7 days — technical how-tos, configs, errors
    "docs":     720,     # 30 days — documentation, stable APIs
    "concepts": 2160,    # 90 days — fundamentals, theory, comparisons
}

def _classify_knowledge(query: str) -> str:
    """Classify query into TTL category"""
    q = query.lower()
    if any(w in q for w in ["news", "price", "prix", "stock", "today", "live", "current", "announce", "release"]):
        return "news"
    if any(w in q for w in ["what is", "concept", "theory", "vs", "versus", "comparison", "difference", "fundamentals", "history"]):
        return "concepts"
    if any(w in q for w in ["documentation", "docs", "api reference", "spec", "rfc", "standard"]):
        return "docs"
    return "tech"

def _knowledge_confidence(saved_at: str, category: str) -> float:
    """Calculate confidence score with temporal decay. Returns 0.0-1.0"""
    try:
        age_hours = (datetime.now() - datetime.fromisoformat(saved_at)).total_seconds() / 3600
        ttl = _KNOWLEDGE_TTL.get(category, 168)
        if age_hours <= 0: return 1.0
        # Exponential decay: confidence halves at 50% of TTL
        half_life = ttl * 0.5
        confidence = 2 ** (-age_hours / half_life)
        return round(max(0.0, min(1.0, confidence)), 3)
    except:
        return 0.0

def _knowledge_age_str(saved_at: str) -> str:
    """Human-readable age string"""
    try:
        delta = datetime.now() - datetime.fromisoformat(saved_at)
        hours = delta.total_seconds() / 3600
        if hours < 1: return f"{int(delta.total_seconds()/60)}min"
        if hours < 24: return f"{int(hours)}h"
        if hours < 720: return f"{int(hours/24)}d"
        return f"{int(hours/720)}mo"
    except:
        return "?"

def _knowledge_key(query: str) -> str:
    """Normalize query into a filesystem-safe key"""
    # Lowercase, strip, collapse spaces, replace non-alnum with dash
    key = re.sub(r'[^a-z0-9]+', '-', query.lower().strip()).strip('-')
    # Truncate but keep unique via hash suffix
    if len(key) > 80:
        key = key[:60] + "-" + hashlib.md5(query.lower().encode()).hexdigest()[:8]
    return key

def _load_knowledge_index() -> dict:
    """Load the knowledge index (query -> metadata mapping)"""
    try:
        if KNOWLEDGE_INDEX.exists():
            return json.loads(KNOWLEDGE_INDEX.read_text())
    except: pass
    return {}

def _save_knowledge_index(index: dict):
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    KNOWLEDGE_INDEX.write_text(json.dumps(index, ensure_ascii=False, indent=2))

def knowledge_save(query: str, results: list, category: str = None) -> str:
    """Save search results to persistent knowledge store"""
    if not results: return ""
    if not category:
        category = _classify_knowledge(query)

    key = _knowledge_key(query)
    cat_dir = KNOWLEDGE_DIR / category
    cat_dir.mkdir(parents=True, exist_ok=True)
    filepath = cat_dir / f"{key}.json"

    entry = {
        "query": query,
        "category": category,
        "saved_at": datetime.now().isoformat(),
        "ttl_hours": _KNOWLEDGE_TTL[category],
        "results": results[:8],  # top 8 max
        "source_urls": [r.get("url", "") for r in results[:8] if r.get("url")],
    }

    # Check if superseding an older entry
    if filepath.exists():
        try:
            old = json.loads(filepath.read_text())
            entry["supersedes"] = old.get("saved_at", "")
        except: pass

    filepath.write_text(json.dumps(entry, ensure_ascii=False, indent=2))

    # Update index
    index = _load_knowledge_index()
    # Store multiple normalized search terms for fuzzy matching
    terms = set(query.lower().split())
    index[key] = {
        "query": query, "category": category,
        "saved_at": entry["saved_at"], "terms": list(terms),
        "file": str(filepath.relative_to(KNOWLEDGE_DIR)),
        "results_count": len(entry["results"]),
    }
    _save_knowledge_index(index)
    return str(filepath)

def knowledge_search(query: str, min_confidence: float = 0.3) -> dict | None:
    """Search knowledge store for matching entries. Returns best match or None."""
    index = _load_knowledge_index()
    if not index: return None

    query_terms = set(query.lower().split())
    # Remove common stop words
    stop_words = {"the","a","an","is","are","was","were","in","on","at","to","for","of","and","or","how","what","why","when","do","does"}
    query_terms -= stop_words

    if not query_terms: return None

    best_match, best_score = None, 0

    for key, meta in index.items():
        entry_terms = set(meta.get("terms", []))
        entry_terms -= stop_words
        if not entry_terms: continue

        # Jaccard-like overlap
        overlap = len(query_terms & entry_terms)
        union = len(query_terms | entry_terms)
        if union == 0: continue
        similarity = overlap / union

        # Boost exact substring match
        if query.lower() in meta.get("query", "").lower() or meta.get("query", "").lower() in query.lower():
            similarity += 0.3

        # Apply confidence decay
        confidence = _knowledge_confidence(meta.get("saved_at", ""), meta.get("category", "tech"))

        # Combined score: relevance * freshness
        combined = similarity * confidence

        if combined > best_score and confidence >= min_confidence and similarity >= 0.4:
            best_score = combined
            best_match = {"key": key, "meta": meta, "confidence": confidence,
                         "similarity": similarity, "combined_score": combined}

    if not best_match: return None

    # Load full entry
    try:
        filepath = KNOWLEDGE_DIR / best_match["meta"]["file"]
        entry = json.loads(filepath.read_text())
        best_match["entry"] = entry
        return best_match
    except:
        return None

def knowledge_list() -> str:
    """List all knowledge entries with confidence scores"""
    index = _load_knowledge_index()
    if not index: return "📚 Knowledge store: empty"
    lines = [f"📚 Knowledge Store ({len(index)} entries)", "=" * 50]
    by_cat = {}
    for key, meta in index.items():
        cat = meta.get("category", "tech")
        by_cat.setdefault(cat, []).append(meta)
    for cat in ["news", "tech", "docs", "concepts"]:
        entries = by_cat.get(cat, [])
        if not entries: continue
        lines.append(f"\n📁 {cat.upper()} ({len(entries)} entries, TTL={_KNOWLEDGE_TTL[cat]}h):")
        for m in sorted(entries, key=lambda x: x.get("saved_at",""), reverse=True):
            conf = _knowledge_confidence(m.get("saved_at",""), cat)
            age = _knowledge_age_str(m.get("saved_at",""))
            icon = "🟢" if conf > 0.7 else "🟡" if conf > 0.3 else "🔴"
            lines.append(f"  {icon} [{conf:.0%} {age}] {m['query']} ({m.get('results_count',0)} results)")
    return "\n".join(lines)

def knowledge_prune(min_confidence: float = 0.05) -> int:
    """Remove expired knowledge entries"""
    index = _load_knowledge_index()
    pruned = 0
    keys_to_remove = []
    for key, meta in index.items():
        conf = _knowledge_confidence(meta.get("saved_at",""), meta.get("category","tech"))
        if conf < min_confidence:
            # Delete file
            try: (KNOWLEDGE_DIR / meta["file"]).unlink(missing_ok=True)
            except: pass
            keys_to_remove.append(key)
            pruned += 1
    for k in keys_to_remove:
        del index[k]
    if keys_to_remove:
        _save_knowledge_index(index)
    return pruned

# === METRICS ===
def _metrics_init():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(METRICS_DB))
    db.execute("""CREATE TABLE IF NOT EXISTS searches (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT DEFAULT (datetime('now','localtime')),
        query TEXT, source TEXT, results_count INTEGER, latency_ms INTEGER,
        cached INTEGER DEFAULT 0, error TEXT)""")
    db.commit(); return db

def _metrics_log(query, source, count, latency_ms, cached=False, error=None):
    try:
        db = _metrics_init()
        db.execute("INSERT INTO searches (query,source,results_count,latency_ms,cached,error) VALUES (?,?,?,?,?,?)",
                   (query, source, count, latency_ms, 1 if cached else 0, error))
        db.commit(); db.close()
    except: pass

def metrics_report() -> str:
    try:
        db = _metrics_init()
        lines = ["📊 Search Metrics (last 24h)", "=" * 40]
        row = db.execute("SELECT COUNT(*),AVG(latency_ms),SUM(cached) FROM searches WHERE ts > datetime('now','-1 day','localtime')").fetchone()
        lines.append(f"Total: {row[0]} searches, avg {row[1]:.0f}ms, {row[2] or 0} cached")
        rows = db.execute("SELECT source,COUNT(*),AVG(latency_ms),AVG(results_count) FROM searches WHERE ts > datetime('now','-1 day','localtime') GROUP BY source ORDER BY COUNT(*) DESC").fetchall()
        lines.append("\nPar source:")
        for r in rows: lines.append(f"  {r[0]}: {r[1]}x, avg {r[2]:.0f}ms, avg {r[3]:.1f} results")
        rows = db.execute("SELECT source,error,COUNT(*) FROM searches WHERE error IS NOT NULL AND ts > datetime('now','-1 day','localtime') GROUP BY source,error").fetchall()
        if rows:
            lines.append("\n⚠️ Erreurs:")
            for r in rows: lines.append(f"  {r[0]}: {r[1]} ({r[2]}x)")
        db.close(); return "\n".join(lines)
    except Exception as e: return f"Metrics error: {e}"

# === LAYER 1: SearXNG ===
def search_searxng(query: str, num: int = DEFAULT_NUM, news: bool = False) -> list:
    params = {"q": query, "format": "json", "language": "auto"}
    if news: params["categories"] = "news"
    url = f"{SEARXNG_URL}?{urllib.parse.urlencode(params)}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "OpenClaw/4.0"})
        with urllib.request.urlopen(req, timeout=SEARXNG_TIMEOUT) as r:
            data = json.loads(r.read())
        return [{"title": item.get("title",""), "url": item.get("url",""),
                 "content": item.get("content","")[:300], "source": "searxng",
                 "engines": item.get("engines",[]), "score": item.get("score",0)}
                for item in data.get("results",[])[:num]]
    except: return []

# === LAYER 2: Brave API Direct ===
def search_brave_api(query: str, num: int = DEFAULT_NUM, news: bool = False) -> list:
    if not BRAVE_API_KEY: return []
    try:
        params = urllib.parse.urlencode({"q": query, "count": min(num, 20)})
        url = f"{BRAVE_API_URL}?{params}"
        req = urllib.request.Request(url, headers={
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": BRAVE_API_KEY
        })
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
        results = []
        for item in data.get("web", {}).get("results", [])[:num]:
            results.append({"title": item.get("title",""), "url": item.get("url",""),
                           "content": item.get("description","")[:300], "source": "brave_api"})
        return results
    except: return []

# === LAYER 3: DDGS Python ===
def search_ddgs(query: str, num: int = DEFAULT_NUM, news: bool = False) -> list:
    try:
        from ddgs import DDGS
        d = DDGS(timeout=DDGS_TIMEOUT)
        raw = d.news(query, max_results=num) if news else d.text(query, max_results=num)
        return [{"title": item.get("title",""), "url": item.get("href", item.get("url","")),
                 "content": item.get("body", item.get("excerpt",""))[:300], "source": "ddgs"} for item in raw]
    except: return []

# === LAYER 4: ddgr CLI ===
def search_ddgr(query: str, num: int = DEFAULT_NUM) -> list:
    try:
        proc = subprocess.run(["ddgr", "--noprompt", "--num", str(num), query],
                            capture_output=True, text=True, timeout=DDGR_TIMEOUT)
        results, current = [], {}
        for line in proc.stdout.strip().split("\n"):
            line = line.strip()
            if line and line[0].isdigit() and "." in line[:4]:
                if current and current.get("url"): results.append(current)
                current = {"title": line.split(".",1)[-1].strip(), "source": "ddgr"}
            elif line.startswith("http"): current["url"] = line
            elif line and current: current["content"] = line[:300]
        if current and current.get("url"): results.append(current)
        return results[:num]
    except: return []

# === CONTENT EXTRACTION ===
def extract_content(url: str, max_chars: int = EXTRACT_MAX_CHARS) -> str:
    """Fetch and extract readable content from a URL"""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"
        })
        with urllib.request.urlopen(req, timeout=8) as r:
            html = r.read().decode("utf-8", errors="ignore")

        # Remove scripts, styles, nav, footer, header
        for tag in ['script', 'style', 'nav', 'footer', 'header', 'aside', 'noscript']:
            html = re.sub(f'<{tag}[^>]*>.*?</{tag}>', '', html, flags=re.DOTALL | re.IGNORECASE)

        # Remove HTML tags
        text = re.sub(r'<[^>]+>', ' ', html)
        # Collapse whitespace
        text = re.sub(r'\s+', ' ', text).strip()
        # Remove common boilerplate patterns
        text = re.sub(r'(Cookie|Privacy|Terms|Subscribe|Sign up|Log in|Accept all)[^.]*\.', '', text, flags=re.IGNORECASE)

        return text[:max_chars] if text else ""
    except:
        return ""

def extract_top_results(results: list, num_extract: int = EXTRACT_NUM) -> list:
    """Parallel extract content from top N results"""
    urls_to_extract = []
    for r in results[:num_extract]:
        url = r.get("url", "")
        if url and not any(x in url for x in [".pdf", ".jpg", ".png", ".mp4", "youtube.com/watch"]):
            urls_to_extract.append((r, url))

    if not urls_to_extract:
        return results

    with ThreadPoolExecutor(max_workers=min(len(urls_to_extract), 5)) as pool:
        futures = {pool.submit(extract_content, url): r for r, url in urls_to_extract}
        for future in as_completed(futures, timeout=15):
            result = futures[future]
            try:
                content = future.result()
                if content and len(content) > 100:
                    result["extracted"] = content
                    result["extracted_len"] = len(content)
            except:
                pass

    return results

# === QUERY EXPANSION ===
def expand_query(query: str) -> list:
    """Generate alternative queries using local LLM (Qwen3-8B)"""
    try:
        payload = json.dumps({
            "model": "swama/Qwen3-8B",
            "messages": [{"role": "user", "content": f"/no_think\nGenerate 2 alternative search queries for: \"{query}\"\nReturn ONLY the queries, one per line, no numbering, no explanation."}],
            "max_tokens": 100, "temperature": 0.7
        }).encode()
        req = urllib.request.Request("http://localhost:28200/v1/chat/completions",
                                     data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
        content = data["choices"][0]["message"]["content"].strip()
        alternatives = [line.strip().strip('"').strip("'").lstrip("0123456789.-) ") for line in content.split("\n") if line.strip() and len(line.strip()) > 5]
        return alternatives[:2]
    except:
        return []

# === DEDUP + RERANK ===
def dedup_and_rerank(results: list, query: str) -> list:
    """Deduplicate by URL, merge scores, rank by relevance signals"""
    seen = {}
    for r in results:
        url = r.get("url", "")
        if not url: continue

        # Normalize URL
        url_clean = re.sub(r'[?#].*$', '', url).rstrip('/')

        if url_clean in seen:
            existing = seen[url_clean]
            # Merge engines
            existing_engines = set(existing.get("engines", []))
            existing_engines.update(r.get("engines", []))
            existing["engines"] = list(existing_engines)
            # Boost score for appearing in multiple sources
            existing["score"] = existing.get("score", 0) + r.get("score", 1) + 2
            existing["multi_source"] = True
        else:
            r["score"] = r.get("score", 1)
            seen[url_clean] = r

    results_deduped = list(seen.values())

    # Score boost for query terms in title
    query_terms = set(query.lower().split())
    for r in results_deduped:
        title_lower = r.get("title", "").lower()
        matches = sum(1 for term in query_terms if term in title_lower)
        r["score"] = r.get("score", 0) + matches * 1.5

        # Penalize ad URLs
        if any(x in r.get("url", "") for x in ["bing.com/aclick", "googleadservices", "/ad/", "doubleclick"]):
            r["score"] = -10

    # Sort by score descending
    results_deduped.sort(key=lambda x: x.get("score", 0), reverse=True)
    # Remove ads
    results_deduped = [r for r in results_deduped if r.get("score", 0) > -5]

    return results_deduped

# === PARALLEL SEARCH ===
def search_parallel(query: str, num: int = DEFAULT_NUM, news: bool = False) -> dict:
    t0 = time.time()
    layers = {"searxng": lambda: search_searxng(query, num * 2, news),
              "ddgs": lambda: search_ddgs(query, num, news),
              "ddgr": lambda: search_ddgr(query, num)}
    if BRAVE_API_KEY:
        layers["brave_api"] = lambda: search_brave_api(query, num, news)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fn): name for name, fn in layers.items()}
        all_results, layers_tried = [], []
        for future in as_completed(futures, timeout=max(SEARXNG_TIMEOUT, DDGS_TIMEOUT) + 2):
            name = futures[future]
            try:
                results = future.result()
                layers_tried.append(f"{name}({len(results)})")
                all_results.extend(results)
            except:
                layers_tried.append(f"{name}(err)")

    # Dedup and rerank all results
    reranked = dedup_and_rerank(all_results, query)
    ms = int((time.time() - t0) * 1000)
    _metrics_log(query, f"parallel", len(reranked), ms)
    return {"results": reranked[:num], "source": "parallel",
            "layers_tried": layers_tried, "cached": False,
            "total": len(reranked), "latency_ms": ms}

# === MAIN SEARCH ===
def search(query: str, num: int = DEFAULT_NUM, deep: bool = False, news: bool = False,
           fast: bool = False, extract: bool = False, expand: bool = False,
           save: bool = False, knowledge_only: bool = False) -> dict:

    # Check knowledge store first (unless deep/news)
    if not deep and not news:
        km = knowledge_search(query)
        if km and km["confidence"] >= 0.5 and km["similarity"] >= 0.5:
            age = _knowledge_age_str(km["entry"]["saved_at"])
            result = {
                "results": km["entry"]["results"][:num],
                "source": f"knowledge:{km['meta']['category']}",
                "layers_tried": [f"knowledge({km['confidence']:.0%},{age})"],
                "cached": True, "total": len(km["entry"]["results"]),
                "latency_ms": 0,
                "knowledge_info": f"confidence={km['confidence']:.0%} age={age} category={km['meta']['category']}",
            }
            if extract:
                result["results"] = extract_top_results(result["results"], min(num, EXTRACT_NUM))
            _metrics_log(query, f"knowledge:{km['meta']['category']}", len(result["results"]), 0, cached=True)
            return result

    if knowledge_only:
        return {"results": [], "source": "knowledge:miss", "layers_tried": ["knowledge(miss)"],
                "cached": False, "total": 0, "latency_ms": 0,
                "error": "No matching knowledge found."}

    # Query expansion
    expanded_queries = []
    if expand:
        expanded_queries = expand_query(query)

    # Check cache
    if not deep:
        for src in ["searxng", "ddgs", "ddgr", "brave_api"]:
            cached = _cache_get(query, src, news)
            if cached:
                _metrics_log(query, f"cache:{src}", len(cached), 0, cached=True)
                result = {"results": cached[:num], "source": f"cache:{src}",
                         "layers_tried": [f"{src}(cached)"], "cached": True,
                         "total": len(cached), "latency_ms": 0}
                if extract:
                    result["results"] = extract_top_results(result["results"], min(num, EXTRACT_NUM))
                return result

    # Fast parallel mode
    if fast:
        result = search_parallel(query, num, news)
        # Also search expanded queries in parallel
        if expanded_queries:
            for eq in expanded_queries:
                extra = search_parallel(eq, num // 2, news)
                result["results"].extend(extra.get("results", []))
            result["results"] = dedup_and_rerank(result["results"], query)[:num]
            result["expanded_queries"] = expanded_queries
        if extract:
            result["results"] = extract_top_results(result["results"], min(num, EXTRACT_NUM))
        if save and result["results"]:
            knowledge_save(query, result["results"])
            result["knowledge_saved"] = True
        return result

    # Sequential with fallback
    layers = [
        ("searxng", lambda q: search_searxng(q, num * 2, news)),
        ("brave_api", lambda q: search_brave_api(q, num, news)),
        ("ddgs", lambda q: search_ddgs(q, num, news)),
        ("ddgr", lambda q: search_ddgr(q, num)),
    ]

    t0 = time.time()
    layers_tried, all_results = [], []

    for name, fn in layers:
        try:
            lt = time.time()
            results = fn(query)
            layer_ms = int((time.time() - lt) * 1000)
            layers_tried.append(f"{name}({layer_ms}ms,{len(results)})")

            if results:
                _cache_set(query, name, results)
                if deep:
                    all_results.extend(results)
                    continue

                # Also run expanded queries
                if expanded_queries:
                    for eq in expanded_queries:
                        extra = fn(eq)
                        results.extend(extra)

                reranked = dedup_and_rerank(results, query)
                ms = int((time.time() - t0) * 1000)
                _metrics_log(query, name, len(reranked), ms)
                result = {"results": reranked[:num], "source": name,
                         "layers_tried": layers_tried, "cached": False,
                         "total": len(reranked), "latency_ms": ms}
                if expanded_queries:
                    result["expanded_queries"] = expanded_queries
                if extract:
                    result["results"] = extract_top_results(result["results"], min(num, EXTRACT_NUM))
                if save and result["results"]:
                    knowledge_save(query, result["results"])
                    result["knowledge_saved"] = True
                return result
            else:
                _metrics_log(query, name, 0, layer_ms, error="empty")
        except Exception as e:
            layers_tried.append(f"{name}(error)")

    ms = int((time.time() - t0) * 1000)

    if deep and all_results:
        if expanded_queries:
            for eq in expanded_queries:
                for _, fn in layers:
                    try: all_results.extend(fn(eq))
                    except: pass
        reranked = dedup_and_rerank(all_results, query)
        _metrics_log(query, "deep_merge", len(reranked), ms)
        result = {"results": reranked[:num * 3], "source": "deep_merge",
                 "layers_tried": layers_tried, "cached": False,
                 "total": len(reranked), "latency_ms": ms}
        if expanded_queries: result["expanded_queries"] = expanded_queries
        if extract: result["results"] = extract_top_results(result["results"], min(num, EXTRACT_NUM))
        if save and result["results"]:
            knowledge_save(query, result["results"])
            result["knowledge_saved"] = True
        return result

    return {"results": [], "source": "none", "layers_tried": layers_tried,
            "cached": False, "total": 0, "latency_ms": ms,
            "error": "All search layers failed."}

# === FORMATTERS ===
def format_json(data: dict) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)

def format_markdown(data: dict) -> str:
    lines = [f"# Search Results ({data['source']}, {data['total']} results, {data.get('latency_ms',0)}ms)"]
    lines.append(f"Layers: {' → '.join(data['layers_tried'])}")
    if data.get("expanded_queries"):
        lines.append(f"Expanded: {data['expanded_queries']}")
    lines.append("")
    for i, r in enumerate(data["results"], 1):
        multi = " 🔄" if r.get("multi_source") else ""
        lines.append(f"## {i}. {r['title']}{multi}")
        lines.append(f"🔗 {r['url']}")
        if r.get("content"): lines.append(f"> {r['content']}")
        if r.get("engines"): lines.append(f"_Engines: {', '.join(r['engines'])} | Score: {r.get('score',0):.1f}_")
        if r.get("extracted"):
            lines.append(f"\n📄 **Extracted content** ({r.get('extracted_len',0)} chars):")
            lines.append(f"```\n{r['extracted'][:800]}\n```")
        lines.append("")
    return "\n".join(lines)

def format_compact(data: dict) -> str:
    ms = data.get('latency_ms', 0)
    lines = [f"[{data['source']}] {data['total']} results ({ms}ms) — {' → '.join(data['layers_tried'])}"]
    if data.get("knowledge_info"):
        lines.append(f"  📚 Knowledge: {data['knowledge_info']}")
    if data.get("knowledge_saved"):
        lines.append(f"  💾 Saved to knowledge store")
    if data.get("expanded_queries"):
        lines.append(f"  📝 Expanded: {data['expanded_queries']}")
    for i, r in enumerate(data["results"], 1):
        multi = " 🔄" if r.get("multi_source") else ""
        score = f" [{r.get('score',0):.0f}]" if r.get("score", 0) > 1 else ""
        lines.append(f"{i}. {r['title']}{multi}{score}")
        lines.append(f"   {r['url']}")
        if r.get("content"): lines.append(f"   {r['content'][:120]}")
        if r.get("extracted"):
            lines.append(f"   📄 [{r.get('extracted_len',0)} chars extracted]")
    return "\n".join(lines)

# === HEALTH CHECK ===
def health_check() -> dict:
    status = {}
    # SearXNG
    try:
        t0 = time.time()
        req = urllib.request.Request(f"{SEARXNG_URL}?q=test&format=json", headers={"User-Agent": "OpenClaw/4.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
        engines = set()
        for item in data.get("results",[]): engines.update(item.get("engines",[]))
        ms = int((time.time()-t0)*1000)
        status["searxng"] = f"✅ UP ({ms}ms, {len(data.get('results',[]))} results, engines: {', '.join(sorted(engines))})"
        unr = data.get("unresponsive_engines",[])
        if unr: status["searxng_issues"] = f"⚠️ Down: {[e[0]+':'+e[1][:20] for e in unr]}"
    except Exception as e: status["searxng"] = f"❌ DOWN ({e})"

    # Brave API
    if BRAVE_API_KEY:
        try:
            t0 = time.time()
            r = search_brave_api("test", 1)
            ms = int((time.time()-t0)*1000)
            status["brave_api"] = f"✅ OK ({ms}ms)" if r else f"⚠️ EMPTY ({ms}ms)"
        except Exception as e: status["brave_api"] = f"❌ ERROR ({e})"
    else:
        status["brave_api"] = "⚪ No API key (set BRAVE_API_KEY)"

    # DDGS
    try:
        t0 = time.time()
        from ddgs import DDGS; r = DDGS(timeout=5).text("test", max_results=1)
        ms = int((time.time()-t0)*1000)
        status["ddgs"] = f"✅ OK ({ms}ms, {len(r)})"
    except Exception as e: status["ddgs"] = f"❌ ({e})"

    # ddgr
    try:
        t0 = time.time()
        proc = subprocess.run(["ddgr","--noprompt","--num","1","test"], capture_output=True, text=True, timeout=10)
        ms = int((time.time()-t0)*1000)
        status["ddgr"] = f"✅ OK ({ms}ms)" if proc.returncode == 0 else f"⚠️ exit {proc.returncode}"
    except FileNotFoundError: status["ddgr"] = "❌ NOT INSTALLED"
    except Exception as e: status["ddgr"] = f"❌ ({e})"

    # Qwen3-8B (for expand)
    try:
        t0 = time.time()
        urllib.request.urlopen("http://localhost:28200/v1/models", timeout=3)
        ms = int((time.time()-t0)*1000)
        status["qwen3_8b"] = f"✅ UP ({ms}ms) — query expansion ready"
    except: status["qwen3_8b"] = "⚠️ DOWN — query expansion disabled"

    # Cache
    if CACHE_DIR.exists():
        files = list(CACHE_DIR.glob("*.json"))
        status["cache"] = f"📦 {len(files)} entries, {sum(f.stat().st_size for f in files)//1024}KB"
    else: status["cache"] = "📦 empty"

    # Knowledge Store
    index = _load_knowledge_index()
    if index:
        cats = {}
        for m in index.values():
            c = m.get("category", "tech")
            conf = _knowledge_confidence(m.get("saved_at",""), c)
            cats.setdefault(c, []).append(conf)
        parts = [f"{c}:{len(v)}({sum(1 for x in v if x>0.3)} fresh)" for c,v in cats.items()]
        status["knowledge"] = f"📚 {len(index)} entries — {', '.join(parts)}"
    else:
        status["knowledge"] = "📚 empty"

    return status

# === CLI ===
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="🔍 Web Search v4 — multi-layer + extract + rerank")
    parser.add_argument("query", nargs="*")
    parser.add_argument("--num", "-n", type=int, default=DEFAULT_NUM)
    parser.add_argument("--format", "-f", choices=["json","markdown","compact"], default="compact")
    parser.add_argument("--deep", "-d", action="store_true", help="All layers, merged + deduped")
    parser.add_argument("--fast", action="store_true", help="Parallel — all layers at once")
    parser.add_argument("--extract", "-x", action="store_true", help="Fetch content from top results")
    parser.add_argument("--expand", "-e", action="store_true", help="Query expansion via local LLM")
    parser.add_argument("--news", action="store_true")
    parser.add_argument("--health", action="store_true")
    parser.add_argument("--metrics", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--clear-cache", action="store_true")
    parser.add_argument("--no-validate", action="store_true", help="Skip query quality check (bypass strict validation)")
    parser.add_argument("--save", "-s", action="store_true", help="Save results to knowledge store")
    parser.add_argument("--knowledge", "-k", action="store_true", help="Search knowledge store only (no web)")
    parser.add_argument("--knowledge-list", action="store_true", help="List all knowledge entries")
    parser.add_argument("--knowledge-prune", action="store_true", help="Remove expired knowledge")
    args = parser.parse_args()

    if args.knowledge_list:
        print(knowledge_list()); sys.exit(0)
    if args.knowledge_prune:
        n = knowledge_prune()
        print(f"🗑️ Pruned {n} expired entries")
        print(knowledge_list()); sys.exit(0)
    if args.health:
        print("🔍 Web Search v5 Health Check"); print("=" * 50)
        for k,v in health_check().items(): print(f"  {k}: {v}")
        sys.exit(0)
    if args.metrics: print(metrics_report()); sys.exit(0)
    if args.clear_cache:
        if CACHE_DIR.exists():
            import shutil; c = len(list(CACHE_DIR.glob("*.json"))); shutil.rmtree(CACHE_DIR)
            print(f"🗑️ Cleared {c} entries")
        sys.exit(0)
    if not args.query: parser.print_help(); sys.exit(1)

    query = " ".join(args.query)

    # Query quality check (strict by default)
    if not args.no_validate:
        qv = validate_query(query)
        if qv["warning"]:
            print(qv["warning"])
            _metrics_log(query, "quality_check", 0, 0, error=f"low_quality:{qv['dimensions']}_dims")
            print("❌ Query refusée. Reformuler avec plus de contexte (ou --no-validate pour bypass).")
            sys.exit(2)
    fmt = "json" if args.json else args.format
    data = search(query, num=args.num, deep=args.deep, news=args.news,
                  fast=args.fast, extract=args.extract, expand=args.expand,
                  save=args.save, knowledge_only=args.knowledge)

    print(format_json(data) if fmt == "json" else format_markdown(data) if fmt == "markdown" else format_compact(data))
    sys.exit(0 if data["results"] else 1)
