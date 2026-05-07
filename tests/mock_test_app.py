import sys
from unittest.mock import MagicMock

# 1. Mock heavy dependencies BEFORE importing our code
mock_pd = MagicMock()
mock_gpd = MagicMock()
mock_ptg = MagicMock()
mock_np = MagicMock()
mock_sklearn = MagicMock()

# Mock DataFrame behavior for comparison and sum
class MockSeries(list):
    def sum(self):
        return sum(self)
    @property
    def iloc(self):
        return self
    def __eq__(self, other):
        return MockSeries([x == other for x in self])

class MockDataFrame:
    def __init__(self, data):
        self.data = data
    @property
    def empty(self):
        return len(self.data) == 0
    def __getitem__(self, key):
        return MockSeries([d[key] for d in self.data])
    def __len__(self):
        return len(self.data)
    def to_dict(self, orient):
        return self.data
    def iterrows(self):
        return enumerate(self.data)
    @property
    def iloc(self):
        return self
    def __getitem__(self, key):
        if isinstance(key, str):
            return MockSeries([d[key] for d in self.data])
        return self

mock_pd.DataFrame = MockDataFrame

sys.modules['pandas'] = mock_pd
sys.modules['geopandas'] = mock_gpd
sys.modules['partridge'] = mock_ptg
sys.modules['numpy'] = mock_np
sys.modules['sklearn'] = mock_sklearn
sys.modules['sklearn.cluster'] = mock_sklearn.cluster
sys.modules['shapely'] = MagicMock()
sys.modules['shapely.geometry'] = MagicMock()
sys.modules['google'] = MagicMock()
sys.modules['google.transit'] = MagicMock()
sys.modules['dotenv'] = MagicMock()

# 2. Now we can import our modules safely
from route_licensing.core.config import Config
from route_licensing.engine.decision_engine import analyse_route
from route_licensing.ingestion.request_parser import parse_new_route_request, parse_excel_request

print("--- Mock Logic Test Starting ---")

# Test 1: Config Initialisation
cfg = Config()
print(f"Config Initialised: Window={cfg.timing_window_minutes}m, Radius={cfg.corridor_radius_metres}m")

# Test 2: Decision Engine Logic (Mocking DataFrames)
# We need to simulate the input DataFrame
mock_data = [
    {"stop_id": "S1", "stop_name": "Test Stop", "arrival_time": "08:00:00", "route_id": "NR-001", "operator": "Go-Ahead"}
]
mock_new_route = MockDataFrame(mock_data)
mock_existing = MagicMock()

# Mock the return values of the detectors
import route_licensing.engine.decision_engine as engine
engine.detect_corridor_overlap = MagicMock(return_value={"S1": []})
engine.check_timing_conflicts = MagicMock(return_value=MockDataFrame([]))
engine.score_stop_frequency = MagicMock(return_value={"frequency_verdict": "underserved", "avg_headway_minutes": 60})

print("Running analyse_route with mocks...")
results = engine.analyse_route(mock_new_route, mock_existing, cfg)

print(f"Analysis Verdict: {results['route_verdict']}")
print(f"Recommendation: {results['route_recommendation']}")

if results['route_verdict'] == "APPROVE":
    print("Test Passed: Logic correctly identified an underserved route.")
else:
    print(f"Test Failed: Expected APPROVE, got {results['route_verdict']}")

print("--- Mock Logic Test Completed ---")
