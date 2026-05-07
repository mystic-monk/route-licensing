import os
import pandas as pd
from route_licensing.core.config import Config
from route_licensing.ingestion.gtfs_static_loader import load_gtfs, build_stop_service_index
from route_licensing.ingestion.request_parser import parse_excel_request
from route_licensing.engine.decision_engine import analyse_route
from route_licensing.engine.storage import save_analysis_result

def main():
    config = Config()
    
    gtfs_path = os.getenv("GTFS_STATIC_PATH", "data/gtfs_ireland.zip")
    request_xl = "Application Input Timetable.xlsx"
    
    print(f"--- NTA Route Licensing Analysis System ---")
    
    # 1. Load Static GTFS
    if not os.path.exists(gtfs_path):
        print(f"WARNING: GTFS static feed not found at {gtfs_path}. Using empty index for demonstration.")
        # Create an empty index with required columns for the engine to run without crashing
        static_index = pd.DataFrame(columns=[
            "stop_id", "stop_name", "stop_lat", "stop_lon", 
            "route_id", "operator", "arrival_time"
        ])
    else:
        print(f"Loading GTFS feed from {gtfs_path}...")
        feed, _ = load_gtfs(gtfs_path)
        static_index = build_stop_service_index(feed)
        print(f"Loaded {len(static_index)} existing service-stop records.")

    # 2. Parse New Request
    if not os.path.exists(request_xl):
        print(f"ERROR: Input proposal {request_xl} not found.")
        return
    
    print(f"Parsing new route proposal: {request_xl}")
    try:
        new_route_df = parse_excel_request(request_xl)
        print(f"Proposal contains {len(new_route_df)} stops.")
    except Exception as e:
        print(f"ERROR: Failed to parse Excel file: {e}")
        # Hint for openpyxl
        if "openpyxl" in str(e).lower():
            print("Try: pip install openpyxl")
        return

    # 3. Process Analysis
    print("Running deterministic analysis engine...")
    analysis_results = analyse_route(new_route_df, static_index, config)
    
    # 4. Save and Export
    ref_id = analysis_results["route_id"]
    save_path = save_analysis_result(ref_id, analysis_results)

    # Export trip-level analysis to CSV (one row per trip)
    trip_rows = []
    for trip in analysis_results["trip_analysis"]:
        trip_rows.append({
            "section_idx":         trip["section_idx"],
            "section_title":       trip["section_title"],
            "section_day_groups":  trip["section_day_groups"],
            "trip_idx":            trip["trip_idx"],
            "total_stops":         trip["total_stops"],
            "red_stops":           trip["red_stops"],
            "amber_stops":         trip["amber_stops"],
            "green_stops":         trip["green_stops"],
            "trip_verdict":        trip["trip_verdict"],
            "trip_recommendation": trip["trip_recommendation"],
        })
    csv_output = f"results_{ref_id}.csv"
    pd.DataFrame(trip_rows).to_csv(csv_output, index=False)

    print("\n--- Summary ---")
    print(f"Route ID:       {analysis_results['route_id']}")
    print(f"Operator:       {analysis_results['operator']}")
    print(f"Verdict:        {analysis_results['route_verdict']}")
    print(f"Recommendation: {analysis_results['route_recommendation']}")
    print(f"Total trips:    {analysis_results['total_trips']}  "
          f"(RED: {analysis_results['red_trips']}, "
          f"AMBER: {analysis_results['amber_trips']}, "
          f"GREEN: {analysis_results['green_trips']})")
    print(f"Detailed analysis saved to: {save_path}")
    print(f"Trip-level CSV exported to: {csv_output}")

if __name__ == "__main__":
    main()


    # set PYTHONPATH=src && uvicorn route_licensing.api.main:app --host 0.0.0.0 --port 8000 --reload
    # http://localhost:8000