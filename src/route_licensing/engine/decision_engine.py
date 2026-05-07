"""
decision_engine.py
==================
Aggregate analysis engine for route licensing.

For each stop on a proposed route, runs three detectors:
    1. Timing conflict checker  — existing services within ±N minutes
    2. Corridor overlap detector — geographic proximity via DBSCAN
    3. Frequency scorer          — average headway at the stop

Aggregates per-stop verdicts (GREEN / AMBER / RED) into an overall
route verdict (APPROVE / APPROVE WITH CHANGES / REJECT).

Policy thresholds are defined as module-level constants below.
All values that carry regulatory significance should be reviewed
by the licensing team before production use.
"""

import logging
from datetime import datetime, timezone

import pandas as pd

from route_licensing.analysis.corridor_detector import detect_corridor_overlap
from route_licensing.analysis.frequency_scorer import score_stop_frequency
from route_licensing.analysis.od_checker import check_od_coverage
from route_licensing.analysis.timing_checker import check_timing_conflicts
from route_licensing.core.config import Config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Policy thresholds
# These values carry regulatory significance. Any change must be documented
# and approved by the licensing team.
# ---------------------------------------------------------------------------

# Service period band boundaries (minutes from midnight).
# A proposed stop is only compared against existing services in the same band,
# preventing a morning proposal being flagged against an evening service.
# Boundaries are illustrative — confirm with your licensing team before production use.
_BAND_AM_PEAK_END:   int = 9 * 60 + 30   # 09:30
_BAND_MIDDAY_END:    int = 16 * 60        # 16:00
_BAND_PM_PEAK_END:   int = 19 * 60        # 19:00
# Everything from 19:00 onwards is treated as "evening".

# ---------------------------------------------------------------------------
# Verdict copy
# ---------------------------------------------------------------------------

VERDICT_COLOUR: dict[str, str] = {
    "GREEN": "Low overlap: stop has limited existing service coverage.",
    "AMBER": "Moderate overlap: stop has nearby service — schedule adjustment may reduce conflict.",
    "RED":   "High overlap: stop is already well served by existing services.",
}


_BAND_LABELS: dict[str, str] = {
    "am_peak": "AM peak (before 09:30)",
    "midday":  "midday (09:30 to 16:00)",
    "pm_peak": "PM peak (16:00 to 19:00)",
    "evening": "evening (after 19:00)",
}


# ---------------------------------------------------------------------------
# Narrative explanation helpers
# ---------------------------------------------------------------------------

def _svc_label(s: dict) -> str:
    """Returns 'Route Label (Operator)' or falls back to route_id."""
    label = s.get("route_label") or s.get("route_id", "?")
    op    = s.get("operator", "")
    return f"{label} ({op})" if op else str(label)


