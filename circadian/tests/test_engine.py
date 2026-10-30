import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "circadian"))

import pytest  # noqa: E402

from engine import OFF, Overrides, Schedule, keyframes_from_options, parse_light  # noqa: E402

TZ = ZoneInfo("America/Chicago")


def sun_winter(day):
    return (datetime.combine(day, time(7, 21), TZ), datetime.combine(day, time(17, 52), TZ))


def sun_summer(day):
    return (datetime.combine(day, time(5, 15), TZ), datetime.combine(day, time(20, 30), TZ))


OPTS = [
    {"name": "wake", "at": "07:00", "step": True, "wake": True, "wake_if_sunrise_after": "06:30",
     "lights": ["light.cab 70", "light.nook 40 3300", "light.island off",
                "light.sink 60 3300 via light.sink_wrap", "switch.balcony on"]},
    {"name": "wake2", "at": "07:30", "wake": True, "wake_if_sunrise_after": "06:30",
     "lights": ["light.cab 85", "light.nook 60 4300", "light.island 1"]},
    {"name": "day", "at": "11:00", "lights": ["light.cab 85", "light.nook 60 4000", "light.island 50",
                                              "light.chand 35"]},
    {"name": "eve", "at": "19:00", "lights": ["light.cab 60", "light.nook 40 3000", "light.island 40",
                                              "light.chand 35"]},
    {"name": "night", "at": "21:00", "lights": ["light.cab 20", "light.nook 10 2600", "light.island 8",
                                                "light.chand 4"]},
]


def at(h, m, d=date(2026, 10, 27)):
    return datetime.combine(d, time(h, m), TZ)


@pytest.fixture
def sched():
    return Schedule(keyframes_from_options(OPTS), sun_winter, TZ)


def test_parse_light():
    assert parse_light("light.a 35")[1].brightness == 35
    e, t = parse_light("light.a 35 2700 via light.b")
    assert (e, t.kelvin, t.via) == ("light.a", 2700, "light.b")
    assert parse_light("light.a off")[1].kind == OFF
    with pytest.raises(ValueError):
        parse_light("light.a 0")


def test_bad_time_rejected():
    with pytest.raises(ValueError):
        keyframes_from_options([{"name": "x", "at": "noonish", "lights": []}])


def test_glide_midpoint(sched):
    t = sched.target_at("light.chand", at(20, 0))
    assert t.brightness == pytest.approx(19.5)


def test_evening_kelvin_in_mireds(sched):
    t = sched.target_at("light.nook", at(20, 0))
    # midpoint in mireds between 3000K and 2600K is ~2786K (not the 2800K Kelvin midpoint)
    assert 2780 <= t.kelvin <= 2792


def test_holds_overnight_no_glide_into_step(sched):
    # between 21:00 and the 07:00 step keyframe, the night value holds
    assert sched.target_at("light.cab", at(2, 0)).brightness == 20
    assert sched.target_at("light.cab", at(6, 59)).brightness == 20
    assert sched.target_at("light.cab", at(7, 0)).brightness == 70


def test_off_then_lowest_then_ramp(sched):
    assert sched.target_at("light.island", at(7, 10)).kind == OFF
    assert sched.target_at("light.island", at(7, 30)).brightness == 1
    mid = sched.target_at("light.island", at(9, 15)).brightness
    assert 1 < mid < 50


def test_wake_gated_by_sunrise():
    s = Schedule(keyframes_from_options(OPTS), sun_winter, TZ)
    assert [k.name for _, k in s.wake_events(date(2026, 12, 1))] == ["wake", "wake2"]
    s2 = Schedule(keyframes_from_options(OPTS), sun_summer, TZ)
    assert s2.wake_events(date(2026, 6, 21)) == []


def test_sun_relative_times():
    s = Schedule(keyframes_from_options([{"name": "a", "at": "sunset-00:30", "lights": ["light.x 50"]},
                                         {"name": "b", "at": "sunset+01:00", "lights": ["light.x 10"]}]),
                 sun_winter, TZ)
    assert s.target_at("light.x", at(17, 22)).brightness == 50
    assert s.target_at("light.x", at(18, 52)).brightness == 10


# ---------------------------------------------------------------- overrides

def test_person_change_is_held():
    ov = Overrides()
    now = at(20, 0)
    ov.note_command("light.chand", "ctx-me", now, 100, 30, None, 120)
    # someone sets 80% (204/255) via voice, unknown context
    a = ov.on_state("light.chand", "on", "on", {"brightness": 204}, "ctx-person", now, None)
    assert a == "held" and ov.track("light.chand").held


def test_our_fade_report_not_held():
    ov = Overrides()
    now = at(20, 0)
    ov.note_command("light.chand", None, now, 100, 30, None, 120)  # 100 -> 77
    # end-of-fade report arrives later with an unknown context: within band, so ignored
    a = ov.on_state("light.chand", "on", "on", {"brightness": 78}, "ctx-bridge", now + timedelta(seconds=120), None)
    assert a == "none" and not ov.track("light.chand").held


