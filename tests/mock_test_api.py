import sys
from unittest.mock import MagicMock, patch

# 1. Mock heavy dependencies
mock_pd = MagicMock()
sys.modules['pandas'] = mock_pd
sys.modules['geopandas'] = MagicMock()
sys.modules['partridge'] = MagicMock()
sys.modules['dotenv'] = MagicMock()
sys.modules['fastapi'] = MagicMock()
sys.modules['uvicorn'] = MagicMock()

# 2. Mock our own internal modules to isolate API logic
import route_licensing.api.main as api_main
api_main.load_static_gtfs = MagicMock()
api_main.parse_excel_request = MagicMock()
api_main.analyse_route = MagicMock()
api_main.save_analysis_result = MagicMock()
api_main.list_all_analyses = MagicMock(return_value=[{"ref_id": "R1"}])
api_main.get_analysis_by_ref = MagicMock(return_value={
    "route_id": "R1",
    "operator": "Op1",
    "route_verdict": "APPROVE",
    "route_recommendation": "Rec1",
    "total_stops": 1,
    "red_stops": 0,
    "amber_stops": 0,
    "green_stops": 1,
    "stop_analysis": [
        {
            "stop_id": "S1", "stop_name": "Stop1", "arrival_time": "08:00",
            "risk_score": 0, "verdict": "GREEN", "avg_headway_minutes": 60,
            "timing_conflict": False
        }
    ]
})

print("--- API Mock Test Starting ---")

# Test Power BI Export Logic
print("Testing Power BI flattening logic...")
# We call the function directly as the FastAPI app itself is mocked
import asyncio

async def test_powerbi():
    data = await api_main.export_for_powerbi(format="json")
    print(f"Flattened records count: {len(data)}")
    if len(data) > 0 and "stop_name" in data[0] and "route_verdict" in data[0]:
        print("Test Passed: Power BI data flattened correctly.")
    else:
        print("Test Failed: Flattening logic error.")

asyncio.run(test_powerbi())

print("--- API Mock Test Completed ---")
