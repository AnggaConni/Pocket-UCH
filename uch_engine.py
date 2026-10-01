"""
Pocket-UCH deterministic external-data engine.

No AI/LLM calls.
Optional sources:
- Copernicus Data Space STAC
- Global Fishing Watch 4Wings
- UNESCO Convention control assessment
"""

from __future__ import annotations

import json
import math
import os
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
import yaml

TIMEOUT = 60
UA = "Pocket-UCH/0.2 (+https://github.com/AnggaConni/Pocket-UCH)"


def _get_json(url: str, *, params=None, headers=None, timeout=TIMEOUT):
    h = {"User-Agent": UA, "Accept": "application/json"}
    if headers:
        h.update(headers)
    r = requests.get(url, params=params or {}, headers=h, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _post_json(url: str, payload: dict, *, headers=None, timeout=TIMEOUT):
    h = {"User-Agent": UA, "Accept": "application/json", "Content-Type": "application/json"}
    if headers:
        h.update(headers)
    r = requests.post(url, json=payload, headers=h, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _circle_polygon(lon: float, lat: float, radius_km: float, vertices: int = 16) -> dict:
    radius_lat = radius_km / 111.32
    radius_lon = radius_km / max(1.0, 111.32 * math.cos(math.radians(lat)))
    coords = []
    for i in range(vertices):
        a = 2 * math.pi * i / vertices
        coords.append([lon + radius_lon * math.cos(a), lat + radius_lat * math.sin(a)])
    coords.append(coords[0])
    return {"type": "Polygon", "coordinates": [coords]}


def query_copernicus(site: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    block = cfg.get("sources", {}).get("copernicus_stac", {})
    if not block.get("enabled"):
        return {"enabled": False}

    lat, lon = site.get("lat"), site.get("lon")
    if lat is None or lon is None:
        return {"enabled": True, "ok": False, "message": "site coordinates missing"}

    days = int(block.get("days_back", 30))
    delta = datetime.now(timezone.utc) - timedelta(days=days)
    radius = float(cfg.get("monitoring", {}).get("satellite_radius_km", 5))
    poly = _circle_polygon(float(lon), float(lat), radius, 12)

    body = {
        "collections": block.get("collections", []),
        "intersects": {
            "type": "Point",
            "coordinates": [float(lon), float(lat)],
        },
        "datetime": f"{delta.isoformat().replace('+00:00','Z')}/{datetime.now(timezone.utc).isoformat().replace('+00:00','Z')}",
        "limit": int(block.get("max_items_per_site", 5)),
        "fields": {"include": ["id", "properties", "collection"]},
    }

    try:
        payload = _post_json(block["url"], body)
        features = payload.get("features", [])
        items = []
        for f in features:
            p = f.get("properties") or {}
            items.append(
                {
                    "id": f.get("id"),
                    "collection": f.get("collection"),
                    "datetime": p.get("datetime"),
                    "cloud_cover": p.get("eo:cloud_cover"),
                }
            )
        return {"enabled": True, "ok": True, "count": len(items), "items": items}
    except Exception as exc:
        return {"enabled": True, "ok": False, "message": str(exc)}



def _bbox(lat: float, lon: float, radius_km: float) -> str:
    dlat = radius_km / 111.32
    dlon = radius_km / max(1.0, 111.32 * math.cos(math.radians(lat)))
    return f"{lon-dlon},{lat-dlat},{lon+dlon},{lat+dlat}"


def _point_in_ring(lon: float, lat: float, ring: list[list[float]]) -> bool:
    inside = False
    if len(ring) < 3:
        return False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        crosses = ((yi > lat) != (yj > lat)) and (
            lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-15) + xi
        )
        if crosses:
            inside = not inside
        j = i
    return inside


def _point_in_geometry(lon: float, lat: float, geometry: dict[str, Any] | None) -> bool:
    if not geometry:
        return False
    kind = geometry.get("type")
    coords = geometry.get("coordinates")
    if kind == "Polygon":
        rings = coords or []
        return bool(rings and _point_in_ring(lon, lat, rings[0]) and not any(_point_in_ring(lon, lat, h) for h in rings[1:]))
    if kind == "MultiPolygon":
        for polygon in coords or []:
            if polygon and _point_in_ring(lon, lat, polygon[0]) and not any(_point_in_ring(lon, lat, h) for h in polygon[1:]):
                return True
    return False


def query_marine_regions(site: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    block = cfg.get("sources", {}).get("marine_regions", {})
    if not block.get("enabled"):
        return {"enabled": False}
    lat, lon = site.get("lat"), site.get("lon")
    if lat is None or lon is None:
        return {"enabled": True, "ok": False, "message": "site coordinates missing"}

    try:
        payload = _get_json(
            block["wfs_url"],
            params={
                "service": "WFS",
                "version": "1.0.0",
                "request": "GetFeature",
                "typeName": "eez_boundaries",
                "bbox": _bbox(float(lat), float(lon), 80),
                "outputFormat": "application/json",
                "maxFeatures": 10,
            },
        )
        matches = []
        for feature in payload.get("features", []):
            if _point_in_geometry(float(lon), float(lat), feature.get("geometry")):
                props = feature.get("properties") or {}
                matches.append(props)
        return {
            "enabled": True,
            "ok": True,
            "source": "Marine Regions",
            "eez_matches": matches[:5],
            "candidate_count": len(payload.get("features", [])),
        }
    except Exception as exc:
        return {"enabled": True, "ok": False, "message": str(exc)}


def query_emodnet(site: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    block = cfg.get("sources", {}).get("emodnet_human_activities", {})
    if not block.get("enabled"):
        return {"enabled": False}
    lat, lon = site.get("lat"), site.get("lon")
    if lat is None or lon is None:
        return {"enabled": True, "ok": False, "message": "site coordinates missing"}

    try:
        cap_url = block["wfs_url"]
        xml = requests.get(
            cap_url,
            params={"SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetCapabilities"},
            headers={"User-Agent": UA},
            timeout=TIMEOUT,
        ).text

        import re
        names = re.findall(r"<(?:[A-Za-z0-9_]+:)?Name>([^<]+)</(?:[A-Za-z0-9_]+:)?Name>", xml)
        titles = re.findall(r"<(?:[A-Za-z0-9_]+:)?Title>([^<]+)</(?:[A-Za-z0-9_]+:)?Title>", xml)
        haystack = [x.lower() for x in names + titles]

        keyword_groups = [
            ("wreck", ["wreck", "wrecks", "cultural", "heritage"]),
            ("dredging", ["dredg"]),
            ("shipping", ["ship", "traffic", "vessel", "port"]),
            ("fishing", ["fish", "trawl"]),
            ("infrastructure", ["cable", "pipeline", "wind", "energy", "construction"]),
            ("waste", ["waste", "dump"]),
        ]

        selected = []
        for name in names:
            low = name.lower()
            score = 0
            for _, words in keyword_groups:
                if any(w in low for w in words):
                    score += 1
            if score:
                selected.append((score, name))
        selected = [x[1] for x in sorted(selected, reverse=True)[:int(block.get("max_layers_per_site", 3))]]

        features = []
        for layer in selected:
            try:
                payload = _get_json(
                    cap_url,
                    params={
                        "service": "WFS",
                        "version": "1.1.0",
                        "request": "GetFeature",
                        "typeName": layer,
                        "bbox": _bbox(float(lat), float(lon), 25),
                        "outputFormat": "application/json",
                        "maxFeatures": 25,
                    },
                    timeout=30,
                )
                for feature in payload.get("features", [])[:25]:
                    props = feature.get("properties") or {}
                    features.append({"layer": layer, "properties": props})
            except Exception:
                continue

        return {
            "enabled": True,
            "ok": True,
            "source": "EMODnet Human Activities",
            "candidate_layers": selected,
            "feature_count": len(features),
            "features": features[:100],
        }
    except Exception as exc:
        return {"enabled": True, "ok": False, "message": str(exc)}


def query_gfw(site: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    block = cfg.get("sources", {}).get("global_fishing_watch", {})
    if not block.get("enabled"):
        return {"enabled": False}

    token = os.environ.get("GFW_API_TOKEN")
    if not token:
        return {"enabled": True, "ok": False, "message": "GFW_API_TOKEN not set"}

    lat, lon = site.get("lat"), site.get("lon")
    if lat is None or lon is None:
        return {"enabled": True, "ok": False, "message": "site coordinates missing"}

    days = int(block.get("days_back", 7))
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days)

    params = [
        ("spatial-resolution", block.get("spatial_resolution", "LOW")),
        ("temporal-resolution", "DAILY"),
        ("spatial-aggregation", "false"),
        ("datasets[0]", "public-global-fishing-effort:latest"),
        ("date-range", f"{start.isoformat()},{end.isoformat()}"),
        ("format", "JSON"),
    ]
    body = {"geojson": _circle_polygon(float(lon), float(lat), float(cfg.get("monitoring", {}).get("fishing_radius_km", 10)))}

    try:
        payload = requests.post(
            block["url"],
            params=params,
            json=body,
            headers={"User-Agent": UA, "Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=120,
        )
        payload.raise_for_status()
        result = payload.json()
        entries = result.get("entries", [])
        fishing_hours = 0.0
        for row in entries:
            try:
                fishing_hours += float(row.get("hours", 0) or 0)
            except (TypeError, ValueError):
                pass
        return {
            "enabled": True,
            "ok": True,
            "days": days,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "fishing_hours": round(fishing_hours, 2),
            "entries": entries[:100],
        }
    except Exception as exc:
        return {"enabled": True, "ok": False, "message": str(exc)}


def load_convention(path: Path) -> dict[str, Any]:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def assess_convention(site: dict[str, Any], linked_events: list[dict[str, Any]], web_signals: list[dict[str, Any]], framework: dict[str, Any]) -> dict[str, Any]:
    rules = framework.get("rules", {})
    articles = framework.get("articles", {})
    categories = set()
    for event in linked_events:
        categories.update(event.get("threat_categories", []))
        if event.get("type") == "earthquake":
            categories.add("major_hazard")
            try:
                if float(event.get("magnitude", 0)) >= 6:
                    categories.add("intrusive_activity")
            except (TypeError, ValueError):
                pass
        if str(event.get("tsunami") or "").lower() not in ("", "tidak berpotensi tsunami", "none", "false"):
            categories.add("major_hazard")
    for signal in web_signals:
        categories.update(signal.get("threat_categories", []))

    trigger_map = {
        "coastal_erosion": ["5", "14", "15", "25", "29"],
        "storm_wave": ["5", "28"],
        "sediment": ["5", "14", "15", "25", "29"],
        "anchoring": ["5", "15", "16", "25"],
        "fishing": ["5", "15", "16", "25", "29"],
        "dredging": ["5", "14", "15", "25", "29"],
        "construction": ["5", "14", "15", "25", "29"],
        "pollution": ["5", "14", "15", "25", "29"],
        "looting": ["2", "13", "14", "15", "16", "17", "18"],
        "commercial_exploitation": ["2", "14", "15", "16", "17", "18"],
        "trafficking": ["2", "14", "17", "18", "19"],
        "major_hazard": ["12", "13", "25", "28", "29"],
        "intrusive_activity": ["1", "3", "4", "25"],
    }

    triggered_rules = set()
    triggered_articles = set()
    for cat in categories:
        for n in trigger_map.get(cat, []):
            if n in rules:
                triggered_rules.add(n)
            if n in articles:
                triggered_articles.add(n)

    site_defaults = {
        "significance": "unknown",
        "vulnerability": "unknown",
        "maritime_zone": "unknown",
        "authorization_status": "unknown",
        "documentation_status": "unknown",
        "monitoring_enabled": False,
        "state_vessel": False,
        "human_remains": False,
        "sensitive_data_policy": "unknown",
        "competent_authority": None,
    }

    profile = dict(site_defaults)
    profile.update(site.get("convention_profile") or {})
    gaps = []
    for key, value in profile.items():
        if value in (None, "", "unknown"):
            gaps.append(key)

    if not profile.get("monitoring_enabled"):
        gaps.append("monitoring_enabled")

    return {
        "framework": "UNESCO 2001 Convention",
        "site_id": site.get("id"),
        "triggered_articles": sorted(triggered_articles, key=lambda x: int(x)),
        "triggered_rules": sorted(triggered_rules, key=lambda x: int(x)),
        "threat_categories": sorted(categories),
        "data_gaps": sorted(set(gaps)),
        "manual_review_required": bool(gaps),
        "notice": "This is an operational monitoring aid, not a legal compliance determination.",
    }


def run_external_engines(sites: list[dict[str, Any]], cfg: dict[str, Any], framework: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    site_external = []
    reports = []

    for site in sites:
        cop = query_copernicus(site, cfg)
        gfw = query_gfw(site, cfg)
        marine_regions = query_marine_regions(site, cfg)
        emodnet = query_emodnet(site, cfg)
        site_external.append(
            {
                "site_id": site.get("id"),
                "site_name": site.get("name"),
                "copernicus": cop,
                "global_fishing_watch": gfw,
                "marine_regions": marine_regions,
                "emodnet_human_activities": emodnet,
            }
        )

    reports.append({"source": "Copernicus STAC", "ok": all(x["copernicus"].get("ok", True) for x in site_external), "sites": len(site_external)})
    reports.append({"source": "Global Fishing Watch", "ok": all(x["global_fishing_watch"].get("ok", True) for x in site_external), "sites": len(site_external)})
    return site_external, reports
