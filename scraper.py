#!/usr/bin/env python3
"""
Pocket-UCH data scraper.

No AI API.
Collects public environmental / hazard signals and links them to
user-defined UCH sites using deterministic spatial rules.

Outputs:
  data/data.json
  data/history.json
  data/status.json
"""

from __future__ import annotations

import json
import math
import os
import sys
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
import yaml
from tinyfish import collect as collect_tinyfish
from uch_engine import assess_convention, load_convention, run_external_engines

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yml"
TIMEOUT = 30
HEADERS = {
    "User-Agent": "Pocket-UCH/0.1 (+https://github.com/AnggaConni/Pocket-UCH)"
}


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return deepcopy(default)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return deepcopy(default)


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_config() -> dict[str, Any]:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def safe_get_json(url: str, params: dict[str, Any] | None = None) -> Any:
    resp = requests.get(url, params=params or {}, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def source_status(name: str, ok: bool, message: str = "") -> dict[str, Any]:
    return {
        "source": name,
        "ok": ok,
        "message": message,
        "checked_at": iso_now(),
    }


def parse_usgs(feed: dict[str, Any], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    min_mag = float(cfg["monitoring"]["earthquake"]["min_magnitude"])
    rows: list[dict[str, Any]] = []

    for f in feed.get("features", []):
        p = f.get("properties") or {}
        geom = f.get("geometry") or {}
        coords = geom.get("coordinates") or []
        if len(coords) < 2:
            continue

        mag = p.get("mag")
        if mag is None or float(mag) < min_mag:
            continue

        rows.append(
            {
                "id": f"usgs:{f.get('id')}",
                "source": "USGS",
                "type": "earthquake",
                "title": p.get("title") or "USGS earthquake",
                "timestamp": datetime.fromtimestamp(
                    (p.get("time") or 0) / 1000, tz=timezone.utc
                ).isoformat(),
                "lat": float(coords[1]),
                "lon": float(coords[0]),
                "depth_km": float(coords[2]) if len(coords) > 2 and coords[2] is not None else None,
                "magnitude": float(mag),
                "url": p.get("url"),
                "place": p.get("place"),
            }
        )
    return rows


def parse_bmkg_quake(payload: dict[str, Any]) -> list[dict[str, Any]]:
    # BMKG autogempa.json contains one recent event.
    eq = payload.get("Infogempa", {}).get("gempa")
    if not eq:
        return []

    lat_raw = str(eq.get("Lintang", "")).replace("LS", "").replace("LU", "").strip()
    lon_raw = str(eq.get("Bujur", "")).replace("BT", "").replace("BB", "").strip()
    try:
        lat = float(lat_raw) * (-1 if "LS" in str(eq.get("Lintang", "")) else 1)
        lon = float(lon_raw) * (-1 if "BB" in str(eq.get("Bujur", "")) else 1)
        mag = float(eq.get("Magnitude", 0))
    except (TypeError, ValueError):
        return []

    return [
        {
            "id": f"bmkg:{eq.get('Tanggal','')}:{eq.get('Jam','')}",
            "source": "BMKG",
            "type": "earthquake",
            "title": f"BMKG M{mag} earthquake — {eq.get('Wilayah','Unknown')}",
            "timestamp": iso_now(),
            "lat": lat,
            "lon": lon,
            "depth_km": eq.get("Kedalaman"),
            "magnitude": mag,
            "place": eq.get("Wilayah"),
            "tsunami": eq.get("Potensi"),
        }
    ]


def parse_eonet(payload: dict[str, Any], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    allowed = {x.lower() for x in cfg["monitoring"]["eonet"]["categories"]}
    max_age = int(cfg["monitoring"]["eonet"]["max_age_days"])
    cutoff = utc_now() - timedelta(days=max_age)
    rows: list[dict[str, Any]] = []

    for event in payload.get("events", []):
        categories = [
            c.get("title", "") for c in event.get("categories", []) if isinstance(c, dict)
        ]
        if allowed and not any(c.lower() in allowed for c in categories):
            continue

        geometries = event.get("geometry") or []
        if not geometries:
            continue

        g = geometries[-1]
        dt_text = g.get("date")
        try:
            dt = datetime.fromisoformat(dt_text.replace("Z", "+00:00")) if dt_text else utc_now()
        except ValueError:
            dt = utc_now()

        if dt < cutoff:
            continue

        coords = g.get("coordinates")
        if not isinstance(coords, list) or len(coords) < 2:
            continue

        lon, lat = coords[:2]
        rows.append(
            {
                "id": f"eonet:{event.get('id')}:{dt_text}",
                "source": "NASA EONET",
                "type": "eonet_event",
                "title": event.get("title") or "NASA EONET event",
                "timestamp": dt.isoformat(),
                "lat": float(lat),
                "lon": float(lon),
                "categories": categories,
                "url": event.get("sources", [{}])[0].get("url") if event.get("sources") else None,
            }
        )

    return rows


def link_events_to_sites(
    sites: list[dict[str, Any]],
    events: list[dict[str, Any]],
    cfg: dict[str, Any],
) -> list[dict[str, Any]]:
    radius = float(cfg["monitoring"]["default_radius_km"])
    linked: list[dict[str, Any]] = []

    for site in sites:
        site_lat = site.get("lat")
        site_lon = site.get("lon")
        if site_lat is None or site_lon is None:
            continue

        for event in events:
            distance = haversine_km(
                float(site_lat),
                float(site_lon),
                float(event["lat"]),
                float(event["lon"]),
            )
            if distance > radius:
                continue

            item = deepcopy(event)
            item["site_id"] = site.get("id")
            item["site_name"] = site.get("name")
            item["distance_km"] = round(distance, 2)
            linked.append(item)

    return linked


def build_site_status(
    sites: list[dict[str, Any]],
    linked_events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for e in linked_events:
        grouped.setdefault(str(e["site_id"]), []).append(e)

    out: list[dict[str, Any]] = []

    for site in sites:
        events = grouped.get(str(site.get("id")), [])
        severity = "stable"

        # Deterministic first-pass status rules.
        if any(e["type"] == "earthquake" and float(e.get("magnitude", 0)) >= 6.0 for e in events):
            severity = "elevated"
        elif events:
            severity = "watch"

        out.append(
            {
                "site_id": site.get("id"),
                "site_name": site.get("name"),
                "status": severity,
                "event_count": len(events),
                "updated_at": iso_now(),
                "events": sorted(events, key=lambda x: x.get("timestamp", ""), reverse=True),
            }
        )

    return out



def build_baseline(site_id: str, history: list[dict[str, Any]], linked_events: list[dict[str, Any]], web_signals: list[dict[str, Any]], external: dict[str, Any], days: int) -> dict[str, Any]:
    site_snapshots = []
    cutoff = utc_now() - timedelta(days=days)

    for snap in history:
        try:
            timestamp_text = snap.get("generated_at") or snap.get("metadata", {}).get("generated_at")
            ts = datetime.fromisoformat(str(timestamp_text).replace("Z", "+00:00"))
        except Exception:
            continue
        if ts >= cutoff:
            site_snapshots.append(snap)

    event_count = 0
    web_count = 0
    fishing_values: list[float] = []

    for snap in site_snapshots:
        legacy = snap.get("legacy", {})
        linked = snap.get("linked_events", legacy.get("linked_events", []))
        web = snap.get("web_signals", legacy.get("web_signals", []))
        external_snapshots = snap.get("external_observations", legacy.get("external_observations", []))
        event_count += sum(1 for e in linked if str(e.get("site_id")) == str(site_id))
        for ws in web:
            if str(site_id) in [str(x) for x in ws.get("matched_site_ids", [])]:
                web_count += 1
        for ext in external_snapshots:
            if str(ext.get("site_id")) == str(site_id):
                try:
                    value = float(ext.get("global_fishing_watch", {}).get("fishing_hours", 0) or 0)
                    if value > 0:
                        fishing_values.append(value)
                except (TypeError, ValueError):
                    pass

    current_fishing = external.get("global_fishing_watch", {}).get("fishing_hours")
    if current_fishing is not None:
        try:
            fishing_values.append(float(current_fishing))
        except (TypeError, ValueError):
            pass

    indicators = {
        "linked_event_count": {
            "observations": len(site_snapshots),
            "current": len([e for e in linked_events if str(e.get("site_id")) == str(site_id)]),
            "historical_total": event_count,
        },
        "web_signal_count": {
            "observations": len(site_snapshots),
            "current": len([w for w in web_signals if str(site_id) in [str(x) for x in w.get("matched_site_ids", [])]]),
            "historical_total": web_count,
        },
        "fishing_hours": {
            "observations": len(fishing_values),
            "current": current_fishing,
            "mean": round(sum(fishing_values) / len(fishing_values), 2) if fishing_values else None,
            "min": round(min(fishing_values), 2) if fishing_values else None,
            "max": round(max(fishing_values), 2) if fishing_values else None,
        },
    }

    return {
        "status": "established" if len(site_snapshots) >= 7 else "establishing",
        "window_days": days,
        "snapshot_count": len(site_snapshots),
        "minimum_snapshots_for_comparison": 7,
        "indicators": indicators,
        "note": "Baseline is descriptive until enough observations are accumulated; no predictive model is used.",
    }


def build_observations(sites: list[dict[str, Any]], linked_events: list[dict[str, Any]], web_signals: list[dict[str, Any]], external_observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []

    for event in linked_events:
        observations.append(
            {
                "id": f"obs:event:{event.get('id')}",
                "site_id": event.get("site_id"),
                "observed_at": event.get("timestamp") or iso_now(),
                "kind": "hazard_event",
                "source": event.get("source"),
                "location": {
                    "lat": event.get("lat"),
                    "lon": event.get("lon"),
                    "distance_km": event.get("distance_km"),
                },
                "measurements": {
                    "magnitude": event.get("magnitude"),
                    "depth_km": event.get("depth_km"),
                },
                "context": {
                    "type": event.get("type"),
                    "title": event.get("title"),
                    "place": event.get("place"),
                    "categories": event.get("categories", []),
                    "tsunami": event.get("tsunami"),
                },
                "source_url": event.get("url"),
            }
        )

    for signal in web_signals:
        for site_id in signal.get("matched_site_ids", []):
            observations.append(
                {
                    "id": f"obs:web:{signal.get('id')}:{site_id}",
                    "site_id": site_id,
                    "observed_at": signal.get("retrieved_at") or iso_now(),
                    "kind": "web_intelligence",
                    "source": signal.get("source"),
                    "measurements": {},
                    "context": {
                        "title": signal.get("title"),
                        "snippet": signal.get("snippet"),
                        "threat_categories": signal.get("threat_categories", []),
                        "query": signal.get("query"),
                    },
                    "source_url": signal.get("url"),
                }
            )

    for ext in external_observations:
        site_id = ext.get("site_id")
        cop = ext.get("copernicus", {})
        gfw = ext.get("global_fishing_watch", {})
        mr = ext.get("marine_regions", {})
        emodnet = ext.get("emodnet_human_activities", {})

        observations.append(
            {
                "id": f"obs:external:{site_id}:{iso_now()}",
                "site_id": site_id,
                "observed_at": iso_now(),
                "kind": "external_context",
                "source": "multi-source",
                "measurements": {
                    "fishing_hours": gfw.get("fishing_hours"),
                    "satellite_scene_count": cop.get("count"),
                    "human_activity_feature_count": emodnet.get("feature_count"),
                },
                "context": {
                    "copernicus": cop,
                    "marine_regions": mr,
                    "emodnet_human_activities": emodnet,
                },
            }
        )

    return observations



def build_signals(site_status: list[dict[str, Any]], linked_events: list[dict[str, Any]], web_signals: list[dict[str, Any]], external_observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []

    for event in linked_events:
        if event.get("type") == "earthquake":
            magnitude = float(event.get("magnitude", 0) or 0)
            severity = "high" if magnitude >= 6 else "moderate"
            signals.append(
                {
                    "id": f"sig:eq:{event.get('id')}",
                    "site_id": event.get("site_id"),
                    "detected_at": event.get("timestamp") or iso_now(),
                    "category": "major_hazard",
                    "severity": severity,
                    "status": "active",
                    "trigger": {"type": "earthquake", "magnitude": magnitude, "distance_km": event.get("distance_km")},
                    "related_sources": [event.get("source")],
                }
            )

    for web in web_signals:
        categories = web.get("threat_categories", [])
        if not categories:
            continue
        for site_id in web.get("matched_site_ids", []):
            for category in categories:
                signals.append(
                    {
                        "id": f"sig:web:{web.get('id')}:{site_id}:{category}",
                        "site_id": site_id,
                        "detected_at": web.get("retrieved_at") or iso_now(),
                        "category": category,
                        "severity": "moderate",
                        "status": "review",
                        "trigger": {
                            "type": "web_signal",
                            "title": web.get("title"),
                            "url": web.get("url"),
                        },
                        "related_sources": [web.get("source")],
                    }
                )

    by_site = {str(x.get("site_id")): x for x in external_observations}
    for row in site_status:
        ext = by_site.get(str(row.get("site_id")), {})
        fishing_hours = ext.get("global_fishing_watch", {}).get("fishing_hours")
        try:
            if fishing_hours is not None and float(fishing_hours) > 0:
                signals.append(
                    {
                        "id": f"sig:fishing:{row.get('site_id')}:{iso_now()[:10]}",
                        "site_id": row.get("site_id"),
                        "detected_at": iso_now(),
                        "category": "fishing_activity",
                        "severity": "informational",
                        "status": "observed",
                        "trigger": {"type": "gfw_4wings", "fishing_hours": float(fishing_hours)},
                        "related_sources": ["Global Fishing Watch"],
                    }
                )
        except (TypeError, ValueError):
            pass

    return signals


def build_recommendations(site_status: list[dict[str, Any]], signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_site: dict[str, list[dict[str, Any]]] = {}
    for signal in signals:
        by_site.setdefault(str(signal.get("site_id")), []).append(signal)

    recs: list[dict[str, Any]] = []
    action_by_category = {
        "major_hazard": ("safety_review", "Review safety conditions before field activity and reassess the site after the hazard window."),
        "storm_wave": ("field_safety", "Defer non-essential diving or field work until marine conditions normalize."),
        "coastal_erosion": ("environmental_review", "Review recent coastal/satellite observations and consider a condition survey."),
        "sediment": ("site_condition_review", "Compare current seabed/sediment context with the established site baseline."),
        "fishing": ("human_activity_review", "Review fishing activity and potential interaction with the archaeological site."),
        "fishing_activity": ("human_activity_review", "Treat fishing activity as contextual pressure; compare against the site's historical baseline before escalating."),
        "dredging": ("project_review", "Review dredging activity and its proximity to the site's protection buffer."),
        "construction": ("project_review", "Review nearby infrastructure or construction activity and potential effects on the site."),
        "pollution": ("environmental_review", "Review available marine/environmental observations and inspect the site when appropriate."),
        "looting": ("heritage_security", "Escalate to the competent heritage authority and verify the reported activity through authorised channels."),
    }

    for row in site_status:
        site_id = str(row.get("site_id"))
        site_signals = by_site.get(site_id, [])
        if not site_signals and row.get("status") == "stable":
            continue

        categories = []
        for s in site_signals:
            if s.get("category") not in categories:
                categories.append(s.get("category"))

        selected = categories[:3]
        actions = [action_by_category[c] for c in selected if c in action_by_category]
        priority = "high" if row.get("status") == "elevated" else ("medium" if actions else "low")

        recs.append(
            {
                "id": f"rec:{site_id}:{iso_now()[:10]}",
                "site_id": row.get("site_id"),
                "created_at": iso_now(),
                "priority": priority,
                "status": "open",
                "trigger_signals": [s.get("id") for s in site_signals[:10]],
                "actions": [
                    {"type": t, "guidance": g} for t, g in actions
                ],
                "note": "Operational monitoring guidance only; human/competent-authority review remains required.",
            }
        )

    return recs


def build_provenance(source_reports: list[dict[str, Any]], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    for report in source_reports:
        items.append(
            {
                "source": report.get("source"),
                "status": "ok" if report.get("ok") else ("skipped" if report.get("optional") else "error"),
                "checked_at": report.get("checked_at") or iso_now(),
                "message": report.get("message", ""),
                "coverage": report.get("coverage", "unspecified"),
                "optional": bool(report.get("optional", True)),
            }
        )

    for name, block in cfg.get("sources", {}).items():
        if block.get("enabled"):
            items.append(
                {
                    "source": name,
                    "status": "configured",
                    "endpoint": block.get("url") or block.get("base_url") or block.get("wfs_url"),
                    "coverage": block.get("coverage", "unspecified"),
                    "optional": bool(block.get("optional", True)),
                }
            )

    return items


def build_structured_data(
    cfg: dict[str, Any],
    sites: list[dict[str, Any]],
    linked_events: list[dict[str, Any]],
    web_signals: list[dict[str, Any]],
    external_observations: list[dict[str, Any]],
    site_status: list[dict[str, Any]],
    convention_framework: dict[str, Any],
    history: list[dict[str, Any]],
    source_reports: list[dict[str, Any]],
) -> dict[str, Any]:
    observations = build_observations(sites, linked_events, web_signals, external_observations)
    signals = build_signals(site_status, linked_events, web_signals, external_observations)
    recommendations = build_recommendations(site_status, signals)

    assessments = [
        row.get("convention_assessment", {})
        for row in site_status
    ]

    baseline_days = int(cfg["output"].get("baseline_days", 90))
    baselines = []
    for site in sites:
        ext = next((x for x in external_observations if str(x.get("site_id")) == str(site.get("id"))), {})
        baselines.append(
            {
                "site_id": site.get("id"),
                "baseline": build_baseline(
                    str(site.get("id")),
                    history,
                    linked_events,
                    web_signals,
                    ext,
                    baseline_days,
                ),
            }
        )

    return {
        "metadata": {
            "schema_version": cfg["project"].get("schema_version", "0.3.0"),
            "pipeline_version": cfg["project"].get("version", "0.2.0"),
            "generated_at": iso_now(),
            "timezone": cfg["project"].get("timezone", "UTC"),
            "history_window_days": int(cfg["output"].get("max_history_days", 180)),
            "baseline_window_days": baseline_days,
            "ai_used": False,
        },
        "summary": {
            "site_count": len(sites),
            "observation_count": len(observations),
            "signal_count": len(signals),
            "recommendation_count": len(recommendations),
            "convention_flagged_sites": sum(1 for x in assessments if x.get("threat_categories")),
            "status_counts": {
                state: sum(1 for s in site_status if s.get("status") == state)
                for state in ("stable", "watch", "elevated")
            },
        },
        "sites": sites,
        "site_status": site_status,
        "observations": observations,
        "baselines": baselines,
        "signals": signals,
        "convention": {
            "framework": convention_framework.get("framework", {}),
            "site_assessments": assessments,
        },
        "recommendations": recommendations,
        "provenance": build_provenance(source_reports, cfg),
        "source_status": source_reports,
    }


def main() -> int:
    cfg = load_config()
    sites = load_json(ROOT / cfg["monitoring"]["sites_file"], [])
    convention_path = ROOT / cfg["output"].get("convention_rules_file", "convention.yml")
    convention_framework = load_convention(convention_path)

    all_events: list[dict[str, Any]] = []
    source_reports: list[dict[str, Any]] = []
    web_signals: list[dict[str, Any]] = []

    sources = cfg.get("sources", {})

    tinyfish_state_path = ROOT / cfg["output"].get("tinyfish_state_file", "data/tinyfish_state.json")
    tinyfish_state = load_json(tinyfish_state_path, {})

    if sources.get("usgs_earthquakes", {}).get("enabled"):
        try:
            payload = safe_get_json(sources["usgs_earthquakes"]["url"])
            rows = parse_usgs(payload, cfg)
            all_events.extend(rows)
            source_reports.append(source_status("USGS", True, f"{len(rows)} earthquakes"))
        except Exception as exc:
            source_reports.append(source_status("USGS", False, str(exc)))

    if sources.get("bmkg_earthquakes", {}).get("enabled"):
        try:
            payload = safe_get_json(sources["bmkg_earthquakes"]["url"])
            rows = parse_bmkg_quake(payload)
            all_events.extend(rows)
            source_reports.append(source_status("BMKG earthquake", True, f"{len(rows)} event(s)"))
        except Exception as exc:
            source_reports.append(source_status("BMKG earthquake", False, str(exc)))

    if sources.get("nasa_eonet", {}).get("enabled"):
        try:
            block = sources["nasa_eonet"]
            payload = safe_get_json(block["url"], block.get("params"))
            rows = parse_eonet(payload, cfg)
            all_events.extend(rows)
            source_reports.append(source_status("NASA EONET", True, f"{len(rows)} event(s)"))
        except Exception as exc:
            source_reports.append(source_status("NASA EONET", False, str(exc)))

    if sources.get("tinyfish", {}).get("enabled"):
        try:
            web_signals, tinyfish_state, tinyfish_reports = collect_tinyfish(
                cfg, sites, tinyfish_state
            )
            source_reports.extend(tinyfish_reports)
        except Exception as exc:
            source_reports.append(source_status("TinyFish", False, str(exc)))
    save_json(tinyfish_state_path, tinyfish_state)

    external_observations: list[dict[str, Any]] = []
    external_reports: list[dict[str, Any]] = []
    try:
        external_observations, external_reports = run_external_engines(
            sites, cfg, convention_framework
        )
        source_reports.extend(external_reports)
    except Exception as exc:
        source_reports.append(source_status("External marine/EO engines", False, str(exc)))

    linked_events = link_events_to_sites(sites, all_events, cfg)

    # Deterministic escalation from web signals that explicitly match a monitored site.
    web_by_site: dict[str, list[dict[str, Any]]] = {}
    for signal in web_signals:
        for site_id in signal.get("matched_site_ids", []):
            web_by_site.setdefault(str(site_id), []).append(signal)

    ext_by_site = {str(x.get("site_id")): x for x in external_observations}

    site_status = build_site_status(sites, linked_events)
    for row in site_status:
        matches = web_by_site.get(str(row["site_id"]), [])
        site_events = [x for x in linked_events if str(x.get("site_id")) == str(row["site_id"])]
        ext = ext_by_site.get(str(row["site_id"]), {})
        actionable = [x for x in matches if x.get("threat_categories")]
        row["web_signal_count"] = len(matches)
        row["web_actionable_signal_count"] = len(actionable)
        row["web_recommendations"] = [
            x.get("recommendation") for x in actionable[:3] if x.get("recommendation")
        ]
        row["external_observations"] = ext

        assessment_events = list(site_events)
        gfw_obs = ext.get("global_fishing_watch", {})
        if float(gfw_obs.get("fishing_hours", 0) or 0) > 0:
            assessment_events.append({
                "type": "external",
                "threat_categories": ["fishing"],
            })

        emodnet_obs = ext.get("emodnet_human_activities", {})
        if emodnet_obs.get("feature_count", 0):
            assessment_events.append({
                "type": "external",
                "threat_categories": ["construction"],
            })

        row["convention_assessment"] = assess_convention(
            next((s for s in sites if str(s.get("id")) == str(row["site_id"])), {}),
            assessment_events,
            matches,
            convention_framework,
        )

        if ext.get("global_fishing_watch", {}).get("fishing_hours", 0) > 0:
            if row["status"] == "stable":
                row["status"] = "watch"

        if actionable and row["status"] == "stable":
            row["status"] = "watch"

    data_path = ROOT / cfg["output"]["data_file"]
    history_path = ROOT / cfg["output"]["history_file"]
    status_path = ROOT / cfg["output"]["status_file"]

    previous = load_json(history_path, [])
    if not isinstance(previous, list):
        previous = []

    snapshot = build_structured_data(
        cfg,
        sites,
        linked_events,
        web_signals,
        external_observations,
        site_status,
        convention_framework,
        previous,
        source_reports,
    )
    snapshot["legacy"] = {
        "generated_at": snapshot["metadata"]["generated_at"],
        "sites": len(sites),
        "raw_events": all_events,
        "linked_events": linked_events,
        "web_signals": web_signals,
        "external_observations": external_observations,
        "site_status": site_status,
    }

    history = previous + [snapshot]
    max_days = int(cfg["output"]["max_history_days"])
    cutoff = utc_now() - timedelta(days=max_days)

    trimmed = []
    for item in history:
        try:
            timestamp_text = item.get("generated_at")
            if not timestamp_text:
                timestamp_text = item.get("metadata", {}).get("generated_at")
            ts = datetime.fromisoformat(str(timestamp_text).replace("Z", "+00:00"))
            if ts >= cutoff:
                trimmed.append(item)
        except Exception:
            trimmed.append(item)

    save_json(data_path, snapshot)
    save_json(history_path, trimmed)

    status = {
        "generated_at": iso_now(),
        "source_status": source_reports,
        "site_count": len(sites),
        "event_count": len(all_events),
        "linked_event_count": len(linked_events),
        "web_signal_count": len(web_signals),
        "web_actionable_signal_count": sum(
            1 for x in web_signals if x.get("threat_categories")
        ),
        "external_observation_count": len(external_observations),
        "convention_flagged_sites": sum(
            1 for s in site_status
            if s.get("convention_assessment", {}).get("threat_categories")
        ),
        "site_status_counts": {
            state: sum(1 for s in site_status if s["status"] == state)
            for state in ["stable", "watch", "elevated"]
        },
    }
    save_json(status_path, status)

    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
