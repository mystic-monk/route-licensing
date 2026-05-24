"""
Route Licensing — FastAPI Application
======================================
Serves both a JSON API and a Jinja2 HTML UI.

HTML endpoints:
  GET  /                              → Dashboard (upload form + recent history)
  GET  /history                       → Full analysis history
  GET  /results/{ref_id}              → Analysis results page
  GET  /results/{ref_id}/timetable    → Submitted timetable + GTFS comparison

JSON API endpoints:
  POST /api/v1/analyze                → Upload Excel, run engine, return JSON
  GET  /api/v1/results                → List all analyses
  GET  /api/v1/results/{ref_id}       → Full detail for one analysis
  DELETE /api/v1/results/{ref_id}     → Delete a stored analysis
  GET  /api/v1/powerbi/export         → Flattened CSV or JSON for Power BI
  GET  /api/v1/status                 → GTFS feed health check
"""

import json
import logging
import os
import shutil
import tempfile
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import requests as http_requests
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask

from route_licensing.core.config import Config
from route_licensing.engine.decision_engine import analyse_route
from route_licensing.engine.storage import (
    delete_analysis,
    get_analysis_by_ref,
    list_all_analyses,
    save_analysis_result,
)
from route_licensing.ingestion.gtfs_static_loader import (
    build_stop_service_index,
    build_trip_index,
    load_all_stop_ids,
    load_gtfs,
)
from route_licensing.ingestion.request_parser import parse_excel_request, generate_timetable_template
from route_licensing.ingestion.demand_loader import (
    DemandIndex,
    generate_demand_template,
    get_cache_meta,
    load_demand_from_cache,
    load_demand_from_excel,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_HERE         = Path(__file__).parent
TEMPLATES_DIR = _HERE / "templates"
STATIC_DIR    = _HERE / "static"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
cfg = Config()

_GTFS_STATIC_URL   = "https://www.transportforireland.ie/transitData/Data/GTFS_Realtime.zip"
_GTFS_MAX_AGE_DAYS = 7

_gtfs_index: pd.DataFrame = pd.DataFrame()
_CACHE_BUST: str = str(int(datetime.now(timezone.utc).timestamp()))
_gtfs_trip_index: pd.DataFrame = pd.DataFrame()
_gtfs_service_date: Optional[date] = None
_gtfs_all_stop_ids: frozenset = frozenset()
_gtfs_stop_id_suffix_map: dict = {}
_gtfs_stop_code_map: dict = {}
_gtfs_stop_id_to_code_map: dict = {}

_demand_index: DemandIndex = {}
_gtfs_refresh_in_progress: bool = False


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _gtfs_refresh_in_progress
    gtfs_path = cfg.static_gtfs_path
    # Immediately serve whatever is on disk — don't block startup on a download.
    if os.path.exists(gtfs_path):
        _load_gtfs_from_disk()
    # Spawn a background download if the zip is absent or older than the max age.
    age_days: Optional[int] = None
    if os.path.exists(gtfs_path):
        age_days = (
            datetime.now(timezone.utc)
            - datetime.fromtimestamp(os.path.getmtime(gtfs_path), tz=timezone.utc)
        ).days
    needs_refresh = not os.path.exists(gtfs_path) or (age_days is not None and age_days >= _GTFS_MAX_AGE_DAYS)
    if needs_refresh:
        _gtfs_refresh_in_progress = True
        t = threading.Thread(target=_refresh_gtfs_background, daemon=True, name="gtfs-refresh")
        t.start()
        logger.info("Background GTFS refresh started (age=%s days).", age_days)
    _load_demand_cache()
    _ensure_timetable_template()
    yield


def _load_demand_cache() -> None:
    global _demand_index
    cached = load_demand_from_cache()
    if cached:
        _demand_index = cached
        logger.info("Demand index restored from cache: %d records.", len(cached))
    _ensure_demand_template()


def _ensure_demand_template() -> None:
    path = Path("data/demand/demand_template.xlsx")
    if not path.exists():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            generate_demand_template(str(path))
            logger.info("Demand template generated at %s.", path)
        except Exception as exc:
            logger.warning("Could not generate demand template: %s", exc)


def _ensure_timetable_template() -> None:
    path = STATIC_DIR / "timetable_template.xlsx"
    if not path.exists():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            generate_timetable_template(str(path))
            logger.info("Timetable template generated at %s.", path)
        except Exception as exc:
            logger.warning("Could not generate timetable template: %s", exc)



def _load_gtfs_from_disk() -> bool:
    """Build all GTFS indices from the local zip — no network call. Returns True on success."""
    global _gtfs_index, _gtfs_trip_index, _gtfs_all_stop_ids
    global _gtfs_stop_id_suffix_map, _gtfs_stop_code_map, _gtfs_stop_id_to_code_map, _gtfs_service_date
    gtfs_path = cfg.static_gtfs_path
    if not os.path.exists(gtfs_path):
        return False
    try:
        logger.info("Loading GTFS feed from %s ...", gtfs_path)
        feed, _gtfs_service_date = load_gtfs(gtfs_path)
        _gtfs_index      = build_stop_service_index(feed)
        _gtfs_trip_index = build_trip_index(feed)
        _gtfs_all_stop_ids, _gtfs_stop_id_suffix_map, _gtfs_stop_code_map = load_all_stop_ids(gtfs_path)
        _gtfs_stop_id_to_code_map = {v: k for k, v in _gtfs_stop_code_map.items()}
        logger.info(
            "GTFS feed loaded: %d stop-service records, %d routes, %d unique stops.",
            len(_gtfs_index),
            _gtfs_index["route_id"].nunique(),
            _gtfs_index["stop_id"].nunique(),
        )
        return True
    except Exception as exc:
        logger.critical("GTFS feed at '%s' could not be loaded: %s.", gtfs_path, exc)
        return False


def _refresh_gtfs_background() -> None:
    """Download fresh GTFS zip, then rebuild indices atomically. Runs in a daemon thread."""
    global _gtfs_refresh_in_progress
    gtfs_path = cfg.static_gtfs_path
    tmp_path  = gtfs_path + ".tmp"
    try:
        logger.info("Background GTFS refresh: downloading from %s ...", _GTFS_STATIC_URL)
        os.makedirs(os.path.dirname(gtfs_path) or ".", exist_ok=True)
        response = http_requests.get(_GTFS_STATIC_URL, timeout=120, stream=True)
        response.raise_for_status()
        with open(tmp_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                f.write(chunk)
        os.replace(tmp_path, gtfs_path)
        logger.info("Background GTFS refresh: download complete — reloading indices ...")
        _load_gtfs_from_disk()
        logger.info("Background GTFS refresh: done.")
    except Exception as exc:
        logger.warning("Background GTFS refresh failed: %s", exc)
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
    finally:
        _gtfs_refresh_in_progress = False


def _gtfs_is_ready() -> bool:
    return not _gtfs_index.empty


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Route Licensing API",
    description="Deterministic rule-based decision support system for bus service licensing in Ireland.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["cache_bust"] = _CACHE_BUST


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# JSON serialisation helper
# ---------------------------------------------------------------------------

def _sanitise_for_json(obj):
    """
    Recursively replaces float nan/inf values with None so the result
    can be serialised by stdlib json (which rejects non-finite floats).
    """
    import math
    if isinstance(obj, dict):
        return {k: _sanitise_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitise_for_json(v) for v in obj]
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    return obj


# ---------------------------------------------------------------------------
# Template context helpers
# ---------------------------------------------------------------------------

def _compile_considered_routes(stop_analysis: list, gtfs_index: pd.DataFrame) -> list:
    """
    Aggregates all existing routes flagged across every stop (via timing
    conflict or corridor overlap) into a deduplicated list for the UI.
    """
    # Build fast route_id → (route_label, operator) lookup from the GTFS index.
    route_meta: dict[str, tuple[str, str]] = {}
    if not gtfs_index.empty:
        for rid, grp in gtfs_index.groupby("route_id"):
            label    = grp["route_label"].iloc[0] if "route_label" in grp.columns else str(rid)
            operator = grp["operator"].iloc[0]    if "operator"    in grp.columns else ""
            route_meta[str(rid)] = (str(label), str(operator))

    def _meta(rid: str, fallback_operator: str = "") -> tuple[str, str]:
        label, op = route_meta.get(rid, (rid, fallback_operator))
        return label, op or fallback_operator

    route_map: dict = {}

    for stop in stop_analysis:
        stop_name = stop["stop_name"]

        for cs in stop.get("conflicting_services", []):
            rid = cs["route_id"]
            label, operator = _meta(rid, cs.get("operator", ""))
            if rid not in route_map:
                route_map[rid] = {
                    "route_id":       rid,
                    "route_label":    label,
                    "operator":       operator,
                    "affected_stops": set(),
                    "conflict_types": set(),
                }
            route_map[rid]["affected_stops"].add(stop_name)
            route_map[rid]["conflict_types"].add("timing")

        for rid in stop.get("corridor_overlap", []):
            label, operator = _meta(rid)
            if rid not in route_map:
                route_map[rid] = {
                    "route_id":       rid,
                    "route_label":    label,
                    "operator":       operator,
                    "affected_stops": set(),
                    "conflict_types": set(),
                }
            route_map[rid]["affected_stops"].add(stop_name)
            route_map[rid]["conflict_types"].add("corridor")

    return [
        {
            "route_id":       e["route_id"],
            "route_label":    e["route_label"],
            "operator":       e["operator"],
            "affected_stops": sorted(e["affected_stops"]),
            "conflict_types": sorted(e["conflict_types"]),
        }
        for e in sorted(route_map.values(), key=lambda x: x["route_id"])
    ]


def _build_timetable_view(stop_analysis: list) -> dict:
    """
    Reconstructs a wide-format timetable from the flat stop_analysis list.
    Groups by stop_id; departure times become columns; cells carry verdict
    for colour-coding in the template.
    """
    if not stop_analysis:
        return {"all_times": [], "stops": [], "total_stops": 0, "total_times": 0}

    stop_order: list[str] = []
    stops: dict[str, dict] = {}

    for entry in stop_analysis:
        sid = entry["stop_id"]
        t   = entry.get("arrival_time", "")
        v   = entry.get("verdict", "")

        if sid not in stops:
            stops[sid] = {
                "stop_id":   sid,
                "stop_name": entry["stop_name"],
                "time_map":  {},
            }
            stop_order.append(sid)

        if t:
            stops[sid]["time_map"][t] = v

    all_times = sorted({
        t for s in stops.values() for t in s["time_map"] if t
    })

    stop_rows = []
    for sid in stop_order:
        s = stops[sid]
        stop_rows.append({
            "stop_id":      sid,
            "stop_name":    s["stop_name"],
            "served_count": len(s["time_map"]),
            "cells": [
                {"time": t, "verdict": s["time_map"].get(t)}
                for t in all_times
            ],
        })

    return {
        "all_times":   all_times,
        "stops":       stop_rows,
        "total_stops": len(stop_rows),
        "total_times": len(all_times),
    }


def _build_gtfs_comparison(stop_analysis: list) -> list:
    """
    Builds a per-stop comparison of proposed times against existing GTFS
    services identified as timing conflicts, grouped by route.
    """
    stops: dict[str, dict] = {}

    for entry in stop_analysis:
        if not entry.get("conflicting_services"):
            continue

        sid           = entry["stop_id"]
        proposed_time = entry.get("arrival_time", "")

        if sid not in stops:
            stops[sid] = {
                "stop_id":        sid,
                "stop_name":      entry["stop_name"],
                "proposed_times": [],
                "route_map":      {},
            }

        if proposed_time and proposed_time not in stops[sid]["proposed_times"]:
            stops[sid]["proposed_times"].append(proposed_time)

        for cs in entry["conflicting_services"]:
            rid = cs.get("route_id", "")
            if rid not in stops[sid]["route_map"]:
                stops[sid]["route_map"][rid] = {
                    "route_id": rid,
                    "operator": cs.get("operator", ""),
                    "conflicts": [],
                }
            stops[sid]["route_map"][rid]["conflicts"].append({
                "existing_time": cs.get("arrival_time", ""),
                "proposed_time": proposed_time,
                "delta_minutes": round(float(cs.get("delta_minutes", 0)), 1),
            })

    result = []
    for sid, data in stops.items():
        services = sorted(
            data["route_map"].values(), key=lambda r: r["route_id"]
        )
        for svc in services:
            svc["conflicts"].sort(key=lambda c: c["existing_time"])
        result.append({
            "stop_id":           sid,
            "stop_name":         data["stop_name"],
            "proposed_times":    sorted(data["proposed_times"]),
            "services_by_route": services,
        })

    return result


# ===========================================================================
# HTML UI Endpoints
# ===========================================================================

@app.get("/download/timetable-template", include_in_schema=False)
async def download_timetable_template():
    """Download the Excel submission template (generated on demand if absent)."""
    template_path = STATIC_DIR / "timetable_template.xlsx"
    if not template_path.exists():
        _ensure_timetable_template()
    if not template_path.exists():
        raise HTTPException(status_code=404, detail="Template file could not be generated.")
    return FileResponse(
        path=str(template_path),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename="Timetable_Submission_Template.xlsx",
    )


@app.get("/download/demand-template", include_in_schema=False)
async def download_demand_template():
    """Download the pre-built Excel demand data template."""
    template_path = Path("data/demand/demand_template.xlsx")
    if not template_path.exists():
        _ensure_demand_template()
    if not template_path.exists():
        raise HTTPException(status_code=404, detail="Demand template could not be generated.")
    return FileResponse(
        path=str(template_path),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename="Demand_Template.xlsx",
    )


@app.post("/upload-demand", include_in_schema=False)
async def upload_demand(file: UploadFile = File(...)):
    """
    Upload an OD demand Excel file (.xlsx). Parses it, normalises stop IDs
    against the loaded GTFS feed, writes the JSON disk cache, and updates
    the in-memory demand index immediately.
    """
    global _demand_index

    if not (file.filename or "").endswith((".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="Please upload an Excel file (.xlsx or .xls).")

    tmp_path: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp:
            shutil.copyfileobj(file.file, tmp)
            tmp_path = tmp.name

        new_index = load_demand_from_excel(
            tmp_path,
            stop_code_map=_gtfs_stop_code_map if _gtfs_stop_code_map else None,
            all_stop_ids=_gtfs_all_stop_ids if _gtfs_all_stop_ids else None,
        )
        _demand_index = new_index
        meta = get_cache_meta()
        logger.info("Demand index updated: %d records from '%s'.", len(new_index), file.filename)
        return JSONResponse(content={
            "status": "ok",
            "records_loaded": len(new_index),
            "source_file": file.filename,
            "cached_at": meta.get("loaded_at", ""),
        })
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Demand file error: {exc}")
    except Exception as exc:
        logger.exception("Demand upload failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Unexpected error: {exc}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


@app.get("/", include_in_schema=False)
async def dashboard(request: Request):
    """Dashboard: upload form and recent analysis history."""
    history    = list_all_analyses(cfg.results_path)
    gtfs_ready = _gtfs_is_ready()

    stats = {
        "total":       len(history),
        "approved":    sum(1 for h in history if h["verdict"] == "APPROVE"),
        "changes":     sum(1 for h in history if h["verdict"] == "APPROVE WITH CHANGES"),
        "rejected":    sum(1 for h in history if h["verdict"] == "REJECT"),
        "gtfs_routes": int(_gtfs_index["route_id"].nunique()) if gtfs_ready else 0,
        "gtfs_stops":  len(_gtfs_index) if gtfs_ready else 0,
    }

    demand_meta = get_cache_meta()

    gtfs_refreshed = None
    if gtfs_ready and os.path.exists(cfg.static_gtfs_path):
        mtime = os.path.getmtime(cfg.static_gtfs_path)
        dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
        gtfs_refreshed = f"{dt.day} {dt.strftime('%b')} {dt.year}"

    return templates.TemplateResponse(request, "dashboard.html", {
        "active_page":              "dashboard",
        "history":                  history[:10],
        "stats":                    stats,
        "gtfs_ready":               gtfs_ready,
        "gtfs_refreshed":           gtfs_refreshed,
        "gtfs_refresh_in_progress": _gtfs_refresh_in_progress,
        "demand_loaded":            bool(_demand_index),
        "demand_meta":              demand_meta,
    })


@app.get("/history", include_in_schema=False)
async def history_page(request: Request):
    """Full analysis history page."""
    history = list_all_analyses(cfg.results_path)
    return templates.TemplateResponse(request, "history.html", {
        "active_page": "history",
        "history":     history,
    })


def _build_sections_view(stop_analysis: list) -> list:
    """
    Reconstructs the original direction sections from the flat stop_analysis
    list and returns them in a trip-indexed structure for the template.

    Sections are grouped by section_idx (stored by the parser per row).
    Falls back to a single section for legacy analyses that pre-date the
    section_idx field.

    Trip columns are positional (1st, 2nd, … Nth departure per stop),
    not time-value based, so each column represents one complete trip
    across all stops — matching the original wide-format Excel layout.

    Each section dict contains:
        title:          from section_title stored by the parser, or auto-generated
        operating_days: operating day label, e.g. "Monday – Sunday"
        trip_count:     number of departure columns
        stop_count:     number of unique stops
        trips:          list of trip labels (Trip 1, Trip 2, …)
        stops:          list of stop row dicts, each with:
                            stop_id, stop_name, stop_location,
                            times: [{"time": str, "verdict": str}]
    """
    if not stop_analysis:
        return []

    # Group entries by section_idx. For legacy results without section_idx,
    # treat the entire list as one section (index 0).
    sections_raw: dict[int, list] = {}
    for entry in stop_analysis:
        idx = int(entry.get("section_idx", 0) or 0)
        sections_raw.setdefault(idx, []).append(entry)

    result = []
    for idx in sorted(sections_raw.keys()):
        raw = sections_raw[idx]

        # Group entries by stop_id preserving first-seen order.
        # Entries arrive in parser order: all trips for stop 1, then stop 2, etc.
        # So consecutive entries for the same stop_id belong to the same stop.
        stop_order: list[str] = []
        stop_map: dict[str, dict] = {}

        for entry in raw:
            sid = entry["stop_id"]
            t   = entry.get("arrival_time", "")
            v   = entry.get("verdict", "")
            if sid not in stop_map:
                stop_map[sid] = {
                    "stop_id":       sid,
                    "stop_name":     entry.get("stop_name", ""),
                    "stop_location": entry.get("stop_location", ""),
                    "departures":    [],
                }
                stop_order.append(sid)
            if t:
                stop_map[sid]["departures"].append({"time": t, "verdict": v})

        # Trip count = max departures across all stops in this section.
        trip_count = max(
            (len(s["departures"]) for s in stop_map.values()), default=0
        )

        # Build stop rows with one cell per trip index.
        stop_rows = []
        for sid in stop_order:
            s = stop_map[sid]
            cells = []
            for i in range(trip_count):
                if i < len(s["departures"]):
                    cells.append(s["departures"][i])
                else:
                    cells.append({"time": "", "verdict": ""})
            stop_rows.append({
                "stop_id":       sid,
                "stop_name":     s["stop_name"],
                "stop_location": s["stop_location"],
                "times":         cells,
            })

        # Use stored section_title if present; otherwise auto-generate.
        stored_title = raw[0].get("section_title", "") if raw else ""
        if stored_title:
            title = stored_title
        else:
            first_name = stop_rows[0]["stop_name"]  if stop_rows else ""
            last_name  = stop_rows[-1]["stop_name"] if stop_rows else ""
            title = f"{first_name} → {last_name}" if first_name != last_name else first_name

        # Parse operating-day groups stored by the parser.
        # Falls back to a single "Monday – Sunday" group for legacy analyses.
        raw_dg = raw[0].get("section_day_groups", "[]") if raw else "[]"
        try:
            day_groups = json.loads(raw_dg) if isinstance(raw_dg, str) else []
        except (ValueError, TypeError):
            day_groups = []
        if not day_groups:
            day_groups = [{"label": "Monday – Sunday", "trip_count": trip_count}]

        result.append({
            "title":      title,
            "day_groups": day_groups,
            "trip_count": trip_count,
            "stop_count": len(stop_rows),
            "trips":      [{"label": f"Trip {i+1}"} for i in range(trip_count)],
            "stops":      stop_rows,
        })

    return result


def _build_map_data(
    stop_analysis: list,
    nearby_routes: list,
    gtfs_index: pd.DataFrame,
    trip_index: pd.DataFrame | None = None,
    max_nearby: int = 5,
) -> dict:
    """
    Builds a JSON-serialisable dict for the Leaflet map on the results page.

    proposed_stops — one entry per unique stop, in section/trip order.
    nearby_lines   — one polyline per nearby route using the longest trip
                     from trip_index (stop_sequence ordered) so lines follow
                     the actual road rather than connecting stops randomly.
    """
    if not stop_analysis:
        return {"proposed_stops": [], "nearby_lines": []}

    # Build stop_id → (lat, lon) from GTFS index as fallback source.
    coord_map: dict[str, tuple] = {}
    if not gtfs_index.empty:
        coord_map = (
            gtfs_index.drop_duplicates("stop_id")
            .set_index("stop_id")[["stop_lat", "stop_lon"]]
            .apply(lambda r: (r["stop_lat"], r["stop_lon"]), axis=1)
            .to_dict()
        )

    # Proposed stops — deduplicated by stop_id, first-seen order.
    # Primary source: stop_lat/stop_lon embedded in the analysis JSON.
    # Fallback: GTFS service index coord_map.
    seen: set = set()
    proposed_stops = []
    for entry in stop_analysis:
        sid = entry["stop_id"]
        if sid in seen:
            continue
        seen.add(sid)
        lat = entry.get("stop_lat")
        lon = entry.get("stop_lon")
        if lat is None or lon is None:
            lat, lon = coord_map.get(sid, (None, None))
        if lat is None:
            continue
        proposed_stops.append({
            "stop_id":   sid,
            "stop_name": entry.get("stop_name", sid),
            "lat":       float(lat),
            "lon":       float(lon),
            "verdict":   entry.get("verdict", ""),
        })

    # Nearby route polylines — use trip_index for correct stop_sequence order.
    # Pick the longest trip (most stops) as the representative line.
    nearby_lines = []
    colours = ["#3b82f6", "#f59e0b", "#8b5cf6", "#ec4899", "#14b8a6"]

    tdf = trip_index if (trip_index is not None and not trip_index.empty) else None

    for i, route in enumerate(nearby_routes[:max_nearby]):
        rid = route["route_id"]
        coords = []

        if tdf is not None:
            rdf = tdf[tdf["route_id"] == rid]
            if not rdf.empty:
                # Pick the trip with the most stops.
                best_trip = rdf.groupby("trip_id")["stop_sequence"].count().idxmax()
                trip_stops = (
                    rdf[rdf["trip_id"] == best_trip]
                    .sort_values("stop_sequence")
                )
                for _, row in trip_stops.iterrows():
                    if pd.notna(row.get("stop_lat")) and pd.notna(row.get("stop_lon")):
                        coords.append([float(row["stop_lat"]), float(row["stop_lon"])])

        # Fallback to service index if trip_index had no coords for this route.
        if not coords:
            rdf = gtfs_index[gtfs_index["route_id"] == rid].drop_duplicates("stop_id")
            for _, row in rdf.iterrows():
                if pd.notna(row["stop_lat"]) and pd.notna(row["stop_lon"]):
                    coords.append([float(row["stop_lat"]), float(row["stop_lon"])])

        if coords:
            nearby_lines.append({
                "route_id":    rid,
                "route_label": route.get("route_label", rid),
                "operator":    route.get("operator", ""),
                "colour":      colours[i % len(colours)],
                "coords":      coords,
            })

    return {"proposed_stops": proposed_stops, "nearby_lines": nearby_lines}


def _find_nearby_routes(
    stop_analysis: list,
    gtfs_index: pd.DataFrame,
    top_n: int = 8,
) -> list:
    """
    Finds existing GTFS routes that share the most stops with the proposed
    route, ranked by overlap count descending.

    Only routes serving at least one proposed stop are included.
    Returns up to top_n results, each containing:
        route_id, operator, shared_stops, total_gtfs_stops,
        proposed_stops, overlap_pct, shared_stop_names (up to 5)
    """
    if gtfs_index.empty or not stop_analysis:
        return []

    # Collect unique proposed stop_ids and their names.
    proposed: dict[str, str] = {}
    for entry in stop_analysis:
        sid = entry["stop_id"]
        if sid not in proposed:
            proposed[sid] = entry.get("stop_name", sid)

    proposed_ids = set(proposed.keys())

    # For each existing route, find which proposed stops it also serves.
    results = []
    for route_id, group in gtfs_index.groupby("route_id"):
        route_stop_ids = set(group["stop_id"].unique())
        shared_ids     = proposed_ids & route_stop_ids
        if not shared_ids:
            continue

        operator    = group["operator"].iloc[0] if "operator" in group.columns else ""
        route_label = group["route_label"].iloc[0] if "route_label" in group.columns else str(route_id)
        overlap_pct = round(len(shared_ids) / len(proposed_ids) * 100, 1)

        results.append({
            "route_id":         str(route_id),
            "route_label":      str(route_label),
            "operator":         str(operator),
            "shared_stops":     len(shared_ids),
            "total_gtfs_stops": len(route_stop_ids),
            "proposed_stops":   len(proposed_ids),
            "overlap_pct":      overlap_pct,
            "shared_stop_names": sorted([
                proposed[sid] for sid in shared_ids
            ])[:5],
        })

    results.sort(key=lambda x: x["shared_stops"], reverse=True)
    return results[:top_n]


_ROUTE_TYPE_LABELS: dict[str, tuple[str, str]] = {
    "0":  ("🚋", "Tram"),
    "1":  ("🚇", "Metro"),
    "2":  ("🚂", "Rail"),
    "3":  ("🚌", "Bus"),
    "4":  ("⛴", "Ferry"),
    "5":  ("🚠", "Cable tram"),
    "6":  ("🚡", "Aerial lift"),
    "7":  ("🚞", "Funicular"),
    "11": ("🚎", "Trolleybus"),
    "12": ("🚝", "Monorail"),
}


def _transport_mode(route_type: str) -> tuple[str, str]:
    """Return (icon, label) for a GTFS route_type value."""
    return _ROUTE_TYPE_LABELS.get(str(route_type).strip(), ("🚌", "Bus"))


def _build_gtfs_route_timetables(
    trip_index: pd.DataFrame,
    service_index: pd.DataFrame,
    page: int = 1,
    per_page: int = 25,
    search: str = "",
    operator_filter: str = "",
    max_cols: int = 500,
) -> dict:
    """
    Builds paginated timetable data for the GTFS routes browser page.

    Uses the trip-structured index (trip_id + stop_sequence preserved) so
    every stop in a trip gets the correct departure time in the right column.
    Selects up to max_cols trips per route (sorted by the first stop's
    departure time). Default of 500 is effectively uncapped for any real route.

    Returns a dict with keys:
        routes, total, page, per_page, total_pages, operators
    """
    def _fmt(td) -> str:
        if pd.isna(td):
            return ""
        raw = str(td)
        return raw.split("days")[-1].strip() if "days" in raw else raw

    if trip_index.empty:
        return {"routes": [], "total": 0, "page": page, "per_page": per_page,
                "total_pages": 0, "operators": []}

    # ── Filter using deduplicated service_index for route list + operators ──
    sdf = service_index.copy()
    if search:
        mask = (
            sdf["route_label"].str.contains(search, case=False, na=False) |
            sdf["route_id"].str.contains(search, case=False, na=False)
        )
        if "route_long_name" in sdf.columns:
            mask = mask | sdf["route_long_name"].str.contains(search, case=False, na=False)
        sdf = sdf[mask]
    if operator_filter:
        sdf = sdf[sdf["operator"].str.contains(operator_filter, case=False, na=False)]

    all_operators  = sorted(service_index["operator"].dropna().unique().tolist())
    all_route_ids  = sdf["route_id"].unique().tolist()
    total          = len(all_route_ids)
    total_pages    = max(1, (total + per_page - 1) // per_page)
    page           = max(1, min(page, total_pages))
    page_route_ids = all_route_ids[(page - 1) * per_page : page * per_page]

    # Pre-filter trip index to matching routes for speed
    tdf = trip_index[trip_index["route_id"].isin(page_route_ids)].copy()

    routes = []
    for route_id in page_route_ids:
        rdf         = tdf[tdf["route_id"] == route_id]
        if rdf.empty:
            continue
        route_label     = str(rdf["route_label"].iloc[0])
        operator        = str(rdf["operator"].iloc[0])
        route_long_name = str(rdf["route_long_name"].iloc[0]) if "route_long_name" in rdf.columns else ""
        route_type      = str(rdf["route_type"].iloc[0])      if "route_type"      in rdf.columns else ""
        mode_icon, mode_label = _transport_mode(route_type)

        # For each trip, find the earliest stop (by stop_sequence or arrival_time)
        # then sort trips by that time and take the first max_cols
        trip_first = (
            rdf.sort_values("stop_sequence")
            .groupby("trip_id")["arrival_time"]
            .first()
            .sort_values()
        )
        selected_trip_ids = trip_first.index[:max_cols].tolist()
        col_times = [_fmt(trip_first[tid]) for tid in selected_trip_ids]

        # Determine stop order using stop_sequence within the first trip
        first_trip_id  = selected_trip_ids[0] if selected_trip_ids else None
        if first_trip_id:
            ordered_stops = (
                rdf[rdf["trip_id"] == first_trip_id]
                .sort_values("stop_sequence")[["stop_id", "stop_name"]]
                .drop_duplicates("stop_id")
            )
        else:
            ordered_stops = rdf[["stop_id", "stop_name"]].drop_duplicates("stop_id")

        # Build per-stop rows: one cell per selected trip
        # Pivot: trip_id → arrival_time for this stop
        trip_stop_map: dict[str, dict] = {}
        for tid in selected_trip_ids:
            trows = rdf[rdf["trip_id"] == tid].set_index("stop_id")["arrival_time"]
            trip_stop_map[tid] = trows.to_dict()

        stop_rows = []
        for _, srow in ordered_stops.iterrows():
            sid  = srow["stop_id"]
            name = str(srow["stop_name"])
            cells = []
            for tid in selected_trip_ids:
                t = trip_stop_map[tid].get(sid)
                cells.append(_fmt(t) if t is not None and not pd.isna(t) else None)
            stop_rows.append({"stop_id": sid, "stop_name": name, "times": cells})

        min_t = _fmt(rdf["arrival_time"].min())
        max_t = _fmt(rdf["arrival_time"].max())

        routes.append({
            "route_id":        str(route_id),
            "route_label":     route_label,
            "route_long_name": route_long_name,
            "operator":        operator,
            "mode_icon":       mode_icon,
            "mode_label":      mode_label,
            "trip_count":      len(trip_first),
            "stop_count":      len(ordered_stops),
            "col_count":       len(col_times),
            "col_times":       col_times,
            "stops":           stop_rows,
            "first_dep":       min_t[:5] if len(min_t) >= 5 else min_t,
            "last_dep":        max_t[:5] if len(max_t) >= 5 else max_t,
        })

    return {
        "routes":      routes,
        "total":       total,
        "page":        page,
        "per_page":    per_page,
        "total_pages": total_pages,
        "operators":   all_operators,
    }


@app.get("/gtfs-routes", include_in_schema=False)
async def gtfs_routes_page(
    request: Request,
    page:     int = 1,
    search:   str = "",
    operator: str = "",
):
    """GTFS routes browser — displays existing routes from the loaded feed."""
    gtfs_ready = _gtfs_is_ready()
    stats = {}
    table = {"routes": [], "total": 0, "page": 1, "per_page": 25,
             "total_pages": 0, "operators": []}

    if gtfs_ready:
        stats = {
            "total_routes":     int(_gtfs_index["route_id"].nunique()),
            "total_stops":      int(_gtfs_index["stop_id"].nunique()),
            "total_operators":  int(_gtfs_index["operator"].nunique()),
            "total_departures": len(_gtfs_index),
        }
        table = _build_gtfs_route_timetables(
            _gtfs_trip_index,
            _gtfs_index,
            page=page,
            search=search,
            operator_filter=operator,
        )

    # Build a human-readable service day label, e.g. "Tuesday 8 Apr 2026"
    _DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    if _gtfs_service_date is not None:
        _d = _gtfs_service_date
        service_day_label = f"{_DAY_NAMES[_d.weekday()]} {_d.day} {_d.strftime('%b')} {_d.year}"
    else:
        service_day_label = "scheduled service"

    return templates.TemplateResponse(request, "gtfs_routes.html", {
        "active_page":       "gtfs",
        "gtfs_ready":        gtfs_ready,
        "stats":             stats,
        "table":             table,
        "search":            search,
        "operator":          operator,
        "service_day_label": service_day_label,
    })


@app.get("/gtfs-stops", include_in_schema=False)
async def gtfs_stops_page(
    request:  Request,
    page:     int = 1,
    search:   str = "",
    operator: str = "",
    route:    str = "",
):
    """GTFS stops browser — per-stop service info with geo coordinates."""
    gtfs_ready = _gtfs_is_ready()
    per_page   = 50
    stops_data = {"stops": [], "total": 0, "page": 1, "per_page": per_page,
                  "total_pages": 0, "operators": [], "routes": []}

    if gtfs_ready:
        df = _gtfs_index.copy()

        # Build filter lists from full index before filtering
        all_operators = sorted(df["operator"].dropna().unique().tolist())
        all_routes    = (
            df[["route_id", "route_label"]]
            .drop_duplicates("route_id")
            .sort_values("route_label")
            [["route_id", "route_label"]]
            .to_dict("records")
        )

        if search:
            # Also match against stop_code (NaPTAN short code) if a reverse map exists.
            stop_ids_matching_code: set = set()
            if _gtfs_stop_code_map:
                stop_ids_matching_code = {
                    sid for sid, code in _gtfs_stop_code_map.items()
                    if search.lower() in code.lower()
                }
            mask = (
                df["stop_name"].str.contains(search, case=False, na=False) |
                df["stop_id"].str.contains(search, case=False, na=False) |
                df["stop_id"].isin(stop_ids_matching_code)
            )
            df = df[mask]
        if operator:
            df = df[df["operator"].str.contains(operator, case=False, na=False)]
        if route:
            df = df[df["route_id"] == route]

        # One row per unique stop — aggregate the services at each stop
        stop_agg = (
            df.groupby("stop_id")
            .agg(
                stop_name   =("stop_name",  "first"),
                stop_lat    =("stop_lat",   "first"),
                stop_lon    =("stop_lon",   "first"),
                num_services=("route_id",   "nunique"),
                routes      =("route_label", lambda s: sorted(s.dropna().unique().tolist())),
                operators   =("operator",    lambda s: sorted(s.dropna().unique().tolist())),
                first_dep   =("arrival_time","min"),
                last_dep    =("arrival_time","max"),
            )
            .reset_index()
            .sort_values("stop_name")
        )

        def _fmt_td(td):
            if pd.isna(td):
                return ""
            raw = str(td)
            raw = raw.split("days")[-1].strip() if "days" in raw else raw
            return raw[:5] if len(raw) >= 5 else raw

        stop_agg["first_dep"] = stop_agg["first_dep"].apply(_fmt_td)
        stop_agg["last_dep"]  = stop_agg["last_dep"].apply(_fmt_td)
        stop_agg["stop_code"] = stop_agg["stop_id"].map(_gtfs_stop_code_map).fillna("")

        total       = len(stop_agg)
        total_pages = max(1, (total + per_page - 1) // per_page)
        page        = max(1, min(page, total_pages))
        page_stops  = stop_agg.iloc[(page - 1) * per_page : page * per_page]

        stops_data = {
            "stops":       page_stops.to_dict("records"),
            "total":       total,
            "page":        page,
            "per_page":    per_page,
            "total_pages": total_pages,
            "operators":   all_operators,
            "routes":      all_routes,
        }

    return templates.TemplateResponse(request, "gtfs_stops.html", {
        "active_page": "gtfs_stops",
        "gtfs_ready":  gtfs_ready,
        "data":        stops_data,
        "search":      search,
        "operator":    operator,
        "route":       route,
        "total_stops": int(_gtfs_index["stop_id"].nunique()) if gtfs_ready else 0,
    })


@app.get("/results/{ref_id}", include_in_schema=False)
async def results_page(request: Request, ref_id: str):
    """Stop-level analysis results page for a specific route submission."""
    data = get_analysis_by_ref(ref_id, cfg.results_path)
    if not data:
        raise HTTPException(status_code=404, detail="Analysis not found.")

    stop_analysis     = data.get("stop_analysis", [])
    considered_routes = _compile_considered_routes(stop_analysis, _gtfs_index)
    sections          = _build_sections_view(stop_analysis)
    nearby_routes     = _find_nearby_routes(stop_analysis, _gtfs_index)

    # Back-fill coordinates into legacy analysis results that pre-date
    # the stop_lat/stop_lon fields (added 2026-05).  Mutates in-memory
    # only — does not rewrite the JSON file on disk.
    if not _gtfs_index.empty and stop_analysis:
        _coord_lookup: dict[str, tuple] = (
            _gtfs_index.drop_duplicates("stop_id")
            .set_index("stop_id")[["stop_lat", "stop_lon"]]
            .apply(lambda r: (r["stop_lat"], r["stop_lon"]), axis=1)
            .to_dict()
        )
        for stop in stop_analysis:
            if stop.get("stop_lat") is None and stop["stop_id"] in _coord_lookup:
                stop["stop_lat"], stop["stop_lon"] = _coord_lookup[stop["stop_id"]]

    # Enrich conflicting_services with route_label for display in stop detail rows.
    if not _gtfs_index.empty and "route_label" in _gtfs_index.columns:
        _label_lookup: dict[str, str] = (
            _gtfs_index.drop_duplicates("route_id")
            .set_index("route_id")["route_label"]
            .to_dict()
        )
        for stop in stop_analysis:
            for svc in stop.get("conflicting_services", []):
                svc.setdefault("route_label", _label_lookup.get(svc["route_id"], svc["route_id"]))

    od_coverage = data.get("od_coverage", {})
    map_data    = _build_map_data(stop_analysis, nearby_routes, _gtfs_index, _gtfs_trip_index)

    return templates.TemplateResponse(request, "results.html", {
        "active_page":       "results",
        "result":            data,
        "considered_routes": considered_routes,
        "sections":          sections,
        "nearby_routes":     nearby_routes,
        "od_coverage":       od_coverage,
        "map_data":          map_data,
    })


@app.get("/results/{ref_id}/timetable", include_in_schema=False)
async def timetable_page(request: Request, ref_id: str):
    """
    Wide-format timetable page showing the submitted timetable
    (stops × departure times) and existing GTFS services that were
    close to the proposed departures.
    """
    data = get_analysis_by_ref(ref_id, cfg.results_path)
    if not data:
        raise HTTPException(status_code=404, detail="Analysis not found.")

    stop_analysis = data.get("stop_analysis", [])

    return templates.TemplateResponse(request, "timetable.html", {
        "active_page":     "results",
        "result":          data,
        "sections":        _build_sections_view(stop_analysis),
        "gtfs_comparison": _build_gtfs_comparison(stop_analysis),
    })


# ===========================================================================
# JSON API Endpoints
# ===========================================================================

@app.post("/api/v1/analyze", tags=["Analysis"], response_class=JSONResponse)
async def run_analysis(
    file:                   UploadFile = File(...),
    operator:               str        = Form(default="Unknown"),
    timing_window_minutes:  int        = Form(default=10),
    corridor_radius_metres: float      = Form(default=300.0),
    min_headway_minutes:    int        = Form(default=20),
    amber_headway_minutes:  int        = Form(default=40),
    stop_red_threshold:     int        = Form(default=4),
    stop_amber_threshold:   int        = Form(default=2),
    route_reject_ratio:     float      = Form(default=0.50),
    route_changes_ratio:    float      = Form(default=0.40),
    od_reject_ratio:        float      = Form(default=0.80),
    od_changes_ratio:       float      = Form(default=0.60),
    od_walk_radius_metres:  float      = Form(default=400.0),
    od_high_pax_threshold:  float      = Form(default=10.0),
    od_low_pax_threshold:   float      = Form(default=2.0),
):
    """
    Upload an Excel timetable proposal (.xlsx / .xls), run the
    deterministic licensing analysis against the loaded GTFS index,
    persist the result, and return the full analysis as JSON.

    Form fields:
        file:     The Excel timetable file.
        operator: The operating company name (optional, defaults to Unknown).

    Returns HTTP 503 if the GTFS feed is not loaded.
    """
    if not _gtfs_is_ready():
        raise HTTPException(
            status_code=503,
            detail=(
                "The GTFS static feed is not loaded. "
                "The server will attempt to download it on restart. "
                f"You can also place the file manually at '{cfg.static_gtfs_path}' "
                "and restart. "
                f"Feed URL: {_GTFS_STATIC_URL}"
            ),
        )

    if not (file.filename or "").endswith((".xlsx", ".xls")):
        raise HTTPException(
            status_code=400,
            detail="Invalid file format. Please upload an Excel file (.xlsx or .xls).",
        )

    # Build a per-submission Config from the form values so every threshold
    # used in this analysis is recorded and auditable.
    submission_cfg = Config(
        timing_window_minutes=timing_window_minutes,
        corridor_radius_metres=corridor_radius_metres,
        min_headway_minutes=min_headway_minutes,
        amber_headway_minutes=amber_headway_minutes,
        stop_red_threshold=stop_red_threshold,
        stop_amber_threshold=stop_amber_threshold,
        route_reject_ratio=route_reject_ratio,
        route_changes_ratio=route_changes_ratio,
        od_reject_ratio=od_reject_ratio,
        od_changes_ratio=od_changes_ratio,
        od_walk_radius_metres=od_walk_radius_metres,
        od_high_pax_threshold=od_high_pax_threshold,
        od_low_pax_threshold=od_low_pax_threshold,
    )

    tmp_path: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp:
            shutil.copyfileobj(file.file, tmp)
            tmp_path = tmp.name

        try:
            new_route_df = parse_excel_request(
                tmp_path,
                stop_coordinate_index=_gtfs_index,
                operator=operator,
                valid_stop_ids=_gtfs_all_stop_ids if _gtfs_all_stop_ids else None,
                stop_id_suffix_map=_gtfs_stop_id_suffix_map if _gtfs_stop_id_suffix_map else None,
                stop_code_map=_gtfs_stop_code_map if _gtfs_stop_code_map else None,
            )
        except (ValueError, RuntimeError) as exc:
            logger.error("Excel parsing error: %s", exc, exc_info=True)
            raise HTTPException(status_code=422, detail=f"Excel parsing error: {exc}")

        if new_route_df.empty:
            raise HTTPException(
                status_code=422,
                detail="No stop rows were found in the uploaded file.",
            )

        try:
            analysis = analyse_route(
                new_route_df,
                _gtfs_index,
                submission_cfg,
                trip_index=_gtfs_trip_index if not _gtfs_trip_index.empty else None,
                demand_index=_demand_index if _demand_index else None,
            )
        except Exception as exc:
            logger.exception("Decision engine error for file: %s", file.filename)
            raise HTTPException(status_code=500, detail=f"Analysis engine error: {exc}")

        # Sanitise NaN/Inf floats to None before saving so stored JSON is
        # always valid and the UI never renders "nan min" or similar.
        analysis = _sanitise_for_json(analysis)
        save_analysis_result(analysis["route_id"], analysis, cfg.results_path)
        logger.info(
            "Analysis complete for '%s'. Verdict: %s.",
            analysis["route_id"],
            analysis["route_verdict"],
        )

        return JSONResponse(content=analysis)

    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


@app.get("/api/v1/results", tags=["Storage"])
async def get_results():
    """List summary metadata for all stored analyses (JSON)."""
    return list_all_analyses(cfg.results_path)


@app.get("/api/v1/results/{ref_id}", tags=["Storage"])
async def get_result_detail(ref_id: str):
    """Return the full stored analysis JSON for a specific route_id."""
    data = get_analysis_by_ref(ref_id, cfg.results_path)
    if not data:
        raise HTTPException(status_code=404, detail=f"Analysis '{ref_id}' not found.")
    return data


@app.delete("/api/v1/results/{ref_id}", tags=["Storage"])
async def delete_result(ref_id: str):
    """Delete all stored result files for a given route_id."""
    count = delete_analysis(ref_id, cfg.results_path)
    if count == 0:
        raise HTTPException(
            status_code=404,
            detail=f"No stored results found for '{ref_id}'.",
        )
    return {"deleted": count, "ref_id": ref_id}


@app.get("/api/v1/powerbi/export", tags=["Reporting"])
async def export_for_powerbi(format: str = "json"):
    """
    Flattened export for Power BI. Each row is one stop-departure with
    full route-level metadata. Use ?format=csv to download as CSV.
    """
    all_summaries = list_all_analyses(cfg.results_path)
    flattened: list[dict] = []

    for summary in all_summaries:
        detail = get_analysis_by_ref(summary["ref_id"], cfg.results_path)
        if not detail:
            continue

        route_meta = {
            "route_id":             detail["route_id"],
            "operator":             detail["operator"],
            "analysed_at":          detail.get("analysed_at", ""),
            "route_verdict":        detail["route_verdict"],
            "route_recommendation": detail["route_recommendation"],
            "total_stops":          detail["total_stops"],
            "red_stops_count":      detail["red_stops"],
            "amber_stops_count":    detail["amber_stops"],
            "green_stops_count":    detail["green_stops"],
        }

        for stop in detail.get("stop_analysis", []):
            row = route_meta.copy()
            row.update({
                "stop_id":           stop["stop_id"],
                "stop_name":         stop["stop_name"],
                "arrival_time":      stop.get("arrival_time", ""),
                "service_band":      stop.get("service_band", ""),
                "risk_score":        stop.get("risk_score"),
                "stop_verdict":      stop.get("verdict", ""),
                "avg_headway":       stop.get("avg_headway_minutes"),
                "headway_basis":     stop.get("headway_basis", ""),
                "timing_conflict":   stop.get("timing_conflict", False),
                "frequency_verdict": stop.get("frequency_verdict", ""),
                "corridor_overlap":  len(stop.get("corridor_overlap", [])) > 0,
            })
            flattened.append(row)

    if format.lower() == "csv":
        df = pd.DataFrame(flattened)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".csv", mode="w") as tmp:
            csv_path = tmp.name
        df.to_csv(csv_path, index=False)
        background = BackgroundTask(os.unlink, csv_path)
        return FileResponse(
            csv_path,
            filename="analysis_export.csv",
            media_type="text/csv",
            background=background,
        )

    return flattened


@app.post("/api/v1/refresh-gtfs", tags=["Health"])
async def refresh_gtfs():
    """Triggers a background re-download of the GTFS static feed. Returns immediately."""
    global _gtfs_refresh_in_progress
    if _gtfs_refresh_in_progress:
        return {"status": "in_progress", "message": "GTFS refresh is already running in the background."}
    _gtfs_refresh_in_progress = True
    t = threading.Thread(target=_refresh_gtfs_background, daemon=True, name="gtfs-refresh")
    t.start()
    return {"status": "started", "message": "GTFS feed refresh started in the background."}


@app.get("/api/v1/refresh-gtfs/status", tags=["Health"])
async def refresh_gtfs_status():
    """Returns the current background refresh status and GTFS readiness."""
    return {
        "refresh_in_progress": _gtfs_refresh_in_progress,
        "gtfs_ready":          _gtfs_is_ready(),
        "gtfs_routes":         int(_gtfs_index["route_id"].nunique()) if _gtfs_is_ready() else 0,
        "gtfs_stops":          int(_gtfs_index["stop_id"].nunique())  if _gtfs_is_ready() else 0,
    }


@app.get("/api/v1/status", tags=["Health"])
async def status():
    """
    Health check. Returns GTFS feed readiness and record counts.
    HTTP 200 in all cases — check gtfs_ready in the response body.
    """
    gtfs_ready = _gtfs_is_ready()
    return {
        "gtfs_ready":   gtfs_ready,
        "gtfs_records": len(_gtfs_index) if gtfs_ready else 0,
        "gtfs_routes":  int(_gtfs_index["route_id"].nunique()) if gtfs_ready else 0,
        "gtfs_stops":   int(_gtfs_index["stop_id"].nunique()) if gtfs_ready else 0,
        "gtfs_path":    cfg.static_gtfs_path,
    }


# ---------------------------------------------------------------------------
# Dev runner
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("route_licensing.api.main:app", host="0.0.0.0", port=8000, reload=True)