def _explain_stop(
    stop_name: str,
    arrival_time: str,
    service_band: str,
    timing_conflict: bool,
    conflicting_services: list,
    corridor_overlap: list,
    freq_verdict: str,
    avg_headway: float | None,
    risk_score: int,
    verdict: str,
    route_label_map: dict | None = None,
) -> str:
    """
    Produces a plain-English explanation of why a stop received its verdict,
    written for a licensing officer. Leads with passenger alternatives,
    then explains scoring, then states the verdict.
    """
    label_map = route_label_map or {}
    band_label = _BAND_LABELS.get(service_band, service_band)
    parts: list[str] = []

    # ── 1. Passenger alternatives (timing conflicts) ──────────────────────────
    if timing_conflict and conflicting_services:
        alt_lines: list[str] = []
        seen_labels: set = set()
        for s in conflicting_services[:5]:
            lbl = _svc_label(s)
            t   = s.get("arrival_time", "?")[:5]
            gap = s.get("delta_minutes")
            gap_str = f"{gap:.0f} min gap" if gap is not None else ""
            alt_lines.append(f"{lbl} at {t}" + (f" ({gap_str})" if gap_str else ""))
            seen_labels.add(lbl)
        extra = len(conflicting_services) - len(alt_lines)

        alternatives_text = "; ".join(alt_lines)
        if extra > 0:
            alternatives_text += f" and {extra} further departure(s)"

        unique_services = ", ".join(sorted(seen_labels))
        parts.append(
            f"Passengers at {stop_name} during the {band_label} already have existing "
            f"service alternatives: {alternatives_text}. "
            f"These services ({unique_services}) provide equivalent travel options "
            f"within the same time window, making the proposed {arrival_time[:5]} "
            f"departure directly competitive. (Timing conflict: +2 risk points.)"
        )
    else:
        parts.append(
            f"No existing services were found within 10 minutes of the proposed "
            f"{arrival_time[:5]} departure at {stop_name} during the {band_label}, "
            f"so passengers have no close alternative at this time."
        )

    # ── 2. Corridor / geographic context ─────────────────────────────────────
    if corridor_overlap:
        corridor_labels = [
            label_map.get(str(r), str(r)) for r in corridor_overlap[:4]
        ]
        routes_str = ", ".join(corridor_labels)
        if len(corridor_overlap) > 4:
            routes_str += f" and {len(corridor_overlap) - 4} others"
        parts.append(
            f"This stop is within 300 m of an established service corridor used by: "
            f"{routes_str}. The stop therefore sits inside an area with "
            f"existing geographic coverage. (+1 risk point.)"
        )
    else:
        parts.append(
            f"This stop is not within 300 m of any existing high density service corridor, "
            f"suggesting limited existing geographic coverage at this location."
        )

    # Frequency / headway
    if avg_headway is not None:
        hw_str = f"{avg_headway:.0f} min"
        if freq_verdict == "well_served":
            parts.append(
                f"Existing services at this stop during the {band_label} run every "
                f"{hw_str} on average — passengers already have a frequent, reliable "
                f"alternative with no meaningful gap in provision. (+2 risk points.)"
            )
        elif freq_verdict == "moderate":
            parts.append(
                f"Existing services run every {hw_str} on average during the {band_label} "
                f"— moderately served, but some additional capacity could be justified. "
                f"(+1 risk point.)"
            )
        else:
            parts.append(
                f"Existing services at this stop run every {hw_str} on average during "
                f"the {band_label} — low existing frequency, additional coverage "
                f"would benefit passengers. (No frequency penalty.)"
            )
    else:
        parts.append(
            f"No scheduled services were found at this stop during the {band_label}, "
            f"confirming it is currently unserved in this period."
        )

    # Scoring and verdict
    score_parts: list[str] = []
    if timing_conflict:
        score_parts.append("timing conflict (+2)")
    if corridor_overlap:
        score_parts.append("corridor overlap (+1)")
    if freq_verdict == "well_served":
        score_parts.append("well served frequency (+2)")
    elif freq_verdict == "moderate":
        score_parts.append("moderate frequency (+1)")
    score_breakdown = ", ".join(score_parts) if score_parts else "no risk factors"

    verdict_text = {
        "GREEN": (
            f"Risk score {risk_score}/5 ({score_breakdown}). "
            f"Stop {stop_name} at {arrival_time[:5]}: low overlap — "
            f"limited existing coverage at this stop and time of day."
        ),
        "AMBER": (
            f"Risk score {risk_score}/5 ({score_breakdown}). "
            f"Stop {stop_name} at {arrival_time[:5]}: moderate overlap — "
            f"partial overlap with existing services. A timing shift of 15 to 20 minutes "
            f"would reduce corridor duplication."
        ),
        "RED": (
            f"Risk score {risk_score}/5 ({score_breakdown}). "
            f"Stop {stop_name} at {arrival_time[:5]}: high overlap — "
            f"passengers already have adequate alternatives at this stop and time."
        ),
    }
    parts.append(verdict_text.get(verdict, f"Risk score {risk_score}/5."))

    return " ".join(p for p in parts if p)


def _explain_trip(
    section_title: str,
    trip_idx: int,
    first_departure: str,
    t_total: int,
    t_red: int,
    t_amber: int,
    t_green: int,
    trip_verdict: str,
) -> str:
    """Plain-English explanation for a trip-level verdict."""
    pct_red   = round(100 * t_red   / t_total) if t_total else 0
    pct_amber = round(100 * t_amber / t_total) if t_total else 0
    pct_green = round(100 * t_green / t_total) if t_total else 0

    intro = (
        f"Trip {trip_idx + 1} of the '{section_title}' section departs at "
        f"{first_departure[:5]}. Across its {t_total} stop-departure(s): "
        f"{t_green} ({pct_green}%) low overlap, "
        f"{t_amber} ({pct_amber}%) moderate overlap, and "
        f"{t_red} ({pct_red}%) high overlap with existing services."
    )

    if trip_verdict == "RED":
        conclusion = (
            f"The majority of stops have high overlap with existing services. "
            f"Significant corridor duplication detected — rerouting or rescheduling is recommended."
        )
    elif trip_verdict == "AMBER":
        conclusion = (
            f"A significant proportion of stops have timing conflicts or moderate overlap. "
            f"Adjusting departure times by 15 to 20 minutes would reduce corridor duplication."
        )
    else:
        conclusion = (
            f"The majority of stops have low overlap with existing services. "
            f"The trip serves areas without equivalent existing coverage."
        )

    return f"{intro} {conclusion}"


