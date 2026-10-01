#!/usr/bin/env python3
"""
Pocket-UCH public UCH inventory ingestion.

No AI/LLM calls.

The inventory layer discovers publicly published underwater cultural
heritage records from authoritative/public GIS services. It creates
monitoring-site records, while keeping source provenance attached.

Current adapters:
- Australian Government / Australasian maritime cultural heritage MCH service
- EMODnet Human Activities heritage shipwrecks (Europe)
"""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

import requests

TIMEOUT = 60
UA = "Pocket-UCH/0.3 (+https://github.com/AnggaConni/Pocket-UCH)"


def _get_json(url: str, *, params: dict[str, Any] | None = None, timeout: int = TIMEOUT) -> Any:
    response = requests.get(
        url,
        params=params or {},
        headers={"User-Agent": UA, "Accept": "application/json"},
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def _first(props: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = props.get(key)
        if value not in (None, "", "unknown"):
            return value
    return default


def _normalise_country(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    aliases = {
        "ASTRALIA": "Australia",
        "AUSTRAILIA": "Australia",
        "AUS": "Australia",
    }
    return aliases.get(text.upper(), text)


def _site_template(
    *,
    site_id: str,
    name: str,
    country: str | None,
    region: str | None,
    lat: float,
    lon: float,
    site_type: str,
    source_name: str,
    source_url: str,
    source_record_id: str | int | None,
    aliases: list[str] | None = None,
    notes: str = "",
) -> dict[str, Any]:
    return {
        "id": site_id,
        "name": name,
        "aliases": aliases or [],
        "country": country,
        "region": region,
        "lat": float(lat),
        "lon": float(lon),
        "depth_m": None,
        "type": site_type,
        "status": "monitor",
        "notes": notes or f"Imported from {source_name}. Treat the source as the authoritative public record for this point.",
        "monitoring": {
            "satellite": False,
            "fishing": False,
            "marine_weather": False,
            "natural_hazards": True,
            "web_monitoring": True,
            "external_context": False,
            "inventory": True,
        },
        "convention_profile": {
            "maritime_zone": "unknown",
            "state_party": country or "unknown",
            "state_vessel": False,
            "human_remains": False,
            "venerated_site": False,
            "significance": "unknown",
            "vulnerability": "unknown",
            "in_situ": True,
            "monitoring_enabled": True,
            "authorization_status": "unknown",
            "documentation_status": "unknown",
            "competent_authority": None,
            "inventory_id": str(source_record_id) if source_record_id is not None else None,
            "reporting_channel": None,
            "sensitive_data_policy": "public-source",
            "public_information": "public",
            "cooperation_partners": [],
        },
        "location": {
            "visibility": "public",
            "generalization_radius_km": 0,
        },
        "inventory": {
            "source": source_name,
            "source_url": source_url,
            "source_record_id": str(source_record_id) if source_record_id is not None else None,
        },
    }


def collect_australia(cfg: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    block = cfg.get("sources", {}).get("uch_inventory", {}).get("australia_mch", {})
    if not block.get("enabled"):
        return [], {
            "source": "Australian Government MCH Features",
            "ok": True,
            "message": "disabled",
            "coverage": "Australia / Australasian",
            "optional": True,
        }

    url = block["query_url"]
    where = (
        "SYMBOLCLASS LIKE 'MCT_SHWR%' OR "
        "SYMBOLCLASS LIKE 'MCT_UNSI%' OR "
        "SYMBOLCLASS LIKE 'MCT_INRE%' OR "
        "SYMBOLCLASS LIKE 'MCT_COUT%' OR "
        "SYMBOLCLASS LIKE 'MCT_SEUS%'"
    )

    try:
        payload = _get_json(
            url,
            params={
                "f": "json",
                "where": where,
                "outFields": (
                    "MCH_ID,LATITUDE,LONGITUDE,VESSEL_NAME,VESSEL_TYPE_DESC,"
                    "SYMBOLCLASS,REGION_NAME,COUNTRY_CODE,YEAR_WRECKED,MCH_CATEGORY_DESC"
                ),
                "returnGeometry": "false",
                "resultRecordCount": int(block.get("max_records", 300)),
                "orderByFields": "MCH_ID ASC",
            },
        )

        sites: list[dict[str, Any]] = []
        for feature in payload.get("features", []):
            props = feature.get("attributes") or {}
            try:
                lat = float(props.get("LATITUDE"))
                lon = float(props.get("LONGITUDE"))
            except (TypeError, ValueError):
                continue

            source_id = props.get("MCH_ID")
            vessel = str(props.get("VESSEL_NAME") or "").strip()
            category = str(props.get("MCH_CATEGORY_DESC") or "").strip()
            type_desc = str(props.get("VESSEL_TYPE_DESC") or "").strip()
            symbol = str(props.get("SYMBOLCLASS") or "").upper()

            if symbol.startswith("MCT_SHWR"):
                site_type = "shipwreck"
            elif symbol.startswith("MCT_ARCR"):
                site_type = "sunken_aircraft"
            elif symbol.startswith("MCT_INRE"):
                site_type = "in_situ_relic"
            else:
                site_type = "underwater_site"

            name = vessel or category or type_desc or f"Underwater heritage site {source_id}"
            aliases = [x for x in [category, type_desc] if x and x.lower() != name.lower()]

            sites.append(
                _site_template(
                    site_id=f"au-mch:{source_id}",
                    name=name,
                    country=_normalise_country(props.get("COUNTRY_CODE")) or "Australia",
                    region=str(props.get("REGION_NAME") or "").strip() or None,
                    lat=lat,
                    lon=lon,
                    site_type=site_type,
                    source_name="Australian Government MCH Features",
                    source_url=block["service_url"],
                    source_record_id=source_id,
                    aliases=aliases,
                    notes=(
                        f"Public point from the Australian Government maritime cultural heritage GIS. "
                        f"Year wrecked: {props.get('YEAR_WRECKED') or 'unknown'}."
                    ),
                )
            )

        return sites, {
            "source": "Australian Government MCH Features",
            "ok": True,
            "message": f"{len(sites)} public record(s)",
            "coverage": "Australia / Australasian",
            "optional": True,
        }
    except Exception as exc:
        return [], {
            "source": "Australian Government MCH Features",
            "ok": False,
            "message": str(exc),
            "coverage": "Australia / Australasian",
            "optional": True,
        }


def _centroid(geometry: dict[str, Any] | None) -> tuple[float, float] | None:
    if not geometry:
        return None
    kind = geometry.get("type")
    coords = geometry.get("coordinates")
    points: list[tuple[float, float]] = []

    def walk(value: Any) -> None:
        if isinstance(value, list) and len(value) >= 2 and all(isinstance(x, (int, float)) for x in value[:2]):
            points.append((float(value[0]), float(value[1])))
        elif isinstance(value, list):
            for item in value:
                walk(item)

    if kind == "Point" and isinstance(coords, list) and len(coords) >= 2:
        return float(coords[0]), float(coords[1])

    walk(coords)
    if not points:
        return None
    return (
        sum(p[0] for p in points) / len(points),
        sum(p[1] for p in points) / len(points),
    )


def collect_emodnet(cfg: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    block = cfg.get("sources", {}).get("uch_inventory", {}).get("emodnet_heritage", {})
    if not block.get("enabled"):
        return [], {
            "source": "EMODnet Human Activities — heritage shipwrecks",
            "ok": True,
            "message": "disabled",
            "coverage": "Europe",
            "optional": True,
        }

    url = block["wfs_url"]
    layer = block.get("layer", "emodnet:heritageshipwrecks")
    bbox = block.get("bbox", "-31,-36,45,72")

    try:
        params = {
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "typeNames": layer,
            "bbox": bbox,
            "srsName": "EPSG:4326",
            "outputFormat": "application/json",
            "count": int(block.get("max_records", 300)),
        }
        try:
            payload = _get_json(url, params=params)
        except Exception:
            params["version"] = "1.1.0"
            params["typeName"] = params.pop("typeNames")
            params["maxFeatures"] = params.pop("count")
            payload = _get_json(url, params=params)

        sites: list[dict[str, Any]] = []
        for feature in payload.get("features", []):
            props = feature.get("properties") or {}
            point = _centroid(feature.get("geometry"))
            if not point:
                continue
            lon, lat = point

            source_id = (
                feature.get("id")
                or _first(props, "id", "site_id", "objectid", "OBJECTID", "fid", default=None)
            )
            name = str(
                _first(
                    props,
                    "name",
                    "site_name",
                    "vessel_name",
                    "wreck_name",
                    "object_name",
                    "title",
                    default=f"European underwater heritage site {source_id or len(sites)+1}",
                )
            ).strip()
            country = _normalise_country(
                _first(props, "country", "country_name", "country_code", "countrycode")
            )
            region = _first(props, "region", "region_name", "sea", "subregion")
            site_type = "shipwreck"

            sites.append(
                _site_template(
                    site_id=f"emodnet-heritage:{source_id or len(sites)+1}",
                    name=name,
                    country=country,
                    region=str(region).strip() if region else None,
                    lat=lat,
                    lon=lon,
                    site_type=site_type,
                    source_name="EMODnet Human Activities — heritage shipwrecks",
                    source_url=block["catalog_url"],
                    source_record_id=source_id,
                    notes="Public point/geometry discovered through the EMODnet heritage shipwrecks layer.",
                )
            )

        return sites, {
            "source": "EMODnet Human Activities — heritage shipwrecks",
            "ok": True,
            "message": f"{len(sites)} public record(s)",
            "coverage": "Europe",
            "optional": True,
        }
    except Exception as exc:
        return [], {
            "source": "EMODnet Human Activities — heritage shipwrecks",
            "ok": False,
            "message": str(exc),
            "coverage": "Europe",
            "optional": True,
        }


def collect_inventory(cfg: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    sources = cfg.get("sources", {}).get("uch_inventory", {})
    if not sources.get("enabled", True):
        return [], [{
            "source": "UCH public inventory layer",
            "ok": True,
            "message": "disabled",
            "coverage": "mixed",
            "optional": True,
        }]

    all_sites: list[dict[str, Any]] = []
    reports: list[dict[str, Any]] = []

    for collector in (collect_australia, collect_emodnet):
        sites, report = collector(cfg)
        all_sites.extend(sites)
        reports.append(report)

    max_total = int(sources.get("max_total_sites", 500))
    deduped: dict[str, dict[str, Any]] = {}
    for site in all_sites:
        deduped.setdefault(str(site["id"]), site)

    result = list(deduped.values())[:max_total]

    return result, reports