def test_kelvin_drift_is_not_a_person_but_jump_is():
    ov = Overrides()
    now = at(20, 0)
    ov.note_command("light.nook", None, now, 100, 39, 2900, 120, from_k=2950)
    ok = ov.on_state("light.nook", "on", "on", {"brightness": 100, "color_mode": "color_temp",
                                                "color_temp_kelvin": 2920}, "x", now, None)
    assert ok == "none"
    # xy representation is not a person (Adaptive Lighting #1619 lesson)
    ok2 = ov.on_state("light.nook", "on", "on", {"brightness": 100, "color_mode": "xy"}, "y", now, None)
    assert ok2 == "none"
    jump = ov.on_state("light.nook", "on", "on", {"brightness": 100, "color_mode": "color_temp",
                                                  "color_temp_kelvin": 5000}, "z", now, None)
    assert jump == "held"


def test_off_releases_and_blocks_rewake():
    ov = Overrides()
    t = ov.track("light.cab")
    t.held = True
    a = ov.on_state("light.cab", "on", "off", {}, "person", at(7, 10), None)
    assert a == "none" and not t.held and t.user_off_day == date(2026, 10, 27)


def test_turn_on_bare_snaps_with_level_holds():
    ov = Overrides()
    assert ov.on_state("light.cab", "off", "on", {"brightness": 255}, "c1", at(20, 0), None) == "snap"
    assert ov.on_state("light.cab", "off", "on", {"brightness": 255}, "c2", at(20, 0),
                       {"bare": True}) == "snap"
    assert ov.on_state("light.cab", "off", "on", {"brightness": 128}, "c3", at(20, 0),
                       {"bare": False}) == "held"


def test_power_restore_snaps():
    ov = Overrides()
    assert ov.on_state("light.sink", "unavailable", "on", {"brightness": 200}, "c", at(7, 1), None) == "snap"


def test_ours_flag_ignored():
    ov = Overrides()
    assert ov.on_state("light.cab", "off", "on", {"brightness": 255}, "c", at(7, 0), {"bare": False},
                       ours=True) == "none"


def test_plain_on_while_on_releases():
    ov = Overrides()
    ov.track("light.cab").held = True
    a = ov.on_state("light.cab", "on", "on", {"brightness": 10}, "c", at(20, 0), {"bare": True})
    assert a == "snap" and not ov.track("light.cab").held


def test_command_within_band_still_held():
    ov = Overrides()
    now = at(20, 0)
    ov.note_command("light.chand", None, now, 255, 40, None, 120)
    a = ov.on_state("light.chand", "on", "on", {"brightness": 200}, "p", now, {"bare": False})
    assert a == "held"


def test_physical_change_outside_band_held():
    ov = Overrides()
    now = at(20, 0)
    ov.note_command("light.chand", None, now, 100, 30, None, 120)
    assert ov.on_state("light.chand", "on", "on", {"brightness": 240}, "wall", now, None) == "held"


def test_overlapping_fades_not_a_person():
    ov = Overrides()
    now = at(7, 20)
    ov.note_command("light.c", None, now, 100, 45, 3800, 120, from_k=3700)          # 100 -> 115
    ov.note_command("light.c", None, now + timedelta(seconds=120), 115, 47, 3900, 120, from_k=3800)
    # bridge reports where the first fade really ended (slightly short), after the second started
    a = ov.on_state("light.c", "on", "on", {"brightness": 104, "color_mode": "color_temp",
                                            "color_temp_kelvin": 3760}, "bridge", now + timedelta(seconds=125), None)
    assert a == "none"


def test_restart_grace():
    ov = Overrides()
    now = at(7, 33)
    ov.grace_until = now + timedelta(minutes=3)
    assert ov.on_state("light.f", "on", "on", {"brightness": 30}, "bridge", now, None) == "none"
    assert ov.on_state("light.f", "on", "on", {"brightness": 30}, "bridge", now + timedelta(minutes=4), None) == "held"


# ---------------------------------------------------------------- daylight

from engine import daylight_zones_from_options  # noqa: E402


def _zone():
    return daylight_zones_from_options([{"name": "k", "sensor": "sensor.lux", "off_above": 250,
                                         "on_below": 160, "off_after_minutes": 10,
                                         "on_after_minutes": 3, "lights": ["light.cab"]}])[0]


def test_daylight_needs_sustained_brightness():
    z = _zone()
    t = at(9, 0)
    z.update("300", t)
    assert not z.evaluate(t + timedelta(minutes=9))
    assert z.evaluate(t + timedelta(minutes=10))


