"""
storage.py
==========
Persists and retrieves route licensing analysis results as JSON files.

All results are written to a configurable directory (default:
data/analysis_results). Each file is named:

    {route_id}_{YYYYMMDD_HHMMSS}.json

The timestamp in the filename is for file-system ordering convenience
only. The authoritative timestamp is the analysed_at field stored
inside the JSON itself (ISO-8601 UTC, set by the decision engine).
list_all_analyses reads analysed_at from the JSON and never parses
the filename, which was fragile when route_ids contained underscores.

Public functions
----------------
    save_analysis_result(ref_id, analysis, results_dir)
    get_analysis_by_ref(ref_id, results_dir)
    list_all_analyses(results_dir)
    delete_analysis(ref_id, results_dir)
"""

import json
import logging
import os
from datetime import datetime
from typing import Dict, Any, Optional, List

logger = logging.getLogger(__name__)

DEFAULT_RESULTS_DIR = "data/analysis_results"


def save_analysis_result(
    ref_id: str,
    analysis: Dict[str, Any],
    results_dir: str = DEFAULT_RESULTS_DIR,
) -> str:
    """
    Saves the analysis result as a JSON file with timestamp.
    """
    _ensure_dir(results_dir)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_ref = _safe_filename_part(ref_id)
    filename = f"{safe_ref}_{timestamp}.json"
    filepath = os.path.join(results_dir, filename)

    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(analysis, f, indent=4, default=str)
    except OSError as exc:
        raise OSError(
            f"Could not write analysis result to '{filepath}': {exc}"
        ) from exc

    logger.info("Analysis result saved: %s", filepath)
    return filepath


def get_analysis_by_ref(
    ref_id: str,
    results_dir: str = DEFAULT_RESULTS_DIR,
) -> Optional[Dict]:
    """
    Retrieves the most recent analysis for a given route_id.

    If multiple files exist for the same ref_id (e.g. a route was
    resubmitted), the most recently written file is returned, determined
    by descending filename sort (which encodes the write timestamp).

    Returns None if no matching file exists.
    """
    if not os.path.isdir(results_dir):
        return None

    safe_ref = _safe_filename_part(ref_id)
    try:
        candidates = [
            f for f in os.listdir(results_dir)
            if f.startswith(f"{safe_ref}_") and f.endswith(".json")
        ]
    except OSError as exc:
        logger.error("Could not list results directory '%s': %s", results_dir, exc)
        return None

    if not candidates:
        return None

    candidates.sort(reverse=True)
    filepath = os.path.join(results_dir, candidates[0])

    return _read_json_file(filepath)


def list_all_analyses(
    results_dir: str = DEFAULT_RESULTS_DIR,
) -> list[dict]:
    """
    Returns summary metadata for all stored analyses, sorted with the
    most recent first.

    Each entry contains the fields needed by the dashboard and history
    templates. The timestamp field is read from analysed_at inside the
    JSON, not parsed from the filename.

    Silently skips files that cannot be read or parsed, logging a
    warning for each.
    """
    if not os.path.isdir(results_dir):
        return []

    try:
        filenames = sorted(
            [f for f in os.listdir(results_dir) if f.endswith(".json")],
            reverse=True,
        )
    except OSError as exc:
        logger.error("Could not list results directory '%s': %s", results_dir, exc)
        return []

    results: list[dict] = []
    for filename in filenames:
        filepath = os.path.join(results_dir, filename)
        data = _read_json_file(filepath)
        if data is None:
            continue

        results.append({
            "ref_id":          data.get("route_id", ""),
            "operator":        data.get("operator", ""),
            "verdict":         data.get("route_verdict", ""),
            "total_stops":     data.get("total_stops", 0),
            "red_stops":       data.get("red_stops", 0),
            "amber_stops":     data.get("amber_stops", 0),
            "green_stops":     data.get("green_stops", 0),
            "recommendation":  data.get("route_recommendation", ""),
            # Authoritative timestamp from inside the JSON, not the filename.
            "timestamp":       data.get("analysed_at", ""),
        })

    # Sort by analysed_at descending. Files without a timestamp sort last.
    results.sort(
        key=lambda r: r["timestamp"] or "",
        reverse=True,
    )

    return results


def delete_analysis(
    ref_id: str,
    results_dir: str = DEFAULT_RESULTS_DIR,
) -> int:
    """
    Deletes all stored result files for a given route_id.

    Returns the number of files deleted.
    Logs a warning for any file that cannot be deleted.
    """
    if not os.path.isdir(results_dir):
        return 0

    safe_ref = _safe_filename_part(ref_id)
    try:
        candidates = [
            f for f in os.listdir(results_dir)
            if f.startswith(f"{safe_ref}_") and f.endswith(".json")
        ]
    except OSError as exc:
        logger.error("Could not list results directory '%s': %s", results_dir, exc)
        return 0

    deleted = 0
    for filename in candidates:
        filepath = os.path.join(results_dir, filename)
        try:
            os.remove(filepath)
            deleted += 1
            logger.info("Deleted analysis result: %s", filepath)
        except OSError as exc:
            logger.warning("Could not delete '%s': %s", filepath, exc)

    return deleted


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ensure_dir(results_dir: str) -> None:
    """
    Creates results_dir if it does not exist.

    Uses exist_ok=True to avoid a race condition where two concurrent
    requests both check os.path.exists and both attempt os.makedirs.
    """
    try:
        os.makedirs(results_dir, exist_ok=True)
    except OSError as exc:
        raise OSError(
            f"Could not create results directory '{results_dir}': {exc}"
        ) from exc


def _read_json_file(filepath: str) -> Optional[dict]:
    """
    Reads and parses a single JSON file.

    Returns None and logs a warning on any read or parse error.
    """
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read result file '%s': %s", filepath, exc)
        return None


def _safe_filename_part(ref_id: str) -> str:
    """
    Produces a filesystem-safe version of a route_id for use in
    filenames and prefix matching.

    Replaces whitespace with underscores and removes characters that are
    problematic on Windows and Linux filesystems.
    """
    return "".join(
        c if c.isalnum() or c in ("-", "_") else "_"
        for c in ref_id
    ).strip("_") or "unknown"
