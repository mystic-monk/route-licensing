import requests
import pandas as pd
from google.transit import gtfs_realtime_pb2
from route_licensing.core.config import Config

GTFSR_BASE = "https://api.nationaltransport.ie/gtfsr/v2"

ENDPOINTS = {
    "feed":         f"{GTFSR_BASE}/gtfsr",
    "trip_updates": f"{GTFSR_BASE}/TripUpdates",
    "vehicles":     f"{GTFSR_BASE}/Vehicles",
}

def _fetch_feed(endpoint: str, api_key: str) -> gtfs_realtime_pb2.FeedMessage:
    """
    Fetches a protobuf GTFS-R feed from the GTFS-R API.
    Returns a parsed FeedMessage object.
    """
    headers = {
        "x-api-key": api_key,
        "Cache-Control": "no-cache",
    }
    response = requests.get(endpoint, headers=headers, timeout=30)
    response.raise_for_status()

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(response.content)
    return feed

def fetch_trip_updates(api_key: str) -> pd.DataFrame:
    """
    Fetches live trip updates from the GTFS-R v2 API.

    Returns a DataFrame with columns:
        trip_id, route_id, stop_id, stop_sequence,
        arrival_delay_seconds, departure_delay_seconds,
        schedule_relationship
    """
    feed = _fetch_feed(ENDPOINTS["trip_updates"], api_key)

    rows = []
    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue

        trip   = entity.trip_update.trip
        trip_id  = trip.trip_id
        route_id = trip.route_id

        for stu in entity.trip_update.stop_time_update:
            rows.append({
                "trip_id":                  trip_id,
                "route_id":                 route_id,
                "stop_id":                  stu.stop_id,
                "stop_sequence":            stu.stop_sequence,
                "arrival_delay_seconds":    stu.arrival.delay   if stu.HasField("arrival")   else None,
                "departure_delay_seconds":  stu.departure.delay if stu.HasField("departure") else None,
                "schedule_relationship":    stu.schedule_relationship,
            })

    return pd.DataFrame(rows)

def fetch_vehicle_positions(api_key: str) -> pd.DataFrame:
    """
    Fetches live vehicle positions from the GTFS-R v2 API.

    Returns a DataFrame with columns:
        vehicle_id, trip_id, route_id,
        latitude, longitude, bearing, speed_mps,
        current_stop_sequence, current_status, timestamp
    """
    feed = _fetch_feed(ENDPOINTS["vehicles"], api_key)

    rows = []
    for entity in feed.entity:
        if not entity.HasField("vehicle"):
            continue

        v  = entity.vehicle
        pos = v.position

        rows.append({
            "vehicle_id":            v.vehicle.id,
            "trip_id":               v.trip.trip_id,
            "route_id":              v.trip.route_id,
            "latitude":              pos.latitude,
            "longitude":             pos.longitude,
            "bearing":               pos.bearing  if pos.HasField("bearing") else None,
            "speed_mps":             pos.speed    if pos.HasField("speed")   else None,
            "current_stop_sequence": v.current_stop_sequence,
            "current_status":        v.current_status,
            "timestamp":             pd.Timestamp(v.timestamp, unit="s", tz="UTC"),
        })

    return pd.DataFrame(rows)

def fetch_full_feed(api_key: str) -> gtfs_realtime_pb2.FeedMessage:
    """
    Fetches the combined GTFS-R feed (trip updates + alerts + vehicles).
    Returns the raw FeedMessage for cases where you need full access.
    """
    return _fetch_feed(ENDPOINTS["feed"], api_key)