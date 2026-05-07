"""
request_parser.py
=================
Parses incoming route licensing proposals into the flat DataFrame
format required by the analysis engine.

Two entry points are provided:

    parse_excel_request(file_path, stop_coordinate_index, operator)
        Parses the wide-format NTA timetable Excel submission.

    parse_new_route_request(request_dict)
        Parses a programmatic dict submission (API / test use).

Excel format — what the file actually contains
-----------------------------------------------
The submitted Excel file is a wide-format timetable, not a flat table.
A single sheet may contain multiple direction sections. Each section has:

    Row 1  — Section title  e.g. "Kinsale to Cork City"
    Row 2  — Header         "Stop Name | Stop Location | Stop ID | Monday - Sunday"
    Row 3+ — Data rows      stop_name | stop_location | stop_id | time | time | ...

The times are datetime.time objects (openpyxl) representing individual
departures from that stop. Each departure is expanded into its own row
in the output DataFrame so the engine can check each one independently.

Missing fields and how they are resolved
-----------------------------------------
- route_id:
    Derived from the first section title found in the sheet.
    Both directions share the same route_id so the engine produces a
    single aggregate verdict for the full route.

- operator:
    Not present in the file. Must be passed as a parameter by the
    caller (e.g. from a form field in the upload UI). Defaults to
    "Unknown" with a warning logged.

- stop_lat / stop_lon:
    Not present in the file. Resolved by looking up the NTA stop_id
    in stop_coordinate_index (the loaded GTFS static index).

    If a stop_id cannot be found in the index, coordinates default to
    None and a warning is logged. The timing conflict checker and
    frequency scorer continue to work normally for those stops. The
    corridor detector skips any stop whose coordinates are None, so
    corridor overlap will not be assessed for unresolved stops.

    This allows the application to run against the demo GTFS data even
    when the submitted timetable contains stop IDs that are not present
    in the demo index (e.g. Cork/Kinsale stops against Dublin demo data).

Post-midnight services
----------------------
datetime.time(0, 20) is treated as 00:20:00 (20 minutes past midnight),
not 24:20:00. This affects frequency scoring for late-evening stops.
The NTA team should confirm the preferred treatment before production use.
"""

import json
import logging
import re
import uuid
from datetime import time as dt_time
from typing import Optional

