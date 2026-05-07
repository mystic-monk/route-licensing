"""
od_checker.py
=============
Origin-Destination pair coverage analysis for proposed bus routes.

For each ordered pair (stop_A, stop_B) in the proposed route, determines
how well the existing GTFS network already serves that journey.

Coverage is assessed in three tiers:

    Tier 1  Direct coverage
        An existing trip visits stop_A then stop_B in the correct direction
        within the same time band as the proposed departure.

    Tier 2  Two-leg interchange journey
        No direct service exists, but the passenger could board an existing
        bus at stop_A, travel to an intermediate stop, and connect to a second
        existing service that reaches stop_B within a reasonable connection
        window (5 to 45 minutes wait).

    Tier 3  Walk access coverage
        No direct or interchange service exists, but the passenger could walk
        to a nearby stop (within od_walk_radius_metres) and catch an existing
        service to a stop near their destination.

Load signal
-----------
For Tier 1 covered pairs, the existing service's headway and (where demand
data is available) pax/hr are combined to classify whether existing supply
is meeting demand:

    unmet_demand  demand is high but existing services are infrequent;
                  the new route would fill a real gap.
    overloaded    demand is high and service is frequent; existing buses
                  may be running at capacity, supporting a new route.
    adequate      demand and supply appear balanced.
    low_demand    demand is low; a new route risks cannibalising sparse
                  existing ridership without commercial justification.
    unknown       no demand data available; signal based on headway only.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict

import pandas as pd

from route_licensing.core.config import Config
from route_licensing.ingestion.demand_loader import DemandIndex, infer_day_type, lookup_demand

logger = logging.getLogger(__name__)

# Service band boundaries (minutes from midnight).
_BAND_AM_PEAK_END = 9 * 60 + 30   # 09:30
_BAND_MIDDAY_END  = 16 * 60        # 16:00
_BAND_PM_PEAK_END = 19 * 60        # 19:00

# Earth radius for haversine calculation (metres)
_EARTH_R = 6_371_000.0

# Two-leg interchange transfer window (minutes)
_MIN_TRANSFER_WAIT = 5
_MAX_TRANSFER_WAIT = 45
_MAX_TRANSFER_STOPS = 150   # cap on unique transfer stops evaluated per OD pair


def _service_band(td: pd.Timedelta) -> str:
    mins = int(td.total_seconds() / 60)
    if mins < _BAND_AM_PEAK_END:
        return "am_peak"
    if mins < _BAND_MIDDAY_END:
        return "midday"
    if mins < _BAND_PM_PEAK_END:
        return "pm_peak"
    return "evening"


def _fmt_td(td: pd.Timedelta) -> str:
    raw = str(td)
    return raw.split("days")[-1].strip()[:5] if "days" in raw else raw[:5]


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Straight-line distance in metres between two WGS-84 points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * _EARTH_R * math.asin(math.sqrt(a))


def _build_stop_index(
    trip_index: pd.DataFrame,
) -> dict[str, list[tuple]]:
    """
    stop_id -> [(trip_id, stop_sequence, route_id, route_label, operator, arrival_time)]
    """
    idx: dict[str, list[tuple]] = defaultdict(list)
    for row in trip_index.itertuples(index=False):
        idx[row.stop_id].append((
            row.trip_id,
            int(row.stop_sequence),
            row.route_id,
            row.route_label,
            row.operator,
            row.arrival_time,
        ))
    return idx


def _build_trip_to_stops_index(
    trip_index: pd.DataFrame,
) -> dict[str, list[tuple]]:
    """
    trip_id -> sorted list of (stop_sequence, stop_id, arrival_time, route_id, route_label, operator).
    Used for the two-leg interchange check.
    """
    idx: dict[str, list[tuple]] = defaultdict(list)
    for row in trip_index.itertuples(index=False):
        idx[row.trip_id].append((
            int(row.stop_sequence),
            row.stop_id,
            row.arrival_time,
            row.route_id,
            row.route_label,
            row.operator,
        ))
    for trip_id in idx:
        idx[trip_id].sort(key=lambda x: x[0])
    return dict(idx)


def _build_coords(trip_index: pd.DataFrame) -> dict[str, tuple[float, float]]:
    """stop_id -> (lat, lon) from trip_index (which includes stop_lat, stop_lon)."""
    if "stop_lat" not in trip_index.columns or "stop_lon" not in trip_index.columns:
        return {}
    sub = trip_index[["stop_id", "stop_lat", "stop_lon"]].drop_duplicates("stop_id")
    return {row.stop_id: (float(row.stop_lat), float(row.stop_lon)) for row in sub.itertuples(index=False)}


def _nearby_stops(
    stop_id: str,
    coords: dict[str, tuple[float, float]],
    all_coords: dict[str, tuple[float, float]],
    radius_m: float,
) -> set[str]:
    """Return all stop_ids in all_coords within radius_m of the given stop_id (excluding itself)."""
    if stop_id not in coords:
        return set()
    lat, lon = coords[stop_id]
    return {
        sid for sid, (slat, slon) in all_coords.items()
        if sid != stop_id and _haversine_m(lat, lon, slat, slon) <= radius_m
    }


def _avg_headway_min(
    stop_idx: dict[str, list[tuple]],
    stop_id: str,
    route_id: str,
    band: str,
) -> float | None:
    """Average headway in minutes for a specific route at a stop within a service band."""
    times = sorted(
        t[5].total_seconds()
        for t in stop_idx.get(stop_id, [])
        if str(t[2]) == str(route_id) and _service_band(t[5]) == band
    )
    if len(times) < 2:
        return None
    gaps = [times[i + 1] - times[i] for i in range(len(times) - 1)]
    return sum(gaps) / len(gaps) / 60.0


def _load_signal(
    headway_min: float | None,
    pax_per_hour: float | None,
    config: Config,
) -> str:
    """
    Classify the load on existing services for this OD pair.

    Returns one of: "unmet_demand", "overloaded", "adequate", "low_demand", "unknown"
    """
    if pax_per_hour is None:
        if headway_min is None:
            return "unknown"
        if headway_min <= config.amber_headway_minutes:
            return "adequate"
        return "unknown"

    if pax_per_hour < config.od_low_pax_threshold:
        return "low_demand"

    if headway_min is None or headway_min > config.amber_headway_minutes:
        return "unmet_demand"

    if headway_min <= config.min_headway_minutes and pax_per_hour >= config.od_high_pax_threshold:
        return "overloaded"

    return "adequate"


def _aggregate_load_signal(signals: list[str]) -> str:
    """Route-level load signal from a list of per-pair signals."""
    if not signals:
        return "unknown"
    counts: dict[str, int] = defaultdict(int)
    for s in signals:
        counts[s] += 1
    for label in ("unmet_demand", "overloaded", "low_demand", "adequate", "unknown"):
        if counts[label] / len(signals) >= 0.4:
            return label
    return "adequate"


def _check_two_leg_journey(
    sid_a: str,
    sid_b: str,
    band_a: str,
    stop_idx: dict[str, list[tuple]],
    trip_to_stops: dict[str, list[tuple]],
) -> dict:
    """
    Check whether a two-leg interchange journey from sid_a to sid_b is possible
    using existing GTFS services.

    Leg 1: the passenger boards an existing service at sid_a and rides to an
    intermediate transfer stop.
    Leg 2: they board a different existing service at the transfer stop and
    continue to sid_b.

    The transfer window is between 5 and 45 minutes.
    Returns a dict with interchange details, or {} if no viable path is found.
    """
    trips_through_a: list[tuple] = [
        t for t in stop_idx.get(sid_a, [])
        if _service_band(t[5]) == band_a
    ]
    if not trips_through_a:
        return {}

    min_wait = pd.Timedelta(minutes=_MIN_TRANSFER_WAIT)
    max_wait = pd.Timedelta(minutes=_MAX_TRANSFER_WAIT)
    checked_transfer_stops: set[str] = set()

    for (trip_id_1, seq_a, route_id_1, route_label_1, op_1, time_a_dep) in trips_through_a:
        trip_stops_1 = trip_to_stops.get(trip_id_1, [])

        for (seq_t, sid_t, arr_t, _, _, _) in trip_stops_1:
            if seq_t <= seq_a or sid_t == sid_b:
                continue
            if sid_t in checked_transfer_stops:
                continue
            if len(checked_transfer_stops) >= _MAX_TRANSFER_STOPS:
                break
            checked_transfer_stops.add(sid_t)

            earliest_dep = arr_t + min_wait
            latest_dep   = arr_t + max_wait

            for (trip_id_2, seq_t2, route_id_2, route_label_2, op_2, dep_t2) in stop_idx.get(sid_t, []):
                if dep_t2 < earliest_dep or dep_t2 > latest_dep:
                    continue
                if str(route_id_2) == str(route_id_1):
                    continue

                trip_stops_2 = trip_to_stops.get(trip_id_2, [])
                for (seq_b2, sid_b2, _, _, _, _) in trip_stops_2:
                    if sid_b2 == sid_b and seq_b2 > seq_t2:
                        transfer_wait = (dep_t2 - arr_t).total_seconds() / 60.0
                        return {
                            "transfer_stop_id":    sid_t,
                            "leg1_route": {
                                "route_id":    str(route_id_1),
                                "route_label": str(route_label_1),
                                "operator":    str(op_1),
                            },
                            "leg2_route": {
                                "route_id":    str(route_id_2),
                                "route_label": str(route_label_2),
                                "operator":    str(op_2),
                            },
                            "transfer_wait_min": round(transfer_wait, 1),
                        }

    return {}


def check_od_coverage(
    new_route: pd.DataFrame,
    trip_index: pd.DataFrame,
    config: Config,
    demand_index: DemandIndex | None = None,
) -> dict:
    """
    For every ordered pair (stop_A, stop_B) in the proposed route, assess
    how well the existing GTFS network already covers that journey.

    Parameters
    ----------
    new_route:
        Flat stop-level DataFrame from parse_excel_request().
    trip_index:
        GTFS trip-level index from build_trip_index(). Must include:
        trip_id, stop_id, stop_sequence, route_id, route_label, operator,
        arrival_time, stop_lat, stop_lon.
    config:
        Config instance with thresholds.
    demand_index:
        Optional DemandIndex from demand_loader.

    Returns
    -------
    dict with keys:
        total_pairs           int
        tier1_pairs           int   directly covered, same time band
        tier2_pairs           int   two-leg interchange journey
        tier3_pairs           int   walk access only
        uncovered_pairs       int
        coverage_pct          float tier1_pairs / total * 100 (backward compat)
        tier1_pct             float
        tier2_pct             float
        tier3_pct             float
        load_signal           str
        od_results            list[dict]
        demand_loaded         bool
    """
    if trip_index.empty or new_route.empty:
        return _empty_result(demand_loaded=demand_index is not None)

    required = {"trip_id", "stop_id", "stop_sequence", "route_id", "route_label", "operator", "arrival_time"}
    if not required.issubset(trip_index.columns):
        logger.warning("trip_index missing columns %s; OD check skipped.", required - set(trip_index.columns))
        return _empty_result(demand_loaded=demand_index is not None)

    stop_idx        = _build_stop_index(trip_index)
    trip_to_stops   = _build_trip_to_stops_index(trip_index)
    all_gtfs_coords = _build_coords(trip_index)

    proposed_stop_ids: set[str] = set(new_route["stop_id"].dropna().unique())
    proposed_coords: dict[str, tuple[float, float]] = {
        sid: all_gtfs_coords[sid]
        for sid in proposed_stop_ids
        if sid in all_gtfs_coords
    }

    walk_radius = getattr(config, "od_walk_radius_metres", 400.0)
    nearby_cache: dict[str, set[str]] = {
        sid: _nearby_stops(sid, proposed_coords, all_gtfs_coords, walk_radius)
        for sid in proposed_stop_ids
    }

    od_map: dict[tuple, dict] = {}

    for (section_idx, trip_idx), trip_df in new_route.groupby(
        ["section_idx", "trip_idx"], sort=True
    ):
        stops = trip_df.sort_index().reset_index(drop=True)
        n = len(stops)
        if n < 2:
            continue

        section_title = str(stops.iloc[0].get("section_title", ""))

        for i in range(n):
            stop_a = stops.iloc[i]
            sid_a  = stop_a["stop_id"]
            time_a = stop_a["arrival_time"]
            band_a = _service_band(time_a)

            trips_at_a: dict[str, tuple] = {
                t[0]: (t[1], t[5])
                for t in stop_idx.get(sid_a, [])
            }

            for j in range(i + 1, n):
                stop_b = stops.iloc[j]
                sid_b  = stop_b["stop_id"]
                key    = (sid_a, sid_b)
                if key in od_map:
                    continue

                # Tier 1: direct coverage
                covering: list[dict] = []
                same_band_found = False

                for (trip_id, seq_b, route_id, route_label, operator, time_b) in stop_idx.get(sid_b, []):
                    if trip_id not in trips_at_a:
                        continue
                    seq_a, ex_time_a = trips_at_a[trip_id]
                    if seq_b <= seq_a:
                        continue
                    covering.append({"route_id": str(route_id), "route_label": str(route_label), "operator": str(operator)})
                    if _service_band(ex_time_a) == band_a:
                        same_band_found = True

                covering = _dedup(covering)

                # Tier 2: two-leg interchange journey
                tier2_result: dict = {}
                if not same_band_found and not covering:
                    tier2_result = _check_two_leg_journey(sid_a, sid_b, band_a, stop_idx, trip_to_stops)

                # Tier 3: walk access coverage
                tier3_result: dict = {}
                if not same_band_found and not covering and not tier2_result:
                    nearby_a = nearby_cache.get(sid_a, set())
                    nearby_b = nearby_cache.get(sid_b, set())

                    for near_a in nearby_a:
                        trips_at_near_a: dict[str, tuple] = {
                            t[0]: (t[1], t[5])
                            for t in stop_idx.get(near_a, [])
                            if _service_band(t[5]) == band_a
                        }
                        if not trips_at_near_a:
                            continue

                        for near_b in nearby_b:
                            for (trip_id, seq_nb, route_id, route_label, operator, time_nb) in stop_idx.get(near_b, []):
                                if trip_id not in trips_at_near_a:
                                    continue
                                seq_na, _ = trips_at_near_a[trip_id]
                                if seq_nb <= seq_na:
                                    continue
                                dist_a = _haversine_m(
                                    *proposed_coords.get(sid_a, (0.0, 0.0)),
                                    *all_gtfs_coords.get(near_a, (0.0, 0.0)),
                                ) if sid_a in proposed_coords and near_a in all_gtfs_coords else None
                                dist_b = _haversine_m(
                                    *proposed_coords.get(sid_b, (0.0, 0.0)),
                                    *all_gtfs_coords.get(near_b, (0.0, 0.0)),
                                ) if sid_b in proposed_coords and near_b in all_gtfs_coords else None
                                tier3_result = {
                                    "walk_origin_stop":   near_a,
                                    "walk_dest_stop":     near_b,
                                    "walk_origin_dist_m": round(dist_a, 0) if dist_a is not None else None,
                                    "walk_dest_dist_m":   round(dist_b, 0) if dist_b is not None else None,
                                    "tier3_route": {"route_id": str(route_id), "route_label": str(route_label), "operator": str(operator)},
                                }
                                break
                            if tier3_result:
                                break
                        if tier3_result:
                            break

                # Determine coverage tier
                if same_band_found:
                    coverage_tier = 1
                elif covering:
                    coverage_tier = 1
                elif tier2_result:
                    coverage_tier = 2
                elif tier3_result:
                    coverage_tier = 3
                else:
                    coverage_tier = None

                primary_route_id = (
                    covering[0]["route_id"] if covering else
                    tier2_result.get("leg1_route", {}).get("route_id") if tier2_result else
                    tier3_result.get("tier3_route", {}).get("route_id") if tier3_result else
                    None
                )
                headway = _avg_headway_min(stop_idx, sid_a, primary_route_id, band_a) if primary_route_id else None

                pax: float | None = None
                if demand_index:
                    raw_dg   = str(stops.iloc[0].get("section_day_groups", ""))
                    day_type = infer_day_type(raw_dg) if raw_dg else "weekday"
                    pax      = lookup_demand(demand_index, sid_a, sid_b, day_type, band_a)

                pair_load = _load_signal(headway, pax, config)

                od_map[key] = {
                    "from_stop_id":       sid_a,
                    "from_stop_name":     str(stop_a.get("stop_name", sid_a)),
                    "to_stop_id":         sid_b,
                    "to_stop_name":       str(stop_b.get("stop_name", sid_b)),
                    "section_title":      section_title,
                    "proposed_departure": _fmt_td(time_a),
                    "service_band":       band_a,
                    "coverage_tier":      coverage_tier,
                    "same_band_covered":  same_band_found,
                    "covering_routes":    covering,
                    # Tier 2 two-leg interchange fields
                    "tier2_transfer_stop":     tier2_result.get("transfer_stop_id"),
                    "tier2_leg1_route":         tier2_result.get("leg1_route"),
                    "tier2_leg2_route":         tier2_result.get("leg2_route"),
                    "tier2_transfer_wait_min":  tier2_result.get("transfer_wait_min"),
                    # Tier 3 walk access fields
                    "walk_origin_stop":   tier3_result.get("walk_origin_stop"),
                    "walk_dest_stop":     tier3_result.get("walk_dest_stop"),
                    "walk_origin_dist_m": tier3_result.get("walk_origin_dist_m"),
                    "walk_dest_dist_m":   tier3_result.get("walk_dest_dist_m"),
                    "tier3_route":        tier3_result.get("tier3_route"),
                    "avg_headway_min":    round(headway, 1) if headway is not None else None,
                    "load_signal":        pair_load,
                    "pax_per_hour":       pax,
                    "has_demand_data":    pax is not None,
                    "is_direct":          bool(covering),
                }

    # Aggregate
    total_pairs   = _count_unique_od_pairs(new_route)
    all_entries   = list(od_map.values())
    tier1_entries = [e for e in all_entries if e["coverage_tier"] == 1]
    tier2_entries = [e for e in all_entries if e["coverage_tier"] == 2]
    tier3_entries = [e for e in all_entries if e["coverage_tier"] == 3]
    uncovered     = total_pairs - len(tier1_entries) - len(tier2_entries) - len(tier3_entries)

    tier1_same_band = sum(1 for e in tier1_entries if e["same_band_covered"])
    tier1_pct = round(len(tier1_entries) / total_pairs * 100, 1) if total_pairs else 0.0
    tier2_pct = round(len(tier2_entries) / total_pairs * 100, 1) if total_pairs else 0.0
    tier3_pct = round(len(tier3_entries) / total_pairs * 100, 1) if total_pairs else 0.0

    route_load = _aggregate_load_signal([e["load_signal"] for e in tier1_entries + tier2_entries])

    logger.info(
        "OD coverage: Tier1=%d, Tier2=%d (interchange), Tier3=%d (walk access), uncovered=%d / %d total. Load: %s",
        len(tier1_entries), len(tier2_entries), len(tier3_entries), uncovered, total_pairs, route_load,
    )

    tier1_entries.sort(key=lambda x: -len(x["covering_routes"]))
    tier2_entries.sort(key=lambda x: x.get("tier2_transfer_wait_min") or 9999)
    tier3_entries.sort(key=lambda x: x.get("walk_origin_dist_m") or 9999)

    return {
        "total_pairs":          total_pairs,
        "tier1_pairs":          len(tier1_entries),
        "tier2_pairs":          len(tier2_entries),
        "tier3_pairs":          len(tier3_entries),
        "uncovered_pairs":      max(uncovered, 0),
        "band_matched_pairs":   tier1_same_band,
        # backward compat
        "covered_pairs":        len(tier1_entries),
        "reverse_only_pairs":   0,
        "coverage_pct":         tier1_pct,
        "tier1_pct":            tier1_pct,
        "tier2_pct":            tier2_pct,
        "tier3_pct":            tier3_pct,
        "load_signal":          route_load,
        "od_results":           tier1_entries + tier2_entries + tier3_entries,
        "demand_loaded":        demand_index is not None,
    }


def _dedup(routes: list[dict]) -> list[dict]:
    seen: set = set()
    out: list[dict] = []
    for r in routes:
        if r["route_id"] not in seen:
            seen.add(r["route_id"])
            out.append(r)
    return out


def _count_unique_od_pairs(new_route: pd.DataFrame) -> int:
    pairs: set = set()
    for (_, _), trip_df in new_route.groupby(["section_idx", "trip_idx"], sort=True):
        stops = trip_df.reset_index(drop=True)["stop_id"].tolist()
        n = len(stops)
        for i in range(n):
            for j in range(i + 1, n):
                pairs.add((stops[i], stops[j]))
    return len(pairs)


def _empty_result(demand_loaded: bool = False) -> dict:
    return {
        "total_pairs":        0,
        "tier1_pairs":        0,
        "tier2_pairs":        0,
        "tier3_pairs":        0,
        "uncovered_pairs":    0,
        "band_matched_pairs": 0,
        "covered_pairs":      0,
        "reverse_only_pairs": 0,
        "coverage_pct":       0.0,
        "tier1_pct":          0.0,
        "tier2_pct":          0.0,
        "tier3_pct":          0.0,
        "load_signal":        "unknown",
        "od_results":         [],
        "demand_loaded":      demand_loaded,
    }
