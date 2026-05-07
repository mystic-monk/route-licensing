import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv()

@dataclass
class Config:
    # ── Analysis thresholds (user-adjustable per submission) ──────────────
    timing_window_minutes:  int   = 10     # ± minutes for timing conflict
    corridor_radius_metres: float = 300.0  # metres for corridor overlap
    min_headway_minutes:    int   = 20     # ≤ this → well served (+2 pts)
    amber_headway_minutes:  int   = 40     # ≤ this → moderate (+1 pt)

    # Per-stop risk thresholds
    stop_red_threshold:     int   = 4      # risk ≥ this → RED
    stop_amber_threshold:   int   = 2      # risk ≥ this → AMBER

    # Route-level verdict ratios (stop/trip scoring)
    route_reject_ratio:     float = 0.50   # ≥ this fraction RED → REJECT
    route_changes_ratio:    float = 0.40   # ≥ this fraction RED+AMBER → CHANGES

    # OD coverage override thresholds (applied after stop/trip scoring)
    od_reject_ratio:        float = 0.80   # ≥ this fraction OD pairs directly covered → REJECT
    od_changes_ratio:       float = 0.60   # ≥ this fraction OD pairs directly covered → at least CHANGES

    # OD walk-access (Tier 3) — nearby stop radius and demand thresholds
    od_walk_radius_metres:  float = 400.0  # walking distance to consider a nearby stop accessible
    od_high_pax_threshold:  float = 10.0   # pax/hr at or above this → high demand OD pair
    od_low_pax_threshold:   float = 2.0    # pax/hr below this → low demand OD pair

    # ── Internal / infrastructure ─────────────────────────────────────────
    dbscan_min_samples:     int   = 2
    crs_projected:          str   = "EPSG:2157"
    crs_geographic:         str   = "EPSG:4326"

    # Load from environment, never from source code
<<<<<<< HEAD
    nta_api_key: str = field(
        default_factory=lambda: os.getenv("NTA_GTFSR_API_KEY", "")
<<<<<<< HEAD
<<<<<<< HEAD
=======
>>>>>>> 061333f (bugfixes)
=======
    gtfsr_api_key: str = field(
        default_factory=lambda: os.getenv("NTA_GTFSR_API_KEY", os.getenv("GTFSR_API_KEY", ""))
>>>>>>> cf9beb7 (made more generic)
    )
    static_gtfs_path: str = field(
        default_factory=lambda: os.getenv("GTFS_STATIC_PATH", "data/gtfs_ireland.zip")
    )
    results_path: str = field(
        default_factory=lambda: os.getenv("RESULTS_PATH", "data/analysis_results")
<<<<<<< HEAD
=======
>>>>>>> 332e14b (first commit)
=======
>>>>>>> 061333f (bugfixes)
    )