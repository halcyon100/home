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


def test_closures_say_when_and_group():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    import roadboard as rb
    now = datetime(2026, 11, 2, 12, 15, tzinfo=ZoneInfo("America/Chicago"))
    d = ["Roadwork - Ramp Full Closure on I-41 SB at OFF RAMP TO GOOD HOPE RD. Daily from 11/02/2026 to 11/03/2026, 09:00 PM - 06:00 AM, M, T",
         "Roadwork - Ramp Full Closure on I-41 SB at ON RAMP FROM COUNTY PP. Daily from 11/02/2026 to 11/03/2026, 09:00 PM - 06:00 AM, M, T",
         "Roadwork - Mainline Left Lane Closed on I-41 SB from ON RAMP FROM COUNTY PP to WIS 100 SB-SILVER SPRING (BRIDGE CROSSING)",
         "Roadwork - Ramp Full Closure on I-41 NB at ON RAMP FROM COUNTY PP. Daily from 11/09/2026 to 11/10/2026, 09:00 PM - 06:00 AM, M, T"]
    ev = [{"road": "I-41", "what": x, "ends": 1795672799} for x in d]
    out = rb.closures(ev, now)
    assert out[0]["when"] == "Tonight, 9 pm – 6 am"
    assert out[0]["text"] == "41 south: Good Hope exit and County PP entrance closed"
    assert out[1]["text"] == "41 south: left lane closed, County PP to Silver Spring"
    assert len(out) == 2                      # next week's is past the horizon


def test_headline_leads_with_the_problem():
    import roadboard as rb
    times = [{"what": "I-41/US 45 SB County Q to Zoo Interchange", "now": 24, "normal": 15, "mid": [43.1, -88.1]},
             {"what": "I-94 EB WIS 16 to Zoo Interchange", "now": 11, "normal": 11}]
    lg = rb.legs(times)
    say, worst = rb.headline(lg, [], [])
    assert worst == "41" and say == "41 south is backed up: 24 min to the Zoo, usually 15."
    assert rb.headline(rb.legs([]), [], [])[0] == "Clear to downtown and the airport."


def test_names_read_like_signs():
    import roadboard as rb
    assert rb.what("Ramp Full Closure on I-94 EB at ON RAMP FROM WIS 181 / S 84TH ST. Daily")[1] == "WIS 181 / S 84th St entrance"


def test_camera_on_the_slow_leg():
    import roadboard as rb
    cams = [{"road": "US 18", "lat": 43.03, "lon": -88.0, "where": "US 18", "views": [{"id": 1, "status": "Enabled"}]},
            {"road": "I-94", "lat": 43.034, "lon": -87.97, "where": "I-94 at 35th", "views": [{"id": 2, "status": "Enabled"}]}]
    assert rb.pick_camera(cams, {}, "94e")["where"] == "I-94 at 35th"


def test_145_only_when_the_zoo_is_bad_and_it_saves_ten():
    import roadboard as rb
    routes = [{"min": 44.4, "streets": ["I-41 S", "WIS-145 S", "W Fond du Lac Ave", "I-43 S"]},
              {"min": 53.2, "streets": ["I-41 S", "I-94 E", "I-794 E"]}]
    red = {"41": {"status": "ok"}, "94e": {"status": "bad"}}
    calm = {"41": {"status": "ok"}, "94e": {"status": "ok"}}
    assert rb.alternate(routes, red, 10)["show"] is False            # 9 minutes isn't worth it
    assert rb.alternate(routes, red, 8)["show"] is True
    assert rb.alternate(routes, calm, 5)["show"] is False            # the Zoo isn't bad: no 145
    a = rb.alternate(routes, red, 8)
    assert (a["via145"], a["zoo"]) == (44, 53)


def test_route_names():
    import roadboard as rb
    assert rb.short_route("I-41 S; WIS-145 S; W Fond du Lac Ave Milwaukee") == "W Fond du Lac Ave"
    assert rb.short_route("Willow Creek Rd Richfield; I-41 S") == "I-41 S"
    assert rb.short_route("County Line Rd Lisbon Twp (Waukesha); Appleton Ave Menomonee Falls") == "Appleton Ave"