def _explain_route(
    route_id: str,
    total_trips: int,
    red_trips: int,
    amber_trips: int,
    green_trips: int,
    total_stops: int,
    red_stops: int,
    amber_stops: int,
    green_stops: int,
    route_verdict: str,
    verdict_driver: str = "stop_trip_scoring",
    od_coverage: dict | None = None,
) -> str:
    """Plain-English executive summary for the overall route verdict."""
    pct_red_t   = round(100 * red_trips   / total_trips) if total_trips else 0
    pct_green_t = round(100 * green_trips / total_trips) if total_trips else 0

    # When the OD journey check is the primary signal, base the explanation on
    # journey-level results only. Stop-level RED counts can be high even when the
    # end-to-end journeys are unique — including them creates a false contradiction.
    if verdict_driver != "stop_trip_scoring" and od_coverage:
        total_od  = od_coverage.get("total_pairs", 0)
        uncovered = od_coverage.get("uncovered_pairs", 0)
        t1        = od_coverage.get("tier1_pairs", 0)
        t2        = od_coverage.get("tier2_pairs", 0)
        t3        = od_coverage.get("tier3_pairs", 0)
        t1_pct    = od_coverage.get("tier1_pct", 0.0)
        t2_pct    = od_coverage.get("tier2_pct", 0.0)
        t3_pct    = od_coverage.get("tier3_pct", 0.0)
        uncov_pct = round(100 * uncovered / total_od) if total_od else 0

        if verdict_driver == "od_approve":
            if uncov_pct >= 40:
                journey_summary = (
                    f"The end-to-end journey check found that {uncovered} of {total_od} proposed "
                    f"journeys ({uncov_pct}%) have no equivalent on the existing network. "
                    f"No significant journey-level overlap detected."
                )
            elif t2 > 0 or t3 > 0:
                covered_desc = []
                if t2 > 0:
                    covered_desc.append(f"{t2} via interchange ({t2_pct}%)")
                if t3 > 0:
                    covered_desc.append(f"{t3} via walk access ({t3_pct}%)")
                journey_summary = (
                    f"Of {total_od} proposed journeys, {uncovered} are not served by any existing route. "
                    f"Some journeys are coverable indirectly: {', '.join(covered_desc)}. "
                    f"Direct coverage is below the threshold that indicates significant duplication."
                )
            else:
                journey_summary = (
                    f"The end-to-end journey check found no significant overlap across {total_od} "
                    f"proposed journeys."
                )
        elif verdict_driver in ("od_tier1", "od_tier1_unmet_demand"):
            journey_summary = (
                f"{t1} of {total_od} proposed journeys ({t1_pct}%) are already directly served "
                f"by an existing route in the same time period."
            )
        elif verdict_driver == "od_tier2":
            journey_summary = (
                f"{t2} of {total_od} proposed journeys ({t2_pct}%) can be made via a single "
                f"interchange on existing services."
            )
        elif verdict_driver == "od_tier3":
            journey_summary = (
                f"{t3} of {total_od} proposed journeys ({t3_pct}%) are accessible by walking "
                f"to a nearby stop and catching an existing service."
            )
        else:
            journey_summary = f"Journey check assessed {total_od} proposed A→B pairs."

        if red_stops > 0:
            stop_note = (
                f" Note: {red_stops} of {total_stops} stop-departures share geography with "
                f"existing services — this is stop-level evidence only and does not determine "
                f"the journey overlap assessment."
            )
        else:
            stop_note = ""

        return (
            f"Route '{route_id}' — {total_trips} trip(s), {total_stops} stop-departure(s). "
            f"{journey_summary}{stop_note}"
        )

    # Fallback path: no OD data — use stop/trip scoring summary only.
    pct_amber_t = round(100 * amber_trips / total_trips) if total_trips else 0

    summary = (
        f"Route '{route_id}' was evaluated across {total_trips} trip(s) and "
        f"{total_stops} stop-departure(s) using stop-level scoring (OD journey check unavailable). "
        f"{green_trips} ({pct_green_t}%) trips have low stop overlap, "
        f"{amber_trips} ({pct_amber_t}%) moderate, "
        f"{red_trips} ({pct_red_t}%) high overlap with existing services."
    )

    if route_verdict == "REJECT":
        context = (
            f"A majority of trips have high stop overlap with existing high frequency services. "
            f"Review stop selection and departure times to reduce corridor duplication."
        )
    elif route_verdict == "APPROVE WITH CHANGES":
        context = (
            f"A significant number of trips have timing conflicts or moderate stop overlap. "
            f"Adjusting departure times by 15–20 minutes at the affected stops would reduce duplication."
        )
    else:
        context = f"The majority of trips have low stop overlap with existing services."

    return f"{summary} {context}"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _od_verdict(od_coverage: dict, config: Config) -> tuple[str, str, str]:
    """
    Compute the OD signal verdict independently of stop/trip scoring.

    Returns (verdict, driver, recommendation).

    Logic (in priority order):
      Tier 1 high + no unmet demand  -> REJECT
      Tier 1 high + unmet demand     -> CHANGES  (capacity gap justifies new route)
      Tier 1 moderate                -> CHANGES
      Tier 2 moderate                -> CHANGES  (two-leg interchange journey possible)
      Tier 3 moderate                -> CHANGES  (walk access only, softer signal)
      Low demand signal              -> CHANGES  (commercial viability risk)
      Otherwise                      -> APPROVE  (no OD signal)
    """
    total_od  = od_coverage.get("total_pairs", 0)
    if total_od == 0:
        return "APPROVE", "stop_trip_scoring", ""

    tier1_pct = od_coverage.get("tier1_pct", 0.0) / 100.0
    tier2_pct = od_coverage.get("tier2_pct", 0.0) / 100.0
    tier3_pct = od_coverage.get("tier3_pct", 0.0) / 100.0
    load_sig  = od_coverage.get("load_signal", "unknown")
    t1        = od_coverage.get("tier1_pairs", 0)
    t2        = od_coverage.get("tier2_pairs", 0)
    t3        = od_coverage.get("tier3_pairs", 0)
    t1_pct_s  = od_coverage.get("tier1_pct", 0.0)
    t2_pct_s  = od_coverage.get("tier2_pct", 0.0)
    t3_pct_s  = od_coverage.get("tier3_pct", 0.0)

    if tier1_pct >= config.od_reject_ratio:
        if load_sig == "unmet_demand":
            return (
                "APPROVE WITH CHANGES",
                "od_tier1_unmet_demand",
                f"OD journey check (capacity gap): {t1} of {total_od} journeys "
                f"({t1_pct_s}%) are already directly served, but existing services show "
                "signs of unmet demand. A new route may relieve capacity pressure; "
                "coordination with the incumbent operator is recommended.",
            )
        return (
            "REJECT",
            "od_tier1",
            f"OD journey check: {t1} of {total_od} proposed passenger journeys "
            f"({t1_pct_s}%) are already directly served by an existing route in the same "
            "time period. High journey-level overlap detected.",
        )

    if tier1_pct >= config.od_changes_ratio:
        return (
            "APPROVE WITH CHANGES",
            "od_tier1",
            f"OD journey check: {t1} of {total_od} proposed journeys ({t1_pct_s}%) already "
            f"have a direct service option. Schedule differentiation or route adjustment is recommended.",
        )

    if tier2_pct >= config.od_changes_ratio:
        return (
            "APPROVE WITH CHANGES",
            "od_tier2",
            f"OD two-leg journey check: {t2} of {total_od} proposed journeys ({t2_pct_s}%) "
            f"can already be made via a single interchange on existing services. "
            "The applicant should demonstrate why a direct service adds sufficient value "
            "over an interchange journey.",
        )

    if tier3_pct >= config.od_changes_ratio:
        return (
            "APPROVE WITH CHANGES",
            "od_tier3",
            f"OD walk access check: {t3} of {total_od} proposed journeys ({t3_pct_s}%) "
            f"can be made by walking to a nearby stop and catching an existing service. "
            "The added convenience of a direct route should be demonstrated in the application.",
        )

    if load_sig == "low_demand":
        return (
            "APPROVE WITH CHANGES",
            "od_low_demand",
            "Low demand signal: existing services on overlapping corridors carry low passenger "
            "volumes. A new direct route may further thin ridership across the network without "
            "sufficient demand to justify both services commercially.",
        )

    uncovered = od_coverage.get("uncovered_pairs", 0)
    uncovered_pct = round(100 * uncovered / total_od) if total_od else 0
    return (
        "APPROVE",
        "od_approve",
        f"OD journey check: {uncovered} of {total_od} proposed journeys ({uncovered_pct}%) "
        f"are not served by any existing route. No significant journey-level overlap detected.",
    )


