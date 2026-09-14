# Shenzhen Residential Morphology Analysis Tool

> A browser-based toolkit for architecture and urban-planning work: compute residential morphological indicators and screen parcels without the manual GIS round-trips. It bundles data prep, manual review, clustering, and publication-ready figure export into one continuous workflow.

[中文](README.md)

## Why this tool exists

The usual workflow reaches for ArcGIS to calculate residential morphological indicators, but two pain points keep showing up:

1. **Map data is capped by export size.** Pulling from OpenStreetMap typically lets you export only a small area at a time, so a city-wide sample means slicing and stitching repeatedly.
2. **Screening forces a GIS detour.** If you only care about *residential* parcels, you first have to load the data into ArcGIS and use field attributes to filter them out, then do another layer of processing.

This tool removes the map-handling and pre-screening overhead: draw a range and click parcels in the browser, and the indicators are computed for you. It also folds **data processing, manual review, clustering, and research-figure export** into the same flow, which cuts the upfront preparation time substantially.

## Features

- **Interactive parcel selection** — draw / click to collect residential cases; city-wide / high-density-zone / custom-boundary range modes.
- **Automatic indicators** — FAR, building coverage ratio (BCR), average / max floors, etc., derived from OSM buildings and roads.
- **Data health check** — flags anomalies (BCR>1, extreme FAR, low floor-coverage) with per-item review and exclusion.
- **Manual correction** — a read-only original table plus an editable corrected table; double-click floors / FAR and FAR recomputes.
- **Conditional filtering** — single / combined / range conditions, with a choice of using the original or corrected table as the source.
- **Clustering** — pick indicators → elbow method for K → KMeans → cluster centers.
- **Research figures** — PCA biplot and boxplots ready for papers.
- **Offline vector basemap** — a self-built `shenzhen.pmtiles` from raw OSM data: sharp at any zoom, works offline, no online tile dependency.

## Workflow

Seven steps unlock left-to-right in the UI:

| Step | What it does |
|---|---|
| 1 Select range | city-wide / high-density zone / custom |
| 2 Pick parcels | click on the map to collect cases |
| 3 Indicators & anomalies | auto-compute + health check |
| 4 Filter | single / combined / range |
| 5 Output | indicator table + site plan |
| 6 Cluster | elbow → KMeans → centers |
| 7 Figures | PCA + boxplots |

## Tech stack

- Backend: Python + FastAPI (indicators, filtering, clustering, plotting exposed as HTTP APIs)
- Frontend: plain HTML / CSS / JavaScript + Leaflet (maps and vector basemap, fully localised, no CDN)
- Data: geopandas / shapely / osmnx / pandas / scikit-learn / matplotlib

## Quick start (local)

Requires Python 3.9+ and internet on first fetch (later runs hit the local cache).

```bash
pip install -r requirements.txt
cd server
python -m uvicorn app:app --host 0.0.0.0 --port 8765
```

Open `http://127.0.0.1:8765` in a browser.

## Project layout

```
residential-tool/
├── README.md                Chinese docs
├── README_EN.md             This file
├── requirements.txt         Dependencies
├── LICENSE                  MIT
├── .gitignore
├── server/                  Compute engine (FastAPI)
│   ├── app.py              routes + progress + metrics/filter/cluster APIs
│   ├── config.py           config & paths
│   ├── osm.py              Overpass fetch + cache + proxy
│   ├── metrics.py          indicators + anomaly detection
│   ├── plots.py            site plan + research figures
│   ├── analysis.py         elbow / KMeans / PCA
│   └── localidx.py         local building / road index
├── web/                     Frontend (static, can be hosted alone)
│   ├── index.html
│   ├── app.js              entry
│   ├── css/style.css
│   ├── js/app.js           main logic
│   └── vendor/             Leaflet + protomaps (localised, no CDN)
└── data/
    ├── basemap/
    │   └── shenzhen.pmtiles   offline vector basemap (single file)
    └── density_zone_mask.json high-density zone mask
```

> `data/cache/`, `data/output/`, `data/basemap/raw/` are runtime-generated and excluded by `.gitignore`, so they never enter the repo.

## Deployment

- **On GitHub (source repo):** this repo is the full source. Anyone can `git clone` and run it locally as above.
- **A public "open-and-use" link for others:** GitHub itself only hosts code and cannot run the Python backend; GitHub Pages serves static front-ends only, and because the computation depends on the backend, a Pages-only deploy shows "engine not connected". To let people open it without local setup, use a host that runs Python — e.g. **Hugging Face Spaces** (free, native Python web services, near-zero changes for this FastAPI app), Render, Railway, or your own server. Keep the code on GitHub and point the host at the repo.
- Once your own host is live, you can take the temporary WorkBuddy link offline anytime from "Published Apps".

## Data & licensing

Map, building and road data come from OpenStreetMap (ODbL). The offline basemap is generated by self-tiling raw OSM vector data (`shenzhen.pmtiles`), per OSM's Tile Usage Policy; no restricted online tile service is used.

## License

[MIT](LICENSE)
