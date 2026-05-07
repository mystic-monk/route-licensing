<<<<<<< HEAD
<<<<<<< HEAD
<<<<<<< HEAD
=======
# Introduction 
TODO: Give a short introduction of your project. Let this section explain the objectives or the motivation behind this project. 

# Getting Started
TODO: Guide users through getting your code up and running on their own system. In this section you can talk about:
1.	Installation process
2.	Software dependencies
3.	Latest releases
4.	API references

# Build and Test
TODO: Describe and show how to build your code and run the tests. 

# Contribute
TODO: Explain how other users and developers can contribute to make your code better. 

If you want to learn more about creating good readme files then refer the following [guidelines](https://docs.microsoft.com/en-us/azure/devops/repos/git/create-a-readme?view=azure-devops). You can also seek inspiration from the below readme files:
- [ASP.NET Core](https://github.com/aspnet/Home)
- [Visual Studio Code](https://github.com/Microsoft/vscode)
- [Chakra Core](https://github.com/Microsoft/ChakraCore)
=======
>>>>>>> 332e14b (first commit)
=======
>>>>>>> 061333f (bugfixes)
# NTA Route Licensing Decision Support System 🇮🇪

Modern AI-driven analysis tool for the National Transport Authority Ireland to evaluate new bus service licensing requests against existing GTFS data.

## Overview

When a new bus service proposal is received by the NTA licensing team, it must be assessed for redundancy — do passengers already have a way to make this journey? This application parses a structured Excel timetable submission, runs an OD (origin–destination) journey coverage check and a stop-level duplication analysis against the NTA GTFS Static feed, and produces a per-stop breakdown and an overall route recommendation (APPROVE / APPROVE WITH CHANGES / REJECT).

All logic is deterministic — there is no AI or ML inference in the decision pipeline. The only ML component is DBSCAN clustering used for geographic corridor detection (scikit-learn).

---

## Project Structure

```
route_licensing/
├── src/route_licensing/
│   ├── api/
│   │   ├── main.py               # FastAPI app — all HTML and JSON endpoints
│   │   ├── static/               # CSS, JS, Excel template
│   │   └── templates/            # Jinja2 HTML templates
│   │       ├── base.html
│   │       ├── dashboard.html    # Upload form + recent history
│   │       ├── results.html      # Per-analysis results page
│   │       ├── timetable.html    # Submitted timetable vs GTFS comparison
│   │       ├── gtfs_routes.html  # GTFS routes browser
│   │       ├── gtfs_stops.html   # GTFS stops browser
│   │       └── history.html      # Full analysis history
│   ├── analysis/
│   │   ├── od_checker.py         # OD journey coverage — Tier 1 (direct) + Tier 2 (walk-access)
│   │   ├── timing_checker.py     # Existing service within ±N minutes window
│   │   ├── corridor_detector.py  # DBSCAN geographic overlap (EPSG:2157)
│   │   └── frequency_scorer.py   # Headway gap scoring at each stop
│   ├── engine/
│   │   ├── decision_engine.py    # Aggregates signals → verdict
│   │   └── storage.py            # JSON result persistence to data/analysis_results/
│   ├── ingestion/
│   │   ├── gtfs_static_loader.py # Loads/indexes GTFS zip, builds stop-service index
│   │   ├── request_parser.py     # Parses submitted Excel timetable
│   │   └── demand_loader.py      # Loads passenger demand Excel → in-memory index
│   └── core/
│       └── config.py             # Config dataclass — all thresholds, reads from .env
├── data/
│   ├── gtfs_ireland.zip          # Downloaded at startup (not committed)
│   ├── demand/                   # Uploaded demand cache (JSON)
│   └── analysis_results/         # JSON results store
├── pyproject.toml
└── README.md
```

---

## Setup

### Requirements

- Python 3.11 or 3.12
- No NTA API key required for standard analysis (GTFS Static feed is public)

### Install

```bash
pip install -e .
# or with hatch (recommended for development)
hatch shell
```

### Environment variables (optional)

```env
GTFS_STATIC_PATH=data/gtfs_ireland.zip      # default
RESULTS_PATH=data/analysis_results           # default
NTA_GTFSR_API_KEY=                           # only for live GTFS-R features
```

### GTFS Feed

Downloaded automatically at startup from the NTA TransportForIreland feed. Cached locally; refreshed if older than 7 days. If download fails the server starts in degraded mode — check `/api/v1/status`.

---

## Running

```bash
uvicorn src.route_licensing.api.main:app --host 0.0.0.0 --port 8000 --reload
```

UI: http://localhost:8000  
API docs: http://localhost:8000/docs

---

## UI Pages

| URL | Description |
|-----|-------------|
| `/` | Dashboard — upload form and recent history |
| `/history` | Full analysis history |
| `/results/{route_id}` | Detailed results for one analysis |
| `/results/{route_id}/timetable` | Submitted timetable vs GTFS comparison |
| `/gtfs-routes` | GTFS Static routes browser |
| `/gtfs-stops` | GTFS Static stops browser |

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v1/analyze` | Upload Excel timetable, run analysis |
| `GET` | `/api/v1/results` | List all stored analyses |
| `GET` | `/api/v1/results/{ref_id}` | Full JSON for one analysis |
| `DELETE` | `/api/v1/results/{ref_id}` | Delete a stored analysis |
| `GET` | `/api/v1/powerbi/export` | Flattened CSV/JSON for Power BI (`?format=csv`) |
| `GET` | `/api/v1/status` | GTFS feed health check |
| `POST` | `/api/v1/refresh-gtfs` | Force re-download of GTFS Static feed |
| `POST` | `/upload-demand` | Upload passenger demand Excel |
| `GET` | `/download/demand-template` | Download blank demand template |

---

## Excel Submission Format

Wide-format timetable — each departure is a separate column. Multiple direction sections per file (outbound + inbound). Template available from the dashboard.

```
Row N:    Kinsale to Cork City                              ← Section title
Row N+1:  Stop Name | Stop Location | Stop ID | Mon–Sun    ← Header row
Row N+2:  Kinsale Town Hall | Kinsale | 123456 | 07:00 | 09:00 | ...
Row N+3:  Cork City Centre  | Cork    | 234567 | 08:10 | 10:10 | ...
                                                           ← Blank row ends section
```

Stop IDs are validated against the GTFS feed. Short numeric IDs (e.g. `247191`) are expanded to NTA form (e.g. `8380B247191`) automatically.

---

## Decision Logic

### Primary signal — OD Journey Check

The verdict is driven by a single primary question: **can passengers already make these A→B journeys using the existing network?**

For every ordered pair of stops (A, B) on the proposed route — every boarding–alighting combination in the correct direction — the system asks whether that journey is already possible.

All forward-direction pairs are evaluated: for a route [A, B, C, D], that is A→B, A→C, A→D, B→C, B→D, C→D.

#### Tier 1 — Direct service (same time band)

An existing GTFS trip visits stop A before stop B, in the same service period (AM peak, Midday, PM peak, Evening). This is the strongest signal against licensing: passengers can already make the exact journey at the same time of day.

| Coverage | Verdict |
|----------|---------|
| ≥ 80% of journeys directly covered | REJECT |
| ≥ 60% of journeys directly covered | APPROVE WITH CHANGES |
| < 60% | No signal from Tier 1 |

#### Load modifier (applied to Tier 1)

Even if Tier 1 coverage is high, the existing service may be running at capacity:

| Signal | Condition | Effect |
|--------|-----------|--------|
| Unmet demand | High pax/hr + existing headway > 40 min | Softens REJECT → CHANGES (capacity gap justifies new route) |
| Low demand | Pax/hr below threshold | Softens APPROVE → CHANGES (commercial viability risk — new route may cannibalize sparse ridership) |

Demand data (pax/hr per OD pair) is sourced from the optional passenger demand Excel upload. Without it, headway alone is used as a supply-side proxy.

#### Tier 2 — Walk-access (same time band)

No direct service, but the passenger could walk ≤ 400 m to a nearby GTFS stop and catch an existing service to a stop near their destination. This is a softer signal — the journey is technically possible but requires a walk at each end.

| Coverage | Verdict |
|----------|---------|
| ≥ 60% of journeys covered by walk-access | APPROVE WITH CHANGES |
| < 60% | No signal from Tier 2 |

#### Fallback

If the OD check cannot run (e.g. all stop IDs unresolved, empty GTFS trip index), the system falls back to stop/trip scoring and flags this on the results page.

---

### Supporting evidence — stop-by-stop scoring

Runs in parallel with the OD check. Does not determine the verdict. Shows the licensing officer *where* on the route duplication occurs and provides the detailed evidence trail.

Each stop is scored out of 5:

| Signal | Condition | Points |
|--------|-----------|--------|
| Timing conflict | Existing service at the same stop within ±10 min | +2 |
| Corridor overlap | Stop within 300 m of existing high-density corridor (DBSCAN) | +1 |
| Frequency — well served | Average headway ≤ 20 min | +2 |
| Frequency — moderate | Average headway ≤ 40 min | +1 |
| Frequency — underserved | Average headway > 40 min or no service | +0 |

Comparisons are **service-period scoped** — a morning stop is only compared against existing AM peak services.

| Score | Colour | Meaning |
|-------|--------|---------|
| ≥ 4 | 🔴 RED | Stop is already well served — redundant |
| 2–3 | 🟡 AMBER | Partial overlap — suggest timing adjustment |
| 0–1 | 🟢 GREEN | Stop is underserved — new service adds value |

---

### Configuration

All thresholds are stored in `Config` (`config.py`) and can be overridden per submission from the dashboard form. Thresholds applied to each analysis are recorded in the result JSON for audit.

| Parameter | Default | Description |
|-----------|---------|-------------|
| `timing_window_minutes` | 10 | ± minutes for timing conflict |
| `corridor_radius_metres` | 300 | DBSCAN epsilon for corridor overlap |
| `min_headway_minutes` | 20 | Headway ≤ this → well served (+2 pts) |
| `amber_headway_minutes` | 40 | Headway ≤ this → moderate (+1 pt) |
| `stop_red_threshold` | 4 | Risk score ≥ this → RED |
| `stop_amber_threshold` | 2 | Risk score ≥ this → AMBER |
| `od_reject_ratio` | 0.80 | Tier 1 coverage ≥ this → REJECT |
| `od_changes_ratio` | 0.60 | Tier 1/2 coverage ≥ this → CHANGES |
| `od_walk_radius_metres` | 400 | Walk radius for Tier 2 nearby stop lookup |
| `od_high_pax_threshold` | 10.0 | pax/hr ≥ this → high demand |
| `od_low_pax_threshold` | 2.0 | pax/hr < this → low demand |

---

## Geospatial Details

- Coordinate system: **EPSG:2157** (Irish Transverse Mercator) for accurate metric distance
- Corridor detection: **DBSCAN** (scikit-learn), ε = 300 m, min_samples = 2
- Walk-access distance: **haversine** on WGS-84 coordinates

---

## Power BI Integration

1. Power BI Desktop → Get Data → Web
2. URL: `http://localhost:8000/api/v1/powerbi/export?format=csv`

Returns a flattened table of all historical decisions ready for dashboards.

---

## CLI Usage

For offline analysis without the web server:

```bash
uvicorn src.route_licensing.api.main:app --host 0.0.0.0 --port 8000 --reload
```
- **Swagger Documentation**: [http://localhost:8000/docs](http://localhost:8000/docs)
- **Analyze Route**: `POST /api/v1/analyze` (Upload Excel)
- **List History**: `GET /api/v1/results`

## 📊 Power BI Integration
Connect Power BI to the live data stream:
1. Open Power BI Desktop.
2. Select **Get Data** > **Web**.
3. Use the URL: `http://localhost:8000/api/v1/powerbi/export?format=csv`
4. This returns a flattened table of all historical licensing decisions, ready for dashboards.

## 🗺️ GTFS Details
- Uses **EPSG:2157** (Irish Transverse Mercator) for accurate geographic distance clustering.
- Supports both static scheduled feeds and live GTFS-Realtime (TripUpdates & VehiclePositions).
<<<<<<< HEAD
<<<<<<< HEAD
=======
# Introduction 
TODO: Give a short introduction of your project. Let this section explain the objectives or the motivation behind this project. 

# Getting Started
TODO: Guide users through getting your code up and running on their own system. In this section you can talk about:
1.	Installation process
2.	Software dependencies
3.	Latest releases
4.	API references

# Build and Test
TODO: Describe and show how to build your code and run the tests. 

# Contribute
TODO: Explain how other users and developers can contribute to make your code better. 

If you want to learn more about creating good readme files then refer the following [guidelines](https://docs.microsoft.com/en-us/azure/devops/repos/git/create-a-readme?view=azure-devops). You can also seek inspiration from the below readme files:
- [ASP.NET Core](https://github.com/aspnet/Home)
- [Visual Studio Code](https://github.com/Microsoft/vscode)
- [Chakra Core](https://github.com/Microsoft/ChakraCore)
>>>>>>> 3f48901 (Added README.md)
=======
>>>>>>> 6a4a1fa (firext commit)
>>>>>>> 332e14b (first commit)
=======
>>>>>>> 061333f (bugfixes)