def _get_service_band(arrival_time: pd.Timedelta) -> str:
    """
    Maps an arrival Timedelta to a named service period band.

    Bands are used to restrict timing conflict comparisons so that a
    proposed morning service is not flagged against existing evening
    services at the same stop.
    """
    minutes = int(arrival_time.total_seconds() // 60)
    if minutes < _BAND_AM_PEAK_END:
        return "am_peak"
    elif minutes < _BAND_MIDDAY_END:
        return "midday"
    elif minutes < _BAND_PM_PEAK_END:
        return "pm_peak"
    return "evening"


def _format_timedelta(td) -> str:
    """
    Converts a pd.Timedelta or NaT to a clean HH:MM:SS string.

    pd.Timedelta serialises by default as '0 days 08:05:00'.
    This helper strips the days prefix so the value is display-ready.
    """
    if pd.isna(td):
        return ""
    raw = str(td)
    if "days" in raw:
        return raw.split("days")[-1].strip()
    return raw


def _serialise_conflicts(conflicts: pd.DataFrame) -> list[dict]:
    """
    Converts a timing conflicts DataFrame to a JSON-safe list of dicts.

    Cleans the arrival_time column before serialisation so that
    Timedelta objects are stored as HH:MM:SS strings rather than the
    raw '0 days HH:MM:SS' representation.
    """
    if conflicts.empty:
        return []
    clean = conflicts.copy()
    clean["arrival_time"] = clean["arrival_time"].apply(_format_timedelta)
    # Ensure route_label is present even if timing_checker didn't return it.
    if "route_label" not in clean.columns:
        clean["route_label"] = clean["route_id"]
    return clean.to_dict("records")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyse_route(
    new_route: pd.DataFrame,
    existing_index: pd.DataFrame,
    config: Config,
    trip_index: pd.DataFrame | None = None,
    demand_index: dict | None = None,
) -> dict:
    """
    Runs all three detectors for every stop on the proposed route and
    aggregates results into a per-stop and overall route verdict.

    Parameters
    ----------
    new_route:
        DataFrame produced by parse_excel_request(). Required columns:
        route_id, operator, stop_id, stop_name, stop_lat, stop_lon, arrival_time.
    existing_index:
        Flat stop-service index from build_stop_service_index() or
        build_demo_index(). Required columns: stop_id, route_id,
        operator, arrival_time.
    config:
        Config instance carrying all tunable thresholds.

    Returns
    -------
    dict with keys:
        route_id, operator, analysed_at, total_stops, red_stops,
        amber_stops, green_stops, route_verdict, route_recommendation,
        stop_analysis (list of per-stop result dicts).

    Raises
    ------
    ValueError
        If new_route is empty (no stops to analyse).
    """
    if new_route.empty:
        raise ValueError(
            "new_route contains no stops. Cannot produce a verdict."
        )

    route_id = new_route["route_id"].iloc[0]
    operator = new_route["operator"].iloc[0]

    # Collect stop IDs flagged as unresolved by the parser (not in GTFS feed).
    if "_gtfs_unresolved" in new_route.columns:
        unresolved_stop_ids = sorted(
            new_route[new_route["_gtfs_unresolved"] == True]["stop_id"].unique().tolist()
        )
        new_route = new_route.drop(columns=["_gtfs_unresolved"])
    else:
        unresolved_stop_ids = []

    logger.info("Starting analysis for Route: %s (Operator: %s)", route_id, operator)

    # ------------------------------------------------------------------
    # Build route_id → route_label lookup for human-readable explanations.
    # ------------------------------------------------------------------
    if "route_label" in existing_index.columns:
        route_label_map: dict[str, str] = (
            existing_index.drop_duplicates("route_id")
            .set_index("route_id")["route_label"]
            .astype(str)
            .to_dict()
        )
    else:
        route_label_map = {}

    # ------------------------------------------------------------------
    # Step 0 — OD pair coverage (runs once for the whole route)
    #
    # For every ordered pair (A, B) of proposed stops, checks whether any
    # existing GTFS trip already serves that journey in the correct
    # direction. Requires the trip-level index (trip_id + stop_sequence).
    # ------------------------------------------------------------------
    od_coverage = check_od_coverage(
        new_route,
        trip_index if trip_index is not None else pd.DataFrame(),
        config,
        demand_index=demand_index,
    )

    # ------------------------------------------------------------------
    # Step 1 — Corridor detection (batch, runs once for the whole route)
    # ------------------------------------------------------------------
    corridor_overlap = detect_corridor_overlap(new_route, existing_index, config)

    # ------------------------------------------------------------------
    # Step 2 — Annotate the existing index with service period bands
    # so per-stop comparisons are band-scoped.
    # ------------------------------------------------------------------
    existing_index = existing_index.copy()
    existing_index["_band"] = existing_index["arrival_time"].apply(_get_service_band)

    # ------------------------------------------------------------------
    # Step 3 — Per-stop analysis
    # ------------------------------------------------------------------
    stop_results: list[dict] = []
    band_indices: dict[str, pd.DataFrame] = {
        band: existing_index[existing_index["_band"] == band]
        for band in existing_index["_band"].unique()
    }

    for _, stop in new_route.iterrows():
        stop_id   = stop["stop_id"]
        stop_band = _get_service_band(stop["arrival_time"])

        # Restrict existing index to the same service period band.
        band_index = band_indices.get(stop_band, existing_index.iloc[:0])

        timing_conflicts = check_timing_conflicts(stop, band_index, config)
        frequency        = score_stop_frequency(stop_id, band_index, config)
        overlapping      = corridor_overlap.get(stop_id, [])

        has_timing_conflict = not timing_conflicts.empty
        is_in_corridor      = len(overlapping) > 0
        freq_verdict        = frequency["frequency_verdict"]

        # Risk score — each signal contributes independently.
        # Maximum possible score is 5.
        risk_score = sum([
            2 if has_timing_conflict                          else 0,
            1 if is_in_corridor                               else 0,
            2 if freq_verdict == "well_served"                else
            1 if freq_verdict == "moderate"                   else 0,
        ])

        if risk_score >= config.stop_red_threshold:
            colour = "RED"
        elif risk_score >= config.stop_amber_threshold:
            colour = "AMBER"
        else:
            colour = "GREEN"

        arrival_str = _format_timedelta(stop["arrival_time"])
        serialised_conflicts = _serialise_conflicts(timing_conflicts)

        explanation = _explain_stop(
            stop_name            = stop["stop_name"],
            arrival_time         = arrival_str,
            service_band         = stop_band,
            timing_conflict      = has_timing_conflict,
            conflicting_services = serialised_conflicts,
            corridor_overlap     = overlapping,
            freq_verdict         = freq_verdict,
            avg_headway          = frequency["avg_headway_minutes"],
            risk_score           = risk_score,
            verdict              = colour,
            route_label_map      = route_label_map,
        )

        stop_results.append({
            "stop_id":              stop_id,
            "stop_name":            stop["stop_name"],
            "stop_location":        str(stop.get("stop_location", "") or ""),
            "section_idx":          int(stop.get("section_idx", 0) or 0),
            "section_title":        str(stop.get("section_title", "") or ""),
            "section_day_groups":   str(stop.get("section_day_groups", "[]") or "[]"),
            "trip_idx":             int(stop.get("trip_idx", 0) or 0),
            "arrival_time":         arrival_str,
            "service_band":         stop_band,
            "timing_conflict":      has_timing_conflict,
            "conflicting_services": serialised_conflicts,
            "corridor_overlap":     overlapping,
            "avg_headway_minutes":  frequency["avg_headway_minutes"],
            "headway_basis":        "combined_all_operators_same_band",
            "frequency_verdict":    freq_verdict,
            "risk_score":           risk_score,
            "verdict":              colour,
            "recommendation":       VERDICT_COLOUR[colour],
            "explanation":          explanation,
        })

    # ------------------------------------------------------------------
    # Step 4 — Aggregate stop results to trip-level verdicts
    # ------------------------------------------------------------------
    df = pd.DataFrame(stop_results)

    # Each stop row carries section_idx and trip_idx from the parser.
    # Group stops that share the same (section_idx, trip_idx) into one trip.
    trip_analysis: list[dict] = []
    for (section_idx, trip_idx), trip_df in df.groupby(
        ["section_idx", "trip_idx"], sort=True
    ):
        t_total       = len(trip_df)
        t_red         = int((trip_df["verdict"] == "RED").sum())
        t_amber       = int((trip_df["verdict"] == "AMBER").sum())
        t_green       = int((trip_df["verdict"] == "GREEN").sum())

        if t_red / t_total >= config.route_reject_ratio:
            trip_verdict = "RED"
            trip_recommendation = (
                f"{t_red}/{t_total} stops have high overlap. "
                "Trip shares corridor with an existing high frequency service."
            )
        elif (t_amber + t_red) / t_total >= config.route_changes_ratio:
            trip_verdict = "AMBER"
            trip_recommendation = (
                "Timing conflicts or moderate stop overlap detected. "
                "Schedule adjustment may reduce corridor duplication."
            )
        else:
            trip_verdict = "GREEN"
            trip_recommendation = (
                "Majority of stops have low overlap. "
                "Trip serves areas without equivalent existing coverage."
            )

        # Carry through section metadata from the first stop in the group.
        first_stop      = trip_df.iloc[0]
        section_title   = str(first_stop.get("section_title", ""))
        first_departure = str(first_stop.get("arrival_time", ""))

        trip_explanation = _explain_trip(
            section_title   = section_title,
            trip_idx        = int(trip_idx),
            first_departure = first_departure,
            t_total         = t_total,
            t_red           = t_red,
            t_amber         = t_amber,
            t_green         = t_green,
            trip_verdict    = trip_verdict,
        )

        trip_analysis.append({
            "section_idx":          int(section_idx),
            "section_title":        section_title,
            "section_day_groups":   first_stop.get("section_day_groups", "[]"),
            "trip_idx":             int(trip_idx),
            "total_stops":          t_total,
            "red_stops":            t_red,
            "amber_stops":          t_amber,
            "green_stops":          t_green,
            "trip_verdict":         trip_verdict,
            "trip_recommendation":  trip_recommendation,
            "trip_explanation":     trip_explanation,
            "stop_analysis":        trip_df.to_dict("records"),
        })

    # ------------------------------------------------------------------
    # Step 5 — OD signal verdict (independent of stop/trip scoring)
    #
    # Computed here so both signals are available for the final combination.
    # The OD signal is the primary/headline question: can passengers already
    # make these journeys?  Stop/trip scoring is the supporting detail:
    # where specifically is there duplication?
    # ------------------------------------------------------------------
    od_preliminary, od_driver, od_recommendation = _od_verdict(od_coverage, config)

    # ------------------------------------------------------------------
    # Step 6 — Aggregate stop-level results to trip verdicts
    # (retained for reporting and evidence — does not drive the verdict)
    # ------------------------------------------------------------------
    trip_verdicts = [t["trip_verdict"] for t in trip_analysis]
    total_trips   = len(trip_verdicts)
    red_trips     = trip_verdicts.count("RED")
    amber_trips   = trip_verdicts.count("AMBER")
    green_trips   = trip_verdicts.count("GREEN")

    # ------------------------------------------------------------------
    # Step 7 — Final verdict
    #
    # OD journey check (Signal A) and stop/trip scoring (Signal B) each
    # catch a different failure mode:
    #
    #   Signal A catches: the end-to-end journey is already served —
    #     passengers don't need the new route.
    #
    #   Signal B catches: the individual stops are so heavily served that
    #     adding more buses creates direct competition at those stops,
    #     even if no single existing trip covers the full A→B sequence.
    #
    # Both are valid grounds for rejection.  The final verdict is the
    # worse (higher severity) of the two signals.  OD is the primary
    # signal — when it says REJECT, stop scoring cannot lower that.
    # When OD says APPROVE, stop scoring can still escalate to REJECT
    # or CHANGES if stop-level duplication is severe.
    #
    # When OD has no data, Signal B is the sole signal and the verdict
    # banner flags that the OD check did not run.
    # ------------------------------------------------------------------
    has_od_data = od_coverage.get("total_pairs", 0) > 0

    # Derive the stop/trip verdict.
    if red_trips / total_trips >= config.route_reject_ratio:
        stop_trip_verdict        = "REJECT"
        stop_trip_recommendation = (
            f"{red_trips}/{total_trips} trips have high stop overlap with "
            "existing high frequency corridors."
        )
    elif (amber_trips + red_trips) / total_trips >= config.route_changes_ratio:
        stop_trip_verdict        = "APPROVE WITH CHANGES"
        stop_trip_recommendation = (
            "Significant timing conflicts or moderate overlap detected across trips. "
            "Schedule differentiation is recommended."
        )
    else:
        stop_trip_verdict        = "APPROVE"
        stop_trip_recommendation = (
            "The majority of trips have low stop overlap. "
            "Route serves areas without equivalent existing coverage."
        )

    if has_od_data:
        # OD journey check is the sole verdict signal.
        # Stop/trip scoring is retained for the evidence tables only.
        route_verdict        = od_preliminary
        route_recommendation = od_recommendation
        od_verdict_driver    = od_driver   # 'od_approve' | 'od_tier1' | 'od_tier2' | etc.
    else:
        # OD check produced no pairs (all stop IDs unresolved or empty
        # trip index). Fall back to stop/trip scoring and flag it.
        route_verdict        = stop_trip_verdict
        route_recommendation = (
            stop_trip_recommendation +
            " (Note: OD journey check could not run — verify GTFS stop IDs.)"
        )
        od_verdict_driver = "stop_trip_scoring"

    route_explanation = _explain_route(
        route_id       = route_id,
        total_trips    = total_trips,
        red_trips      = red_trips,
        amber_trips    = amber_trips,
        green_trips    = green_trips,
        total_stops    = sum(len(t["stop_analysis"]) for t in trip_analysis),
        red_stops      = sum(t["red_stops"]   for t in trip_analysis),
        amber_stops    = sum(t["amber_stops"] for t in trip_analysis),
        green_stops    = sum(t["green_stops"] for t in trip_analysis),
        route_verdict  = route_verdict,
        verdict_driver = od_verdict_driver,
        od_coverage    = od_coverage,
    )

    logger.info("Analysis complete for %s. Verdict: %s", route_id, route_verdict)

    # Flatten stop_results for backward-compatible stop_analysis key.
    all_stop_results = [s for t in trip_analysis for s in t["stop_analysis"]]
    total_stops  = len(all_stop_results)
    red_stops    = sum(1 for s in all_stop_results if s["verdict"] == "RED")
    amber_stops  = sum(1 for s in all_stop_results if s["verdict"] == "AMBER")
    green_stops  = sum(1 for s in all_stop_results if s["verdict"] == "GREEN")

    return {
        "route_id":             route_id,
        "operator":             operator,
        # ISO-8601 UTC timestamp stored in the result dict so that
        # storage.py never needs to parse it from a filename.
        "analysed_at":          datetime.now(timezone.utc).isoformat(),
        "total_trips":          total_trips,
        "red_trips":            red_trips,
        "amber_trips":          amber_trips,
        "green_trips":          green_trips,
        "total_stops":          total_stops,
        "red_stops":            red_stops,
        "amber_stops":          amber_stops,
        "green_stops":          green_stops,
        "route_verdict":        route_verdict,
        "route_recommendation": route_recommendation,
        "route_explanation":    route_explanation,
        "trip_analysis":        trip_analysis,
        "stop_analysis":        all_stop_results,
        "unresolved_stop_ids":  unresolved_stop_ids,
        "od_coverage":          od_coverage,
        "verdict_driver":       od_verdict_driver,
        "applied_thresholds": {
            "timing_window_minutes":  config.timing_window_minutes,
            "corridor_radius_metres": config.corridor_radius_metres,
            "min_headway_minutes":    config.min_headway_minutes,
            "amber_headway_minutes":  config.amber_headway_minutes,
            "stop_red_threshold":     config.stop_red_threshold,
            "stop_amber_threshold":   config.stop_amber_threshold,
            "route_reject_ratio":     config.route_reject_ratio,
            "route_changes_ratio":    config.route_changes_ratio,
            "od_reject_ratio":        config.od_reject_ratio,
            "od_changes_ratio":       config.od_changes_ratio,
            "od_walk_radius_metres":  getattr(config, "od_walk_radius_metres", 400.0),
            "od_high_pax_threshold":  getattr(config, "od_high_pax_threshold", 10.0),
            "od_low_pax_threshold":   getattr(config, "od_low_pax_threshold",  2.0),
        },
    }