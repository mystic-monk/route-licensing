"""
Synthetic demo GTFS data for development and demonstration.

Generates a realistic stop-service index DataFrame mimicking Dublin Bus
routes so the application is usable without a real GTFS feed.
"""
import pandas as pd
import numpy as np

# Dublin city centre and surrounding area stop data
DEMO_STOPS = [
    # Route 39A: UCD → Ongar (via City Centre)
    {"stop_id": "8220DB000001", "stop_name": "O'Connell Street",         "stop_lat": 53.3498, "stop_lon": -6.2603},
    {"stop_id": "8220DB000002", "stop_name": "Parnell Square",           "stop_lat": 53.3533, "stop_lon": -6.2638},
    {"stop_id": "8220DB000003", "stop_name": "Dorset Street",            "stop_lat": 53.3582, "stop_lon": -6.2636},
    {"stop_id": "8220DB000004", "stop_name": "Drumcondra Road",          "stop_lat": 53.3665, "stop_lon": -6.2570},
    {"stop_id": "8220DB000005", "stop_name": "Glasnevin",                "stop_lat": 53.3716, "stop_lon": -6.2697},
    {"stop_id": "8220DB000006", "stop_name": "Finglas Road",             "stop_lat": 53.3823, "stop_lon": -6.2902},
    {"stop_id": "8220DB000007", "stop_name": "Blanchardstown Centre",    "stop_lat": 53.3883, "stop_lon": -6.3765},

    # Route 16: Dublin Airport → Ballinteer
    {"stop_id": "8220DB000008", "stop_name": "Dublin Airport",           "stop_lat": 53.4264, "stop_lon": -6.2499},
    {"stop_id": "8220DB000009", "stop_name": "Santry",                   "stop_lat": 53.3930, "stop_lon": -6.2426},
    {"stop_id": "8220DB000010", "stop_name": "Whitehall",                "stop_lat": 53.3773, "stop_lon": -6.2466},
    {"stop_id": "8220DB000011", "stop_name": "Grafton Street",           "stop_lat": 53.3414, "stop_lon": -6.2590},
    {"stop_id": "8220DB000012", "stop_name": "Ranelagh",                 "stop_lat": 53.3265, "stop_lon": -6.2615},
    {"stop_id": "8220DB000013", "stop_name": "Dundrum",                  "stop_lat": 53.2930, "stop_lon": -6.2448},
    {"stop_id": "8220DB000014", "stop_name": "Ballinteer",               "stop_lat": 53.2837, "stop_lon": -6.2575},

    # Route 46A: Phoenix Park → Dún Laoghaire
    {"stop_id": "8220DB000015", "stop_name": "Phoenix Park Gate",        "stop_lat": 53.3558, "stop_lon": -6.3138},
    {"stop_id": "8220DB000016", "stop_name": "Heuston Station",          "stop_lat": 53.3464, "stop_lon": -6.2922},
    {"stop_id": "8220DB000017", "stop_name": "Thomas Street",            "stop_lat": 53.3431, "stop_lon": -6.2836},
    {"stop_id": "8220DB000018", "stop_name": "Dame Street",              "stop_lat": 53.3439, "stop_lon": -6.2656},
    {"stop_id": "8220DB000019", "stop_name": "Pearse Street",            "stop_lat": 53.3435, "stop_lon": -6.2508},
    {"stop_id": "8220DB000020", "stop_name": "Booterstown",              "stop_lat": 53.3159, "stop_lon": -6.1985},
    {"stop_id": "8220DB000021", "stop_name": "Blackrock",                "stop_lat": 53.3016, "stop_lon": -6.1782},
    {"stop_id": "8220DB000022", "stop_name": "Dún Laoghaire",            "stop_lat": 53.2936, "stop_lon": -6.1347},

    # Route 77A: Ringsend → Tallaght
    {"stop_id": "8220DB000023", "stop_name": "Ringsend Road",            "stop_lat": 53.3390, "stop_lon": -6.2308},
    {"stop_id": "8220DB000024", "stop_name": "Grand Canal Dock",         "stop_lat": 53.3396, "stop_lon": -6.2380},
    {"stop_id": "8220DB000025", "stop_name": "Rathmines",                "stop_lat": 53.3243, "stop_lon": -6.2632},
    {"stop_id": "8220DB000026", "stop_name": "Terenure",                 "stop_lat": 53.3105, "stop_lon": -6.2791},
    {"stop_id": "8220DB000027", "stop_name": "Templeogue",               "stop_lat": 53.2977, "stop_lon": -6.3037},
    {"stop_id": "8220DB000028", "stop_name": "Tallaght",                 "stop_lat": 53.2867, "stop_lon": -6.3562},

    # Route 145: Heuston → Bray
    {"stop_id": "8220DB000029", "stop_name": "Donnybrook",               "stop_lat": 53.3189, "stop_lon": -6.2383},
    {"stop_id": "8220DB000030", "stop_name": "Stillorgan",               "stop_lat": 53.2904, "stop_lon": -6.2101},
    {"stop_id": "8220DB000031", "stop_name": "Cornelscourt",             "stop_lat": 53.2769, "stop_lon": -6.1758},
    {"stop_id": "8220DB000032", "stop_name": "Bray",                     "stop_lat": 53.2044, "stop_lon": -6.0986},
]

