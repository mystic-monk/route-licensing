"""
gtfs_static_loader.py
=====================
Loads a GTFS static feed and builds the flat stop-service
index used by the analysis engine.

Two public functions are exposed:

    load_gtfs(path, service_date)
        Loads the feed zip, filtered to the service IDs active on
        service_date (defaults to the busiest date in the feed).

    build_stop_service_index(feed)
        Joins stop_times -> trips -> routes -> agency -> stops and
        returns a clean, deduplicated DataFrame ready for analysis.

Design decisions that carry policy significance
-----------------------------------------------
- Calendar filtering: only trips whose service_id is active on the
  chosen date are included. Mixing weekday, Saturday, Sunday and bank-
  holiday trips in the same index would inflate service frequency and
  produce false GREEN verdicts. The default is the busiest date in the
  feed (typically a representative Tuesday or Wednesday).

- Operator field: populated from agency.agency_name, not from
  route_short_name. route_short_name is the public route label (e.g.
  "39A") and is stored separately as route_label. This distinction
  matters for the "All Existing Routes Considered" table in the UI.

- Deduplication: the index is deduplicated on (stop_id, route_id,
  arrival_time) after the merge. Without this, shared trip patterns
  produce duplicate rows that inflate the timing conflict list and
  distort frequency calculations.
"""

import gc
import logging
from datetime import date
from typing import Optional, Tuple

import pandas as pd
import partridge as ptg

logger = logging.getLogger(__name__)

