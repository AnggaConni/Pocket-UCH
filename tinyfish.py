"""
TinyFish Search/Fetch integration for Pocket-UCH.

Important:
- Uses ONLY TinyFish Search + Fetch.
- Never calls TinyFish Agent or Browser.
- No LLM/API model is used by Pocket-UCH itself.
- Requires TINYFISH_API_KEY only when enabled.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import requests

TIMEOUT = 45
HEADERS_BASE = {
    "Accept": "application/json",
    "User-Agent": "Pocket-UCH/0.1 (+https://github.com/AnggaConni/Pocket-UCH)",
}

THREAT_KEYWORDS = {
    "coastal_erosion": ["coastal erosion", "shoreline erosion", "erosion", "retreating shoreline"],
    "storm_wave": ["storm surge", "storm wave", "extreme wave", "high waves", "rough seas", "cyclone", "typhoon"],
    "sediment": ["sediment", "sedimentation", "siltation", "buried", "exposed seabed", "scour"],
    "anchoring": ["anchoring", "anchor damage", "anchor", "mooring damage"],
    "fishing": ["bottom trawling", "trawling", "fishing gear", "ghost gear", "fishing activity"],
    "dredging": ["dredging", "dredged", "dredge"],
    "construction": ["coastal construction", "port expansion", "reclamation", "breakwater", "offshore construction"],
    "pollution": ["oil spill", "marine pollution", "plastic pollution", "wastewater", "contamination"],
    "looting": ["looting", "salvage", "illegal salvage", "treasure hunting"],
    "disturbance": ["disturbance", "damage", "damaged", "collapsed", "displaced", "destruction"],
}

RECOMMENDATIONS = {
    "coastal_erosion": "Review recent satellite/coastline change and consider a site condition survey.",
    "storm_wave": "Defer routine diving during hazardous sea conditions and reassess after conditions normalize.",
    "sediment": "Compare seabed/sediment conditions with the previous baseline or survey.",
    "anchoring": "Check vessel/anchoring activity near the site and consider protective buoyage or access controls.",
    "fishing": "Review fishing activity and potential gear interaction with the archaeological site.",
    "dredging": "Check project boundaries and sediment plume risk; coordinate with relevant authorities before field work.",
    "construction": "Review the development footprint and potential direct/indirect impacts on the site's buffer.",
    "pollution": "Increase condition monitoring and review available marine-water observations.",
    "looting": "Treat as a heritage-security signal and verify through authorised site monitoring channels.",
    "disturbance": "Schedule verification against the latest available survey or imagery before concluding site damage.",
}


def _api_headers(api_key: str) -> dict[str, str]:
    return {**HEADERS_BASE, "X-API-Key": api_key}


def search(api_key: str, endpoint: str, query: str, limit: int = 5) -> list[dict[str, Any]]:
    response = requests.get(
        endpoint,
        params={"query": query},
        headers=_api_headers(api_key),
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    results = payload.get("results", [])
    rows: list[dict[str, Any]] = []
    for item in results[:limit]:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not url:
            continue
        rows.append(
            {
                "title": item.get("title") or "",
                "snippet": item.get("snippet") or "",
                "site_name": item.get("site_name") or "",
                "url": url,
                "query": query,
            }
        )
    return rows


def fetch(api_key: str, endpoint: str, urls: list[str]) -> list[dict[str, Any]]:
    if not urls:
        return []
    response = requests.post(
        endpoint,
        json={"urls": urls},
        headers={**_api_headers(api_key), "Content-Type": "application/json"},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    return [x for x in payload.get("results", []) if isinstance(x, dict)]


def _keyword_hits(text: str) -> list[str]:
    lowered = text.lower()
    hits = []
    for category, words in THREAT_KEYWORDS.items():
        if any(re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", lowered) for word in words):
            hits.append(category)
    return hits


def _recommendation(categories: list[str]) -> str:
    if not categories:
        return "Keep this item as contextual intelligence; no deterministic action trigger was identified."
    return " ".join(RECOMMENDATIONS[c] for c in categories[:2])


def classify(title: str, snippet: str, text: str) -> tuple[list[str], str]:
    categories = _keyword_hits(f"{title}\n{snippet}\n{text}"[:60000])
    return categories, _recommendation(categories)


def collect(
    cfg: dict[str, Any],
    sites: list[dict[str, Any]],
    state: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    block = cfg.get("sources", {}).get("tinyfish", {})
    if not block.get("enabled"):
        return [], state, []

    api_key = __import__("os").environ.get("TINYFISH_API_KEY")
    if not api_key:
        return [], state, [{"source": "TinyFish", "ok": False, "message": "TINYFISH_API_KEY not set"}]

    today = datetime.now(timezone.utc).date().isoformat()
    if block.get("run_once_per_day", True) and state.get("last_run_date") == today:
        return state.get("signals", []), state, [{"source": "TinyFish", "ok": True, "message": "already ran today"}]

    max_searches = int(block.get("max_searches_per_run", 6))
    max_results = int(block.get("max_results_per_query", 5))
    max_fetches = int(block.get("max_fetches_per_run", 12))

    queries = list(block.get("queries", []))

    # Prioritise a small number of site-specific searches when site names exist.
    for site in sites:
        if len(queries) >= max_searches:
            break
        name = str(site.get("name", "")).strip()
        if name:
            country = str(site.get("country", "")).strip()
            queries.append(f'"{name}" underwater cultural heritage monitoring {country}'.strip())

    queries = queries[:max_searches]

    discovered: list[dict[str, Any]] = []
    source_reports: list[dict[str, Any]] = []

    try:
        for query in queries:
            discovered.extend(search(api_key, block["search_endpoint"], query, max_results))
        source_reports.append(
            {"source": "TinyFish Search", "ok": True, "message": f"{len(discovered)} search result(s)"}
        )
    except Exception as exc:
        source_reports.append({"source": "TinyFish Search", "ok": False, "message": str(exc)})

    unique: dict[str, dict[str, Any]] = {}
    for row in discovered:
        unique.setdefault(row["url"], row)

    urls = list(unique)[:max_fetches]
    fetched: dict[str, dict[str, Any]] = {}

    if urls:
        try:
            for row in fetch(api_key, block["fetch_endpoint"], urls):
                if row.get("url"):
                    fetched[row["url"]] = row
            source_reports.append(
                {"source": "TinyFish Fetch", "ok": True, "message": f"{len(fetched)} page(s) fetched"}
            )
        except Exception as exc:
            source_reports.append({"source": "TinyFish Fetch", "ok": False, "message": str(exc)})

    signals: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc).isoformat()

    for url, discovery in unique.items():
        page = fetched.get(url, {})
        text = str(page.get("text") or "")
        title = str(page.get("title") or discovery.get("title") or "")
        snippet = str(discovery.get("snippet") or "")
        categories, recommendation = classify(title, snippet, text)

        lower_blob = f"{title}\n{snippet}\n{text[:30000]}".lower()
        matched_sites = []
        for site in sites:
            terms = [
                str(site.get("name", "")).strip().lower(),
                str(site.get("country", "")).strip().lower(),
                str(site.get("region", "")).strip().lower(),
            ]
            terms = [x for x in terms if len(x) >= 4]
            if any(term in lower_blob for term in terms):
                matched_sites.append(site.get("id"))

        signals.append(
            {
                "id": f"tinyfish:{abs(hash(url))}",
                "source": "TinyFish Search/Fetch",
                "retrieved_at": now,
                "title": title,
                "snippet": snippet,
                "url": url,
                "source_site": discovery.get("site_name"),
                "query": discovery.get("query"),
                "threat_categories": categories,
                "recommendation": recommendation,
                "matched_site_ids": matched_sites,
            }
        )

    new_state = {
        "last_run_date": today,
        "signals": signals,
    }
    return signals, new_state, source_reports