DEMO_ROUTES = {
    "39A": {
        "operator": "Go-Ahead Ireland",
        "stops": ["8220DB000001", "8220DB000002", "8220DB000003", "8220DB000004",
                   "8220DB000005", "8220DB000006", "8220DB000007"],
    },
    "16": {
        "operator": "Dublin Bus",
        "stops": ["8220DB000008", "8220DB000009", "8220DB000010", "8220DB000001",
                   "8220DB000011", "8220DB000012", "8220DB000013", "8220DB000014"],
    },
    "46A": {
        "operator": "Dublin Bus",
        "stops": ["8220DB000015", "8220DB000016", "8220DB000017", "8220DB000018",
                   "8220DB000019", "8220DB000020", "8220DB000021", "8220DB000022"],
    },
    "77A": {
        "operator": "Go-Ahead Ireland",
        "stops": ["8220DB000023", "8220DB000024", "8220DB000011", "8220DB000025",
                   "8220DB000026", "8220DB000027", "8220DB000028"],
    },
    "145": {
        "operator": "Dublin Bus",
        "stops": ["8220DB000016", "8220DB000011", "8220DB000029", "8220DB000030",
                   "8220DB000031", "8220DB000032"],
    },
}

# Standard Dublin Bus departure windows (6am – 11pm, varying headways)
_PEAK_HEADWAY = 8     # minutes in peak
_OFFPEAK_HEADWAY = 15  # minutes off-peak
_EVENING_HEADWAY = 25  # minutes evening


def build_demo_index() -> pd.DataFrame:
    """
    Builds a synthetic stop-service index DataFrame that mimics what
    `build_stop_service_index()` would produce from a real GTFS feed.

    Columns: stop_id, stop_name, stop_lat, stop_lon, route_id, operator, arrival_time
    """
    stop_lookup = {s["stop_id"]: s for s in DEMO_STOPS}
    rows = []

    for route_id, route_info in DEMO_ROUTES.items():
        operator = route_info["operator"]
        stop_ids = route_info["stops"]

        # Generate realistic departure patterns for a single weekday
        # Morning peak: 07:00 – 09:30 every 8 min
        # Midday: 09:30 – 16:00 every 15 min
        # Evening peak: 16:00 – 19:00 every 8 min
        # Evening: 19:00 – 23:00 every 25 min
        departure_minutes = []

        # Morning peak
        t = 7 * 60  # 07:00
        while t < 9 * 60 + 30:
            departure_minutes.append(t)
            t += _PEAK_HEADWAY

        # Midday
        t = 9 * 60 + 30
        while t < 16 * 60:
            departure_minutes.append(t)
            t += _OFFPEAK_HEADWAY

        # Evening peak
        t = 16 * 60
        while t < 19 * 60:
            departure_minutes.append(t)
            t += _PEAK_HEADWAY

        # Evening
        t = 19 * 60
        while t < 23 * 60:
            departure_minutes.append(t)
            t += _EVENING_HEADWAY

        for dep_min in departure_minutes:
            for stop_idx, sid in enumerate(stop_ids):
                stop = stop_lookup.get(sid)
                if not stop:
                    continue
                # Each subsequent stop is ~3 minutes later
                arr_min = dep_min + (stop_idx * 3)
                rows.append({
                    "stop_id":      sid,
                    "stop_name":    stop["stop_name"],
                    "stop_lat":     stop["stop_lat"],
                    "stop_lon":     stop["stop_lon"],
                    "route_id":     route_id,
                    "operator":     operator,
                    "arrival_time": pd.to_timedelta(arr_min, unit="m"),
                })

    return pd.DataFrame(rows)
