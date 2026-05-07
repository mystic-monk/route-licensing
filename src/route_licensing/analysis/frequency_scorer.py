import pandas as pd
from route_licensing.core.config import Config

def score_stop_frequency(
    stop_id: str,
    existing_index: pd.DataFrame,
    config: Config,
) -> dict:
    """
    Computes the average headway (gap between services) at a stop
    across all existing routes, and returns a frequency verdict.

    Returns:
        {
            "stop_id": ...,
            "service_count": ...,
            "avg_headway_minutes": ...,
            "frequency_verdict": "well_served" | "moderate" | "underserved"
        }
    """
    services = existing_index[existing_index["stop_id"] == stop_id].copy()

    if services.empty:
        return {
            "stop_id": stop_id,
            "service_count": 0,
            "avg_headway_minutes": None,
            "frequency_verdict": "underserved",
        }

    sorted_times = services["arrival_time"].sort_values()
    gaps = sorted_times.diff().dropna().dt.total_seconds() / 60

    avg_headway = gaps.mean() if not gaps.empty else None

    if avg_headway is None or avg_headway > config.amber_headway_minutes:
        verdict = "underserved"
    elif avg_headway <= config.min_headway_minutes:
        verdict = "well_served"
    else:
        verdict = "moderate"

    return {
        "stop_id": stop_id,
        "service_count": len(services),
        "avg_headway_minutes": round(avg_headway, 1) if avg_headway else None,
        "frequency_verdict": verdict,
    }