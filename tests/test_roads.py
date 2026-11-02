"""Roads from 511 Wisconsin: only what's near home, the worst first."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "house" / "house"))
from roads import miles, nearby_events, nearby_times  # noqa: E402

HOME = (43.20, -88.22)


def test_miles():
    assert abs(miles(43.0389, -87.9065, 43.0731, -89.4012) - 75) < 3     # Milwaukee to Madison, about 75 mi


def test_events_near_home_closures_first():
    ev = [
        {"RoadwayName": "I-94", "Latitude": 43.04, "Longitude": -87.95, "EventType": "roadwork", "IsFullClosure": False},
        {"RoadwayName": "I-41", "Latitude": 43.12, "Longitude": -88.10, "EventType": "accidentsAndIncidents", "IsFullClosure": False},
        {"RoadwayName": "WIS 167", "Latitude": 43.21, "Longitude": -88.20, "EventType": "closures", "IsFullClosure": True},
        {"RoadwayName": "I-39", "Latitude": 44.5, "Longitude": -89.5, "EventType": "closures", "IsFullClosure": True},
        {"RoadwayName": "No place"},
    ]
    out = nearby_events(ev, HOME)
    assert [e["road"] for e in out] == ["WIS 167", "I-41", "I-94"]


def test_times_worst_delay_first():
    rows = [
        {"RoadwayName": "I-41", "StartLatitude": "43.15", "StartLongitude": "-88.12", "EndLatitude": "43.10",
         "EndLongitude": "-88.05", "CurrentTime": "9", "NormalTime": "7", "Delay": "2"},
        {"RoadwayName": "I-94", "StartLatitude": "43.03", "StartLongitude": "-88.00", "EndLatitude": "43.03",
         "EndLongitude": "-87.92", "CurrentTime": "14", "NormalTime": "6", "Delay": "8"},
        {"RoadwayName": "I-90", "StartLatitude": "42.6", "StartLongitude": "-89.0", "EndLatitude": "42.5",
         "EndLongitude": "-89.0", "Delay": "30"},
    ]
    out = nearby_times(rows, HOME)
    assert [t["road"] for t in out] == ["I-94", "I-41"]