import openpyxl
import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Required output columns — must match decision_engine.py expectations
# ---------------------------------------------------------------------------
_OUTPUT_COLUMNS: list[str] = [
    "route_id",
    "operator",
    "section_idx",
    "section_title",
    "section_day_groups",
    "trip_idx",
    "stop_id",
    "stop_name",
    "stop_location",
    "stop_lat",
    "stop_lon",
    "arrival_time",
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_excel_request(
    file_path: str,
    stop_coordinate_index: Optional[pd.DataFrame] = None,
    operator: str = "Unknown",
    valid_stop_ids: Optional[frozenset] = None,
    stop_id_suffix_map: Optional[dict] = None,
) -> pd.DataFrame:
    """
    Loads an NTA wide-format timetable Excel file and returns a flat
    DataFrame ready for the analysis engine.

    Parameters
    ----------
    file_path:
        Path to the .xlsx or .xls file uploaded by the applicant.
    stop_coordinate_index:
        DataFrame containing at minimum stop_id, stop_lat, stop_lon
        columns, typically the loaded GTFS static index. Used to look
        up coordinates for stops not present in the Excel file.
        If None, or if a stop_id is not found in the index, coordinates
        default to None and a warning is logged. Corridor detection is
        skipped for those stops but all other analysis continues.
    operator:
        The operating company name. Not present in the standard NTA
        timetable template; must be passed by the caller. Defaults to
        "Unknown" with a warning.

    Returns
    -------
    DataFrame with columns: route_id, operator, stop_id, stop_name,
    stop_lat, stop_lon, arrival_time (pd.Timedelta).
    stop_lat and stop_lon may be None for stops whose coordinates could
    not be resolved.

    Raises
    ------
    ValueError
        If the file contains no parseable timetable sections, or if no
        valid stop rows with departure times are found.
    RuntimeError
        If the file cannot be opened.
    """
    if operator == "Unknown":
        logger.warning(
            "No operator name provided to parse_excel_request. "
            "Defaulting to 'Unknown'. Pass operator= to set it correctly."
        )

    try:
        wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
    except Exception as exc:
        raise RuntimeError(
            f"Could not open Excel file '{file_path}': {exc}"
        ) from exc

    try:
        ws = wb.worksheets[0]
        all_rows = list(ws.iter_rows(values_only=True))
    finally:
        # Always close the workbook to release the file handle.
        # On Windows, failing to close causes a PermissionError when the
        # caller tries to delete the temp file after parsing.
        wb.close()

    # ------------------------------------------------------------------
    # Step 1 — Locate section boundaries
    # A section begins with a title row (single string value) followed
    # by a header row containing "Stop Name".
    # ------------------------------------------------------------------
    sections = _locate_sections(all_rows)
    if not sections:
        raise ValueError(
            "No timetable sections found in the Excel file. "
            "Expected a header row containing 'Stop Name'. "
            f"Check the file format. File: {file_path}"
        )

    # ------------------------------------------------------------------
    # Step 2 — Derive route_id from the first section title
    # ------------------------------------------------------------------
    route_id = (
        _sanitise_route_id(sections[0]["title"])
        if sections[0]["title"]
        else f"NR-{uuid.uuid4().hex[:8]}"
    )
    logger.info(
        "Parsing %d section(s) from '%s'. Derived route_id: '%s'.",
        len(sections),
        file_path,
        route_id,
    )

    # ------------------------------------------------------------------
    # Step 3 — Extract stop rows and melt departure times
    # Each departure time becomes its own row in the output so the
    # engine can check every proposed service independently.
    # ------------------------------------------------------------------
    rows: list[dict] = []
    for section_idx, section in enumerate(sections):
        day_groups_json = json.dumps(section.get("day_groups", []))
        for stop in section["stops"]:
            for trip_idx, dep_time in enumerate(stop["departure_times"]):
                rows.append({
                    "route_id":           route_id,
                    "operator":           operator,
                    "section_idx":        section_idx,
                    "section_title":      section.get("title", ""),
                    "section_day_groups": day_groups_json,
                    "trip_idx":           trip_idx,
                    "stop_id":            stop["stop_id"],
                    "stop_name":          stop["stop_name"],
                    "stop_location":      stop.get("stop_location", ""),
                    "stop_lat":           None,   # resolved in Step 4
                    "stop_lon":           None,
                    "arrival_time":       _clean_arrival_time(dep_time),
                })

    if not rows:
        raise ValueError(
            "The Excel file was parsed but contained no valid stop rows with "
            "departure times. Check that data rows follow the expected "
            "'Stop Name | Stop Location | Stop ID | Time...' format."
        )

    df = pd.DataFrame(rows)

    # Drop rows where arrival_time could not be parsed.
    invalid_times = int(df["arrival_time"].isna().sum())
    if invalid_times > 0:
        logger.warning(
            "Dropped %d row(s) where arrival_time could not be parsed.",
            invalid_times,
        )
        df = df.dropna(subset=["arrival_time"])

    if df.empty:
        raise ValueError(
            "All rows were dropped due to unparseable arrival times. "
            "Check that departure times in the Excel file are valid time values."
        )

    # ------------------------------------------------------------------
    # Step 4 — Normalise short numeric stop IDs to full GTFS stop IDs
    #
    # Submitted timetables sometimes use only the trailing numeric portion
    # of an NTA stop ID (e.g. '247191' instead of '8380B247191').
    # When a suffix map is provided, rewrite any unrecognised short ID to
    # its canonical full form where the mapping is unambiguous (1:1).
    # IDs that are already in their full form, or whose suffix is
    # ambiguous, are left unchanged.
    # ------------------------------------------------------------------
    if stop_id_suffix_map:
        known = valid_stop_ids or (
            frozenset(stop_coordinate_index["stop_id"].astype(str).unique())
            if stop_coordinate_index is not None and not stop_coordinate_index.empty
            else frozenset()
        )

        def _normalise_id(sid: str) -> str:
            if sid in known:
                return sid
            resolved = stop_id_suffix_map.get(sid)
            if resolved:
                logger.info("Normalised stop_id '%s' → '%s'.", sid, resolved)
                return resolved
            return sid

        df["stop_id"] = df["stop_id"].astype(str).map(_normalise_id)

    # ------------------------------------------------------------------
    # Step 5 — Resolve coordinates from GTFS feed
    # ------------------------------------------------------------------
    df = _resolve_coordinates(df, stop_coordinate_index)

    # ------------------------------------------------------------------
    # Step 6 — Validate stop IDs against the GTFS feed
    #
    # The GTFS feed is the authoritative set of NTA stops. All submitted
    # stop IDs must exist in it — no new stops are created by this tool.
    #
    # valid_stop_ids (preferred): complete unfiltered stop set read
    #   directly from stops.txt — covers stops not active on the
    #   busiest service date used by the analysis index.
    # Fallback: derive the valid set from stop_coordinate_index rows
    #   (date-filtered — may miss some valid stops).
    # ------------------------------------------------------------------
    if valid_stop_ids:
        gtfs_stop_ids = valid_stop_ids
    elif stop_coordinate_index is not None and not stop_coordinate_index.empty:
        gtfs_stop_ids = frozenset(stop_coordinate_index["stop_id"].astype(str).unique())
    else:
        gtfs_stop_ids = None

    if gtfs_stop_ids:
        submitted_ids    = set(df["stop_id"].astype(str).unique())
        unknown_stop_ids = sorted(submitted_ids - gtfs_stop_ids)

        if unknown_stop_ids:
            # Non-fatal: the GTFS snapshot may not cover all active NTA stops
            # (e.g. recently added stops, regional gaps, or feed date filtering).
            # Warn clearly so the issue is visible in logs and results, but allow
            # the analysis to continue — timing and frequency checks still run;
            # corridor detection is automatically skipped for unresolved stops.
            logger.warning(
                "GTFS STOP VALIDATION: %d stop ID(s) not found in GTFS feed: %s. "
                "Corridor detection will be skipped for these stops. "
                "Timing and frequency analysis will proceed normally. "
                "Verify these are valid NTA stop codes or update the GTFS feed.",
                len(unknown_stop_ids),
                unknown_stop_ids,
            )
            df["_gtfs_unresolved"] = df["stop_id"].isin(unknown_stop_ids)

    # ------------------------------------------------------------------
    # Step 7 — Structural sanity checks
    # ------------------------------------------------------------------
    for section_idx, section_df in df.groupby("section_idx"):
        unique_stops = section_df["stop_id"].nunique()
        if unique_stops < 2:
            title = section_df["section_title"].iloc[0] or f"Section {section_idx}"
            raise ValueError(
                f"Section '{title}' contains only {unique_stops} unique stop(s). "
                "A valid route section must have at least 2 stops."
            )

        # Warn about duplicate stop IDs within a section (non-fatal —
        # terminal reversals legitimately reuse the same stop).
        dup_stops = (
            section_df.groupby("stop_id")["stop_name"]
            .nunique()
            .pipe(lambda s: s[s > 0].index.tolist())
        )
        counts = section_df["stop_id"].value_counts()
        repeated = counts[counts > section_df["trip_idx"].nunique()].index.tolist()
        if repeated:
            logger.warning(
                "Section '%s' contains stop_id(s) that appear more than once "
                "per trip: %s. This may indicate a data error unless the route "
                "is a terminal reversal.",
                section_df["section_title"].iloc[0],
                repeated,
            )

    logger.info(
        "Parsed route '%s': %d stop-departure rows, %d unique stops, %d section(s).",
        route_id,
        len(df),
        df["stop_id"].nunique(),
        df["section_idx"].nunique(),
    )

    return df[_OUTPUT_COLUMNS].reset_index(drop=True)


def parse_new_route_request(request_dict: dict) -> pd.DataFrame:
    """
    Normalises a programmatic licensing request (dict) into the flat
    DataFrame format required by the analysis engine.

    Expected dict structure:
        {
            "route_id":  str,
            "operator":  str,
            "stops": [
                {
                    "stop_id":      str,
                    "stop_name":    str,
                    "stop_lat":     float,
                    "stop_lon":     float,
                    "arrival_time": str | datetime.time
                },
                ...
            ]
        }

    Raises
    ------
    ValueError if required top-level keys or stop fields are missing.
    """
    required_top = {"route_id", "operator", "stops"}
    missing_top = required_top - set(request_dict.keys())
    if missing_top:
        raise ValueError(
            f"Request dict is missing required key(s): {sorted(missing_top)}"
        )

    required_stop = {"stop_id", "stop_name", "stop_lat", "stop_lon", "arrival_time"}
    rows: list[dict] = []

    for i, stop in enumerate(request_dict["stops"]):
        missing_stop = required_stop - set(stop.keys())
        if missing_stop:
            raise ValueError(
                f"Stop at index {i} is missing required field(s): "
                f"{sorted(missing_stop)}"
            )
        rows.append({
            "route_id":     request_dict["route_id"],
            "operator":     request_dict["operator"],
            "stop_id":      str(stop["stop_id"]),
            "stop_name":    stop["stop_name"],
            "stop_lat":     float(stop["stop_lat"]),
            "stop_lon":     float(stop["stop_lon"]),
            "arrival_time": _clean_arrival_time(stop["arrival_time"]),
        })

    if not rows:
        raise ValueError("Request dict contains no stops.")

    return pd.DataFrame(rows)[_OUTPUT_COLUMNS]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_day_groups(row_list: list, start_col: int) -> list[dict]:
    """
    Parses operating-day group headers from the timetable header row.

    In the Excel wide-format timetable, the columns after Stop ID carry one
    or more operating-day labels (e.g. "Monday – Sunday", "Monday – Friday",
    "Saturday", "Sunday"). Each label occupies one or more columns via a
    merged cell; openpyxl read_only mode surfaces only the first cell of
    each merge with its value — the remaining cells in the merge appear as
    None.

    This function counts consecutive None cells after each label to derive
    the trip count for that day group.

    Example header row (simplified):
        [None, 'Stop Name', 'Stop Location', 'Stop ID',
         'Monday – Friday', None, None, None,   # 4 trips
         'Saturday', None,                       # 2 trips
         'Sunday']                               # 1 trip

    Returns
    -------
    list of {"label": str, "trip_count": int}
    """
    groups: list[dict] = []
    current_label: Optional[str] = None
    current_count: int = 0

    for val in row_list[start_col:]:
        if isinstance(val, str) and val.strip():
            # Flush the previous group before starting a new one.
            if current_label is not None:
                groups.append({"label": current_label, "trip_count": current_count})
            current_label = val.strip()
            current_count = 1
        elif val is None and current_label is not None:
            current_count += 1
        # Non-string, non-None values (e.g. a stray number) stop the scan.
        elif val is not None:
            break

    if current_label is not None:
        groups.append({"label": current_label, "trip_count": current_count})

    return groups


def _locate_sections(all_rows: list[tuple]) -> list[dict]:
    """
    Scans the raw row list and returns a list of section dicts.
    Each dict has keys: title (str), day_groups (list), stops (list).

    A section is identified by a header row containing the text
    "Stop Name". The title is the closest preceding non-empty single-
    value string row. Operating-day group labels (e.g. "Monday – Sunday")
    are parsed from the same header row and stored in day_groups.
    """
    sections: list[dict] = []
    pending_title: str = ""
    pending_day_groups: list[dict] = []
    in_section: bool = False
    current_stops: list[dict] = []

    for row in all_rows:
        row_list = list(row)
        non_null = [c for c in row_list if c is not None]

        # Blank row — close any open section.
        if not non_null:
            if in_section and current_stops:
                sections.append({
                    "title":      pending_title,
                    "day_groups": pending_day_groups,
                    "stops":      current_stops,
                })
                current_stops = []
                pending_title = ""
                pending_day_groups = []
                in_section = False
            continue

        # Header row — starts a new section.
        if "Stop Name" in non_null:
            if in_section and current_stops:
                sections.append({
                    "title":      pending_title,
                    "day_groups": pending_day_groups,
                    "stops":      current_stops,
                })
                current_stops = []

            # Locate "Stop Name" in the raw row to find the fixed-col offset.
            stop_name_col = next(
                (i for i, v in enumerate(row_list) if v == "Stop Name"), None
            )
            if stop_name_col is not None:
                # Fixed columns: Stop Name, Stop Location, Stop ID (3 cols).
                pending_day_groups = _parse_day_groups(row_list, stop_name_col + 3)
            else:
                pending_day_groups = []

            in_section = True
            continue

        # Title row — single string value not yet inside a section.
        if not in_section and len(non_null) == 1 and isinstance(non_null[0], str):
            pending_title = non_null[0]
            continue

        # Data row — extract stop details and departure times.
        if in_section:
            stop = _extract_stop_row(tuple(row_list))
            if stop:
                current_stops.append(stop)

    # Close the final section if the file does not end with a blank row.
    if in_section and current_stops:
        sections.append({
            "title":      pending_title,
            "day_groups": pending_day_groups,
            "stops":      current_stops,
        })

    return sections


def _extract_stop_row(row: tuple) -> Optional[dict]:
    """
    Extracts stop fields and departure times from a single data row.

    Expected layout (starting from first non-None cell):
        [0] stop_name
        [1] stop_location  (informational only, not used downstream)
        [2] stop_id
        [3+] departure times (datetime.time objects)

    Returns None if the row does not contain the minimum required fields.
    """
    row_list = list(row)

    # Locate the first non-None cell to handle leading blank columns.
    start = next((i for i, v in enumerate(row_list) if v is not None), None)
    if start is None:
        return None

    try:
        stop_name = row_list[start]
        stop_id   = row_list[start + 2] if (start + 2) < len(row_list) else None
    except IndexError:
        return None

    if stop_id is None or not isinstance(stop_name, str):
        return None

    departure_times = [
        v for v in row_list[start + 3:]
        if v is not None and isinstance(v, dt_time)
    ]

    if not departure_times:
        return None

    raw_location = row_list[start + 1] if (start + 1) < len(row_list) else None
    stop_location = str(raw_location).strip() if raw_location is not None else ""

    return {
        "stop_id":         str(stop_id),
        "stop_name":       stop_name.strip(),
        "stop_location":   stop_location,
        "departure_times": departure_times,
    }


def _clean_arrival_time(time_val) -> pd.Timedelta:
    """
    Converts a time value to a pd.Timedelta.

    Accepts:
        datetime.time objects (from openpyxl)
        HH:MM:SS strings
        HH:MM strings

    Returns pd.NaT if conversion fails.
    """
    if time_val is None:
        return pd.NaT
    if not isinstance(time_val, dt_time):
        try:
            if pd.isna(time_val):
                return pd.NaT
        except (TypeError, ValueError):
            pass
    try:
        if isinstance(time_val, dt_time):
            return pd.to_timedelta(str(time_val))
        if isinstance(time_val, (str, bytes)):
            return pd.to_timedelta(str(time_val))
        return pd.to_timedelta(str(time_val))
    except (ValueError, TypeError) as exc:
        logger.warning("Could not parse arrival time '%s': %s", time_val, exc)
        return pd.NaT


def _resolve_coordinates(
    df: pd.DataFrame,
    stop_coordinate_index: Optional[pd.DataFrame],
) -> pd.DataFrame:
    """
    Attempts to populate stop_lat and stop_lon for each row using the
    GTFS static index as a lookup.

    Behaviour
    ---------
    - If all coordinates are already present, returns the DataFrame unchanged.
    - If stop_coordinate_index is provided, looks up each stop_id.
    - If a stop_id is not found in the index, coordinates remain None
      and a warning is logged. This is non-fatal: timing conflict and
      frequency analysis continue normally. The corridor detector is
      responsible for skipping stops with None coordinates.
    - If no index is provided at all, logs a warning and returns the
      DataFrame with all coordinates as None.

    The GTFS index typically contains many rows per stop_id (one per
    departure). It is deduplicated to one coordinate pair per stop_id
    before the lookup.
    """
    if df["stop_lat"].notna().all() and df["stop_lon"].notna().all():
        return df

    if stop_coordinate_index is None or stop_coordinate_index.empty:
        unresolved = df["stop_id"].unique().tolist()
        logger.warning(
            "No stop_coordinate_index provided. Coordinates will be None "
            "for all %d stop_id(s): %s. Corridor detection will be skipped "
            "for these stops.",
            len(unresolved),
            unresolved,
        )
        return df

    # Deduplicate to one coordinate pair per stop_id.
    coord_lookup = (
        stop_coordinate_index[["stop_id", "stop_lat", "stop_lon"]]
        .drop_duplicates(subset="stop_id")
        .set_index("stop_id")
    )

    df = df.copy()
    for col in ("stop_lat", "stop_lon"):
        df[col] = df.apply(
            lambda row, c=col: (
                coord_lookup.at[row["stop_id"], c]
                if row[c] is None and row["stop_id"] in coord_lookup.index
                else row[c]
            ),
            axis=1,
        )

    unresolved = (
        df[df["stop_lat"].isna() | df["stop_lon"].isna()]["stop_id"]
        .unique()
        .tolist()
    )
    if unresolved:
        logger.warning(
            "Coordinates not found in GTFS index for %d stop_id(s): %s. "
            "Corridor detection will be skipped for these stops. "
            "Verify these are valid NTA stop codes, or supply a GTFS feed "
            "that covers the submitted route.",
            len(unresolved),
            unresolved,
        )

    return df


def _sanitise_route_id(title: str) -> str:
    """
    Converts a free-text section title to a filesystem-safe route_id.

    "Kinsale to Cork City" -> "Kinsale-to-Cork-City"
    """
    sanitised = re.sub(r"[^\w\s-]", "", title).strip()
    sanitised = re.sub(r"\s+", "-", sanitised)
    return sanitised or f"NR-{uuid.uuid4().hex[:8]}"