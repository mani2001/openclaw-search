#!/usr/bin/env python3
"""
Advanced Search Engine CLI
Combines multiple sources, re-ranks with LLM embeddings, caches results.
"""

import argparse
import json
import re
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from hashlib import md5
from math import sqrt
from pathlib import Path
from typing import Dict, List, Optional, Tuple

__version__ = "1.0.0"

# Integration: pipe results to knowledge_vault.py for long-term storage
# See also: knowledge_vault.py, websearch.py


class SearchEngine:
    def __init__(self, cache_path: str = None):
        self.cache_path = cache_path or str(Path.home() / ".openclaw/workspace/data/search_cache.db")
        self.cache_duration = 24 * 3600  # 24 hours
        self.searxng_url = "http://localhost:8888"
        self.swama_url = "http://localhost:28100"
        self.embed_model = "nomic-ai/nomic-embed-text-v1.5"
        self.chat_model = "mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit"
        
        # Ensure cache directory exists
        Path(self.cache_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_cache_db()

    def _init_cache_db(self):
        """Initialize SQLite cache database."""
        try:
            with sqlite3.connect(self.cache_path) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS searches (
                        query TEXT PRIMARY KEY,
                        results TEXT,
                        timestamp INTEGER,
                        source TEXT
                    )
                """)
                conn.commit()
        except sqlite3.Error:
            pass  # Proceed without cache if DB locked

    def _normalize_query(self, query: str) -> str:
        """Normalize query for cache lookup."""
        return query.lower().strip()

    def _get_cached_results(self, query: str) -> Optional[Dict]:
        """Get cached results if they exist and are fresh."""
        try:
            normalized_query = self._normalize_query(query)
            with sqlite3.connect(self.cache_path) as conn:
                cursor = conn.execute(
                    "SELECT results, timestamp, source FROM searches WHERE query = ?",
                    (normalized_query,)
                )
                row = cursor.fetchone()
                if row and (time.time() - row[1]) < self.cache_duration:
                    return {
                        'results': json.loads(row[0]),
                        'source': row[2],
                        'cached': True
                    }
        except (sqlite3.Error, json.JSONDecodeError):
            pass
        return None

    def _cache_results(self, query: str, results: List[Dict], source: str):
        """Cache search results."""
        try:
            normalized_query = self._normalize_query(query)
            with sqlite3.connect(self.cache_path) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO searches (query, results, timestamp, source) VALUES (?, ?, ?, ?)",
                    (normalized_query, json.dumps(results), int(time.time()), source)
                )
                conn.commit()
        except sqlite3.Error:
            pass  # Proceed without caching if DB locked

    def _make_request(self, url: str, data: Dict = None, headers: Dict = None) -> Optional[Dict]:
        """Make HTTP request using urllib."""
        try:
            req_headers = {'Content-Type': 'application/json'} if data else {}
            if headers:
                req_headers.update(headers)
            
            if data:
                data_bytes = json.dumps(data).encode('utf-8')
                req = urllib.request.Request(url, data=data_bytes, headers=req_headers)
            else:
                req = urllib.request.Request(url, headers=req_headers)
            
            with urllib.request.urlopen(req, timeout=10) as response:
                return json.loads(response.read().decode('utf-8'))
        except Exception:
            return None

    def _search_searxng(self, query: str, num_results: int = 10) -> List[Dict]:
        """Search using local SearXNG instance."""
        try:
            encoded_query = urllib.parse.quote_plus(query)
            url = f"{self.searxng_url}/search?q={encoded_query}&format=json&engines=google,bing,duckduckgo&safesearch=1"
            
            with urllib.request.urlopen(url, timeout=10) as response:
                data = json.loads(response.read().decode('utf-8'))
                
                results = []
                for result in data.get('results', [])[:num_results]:
                    results.append({
                        'title': result.get('title', ''),
                        'url': result.get('url', ''),
                        'snippet': result.get('content', ''),
                        'engine': result.get('engine', 'searxng')
                    })
                return results
        except Exception:
            return []

    def _search_brave(self, query: str, num_results: int = 10) -> List[Dict]:
        """Search using Brave API or websearch.py fallback."""
        # Try direct Brave API first if key is available
        brave_key = self._get_env_var('BRAVE_API_KEY')
        if brave_key:
            try:
                headers = {'X-Subscription-Token': brave_key}
                url = f"https://api.search.brave.com/res/v1/web/search?q={urllib.parse.quote_plus(query)}&count={num_results}"
                
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=10) as response:
                    data = json.loads(response.read().decode('utf-8'))
                    
                    results = []
                    for result in data.get('web', {}).get('results', []):
                        results.append({
                            'title': result.get('title', ''),
                            'url': result.get('url', ''),
                            'snippet': result.get('description', ''),
                            'engine': 'brave'
                        })
                    return results
            except Exception:
                pass
        
        # Fallback to websearch.py if available
        try:
            websearch_path = Path.home() / ".openclaw/workspace/scripts/websearch.py"
            if websearch_path.exists():
                result = subprocess.run([
                    sys.executable, str(websearch_path), query, str(num_results)
                ], capture_output=True, text=True, timeout=30)
                
                if result.returncode == 0:
                    # Parse websearch.py output (assuming it returns JSON)
                    try:
                        data = json.loads(result.stdout)
                        return data if isinstance(data, list) else []
                    except json.JSONDecodeError:
                        # Parse plain text output
                        lines = result.stdout.strip().split('\n')
                        results = []
                        for i, line in enumerate(lines[:num_results]):
                            if line.strip():
                                results.append({
                                    'title': f"Result {i+1}",
                                    'url': line.strip(),
                                    'snippet': '',
                                    'engine': 'brave'
                                })
                        return results
        except Exception:
            pass
        
        return []

    def _search_tavily(self, query: str, num_results: int = 10) -> List[Dict]:
        """Search using Tavily API."""
        tavily_key = self._get_env_var('TAVILY_API_KEY')
        if not tavily_key:
            return []
        try:
            from tavily import TavilyClient
            client = TavilyClient(api_key=tavily_key)
            response = client.search(
                query=query,
                max_results=num_results,
                search_depth="basic",
            )
            results = []
            for result in response.get('results', []):
                results.append({
                    'title': result.get('title', ''),
                    'url': result.get('url', ''),
                    'snippet': result.get('content', ''),
                    'engine': 'tavily',
                })
            return results
        except Exception:
            return []

    def _get_env_var(self, name: str) -> Optional[str]:
        """Get environment variable."""
        import os
        return os.environ.get(name)

    def _get_embeddings(self, texts: List[str]) -> List[List[float]]:
        """Get embeddings from Swama."""
        data = {
            "model": self.embed_model,
            "input": texts
        }
        
        result = self._make_request(f"{self.swama_url}/v1/embeddings", data)
        if result and 'data' in result:
            return [item['embedding'] for item in result['data']]
        return []

    def _cosine_similarity(self, a: List[float], b: List[float]) -> float:
        """Compute cosine similarity between two vectors."""
        if not a or not b or len(a) != len(b):
            return 0.0
        
        dot_product = sum(x * y for x, y in zip(a, b))
        magnitude_a = sqrt(sum(x * x for x in a))
        magnitude_b = sqrt(sum(x * x for x in b))
        
        if magnitude_a == 0 or magnitude_b == 0:
            return 0.0
        
        return dot_product / (magnitude_a * magnitude_b)

    def _rerank_results(self, query: str, results: List[Dict]) -> List[Dict]:
        """Re-rank results using embeddings."""
        if not results:
            return results
        
        try:
            # Get query embedding
            query_embedding = self._get_embeddings([query])
            if not query_embedding:
                return results
            
            # Get result embeddings
            result_texts = [f"{r.get('title', '')} {r.get('snippet', '')}" for r in results]
            result_embeddings = self._get_embeddings(result_texts)
            
            if len(result_embeddings) != len(results):
                return results
            
            # Calculate similarities and add scores
            scored_results = []
            for result, embedding in zip(results, result_embeddings):
                similarity = self._cosine_similarity(query_embedding[0], embedding)
                result_copy = result.copy()
                result_copy['relevance_score'] = similarity
                scored_results.append(result_copy)
            
            # Sort by relevance score (descending)
            scored_results.sort(key=lambda x: x.get('relevance_score', 0), reverse=True)
            return scored_results
            
        except Exception:
            # Return original order if re-ranking fails
            return results

    def _fetch_url_content(self, url: str) -> str:
        """Fetch URL content using curl."""
        try:
            result = subprocess.run([
                'curl', '-s', '-L', '-m', '10', url
            ], capture_output=True, text=True, timeout=15)
            
            if result.returncode == 0:
                return self._extract_text(result.stdout)
        except Exception:
            pass
        return ""

    def _extract_text(self, html: str) -> str:
        """Extract readable text from HTML."""
        # Remove script and style elements
        html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)
        
        # Remove HTML tags
        text = re.sub(r'<[^>]+>', '', html)
        
        # Clean up whitespace
        text = re.sub(r'\s+', ' ', text)
        text = text.strip()
        
        return text

    def _chunk_text(self, text: str) -> List[str]:
        """Chunk text into paragraphs."""
        # Split by double newlines or periods followed by whitespace
        chunks = re.split(r'\n\s*\n|\.(?:\s+)', text)
        
        # Filter out very short chunks and limit length
        return [chunk.strip() for chunk in chunks 
                if len(chunk.strip()) > 50 and len(chunk.strip()) < 1000]

    def _get_relevant_chunks(self, query: str, text: str, max_chunks: int = 3) -> List[str]:
        """Get most relevant text chunks for query."""
        chunks = self._chunk_text(text)
        if not chunks:
            return []
        
        try:
            # Get embeddings for query and chunks
            query_embedding = self._get_embeddings([query])
            if not query_embedding:
                return chunks[:max_chunks]
            
            chunk_embeddings = self._get_embeddings(chunks)
            if not chunk_embeddings:
                return chunks[:max_chunks]
            
            # Score and sort chunks
            scored_chunks = []
            for chunk, embedding in zip(chunks, chunk_embeddings):
                similarity = self._cosine_similarity(query_embedding[0], embedding)
                scored_chunks.append((similarity, chunk))
            
            scored_chunks.sort(key=lambda x: x[0], reverse=True)
            return [chunk for _, chunk in scored_chunks[:max_chunks]]
            
        except Exception:
            return chunks[:max_chunks]

    def _summarize_content(self, query: str, content: str) -> str:
        """Summarize content using Swama LLM."""
        data = {
            "model": self.chat_model,
            "messages": [
                {
                    "role": "system",
                    "content": "Summarize the following search results concisely. Focus on answering the query. Include sources."
                },
                {
                    "role": "user", 
                    "content": f"Query: {query}\n\nSearch Results:\n{content}"
                }
            ],
            "max_tokens": 500,
            "temperature": 0.3
        }
        
        result = self._make_request(f"{self.swama_url}/v1/chat/completions", data)
        if result and 'choices' in result and result['choices']:
            return result['choices'][0]['message']['content']
        return "Summary unavailable - Swama LLM not accessible"

    def get_cache_stats(self) -> Dict:
        """Get cache statistics."""
        try:
            with sqlite3.connect(self.cache_path) as conn:
                cursor = conn.execute("SELECT COUNT(*), MIN(timestamp), MAX(timestamp) FROM searches")
                row = cursor.fetchone()
                
                total_queries = row[0] if row[0] else 0
                oldest = row[1] if row[1] else 0
                newest = row[2] if row[2] else 0
                
                # Get cache file size
                cache_size = Path(self.cache_path).stat().st_size if Path(self.cache_path).exists() else 0
                
                return {
                    'total_queries': total_queries,
                    'cache_size_mb': round(cache_size / (1024 * 1024), 2),
                    'oldest_entry': time.strftime('%Y-%m-%d %H:%M', time.localtime(oldest)) if oldest else 'None',
                    'newest_entry': time.strftime('%Y-%m-%d %H:%M', time.localtime(newest)) if newest else 'None'
                }
        except sqlite3.Error:
            return {'error': 'Cache database unavailable'}

    def search(self, query: str, source: str = 'auto', num_results: int = 5, 
               use_cache: bool = True, cache_only: bool = False) -> Dict:
        """Perform search with specified parameters."""
        
        # Check cache first
        if use_cache:
            cached = self._get_cached_results(query)
            if cached:
                cached['results'] = cached['results'][:num_results]  # Limit results
                return cached
            elif cache_only:
                return {'results': [], 'source': 'cache', 'error': 'No cached results found'}
        
        if cache_only:
            return {'results': [], 'source': 'cache', 'error': 'Cache-only mode but no cached results'}
        
        results = []
        used_source = 'none'
        
        # Try SearXNG first (unless explicitly told to use brave only)
        if source in ['auto', 'searxng', 'all']:
            results = self._search_searxng(query, num_results * 2)  # Get more for re-ranking
            if results:
                used_source = 'searxng'
        
        # Fallback to Brave if needed
        if (not results or len(results) < 3) and source in ['auto', 'brave', 'all']:
            brave_results = self._search_brave(query, num_results * 2)
            if source == 'all':
                results.extend(brave_results)
                used_source = 'searxng+brave' if used_source == 'searxng' else 'brave'
            elif not results:
                results = brave_results
                used_source = 'brave'

        # Try Tavily if applicable
        if source == 'tavily' or source == 'all' or (source == 'auto' and (not results or len(results) < 3) and self._get_env_var('TAVILY_API_KEY')):
            tavily_results = self._search_tavily(query, num_results * 2)
            if source == 'tavily':
                results = tavily_results
                used_source = 'tavily'
            elif source == 'all' and tavily_results:
                results.extend(tavily_results)
                used_source = (used_source + '+tavily') if used_source != 'none' else 'tavily'
            elif source == 'auto' and (not results or len(results) < 3) and tavily_results:
                if not results:
                    results = tavily_results
                    used_source = 'tavily'
                else:
                    results.extend(tavily_results)
                    used_source = used_source + '+tavily'

        if not results:
            error_msg = "SearXNG unavailable, use --source brave" if source == 'searxng' else "No results found"
            return {'results': [], 'source': used_source, 'error': error_msg}
        
        # Re-rank with embeddings
        results = self._rerank_results(query, results)
        
        # Limit to requested number
        results = results[:num_results]
        
        # Cache results
        if use_cache:
            self._cache_results(query, results, used_source)
        
        return {'results': results, 'source': used_source, 'cached': False}

    def deep_fetch(self, query: str, results: List[Dict], num_urls: int) -> List[Dict]:
        """Fetch and extract content from top N URLs."""
        enhanced_results = []
        
        for i, result in enumerate(results[:num_urls]):
            enhanced_result = result.copy()
            
            print(f"Fetching content from {result['url']}...", file=sys.stderr)
            content = self._fetch_url_content(result['url'])
            
            if content:
                relevant_chunks = self._get_relevant_chunks(query, content, 3)
                enhanced_result['content_chunks'] = relevant_chunks
                enhanced_result['content_available'] = True
            else:
                enhanced_result['content_chunks'] = []
                enhanced_result['content_available'] = False
            
            enhanced_results.append(enhanced_result)
        
        return enhanced_results


def format_results(query: str, search_data: Dict, json_output: bool = False, 
                  deep_results: List[Dict] = None, summary: str = None) -> str:
    """Format search results for display."""
    
    if json_output:
        output_data = {
            'query': query,
            'source': search_data.get('source', 'unknown'),
            'cached': search_data.get('cached', False),
            'results': deep_results or search_data['results'],
            'summary': summary
        }
        return json.dumps(output_data, indent=2)
    
    # Text format
    results = deep_results or search_data['results']
    total = len(results)
    source = search_data.get('source', 'unknown')
    cached_str = " (cached)" if search_data.get('cached') else ""
    
    output = [f'🔍 "{query}" — {total} results from {source}{cached_str}\n']
    
    for i, result in enumerate(results, 1):
        score = result.get('relevance_score', 0)
        score_str = f"[{score:.2f}] " if score > 0 else ""
        
        output.append(f"{i}. {score_str}{result.get('title', 'No title')}")
        output.append(f"   {result.get('url', 'No URL')}")
        
        if result.get('snippet'):
            snippet = result['snippet'][:200] + "..." if len(result['snippet']) > 200 else result['snippet']
            output.append(f"   {snippet}")
        
        # Show deep content if available
        if result.get('content_chunks'):
            output.append(f"   📄 Content preview:")
            for chunk in result['content_chunks']:
                chunk_preview = chunk[:150] + "..." if len(chunk) > 150 else chunk
                output.append(f"      {chunk_preview}")
        elif result.get('content_available') is False:
            output.append(f"   ⚠️  Content fetch failed")
        
        output.append("")
    
    # Add summary if available
    if summary:
        output.extend([
            "📝 Summary:",
            summary,
            ""
        ])
    
    return "\n".join(output)


def main():
    parser = argparse.ArgumentParser(description='Advanced Search Engine CLI')
    parser.add_argument('query', nargs='?', help='Search query')
    parser.add_argument('--deep', type=int, metavar='N', help='Fetch content from top N URLs')
    parser.add_argument('--summarize', action='store_true', help='Summarize findings via LLM')
    parser.add_argument('--source', choices=['searxng', 'brave', 'tavily', 'all', 'auto'],
                       default='auto', help='Search source to use')
    parser.add_argument('--num', type=int, default=5, help='Number of results to show')
    parser.add_argument('--cache-only', action='store_true', help='Search cache only')
    parser.add_argument('--no-cache', action='store_true', help='Skip cache')
    parser.add_argument('--json', action='store_true', help='JSON output format')
    parser.add_argument('--stats', action='store_true', help='Show cache statistics')
    parser.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    
    args = parser.parse_args()
    
    engine = SearchEngine()
    
    # Handle stats command
    if args.stats:
        stats = engine.get_cache_stats()
        if args.json:
            print(json.dumps(stats, indent=2))
        else:
            print("📊 Cache Statistics:")
            for key, value in stats.items():
                formatted_key = key.replace('_', ' ').title()
                print(f"  {formatted_key}: {value}")
        return
    
    if not args.query:
        parser.error("Query required unless using --stats")
    
    # Perform search
    use_cache = not args.no_cache
    search_data = engine.search(
        query=args.query,
        source=args.source,
        num_results=args.num,
        use_cache=use_cache,
        cache_only=args.cache_only
    )
    
    if 'error' in search_data:
        print(f"Error: {search_data['error']}", file=sys.stderr)
        return 1
    
    results = search_data['results']
    deep_results = None
    summary = None
    
    # Deep fetch if requested
    if args.deep and results:
        deep_results = engine.deep_fetch(args.query, results, args.deep)
        results = deep_results
    
    # Summarize if requested
    if args.summarize and results:
        if not deep_results and args.deep:
            # Already have deep results, use them
            content_parts = []
            for result in results:
                if result.get('content_chunks'):
                    content_parts.append(f"Source: {result['url']}\n" + "\n".join(result['content_chunks']))
            content = "\n\n".join(content_parts)
        else:
            # Use snippets only
            content = "\n".join([f"{r.get('title', '')}: {r.get('snippet', '')}" for r in results])
        
        if content:
            summary = engine._summarize_content(args.query, content)
    
    # Format and print results
    output = format_results(args.query, search_data, args.json, deep_results, summary)
    print(output)


if __name__ == '__main__':
    sys.exit(main())