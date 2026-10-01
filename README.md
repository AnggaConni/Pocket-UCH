# Pocket-UCH

Free, deterministic monitoring engine for Underwater Cultural Heritage (UCH).

## Current scope

Pocket-UCH intentionally uses **no AI API** in its data pipeline.

The first layer collects public/open feeds and links environmental or hazard events to user-defined UCH monitoring sites.

Current sources:
- USGS earthquake GeoJSON
- BMKG earthquake feed
- NASA EONET
- Configurable placeholders for Copernicus STAC / marine data
- UCH monitoring sites in `data/sites.json`

## Architecture

```
Public APIs
   ↓
scraper.py
   ↓
data/data.json
   ↓
future PWA / desktop-mobile UI
```

## Run locally

```bash
python -m pip install -r requirements.txt
python scraper.py
```

## Monitoring model

Each UCH site is configured in `data/sites.json`.

The scraper uses deterministic spatial rules to associate external events with a site. A linked event is a **monitoring signal**, not a finding that the site is damaged.

## Automation

GitHub Actions runs the scraper every 6 hours and commits generated JSON back into the repository.

## Next layers

The intended next steps are:
1. richer marine/ocean observations;
2. satellite change indicators;
3. site baselines and persistence/change detection;
4. recommendation rules;
5. PWA interface.

No AI dependency is required for the core pipeline.


## Optional TinyFish web monitoring

Pocket-UCH can optionally use TinyFish **Search + Fetch** as a web-monitoring layer.

It does **not** use TinyFish Agent or Browser, and it does not call an external LLM API.

Set the GitHub Actions secret `TINYFISH_API_KEY` to enable it. The scraper is configured to run the TinyFish layer at most once per UTC day.

If the secret is absent, the official data feeds continue to run normally.
