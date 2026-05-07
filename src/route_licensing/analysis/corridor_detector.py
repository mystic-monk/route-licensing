"""
corridor_detector.py
====================
Uses DBSCAN clustering in projected CRS (EPSG:2157 Irish Transverse
Mercator) to identify whether stops on a proposed route fall within
geographic corridors already served by existing routes.

Stops with missing coordinates (stop_lat or stop_lon is None/NaN) are
excluded from the DBSCAN input and returned with an empty overlap list.
This occurs when the submitted timetable contains stop IDs that are not
present in the loaded GTFS feed (e.g. Cork stops against Dublin demo
data). Timing conflict and frequency analysis still run normally for
those stops; only corridor overlap is unavailable.
"""

import logging

import geopandas as gpd
import numpy as np
import pandas as pd
from sklearn.cluster import DBSCAN

from route_licensing.core.config import Config

logger = logging.getLogger(__name__)


def detect_corridor_overlap(
    new_route: pd.DataFrame,
    existing_index: pd.DataFrame,
    config: Config,
) -> dict[str, list[str]]:
    """
    Detects geographic corridor overlap between a proposed route and
    existing services.

    Parameters
    ----------
    new_route:
        DataFrame of proposed stops. Required columns: stop_id,
        stop_lat, stop_lon. Rows where stop_lat or stop_lon is NaN
        are skipped and returned with an empty overlap list.
    existing_index:
        Flat stop-service index from the loaded GTFS feed. Required
        columns: stop_id, stop_lat, stop_lon, route_id.
    config:
        Config instance. Uses corridor_radius_metres, dbscan_min_samples,
        crs_geographic, and crs_projected.

    Returns
    -------
    dict mapping stop_id -> list of overlapping existing route_ids.
    Stops without coordinates map to an empty list.
    Stops outside any cluster (DBSCAN label -1) map to an empty list.
    """
    # ------------------------------------------------------------------
    # Separate new stops into those with and without coordinates.
    # Stops without coordinates cannot be clustered and are returned
    # immediately with an empty overlap list.
    # ------------------------------------------------------------------
    has_coords = new_route[
        new_route["stop_lat"].notna() & new_route["stop_lon"].notna()
    ]
    no_coords = new_route[
        new_route["stop_lat"].isna() | new_route["stop_lon"].isna()
    ]

    overlap_map: dict[str, list[str]] = {}

    for stop_id in no_coords["stop_id"].unique():
        overlap_map[stop_id] = []
        logger.debug(
            "Corridor detection skipped for stop '%s': no coordinates.", stop_id
        )

    if no_coords.shape[0] > 0:
        logger.warning(
            "Corridor detection skipped for %d stop_id(s) due to missing "
            "coordinates. Only timing and frequency analysis will apply.",
            no_coords["stop_id"].nunique(),
        )

    if has_coords.empty:
        # No stops with coordinates — nothing to cluster.
        return overlap_map

    # ------------------------------------------------------------------
    # Drop existing index rows with missing coordinates before concat.
    # A NaN in the existing index would also cause DBSCAN to crash.
    # ------------------------------------------------------------------
    existing_clean = existing_index[
        existing_index["stop_lat"].notna() & existing_index["stop_lon"].notna()
    ]

    if existing_clean.empty:
        # No existing services have coordinates — no overlap possible.
        for stop_id in has_coords["stop_id"].unique():
            overlap_map[stop_id] = []
        return overlap_map

    # ------------------------------------------------------------------
    # Build the combined stop set for clustering.
    # Assign empty string for route_id on new route rows to avoid NaN.
    # ------------------------------------------------------------------
    new_stops = has_coords[["stop_id", "stop_lat", "stop_lon"]].copy()
    new_stops["route_id"] = ""
    new_stops["source"] = "new"

    existing_stops = existing_clean[
        ["stop_id", "stop_lat", "stop_lon", "route_id"]
    ].copy()
    existing_stops["source"] = "existing"

    all_stops = pd.concat([new_stops, existing_stops], ignore_index=True)

    # ------------------------------------------------------------------
    # Project to Irish Transverse Mercator for accurate metre distances.
    # ------------------------------------------------------------------
    gdf = gpd.GeoDataFrame(
        all_stops,
        geometry=gpd.points_from_xy(all_stops["stop_lon"], all_stops["stop_lat"]),
        crs=config.crs_geographic,
    ).to_crs(config.crs_projected)

    coords = np.column_stack([gdf.geometry.x, gdf.geometry.y])

    # ------------------------------------------------------------------
    # Run DBSCAN.
    # eps is in metres (meaningful because we are in a projected CRS).
    # min_samples=2 means a new stop must be within corridor_radius_metres
    # of at least one other point to be considered part of a corridor.
    # This is a policy threshold — see config.py.
    # ------------------------------------------------------------------
    db = DBSCAN(
        eps=config.corridor_radius_metres,
        min_samples=config.dbscan_min_samples,
        metric="euclidean",
    ).fit(coords)

    gdf = gdf.copy()
    gdf["cluster"] = db.labels_

    # ------------------------------------------------------------------
    # Build the overlap map for stops that have coordinates.
    # ------------------------------------------------------------------
    for stop_id in has_coords["stop_id"].unique():
        new_rows = gdf[(gdf["stop_id"] == stop_id) & (gdf["source"] == "new")]
        if new_rows.empty:
            overlap_map[stop_id] = []
            continue

        cluster_id = new_rows.iloc[0]["cluster"]

        if cluster_id == -1:
            # Noise point — not part of any corridor.
            overlap_map[stop_id] = []
            continue

        overlapping = gdf[
            (gdf["cluster"] == cluster_id)
            & (gdf["source"] == "existing")
            & (gdf["stop_id"] != stop_id)
        ]
        overlap_map[stop_id] = (
            overlapping["route_id"].dropna().unique().tolist()
        )

    return overlap_map