def test_daylight_dip_resets_clock_and_hysteresis_holds():
    z = _zone()
    t = at(9, 0)
    z.update(300, t)
    z.update(240, t + timedelta(minutes=5))      # dip below off_above restarts the clock
    z.update(300, t + timedelta(minutes=6))
    assert not z.evaluate(t + timedelta(minutes=12))
    assert z.evaluate(t + timedelta(minutes=16))
    z.update(200, t + timedelta(minutes=20))     # between thresholds: stays bright
    assert z.evaluate(t + timedelta(minutes=40))
    z.update(150, t + timedelta(minutes=41))
    assert z.evaluate(t + timedelta(minutes=43))
    assert not z.evaluate(t + timedelta(minutes=44))


def test_daylight_unavailable_sensor_means_dark():
    z = _zone()
    t = at(9, 0)
    z.update(500, t)
    assert z.evaluate(t + timedelta(minutes=11))
    z.update("unavailable", t + timedelta(minutes=12))
    assert not z.evaluate(t + timedelta(minutes=12))


def test_daylight_zone_validation():
    with pytest.raises(ValueError):
        daylight_zones_from_options([{"sensor": "s", "off_above": 100, "on_below": 200, "lights": []}])
    with pytest.raises(ValueError):
        daylight_zones_from_options([
            {"sensor": "a", "off_above": 200, "on_below": 100, "lights": ["light.x"]},
            {"sensor": "b", "off_above": 200, "on_below": 100, "lights": ["light.x"]}])


SPLIT = [
    {"name": "day", "at": "12:00", "lights": ["light.a 80 4000"]},
    {"name": "dusk", "at": "18:30", "channel": "brightness", "lights": ["light.a 80"]},
    {"name": "evening", "at": "sunset", "channel": "color", "lights": ["light.a - 3100"]},
    {"name": "night", "at": "21:30", "lights": ["light.a 12 2600"]},
]


def test_color_and_brightness_follow_their_own_keyframes():
    s = Schedule(keyframes_from_options(SPLIT), sun_winter, TZ)   # winter sunset is before 18:30
    sunset = sun_winter(date(2026, 12, 1))[1]
    # afternoon: brightness flat at 80 until 18:30; color already warming toward sunset
    t = s.target_at("light.a", datetime.combine(date(2026, 12, 1), time(15, 0), TZ))
    assert t.brightness == 80 and 3100 < t.kelvin < 4000
    assert s.target_at("light.a", sunset).kelvin == 3100
    t = s.target_at("light.a", datetime.combine(date(2026, 12, 1), time(18, 30), TZ))
    assert t.brightness == 80 and 2600 < t.kelvin < 3100
    t = s.target_at("light.a", datetime.combine(date(2026, 12, 1), time(20, 0), TZ))
    assert 12 < t.brightness < 80
    t = s.target_at("light.a", datetime.combine(date(2026, 12, 1), time(21, 30), TZ))
    assert (t.brightness, t.kelvin) == (12, 2600)


def test_lights_without_split_keyframes_are_unchanged():
    s = Schedule(keyframes_from_options(OPTS), sun_winter, TZ)
    t = s.target_at("light.nook", at(20, 0, date(2026, 12, 1)))
    assert t.brightness == 25 and t.kelvin is not None


def test_color_keyframe_rules():
    with pytest.raises(ValueError):
        keyframes_from_options([{"name": "x", "at": "18:00", "channel": "color", "lights": ["light.a 40"]}])
    with pytest.raises(ValueError):
        keyframes_from_options([{"name": "x", "at": "18:00", "lights": ["light.a - 3000"]}])
    with pytest.raises(ValueError):
        keyframes_from_options([{"name": "x", "at": "18:00", "channel": "color", "wake": True, "lights": ["light.a - 3000"]}])


SUN = [
    {"name": "wake", "at": "07:00", "lights": ["light.a 40 2400"]},
    {"name": "bright", "at": "09:00", "lights": ["light.a 80"]},            # brightness only: no color
    {"name": "rise", "at": "sunrise", "channel": "color", "lights": ["light.a - 2500"]},
    {"name": "peak", "at": "noon", "channel": "color", "lights": ["light.a - 5000"]},
    {"name": "set", "at": "sunset", "channel": "color", "lights": ["light.a - 2700"]},
    {"name": "night", "at": "21:30", "lights": ["light.a 12 2400"]},
]


def test_color_follows_the_sun_while_brightness_follows_the_day():
    s = Schedule(keyframes_from_options(SUN), sun_winter, TZ)
    d = date(2026, 12, 1)
    rise, set_ = sun_winter(d)
    noon = rise + (set_ - rise) / 2
    assert s.target_at("light.a", noon).kelvin == 5000
    assert 12 < s.target_at("light.a", noon).brightness < 80   # gliding 80 -> 12 by 21:30
    assert s.target_at("light.a", set_).kelvin == 2700
    # a brightness-only keyframe doesn't drag the color toward anything
    mid_morning = rise + (noon - rise) / 2
    k = s.target_at("light.a", mid_morning).kelvin
    assert 2500 < k < 5000
    assert s.target_at("light.a", datetime.combine(d, time(21, 30), TZ)).kelvin == 2400
