"""The kitchen board's words: what's notable, and when."""
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "house" / "house"))
from board import build, clock, windows  # noqa: E402

TZ = ZoneInfo("America/Chicago")


def hourly(start: datetime, rows):
    return [{"datetime": (start + timedelta(hours=i)).isoformat(), "temperature": t,
             "precipitation_probability": p, "condition": c, "wind_speed": w}
            for i, (t, p, c, w) in enumerate(rows)]


def saturday():
    """The NWS forecast for Saturday 31 October 2026, the day before the clocks fall back."""
    start = datetime(2026, 10, 31, 7, tzinfo=TZ)
    probs = [44, 43, 42, 40, 39, 38, 37, 41, 45, 49, 42, 36, 29, 24, 20, 17, 13, 11, 9, 9, 9, 8, 4, 2, 2, 2, 2]
    temps = [48, 48, 50, 52, 53, 55, 55, 55, 55, 55, 55, 56, 54, 53, 52, 52, 52, 52, 52, 50, 50, 49, 50, 50, 50, 53, 58]
    rows = [(t, p, "rainy" if p >= 10 else "cloudy", 5) for t, p in zip(temps, probs, strict=True)]
    h = hourly(start, rows)
    # Monday: dry morning, rain from noon, likeliest in the evening
    mon = datetime(2026, 11, 2, 6, tzinfo=TZ)
    h += hourly(mon, [(55, 10, "cloudy", 5)] * 6 + [(62, 44, "rainy", 10)] * 6 + [(60, 66, "rainy", 10)] * 6)
    daily = [
        {"datetime": "2026-10-31T06:00:00-05:00", "is_daytime": True, "condition": "rainy", "temperature": 56,
         "precipitation_probability": 49},
        {"datetime": "2026-10-31T18:00:00-05:00", "is_daytime": False, "condition": "rainy", "temperature": 48},
        {"datetime": "2026-11-01T06:00:00-06:00", "is_daytime": True, "condition": "cloudy", "temperature": 65,
         "precipitation_probability": 10},
        {"datetime": "2026-11-01T18:00:00-06:00", "is_daytime": False, "condition": "cloudy", "temperature": 51},
        {"datetime": "2026-11-02T06:00:00-06:00", "is_daytime": True, "condition": "rainy", "temperature": 63,
         "precipitation_probability": 44},
        {"datetime": "2026-11-02T18:00:00-06:00", "is_daytime": False, "condition": "rainy", "temperature": 55},
        {"datetime": "2026-11-03T06:00:00-06:00", "is_daytime": True, "condition": "rainy", "temperature": 67,
         "precipitation_probability": 63},
    ]
    return h, daily


def test_clock_words():
    assert clock(datetime(2026, 1, 1, 12, tzinfo=TZ)) == "Noon"
    assert clock(datetime(2026, 1, 1, 0, tzinfo=TZ)) == "Midnight"
    assert clock(datetime(2026, 1, 1, 7, 10, tzinfo=TZ), exact=True) == "7:10 am"
    assert clock(datetime(2026, 1, 1, 17, tzinfo=TZ)) == "5 pm"


def test_windows_bridge_one_dry_hour():
    assert windows([1, 1, 0, 1, 0, 0, 1], bool) == [(0, 3), (6, 6)]


def test_a_wet_saturday_says_when():
    h, daily = saturday()
    now = datetime(2026, 10, 31, 7, 10, tzinfo=TZ)
    current = {"state": "rainy", "attributes": {"temperature": 48, "humidity": 96}}
    b = build(now, TZ, current, h, daily, datetime(2026, 10, 31, 7, 25, tzinfo=TZ), None, [], "Colgate")
    whats = [n["what"] for n in b["notes"]]
    assert whats[0] == "Showers on and off"
    assert b["notes"][0]["when"].startswith("until 7 pm")
    assert "Clocks fall back tonight" in whats
    assert "Milder tomorrow" in whats
    assert b["say"] == "Grey and damp, with showers on and off until 7."
    assert b["sky"] == "rain" and b["image"] == "sky/rain-morning.jpg"    # no dawn picture yet: morning stands in
    assert (b["high"], b["low"]) == (56, 48)
    assert [d["name"] for d in b["days"]] == ["Sunday", "Monday", "Tuesday"]
    assert b["days"][0]["note"].startswith("Cloudy and dry")
    assert b["days"][1]["note"].startswith("Rain from noon")
    assert b["hours"][0]["h"] == "Now"


def test_pollen_is_only_notable_when_it_matters():
    h, daily = saturday()
    now = datetime(2026, 10, 31, 7, 10, tzinfo=TZ)
    cur = {"state": "cloudy", "attributes": {"temperature": 48}}
    low = build(now, TZ, cur, h, daily, None, {"index": "0.7", "rating": "Low", "top": ["Ragweed"]}, [])
    high = build(now, TZ, cur, h, daily, None, {"index": "8.1", "rating": "Medium-High", "top": ["Ragweed"]}, [])
    assert not any(n["what"].startswith("Pollen") for n in low["notes"])
    assert any(n["what"] == "Pollen medium-high" for n in high["notes"])


def test_clocks_on_friday_afternoon_are_tomorrow_night():
    h, daily = saturday()
    now = datetime(2026, 10, 30, 14, 30, tzinfo=TZ)
    b = build(now, TZ, {"state": "partlycloudy", "attributes": {"temperature": 57}}, h, daily, None, None, [])
    assert {"what": "Clocks fall back tomorrow night", "when": "2 am becomes 1 am"} in b["notes"]


def test_the_sky_now_is_the_station_report_not_the_model():
    from board import observed
    nws = {"state": "sunny", "attributes": {"temperature": 52, "humidity": 67}}
    model = {"state": "cloudy", "attributes": {"temperature": 57, "uv_index": 1.3, "humidity": 58}}
    now = observed(nws, model)
    assert now["state"] == "sunny"
    assert now["attributes"]["temperature"] == 52 and now["attributes"]["uv_index"] == 1.3
    assert observed({"state": "unavailable"}, model) == model


def test_a_clear_night_says_so():
    h, daily = saturday()
    now = datetime(2026, 10, 31, 19, 55, tzinfo=TZ)
    b = build(now, TZ, {"state": "clear-night", "attributes": {"temperature": 50}}, h, daily,
              datetime(2026, 10, 31, 7, 25, tzinfo=TZ), None, [], sunset=datetime(2026, 10, 31, 17, 47, tzinfo=TZ))
    assert b["light"] == "night" and b["label"] == "Tonight"
    assert b["say"].startswith("Clear tonight")
    assert b["dim"] is True          # no night photo yet: the day sky, dimmed


def test_moon_phase():
    from board import moon
    m = moon(datetime(2026, 10, 30, 20, tzinfo=TZ))
    assert m["name"] in ("Waning gibbous", "Full moon") and m["pct"] > 60


def test_moon_arc_rises_late_and_peaks_before_dawn_this_week():
    from board import moon_elevation
    at = lambda h: datetime(2026, 10, 31, h, tzinfo=TZ)  # noqa: E731
    assert moon_elevation(at(18), 43.19, -88.21) < 0      # waning gibbous: not up at dusk
    assert moon_elevation(at(6), 43.19, -88.21) > 50      # high before sunrise
