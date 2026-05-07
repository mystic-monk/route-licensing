import pandas as pd
from route_licensing.core.config import Config

def check_timing_conflicts(
    new_stop: pd.Series,
    existing_index: pd.DataFrame,
    config: Config,
) -> pd.DataFrame:
    """
    For a single stop on the new route, find all existing services
    that serve the same stop_id within the configured timing window.

    Returns a DataFrame of conflicting services with columns:
        route_id, operator, arrival_time, delta_minutes
    """
    same_stop = existing_index[
        existing_index["stop_id"] == new_stop["stop_id"]
    ].copy()

    if same_stop.empty:
        return pd.DataFrame()

    window = pd.Timedelta(minutes=config.timing_window_minutes)
    new_time = new_stop["arrival_time"]
    if not isinstance(new_time, pd.Timedelta):
        return pd.DataFrame()

    same_stop["delta_minutes"] = (
        (same_stop["arrival_time"] - new_time).abs()
        .dt.total_seconds() / 60
    )

    conflicts = same_stop[same_stop["delta_minutes"] <= config.timing_window_minutes]
    cols = ["route_id", "operator", "arrival_time", "delta_minutes"]
    if "route_label" in conflicts.columns:
        cols = ["route_id", "route_label", "operator", "arrival_time", "delta_minutes"]
    return conflicts[cols]