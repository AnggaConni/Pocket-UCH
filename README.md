# Pocket-UCH

Free, deterministic monitoring engine for Underwater Cultural Heritage (UCH).

## Current scope

Pocket-UCH intentionally uses **no AI API** in its data pipeline.

The first layer combines public/open UCH inventories with hazard, environmental and web signals.

Current inventory sources:
- Australian Government maritime cultural heritage GIS / MCH Features
- EMODnet Human Activities heritage shipwrecks layer (Europe)

Current monitoring sources:
- USGS earthquake GeoJSON
- BMKG earthquake feed
- NASA EONET
- Copernicus STAC
- Marine Regions
- EMODnet Human Activities
- Optional Global Fishing Watch
- Optional TinyFish Search + Fetch
- Optional manual records in `data/sites.json`

## Architecture

```
Public UCH inventories
   ↓
inventory.py
   ↓
site registry
   ↓
hazard / EO / maritime / web signals
   ↓
scraper.py
   ↓
data/data.json
   ↓
PWA / desktop-mobile UI
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

## Convention-aware monitoring

The pipeline is designed around the UNESCO 2001 Convention and its Annex Rules, but it does **not** make legal compliance determinations.

The scraper can operationalize observable signals related to:
- Article 5 incidental effects;
- Articles 9–12 maritime-zone reporting/protection context;
- Articles 14–19 illicit recovery, dealing, enforcement and cooperation;
- Article 22 inventories/competent-authority context;
- Annex Rules 1–8 general principles;
- Rules 9–16 project design and preliminary assessment;
- Rules 22–29 competence, conservation, documentation, safety and environment;
- Rules 30–36 reporting, archives and dissemination.

The machine-readable control framework is in convention.yml.

## External data layers

Optional/non-AI layers now include:
- Copernicus Data Space STAC: Sentinel-2 L2A and Sentinel-1 GRD scene discovery.
- Global Fishing Watch 4Wings: recent apparent fishing effort around a monitored site. Requires GFW_API_TOKEN for non-commercial API access.
- Marine Regions WFS: EEZ spatial context.
- EMODnet Human Activities WFS: regional human-activity context where available, including cultural heritage and activities such as dredging, shipping, fisheries and infrastructure.
- BMKG, USGS and NASA EONET for hazards and marine/environmental events.
- TinyFish Search/Fetch for deterministic web discovery only; no TinyFish Agent/Browser and no LLM is used by Pocket-UCH.

All external sources are optional and fail gracefully so one unavailable source does not stop the scraper.

## Data model

The generated `data/data.json` is the main contract for the future Pocket App.

Top-level structure:

```
metadata
summary
sites
observations
baselines
signals
convention
recommendations
provenance
source_status
legacy
```

### Design principles

- **Observation** = something a source measured/reported.
- **Signal** = a deterministic interpretation of one or more observations.
- **Baseline** = descriptive historical reference; it becomes usable after enough snapshots accumulate.
- **Convention** = UCH-specific context mapped to UNESCO 2001 Convention Articles and Annex Rules.
- **Recommendation** = deterministic operational guidance, not an automated legal decision.
- **Provenance** = where the data came from and when it was checked.
- **legacy** = compatibility copy of the earlier scraper output while the PWA is being developed.

The JSON contract is documented in `data.schema.json`.

This structure is intentionally AI-independent. A future UI can be built entirely from `data/data.json` without calling an AI service.

## Location disclosure

Pocket-UCH does not generate fake coordinates.

Each site has a location disclosure policy:

- `public`: the public `data.json` contains the real point.
- `protected`: the public `data.json` contains a generalized area polygon, not a fake point.
- Exact protected coordinates can be injected at runtime through the GitHub Actions secret `UCH_PRIVATE_SITES_JSON`. They are used for monitoring calculations but are never written to `data/data.json`.

Example private secret payload:

```json
[
  {
    "id": "UCH-001",
    "lat": -5.123456,
    "lon": 105.654321
  }
]
```

This keeps the operational monitoring coordinate separate from the public visualization layer.


## Public UCH inventory model

Pocket-UCH no longer uses a hard-coded demo site as the production registry. The scraper discovers public UCH records from configured inventory services and merges them with optional curated records in `data/sites.json`.

The current public inventory adapters are intentionally explicit about coverage:
- Australian Government MCH Features: Australia / Australasian coverage.
- EMODnet Human Activities heritage shipwrecks: Europe.

This is a **global monitoring interface with partial public-inventory coverage**, not a claim that every UCH site worldwide is represented.

Inventory records retain their source URL and source record ID. Public source points are displayed as real public points. Sensitive/private records can still be injected through `UCH_PRIVATE_SITES_JSON` and generalized before publication.

Inventory points are kept lightweight: hazard/event linking is enabled by default, while expensive deep per-site external queries such as Copernicus and Global Fishing Watch are reserved for curated sites. This prevents an expanding public inventory from turning into hundreds of paid/API-heavy calls per run.
