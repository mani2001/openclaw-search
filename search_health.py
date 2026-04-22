#!/usr/bin/env python3
"""
search_health.py — Cron health-check pour le système de recherche web
Conçu pour être exécuté par cron_runner.py (JSON output, alert-only)

Usage:
  python3 scripts/search_health.py          # full check
  python3 scripts/search_health.py --json   # JSON output pour cron
"""

import json
import os
import sys
import time
import subprocess
import urllib.request

SEARXNG_URL = "http://localhost:8888/search"
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")

def check_searxng() -> dict:
    try:
        t0 = time.time()
        req = urllib.request.Request(f"{SEARXNG_URL}?q=health+check&format=json",
                                     headers={"User-Agent": "OpenClaw-HealthCheck/1.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
        ms = int((time.time() - t0) * 1000)
        engines = set()
        for item in data.get("results", []):
            engines.update(item.get("engines", []))
        unresponsive = [e[0] for e in data.get("unresponsive_engines", [])]
        return {
            "status": "ok" if len(engines) >= 2 else "degraded",
            "latency_ms": ms,
            "results": len(data.get("results", [])),
            "engines_active": sorted(engines),
            "engines_down": unresponsive,
        }
    except Exception as e:
        return {"status": "down", "error": str(e)}

def check_ddgs() -> dict:
    try:
        t0 = time.time()
        from ddgs import DDGS
        r = DDGS(timeout=5).text("health check", max_results=1)
        ms = int((time.time() - t0) * 1000)
        return {"status": "ok" if r else "empty", "latency_ms": ms, "results": len(r)}
    except Exception as e:
        return {"status": "down", "error": str(e)}

def check_ddgr() -> dict:
    try:
        t0 = time.time()
        proc = subprocess.run(["ddgr", "--noprompt", "--num", "1", "health check"],
                            capture_output=True, text=True, timeout=10)
        ms = int((time.time() - t0) * 1000)
        return {"status": "ok" if proc.returncode == 0 else "error", "latency_ms": ms}
    except FileNotFoundError:
        return {"status": "not_installed"}
    except Exception as e:
        return {"status": "down", "error": str(e)}

def check_tavily() -> dict:
    if not TAVILY_API_KEY:
        return {"status": "no_key"}
    try:
        t0 = time.time()
        from tavily import TavilyClient
        client = TavilyClient(api_key=TAVILY_API_KEY)
        response = client.search(query="health check", max_results=1, search_depth="basic")
        ms = int((time.time() - t0) * 1000)
        results_count = len(response.get("results", []))
        return {"status": "ok" if results_count > 0 else "empty", "latency_ms": ms, "results": results_count}
    except Exception as e:
        return {"status": "down", "error": str(e)}

def main():
    results = {
        "searxng": check_searxng(),
        "tavily": check_tavily(),
        "ddgs": check_ddgs(),
        "ddgr": check_ddgr(),
    }
    
    # Count healthy layers
    healthy = sum(1 for v in results.values() if v["status"] == "ok")
    total = len(results)
    
    # Alert level
    if healthy == 0:
        alert = "critical"
    elif healthy < total:
        alert = "warning"
    else:
        alert = "ok"
    
    output = {
        "service": "web_search",
        "alert": alert,
        "healthy_layers": f"{healthy}/{total}",
        "layers": results,
        "message": f"Search: {healthy}/{total} layers healthy" + (
            f" — DOWN: {[k for k,v in results.items() if v['status'] != 'ok']}" if alert != "ok" else ""
        ),
    }
    
    if "--json" in sys.argv:
        print(json.dumps(output, indent=2))
    else:
        print(f"🔍 Search Health: {output['healthy_layers']} layers healthy [{alert.upper()}]")
        for name, info in results.items():
            icon = "✅" if info["status"] == "ok" else "❌" if info["status"] == "down" else "⚠️"
            ms = f" ({info.get('latency_ms', '?')}ms)" if "latency_ms" in info else ""
            extra = f" — engines: {', '.join(info.get('engines_active', []))}" if info.get("engines_active") else ""
            down = f" — DOWN: {info.get('engines_down')}" if info.get("engines_down") else ""
            print(f"  {icon} {name}: {info['status']}{ms}{extra}{down}")
    
    sys.exit(0 if alert == "ok" else 1)

if __name__ == "__main__":
    main()