# Columns that must be present in the final index.
# Any change here must be reflected in decision_engine.py and
# the analysis modules.
_REQUIRED_OUTPUT_COLUMNS: list[str] = [
    "stop_id",
    "stop_name",
    "stop_lat",
    "stop_lon",
    "route_id",
    "route_label",   # route_short_name or route_long_name — display label
    "operator",      # agency_name — the operating company
    "arrival_time",  # pd.Timedelta from midnight
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_gtfs(
    path: str,
    service_date: Optional[date] = None,
) -> Tuple[ptg.gtfs.Feed, date]:
    """
    Loads a GTFS static feed zip, filtered to trips active on service_date.

    Parameters
    ----------
    path:
        File system path to the GTFS zip file.
    service_date:
        The date whose active service_ids will be used to filter the feed.
        If None, the busiest date in the feed is used automatically.
        Pass an explicit date to analyse Sunday or bank-holiday service.

    Returns
    -------
    A tuple (Feed, service_date) where Feed is the partridge Feed object
    pre-filtered to the chosen date and service_date is the resolved date.
    Raises RuntimeError if the zip cannot be loaded or contains no
    service on the chosen date.
    """
    try:
        if service_date is None:
            service_date, service_ids = ptg.read_busiest_date(path)
            logger.info(
                "No service_date specified. Using busiest date: %s "
                "(%d active service_id(s)).",
                service_date,
                len(service_ids),
            )
        else:
            ids_by_date = ptg.read_service_ids_by_date(path)
            service_ids = ids_by_date.get(service_date, frozenset())
            if not service_ids:
                raise RuntimeError(
                    f"No active service found in the GTFS feed for {service_date}. "
                    "Check the feed's calendar.txt and calendar_dates.txt."
                )
            logger.info(
                "Filtering GTFS feed to service_date=%s (%d active service_id(s)).",
                service_date,
                len(service_ids),
            )

        view = {"trips.txt": {"service_id": service_ids}}
        feed = ptg.load_feed(path, view=view)
        logger.info("GTFS feed loaded successfully from %s.", path)
        return feed, service_date

    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(
            f"Failed to load GTFS feed from '{path}'. "
            f"Ensure the file is a valid GTFS zip. Detail: {exc}"
        ) from exc


def load_all_stop_ids(path: str) -> tuple:
    """
    Loads all stop_ids from stops.txt with no date filtering and builds two
    reverse lookups for normalising short or NaPTAN-format stop IDs.

    GTFS stop IDs use the format <prefix><digits> (e.g. '8380B247191').
    Submitted timetables may use:
      - trailing numeric suffix only  ('247191')  → suffix_map
      - NaPTAN stop_code field value  ('158131')  → stop_code_map

    Returns
    -------
    (all_stop_ids, suffix_map, stop_code_map)
        all_stop_ids  : frozenset of every stop_id in stops.txt
        suffix_map    : dict mapping trailing-digits of stop_id →
                          full stop_id (unambiguous 1:1 matches only)
        stop_code_map : dict mapping stop_code field value →
                          full stop_id (unambiguous 1:1 matches only)
    """
    import io
    import zipfile
    try:
        with zipfile.ZipFile(path, "r") as zf:
            if "stops.txt" not in zf.namelist():
                logger.warning("stops.txt not found in GTFS zip '%s'.", path)
                return frozenset(), {}, {}
            with zf.open("stops.txt") as f:
                raw = pd.read_csv(
                    io.TextIOWrapper(f, encoding="utf-8-sig"),
                    dtype=str,
                )

        available_cols = raw.columns.tolist()
        usecols = ["stop_id"] + (["stop_code"] if "stop_code" in available_cols else [])
        stops_df = raw[usecols].copy()

        stops_df["stop_id"] = stops_df["stop_id"].str.strip()
        stops_df = stops_df.dropna(subset=["stop_id"])
        all_ids = frozenset(stops_df["stop_id"].unique())

        # Suffix map: trailing digits of stop_id (e.g. '8380B247191' → '247191')
        stops_df["suffix"] = stops_df["stop_id"].str.extract(r"(\d+)$")[0]
        suffix_counts = stops_df.groupby("suffix")["stop_id"].nunique()
        unambiguous_suffix = suffix_counts[suffix_counts == 1].index
        suffix_map: dict[str, str] = (
            stops_df[stops_df["suffix"].isin(unambiguous_suffix)]
            .set_index("suffix")["stop_id"]
            .to_dict()
        )

        # stop_code map: direct NaPTAN stop_code field → stop_id
        stop_code_map: dict[str, str] = {}
        if "stop_code" in stops_df.columns:
            sc = stops_df[["stop_id", "stop_code"]].copy()
            sc["stop_code"] = sc["stop_code"].str.strip()
            sc = sc.dropna(subset=["stop_code"])
            sc = sc[sc["stop_code"] != ""]
            # Only keep codes that resolve to exactly one stop_id
            sc_counts = sc.groupby("stop_code")["stop_id"].nunique()
            unambiguous_sc = sc_counts[sc_counts == 1].index
            stop_code_map = (
                sc[sc["stop_code"].isin(unambiguous_sc)]
                .set_index("stop_code")["stop_id"]
                .to_dict()
            )

        logger.info(
            "Loaded %d total stop IDs; %d suffix mappings, %d stop_code mappings.",
            len(all_ids),
            len(suffix_map),
            len(stop_code_map),
        )
        return all_ids, suffix_map, stop_code_map

    except Exception as exc:
        logger.warning("Could not load stop IDs from '%s': %s.", path, exc)
        return frozenset(), {}, {}


def build_stop_service_index(feed: ptg.gtfs.Feed) -> pd.DataFrame:
    """
    Builds a flat, searchable stop-service index from a loaded GTFS feed.

    Joins: stop_times -> trips -> routes -> agency -> stops.

    Returns
    -------
    DataFrame with columns defined in _REQUIRED_OUTPUT_COLUMNS.
    Each row represents one scheduled arrival of one route at one stop.

    Raises
    ------
    RuntimeError if any required GTFS table is empty or missing a
    critical join column, or if arrival_time conversion produces an
    unexpected dtype.
    """
    # ------------------------------------------------------------------
    # 1. Load individual GTFS tables
    # ------------------------------------------------------------------
    try:
        stops      = feed.stops
        stop_times = feed.stop_times
        trips      = feed.trips
        routes     = feed.routes
        agency     = feed.agency
    except Exception as exc:
        raise RuntimeError(f"Could not read GTFS tables from feed: {exc}") from exc

    _assert_not_empty(stop_times, "stop_times")
    _assert_not_empty(trips,      "trips")
    _assert_not_empty(routes,     "routes")
    _assert_not_empty(stops,      "stops")

    # ------------------------------------------------------------------
    # 2. Attach operator name from agency table
    #
    # agency_id is optional in routes.txt when the feed has exactly one
    # agency. Handle both cases.
    # ------------------------------------------------------------------
    if not agency.empty and "agency_id" in routes.columns:
        agency_slim = agency[["agency_id", "agency_name"]].drop_duplicates("agency_id")
        routes = routes.merge(agency_slim, on="agency_id", how="left")
        # Fall back to agency_id string if the join produced NaN (unmatched agency)
        if "agency_id" in routes.columns:
            routes["agency_name"] = routes["agency_name"].fillna(
                routes["agency_id"].astype(str)
            )
    elif not agency.empty:
        # Single-agency feed: broadcast the sole agency name to all routes.
        routes = routes.copy()
        routes["agency_name"] = agency["agency_name"].iloc[0]
    else:
        routes = routes.copy()
        routes["agency_name"] = "Unknown"

    # ------------------------------------------------------------------
    # 3. Resolve route label and preserve long name + transport mode
    #
    # route_short_name is optional in the GTFS spec. Fall back to
    # route_long_name, then route_id.
    # ------------------------------------------------------------------
    routes = routes.copy()
    if "route_short_name" in routes.columns and routes["route_short_name"].notna().any():
        routes["route_label"] = routes["route_short_name"].fillna(
            routes.get("route_long_name", routes["route_id"])
        )
    elif "route_long_name" in routes.columns:
        routes["route_label"] = routes["route_long_name"].fillna(routes["route_id"])
    else:
        routes["route_label"] = routes["route_id"]

    if "route_long_name" not in routes.columns:
        routes["route_long_name"] = ""
    routes["route_long_name"] = routes["route_long_name"].fillna("").astype(str)

    if "route_type" not in routes.columns:
        routes["route_type"] = ""
    routes["route_type"] = routes["route_type"].fillna("").astype(str)

    # ------------------------------------------------------------------
    # 4. Join stop_times -> trips -> routes -> stops
    # ------------------------------------------------------------------
    _assert_join_key(stop_times, trips,  "trip_id")
    _assert_join_key(trips,      routes, "route_id")
    _assert_join_key(stop_times, stops,  "stop_id")

    merged = (
        stop_times
        .merge(trips[["trip_id", "route_id"]], on="trip_id", how="inner")
        .merge(
            routes[["route_id", "route_label", "route_long_name", "route_type", "agency_name"]],
            on="route_id",
            how="inner",
        )
        .merge(
            stops[["stop_id", "stop_name", "stop_lat", "stop_lon"]],
            on="stop_id",
            how="inner",
        )
    )

    if merged.empty:
        raise RuntimeError(
            "The join of stop_times, trips, routes and stops produced an empty "
            "DataFrame. Check that the GTFS feed is valid and not empty for the "
            "chosen service date."
        )

    # ------------------------------------------------------------------
    # 5. Select and rename output columns
    # ------------------------------------------------------------------
    index = merged[[
        "stop_id",
        "stop_name",
        "stop_lat",
        "stop_lon",
        "route_id",
        "route_label",
        "route_long_name",
        "route_type",
        "agency_name",
        "arrival_time",
    ]].rename(columns={"agency_name": "operator"}).copy()

    # ------------------------------------------------------------------
    # 6. Convert arrival_time
    #
    # Partridge stores arrival_time as integer seconds past midnight.
    # Validate the dtype after conversion so a future Partridge version
    # change cannot silently corrupt the index.
    # ------------------------------------------------------------------
    index["arrival_time"] = pd.to_timedelta(index["arrival_time"], unit="s")

    if not pd.api.types.is_timedelta64_dtype(index["arrival_time"]):
        raise RuntimeError(
            "arrival_time conversion produced an unexpected dtype: "
            f"{index['arrival_time'].dtype}. "
            "Check the installed version of Partridge."
        )

    # ------------------------------------------------------------------
    # 7. Drop rows with missing coordinates or arrival times
    #
    # NaN coordinates would cause the DBSCAN projection to crash.
    # NaT arrival times would corrupt the timing conflict calculation.
    # ------------------------------------------------------------------
    before = len(index)
    index = index.dropna(subset=["stop_lat", "stop_lon", "arrival_time"])
    dropped = before - len(index)
    if dropped > 0:
        logger.warning(
            "Dropped %d stop-service record(s) with missing coordinates "
            "or arrival times.",
            dropped,
        )

    # ------------------------------------------------------------------
    # 8. Deduplicate
    #
    # Multiple trips with identical patterns on the same service day
    # produce duplicate (stop_id, route_id, arrival_time) triplets.
    # Duplicates bloat the timing conflict list and distort headway
    # calculations without adding analytical value.
    # ------------------------------------------------------------------
    before = len(index)
    index = index.drop_duplicates(subset=["stop_id", "route_id", "arrival_time"])
    deduped = before - len(index)
    if deduped > 0:
        logger.info(
            "Removed %d duplicate stop-service record(s) after deduplication.",
            deduped,
        )

    # ------------------------------------------------------------------
    # 9. Final validation
    # ------------------------------------------------------------------
    _assert_output_columns(index)

    index = index.reset_index(drop=True)

    logger.info(
        "Stop-service index built: %d records, %d unique stops, %d unique routes.",
        len(index),
        index["stop_id"].nunique(),
        index["route_id"].nunique(),
    )

    return _apply_memory_opts(index)


def build_trip_index(feed: ptg.gtfs.Feed) -> pd.DataFrame:
    """
    Builds a trip-structured index from a loaded GTFS feed.

    Unlike build_stop_service_index, this preserves trip_id and stop_sequence
    so that individual trips can be reconstructed for display purposes.
    It is NOT deduplicated — each row is one stop-visit within one trip.

    Returns
    -------
    DataFrame with columns:
        trip_id, route_id, route_label, operator,
        stop_sequence, stop_id, stop_name, stop_lat, stop_lon, arrival_time
    """
    try:
        stops      = feed.stops
        stop_times = feed.stop_times
        trips      = feed.trips
        routes     = feed.routes
        agency     = feed.agency
    except Exception as exc:
        raise RuntimeError(f"Could not read GTFS tables from feed: {exc}") from exc

    _assert_not_empty(stop_times, "stop_times")
    _assert_not_empty(trips,      "trips")
    _assert_not_empty(routes,     "routes")
    _assert_not_empty(stops,      "stops")

    # Operator name
    if not agency.empty and "agency_id" in routes.columns:
        agency_slim = agency[["agency_id", "agency_name"]].drop_duplicates("agency_id")
        routes = routes.merge(agency_slim, on="agency_id", how="left")
        if "agency_id" in routes.columns:
            routes["agency_name"] = routes["agency_name"].fillna(
                routes["agency_id"].astype(str)
            )
    elif not agency.empty:
        routes = routes.copy()
        routes["agency_name"] = agency["agency_name"].iloc[0]
    else:
        routes = routes.copy()
        routes["agency_name"] = "Unknown"

    # Route label and long name
    routes = routes.copy()
    if "route_short_name" in routes.columns and routes["route_short_name"].notna().any():
        routes["route_label"] = routes["route_short_name"].fillna(
            routes.get("route_long_name", routes["route_id"])
        )
    elif "route_long_name" in routes.columns:
        routes["route_label"] = routes["route_long_name"].fillna(routes["route_id"])
    else:
        routes["route_label"] = routes["route_id"]

    if "route_long_name" not in routes.columns:
        routes["route_long_name"] = ""
    routes["route_long_name"] = routes["route_long_name"].fillna("").astype(str)

    if "route_type" not in routes.columns:
        routes["route_type"] = ""
    routes["route_type"] = routes["route_type"].fillna("").astype(str)

    # Keep stop_sequence alongside trip_id
    st_cols = ["trip_id", "stop_id", "arrival_time"]
    if "stop_sequence" in stop_times.columns:
        st_cols.append("stop_sequence")
    st = stop_times[st_cols].copy()

    merged = (
        st
        .merge(trips[["trip_id", "route_id"]], on="trip_id", how="inner")
        .merge(
            routes[["route_id", "route_label", "route_long_name", "route_type", "agency_name"]],
            on="route_id",
            how="inner",
        )
        .merge(
            stops[["stop_id", "stop_name", "stop_lat", "stop_lon"]],
            on="stop_id",
            how="inner",
        )
    )

    if merged.empty:
        logger.warning("build_trip_index: join produced an empty DataFrame.")
        return pd.DataFrame()

    merged = merged.rename(columns={"agency_name": "operator"})
    merged["arrival_time"] = pd.to_timedelta(merged["arrival_time"], unit="s")
    merged = merged.dropna(subset=["stop_lat", "stop_lon", "arrival_time"])

    if "stop_sequence" in merged.columns:
        merged["stop_sequence"] = merged["stop_sequence"].fillna(0).astype(int)
    else:
        merged["stop_sequence"] = 0

    merged = merged.reset_index(drop=True)

    logger.info(
        "Trip index built: %d rows, %d trips, %d routes, %d unique stops.",
        len(merged),
        merged["trip_id"].nunique(),
        merged["route_id"].nunique(),
        merged["stop_id"].nunique(),
    )

    return _apply_memory_opts(merged)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _apply_memory_opts(df: pd.DataFrame) -> pd.DataFrame:
    """
    Reduce in-memory footprint of a GTFS DataFrame without changing semantics.

    - Object (string) columns → pd.Categorical.  High-cardinality repeated
      values (stop_id, route_id, trip_id) go from ~80 bytes/row to ~4 bytes/row
      plus a single shared string pool per column.  All pandas operations
      (==, isin, groupby, str accessor) work identically on categorical.
    - stop_lat / stop_lon → float32.  ±5 m resolution is far more than needed
      for 300 m corridor detection; halves coordinate storage.
    - stop_sequence → int32.
    """
    for col in df.select_dtypes(include="object").columns:
        df[col] = df[col].astype("category")
    for col in ("stop_lat", "stop_lon"):
        if col in df.columns:
            df[col] = df[col].astype("float32")
    if "stop_sequence" in df.columns:
        df["stop_sequence"] = df["stop_sequence"].astype("int32")
    return df


def _assert_not_empty(df: pd.DataFrame, table_name: str) -> None:
    """Raises RuntimeError if a required GTFS table is empty."""
    if df.empty:
        raise RuntimeError(
            f"GTFS table '{table_name}' is empty. "
            "The feed may be invalid or the chosen service_date may have no trips."
        )


def _assert_join_key(left: pd.DataFrame, right: pd.DataFrame, key: str) -> None:
    """Raises RuntimeError if a required join key column is missing."""
    for side, df in (("left", left), ("right", right)):
        if key not in df.columns:
            raise RuntimeError(
                f"Join key '{key}' is missing from the {side} DataFrame. "
                "The GTFS feed may be malformed."
            )


def _assert_output_columns(index: pd.DataFrame) -> None:
    """Raises RuntimeError if any required output column is absent."""
    missing = [col for col in _REQUIRED_OUTPUT_COLUMNS if col not in index.columns]
    if missing:
        raise RuntimeError(
            f"Stop-service index is missing required output column(s): {missing}. "
            "This is an internal error — check the build_stop_service_index joins."
        )