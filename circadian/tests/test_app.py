"""Integration test: run the app against a fake Home Assistant websocket."""
import asyncio
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "circadian"))

import pytest  # noqa: E402
from aiohttp import web  # noqa: E402

import app as appmod  # noqa: E402
from test_engine import OPTS  # noqa: E402

TZ = ZoneInfo("America/Chicago")
ME = "user-circadian"


def st(e, state, **attrs):
    return {"entity_id": e, "state": state, "attributes": attrs, "context": {"id": "init", "user_id": None}}


class FakeHA:
    def __init__(self):
        self.states = {
            "light.cab": st("light.cab", "on", brightness=255, supported_color_modes=["brightness"]),
            "light.nook": st("light.nook", "on", brightness=255, supported_color_modes=["color_temp", "xy"],
                             color_mode="color_temp", color_temp_kelvin=4000,
                             min_color_temp_kelvin=2000, max_color_temp_kelvin=6535),
            "light.island": st("light.island", "off", supported_color_modes=["brightness"]),
            "light.chand": st("light.chand", "off", supported_color_modes=["brightness"]),
            "light.sink": st("light.sink", "unavailable", supported_color_modes=["color_temp"]),
            "input_boolean.circadian": st("input_boolean.circadian", "on"),
        }
        self.calls = []
        self.ws = None
        self.subs = {}
        self.n = 0

    async def handler(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.ws = ws
        await ws.send_json({"type": "auth_required"})
        await ws.receive_json()
        await ws.send_json({"type": "auth_ok"})
        async for msg in ws:
            m = msg.json()
            t, i = m["type"], m["id"]
            if t == "get_config":
                res = {"time_zone": "America/Chicago", "latitude": 43.2, "longitude": -88.2}
            elif t == "auth/current_user":
                res = {"id": ME}
            elif t == "get_states":
                res = list(self.states.values())
            elif t == "subscribe_events":
                self.subs[m["event_type"]] = i
                res = None
            elif t == "call_service":
                self.n += 1
                ctx = {"id": f"ctx-{self.n}", "user_id": ME}
                self.calls.append((m["domain"], m["service"], m["target"]["entity_id"], m["service_data"]))
                res = {"context": ctx}
                await ws.send_json({"id": i, "type": "result", "success": True, "result": res})
                # apply the effect and emit a state change carrying our context
                e = m["target"]["entity_id"]
                if e in self.states and m["service"] == "turn_on":
                    old = dict(self.states[e])
                    new = dict(old, state="on", context=ctx,
                               attributes=dict(old["attributes"]))
                    if "brightness_pct" in m["service_data"]:
                        new["attributes"]["brightness"] = round(m["service_data"]["brightness_pct"] * 2.55)
                    self.states[e] = new
                    await self.event("state_changed", {"entity_id": e, "old_state": old, "new_state": new})
                continue
            else:
                res = None
            await ws.send_json({"id": i, "type": "result", "success": True, "result": res})
        return ws

    async def event(self, etype, data, context=None):
        sid = self.subs.get(etype)
        if sid is None:
            return
        ev = {"event_type": etype, "data": data, "context": context or {"id": "evt", "user_id": None}}
        await self.ws.send_json({"id": sid, "type": "event", "event": ev})

    async def person_sets(self, e, **attrs):
        old = dict(self.states[e])
        new = dict(old, state="on", attributes=dict(old["attributes"], **attrs),
                   context={"id": "person-ctx", "user_id": "someone"})
        self.states[e] = new
        await self.event("state_changed", {"entity_id": e, "old_state": old, "new_state": new})


class Clock:
    def __init__(self, dt):
        self.dt = dt

    def __call__(self):
        return self.dt


async def boot(aiohttp_server, opts, clock):
    fake = FakeHA()
    srv_app = web.Application()
    srv_app.router.add_get("/ws", fake.handler)
    server = await aiohttp_server(srv_app)
    ha = appmod.HA(f"ws://127.0.0.1:{server.port}/ws", "t")
    await ha.connect()
    c = appmod.Circadian(opts, ha)
    c.now = clock
    reader = asyncio.create_task(ha.reader())
    await c.start()
    return fake, c, ha, reader


def options(shadow=False):
    return {"shadow": shadow, "interval_seconds": 120, "enable_entity": "input_boolean.circadian",
            "morning_reset": "07:00", "keyframes": OPTS}


@pytest.mark.asyncio
async def test_evening_tick_only_touches_on_lights(aiohttp_server):
    clock = Clock(datetime.combine(date(2026, 12, 1), time(20, 0), TZ))
    fake, c, ha, reader = await boot(aiohttp_server, options(), clock)
    await c.tick()
    await asyncio.sleep(0.05)
    touched = {call[2] for call in fake.calls}
    assert touched == {"light.cab", "light.nook"}          # island/chand are off: never turned on
    nook = [x for x in fake.calls if x[2] == "light.nook"][0][3]
    assert nook["transition"] == 120 and 2780 <= nook["color_temp_kelvin"] <= 2792
    reader.cancel(); await ha.close()


@pytest.mark.asyncio
async def test_shadow_mode_sends_nothing(aiohttp_server):
    clock = Clock(datetime.combine(date(2026, 12, 1), time(20, 0), TZ))
    fake, c, ha, reader = await boot(aiohttp_server, options(shadow=True), clock)
    await c.tick()
    await asyncio.sleep(0.05)
    assert fake.calls == []
    reader.cancel(); await ha.close()


@pytest.mark.asyncio
async def test_kill_switch(aiohttp_server):
    clock = Clock(datetime.combine(date(2026, 12, 1), time(20, 0), TZ))
    fake, c, ha, reader = await boot(aiohttp_server, options(), clock)
    c.states["input_boolean.circadian"]["state"] = "off"
    await c.tick()
    await c.wake_check()
    assert fake.calls == []
    reader.cancel(); await ha.close()


@pytest.mark.asyncio
async def test_person_change_held_then_skipped(aiohttp_server):
    clock = Clock(datetime.combine(date(2026, 12, 1), time(20, 0), TZ))
    fake, c, ha, reader = await boot(aiohttp_server, options(), clock)
    await c.tick()
    await asyncio.sleep(0.05)
    # "Alexa, kitchen cabinet to 98" goes through HA: a call_service event, then the state change
    await fake.event("call_service", {"domain": "light", "service": "turn_on",
                                      "service_data": {"entity_id": "light.cab", "brightness_pct": 98}},
                     context={"id": "person-ctx", "user_id": "someone"})
    await fake.person_sets("light.cab", brightness=250)   # within our fade band, still a person
    await asyncio.sleep(0.05)
    assert c.ov.track("light.cab").held
    fake.calls.clear()
    await c.tick()
    await asyncio.sleep(0.05)
    assert "light.cab" not in {x[2] for x in fake.calls}
    reader.cancel(); await ha.close()


@pytest.mark.asyncio
async def test_wake_turns_on_path_on_dark_morning(aiohttp_server):
    clock = Clock(datetime.combine(date(2026, 12, 1), time(7, 0, 30), TZ))
    fake, c, ha, reader = await boot(aiohttp_server, options(), clock)
    fake.states["light.cab"]["state"] = "off"
    c.states["light.cab"]["state"] = "off"
    await c.wake_check()
    await asyncio.sleep(0.05)
    targets = [x[2] for x in fake.calls]
    assert "light.cab" in targets and "light.nook" not in targets  # nook already on
    assert "light.sink_wrap" in targets                            # powered via wrapper
    assert ("switch", "turn_on", "switch.balcony", {}) in fake.calls
    assert "light.island" not in targets                           # held off until 7:30
    # does not fire twice
    n = len(fake.calls)
    await c.wake_check()
    assert len(fake.calls) == n
    reader.cancel(); await ha.close()


@pytest.mark.asyncio
async def test_no_wake_after_user_turned_it_off(aiohttp_server):
    clock = Clock(datetime.combine(date(2026, 12, 1), time(7, 31), TZ))
    fake, c, ha, reader = await boot(aiohttp_server, options(), clock)
    c.ov.track("light.island").user_off_day = date(2026, 12, 1)
    await c.wake_check()
    assert "light.island" not in [x[2] for x in fake.calls]
    reader.cancel(); await ha.close()


@pytest.mark.asyncio
async def test_early_wake_runs_morning_now(aiohttp_server):
    clock = Clock(datetime.combine(date(2026, 12, 1), time(5, 30), TZ))
    fake, c, ha, reader = await boot(aiohttp_server, options(), clock)
    fake.states["light.cab"]["state"] = "off"
    c.states["light.cab"]["state"] = "off"
    await fake.event("circadian_wake", {})
    await asyncio.sleep(0.05)
    assert c.vnow().hour == 7 and c.vnow().minute == 0
    await c.wake_check()
    await asyncio.sleep(0.05)
    targets = [x[2] for x in fake.calls]
    assert "light.cab" in targets and "light.island" not in targets
    # half way to noon, the schedule has caught up partially
    clock.dt = datetime.combine(date(2026, 12, 1), time(8, 45), TZ)
    v = c.vnow()
    assert datetime.combine(date(2026, 12, 1), time(8, 45), TZ) < v < datetime.combine(date(2026, 12, 1), time(12, 0), TZ)
    clock.dt = datetime.combine(date(2026, 12, 1), time(12, 0), TZ)
    assert c.vnow() == clock.dt
    reader.cancel(); await ha.close()


@pytest.mark.asyncio
async def test_wake_word_on_light_morning(aiohttp_server):
    # summer: the sun gate skips the automatic wake, but asking still works
    from engine import Schedule
    clock = Clock(datetime.combine(date(2026, 6, 21), time(7, 40), TZ))
    fake, c, ha, reader = await boot(aiohttp_server, options(), clock)
    c.schedule = Schedule(c.keyframes, lambda d: (datetime.combine(d, time(5, 15), TZ),
                                                   datetime.combine(d, time(20, 30), TZ)), TZ)
    fake.states["light.cab"]["state"] = "off"
    c.states["light.cab"]["state"] = "off"
    await c.wake_check()
    assert fake.calls == []
    await fake.event("circadian_wake", {})
    await asyncio.sleep(0.05)
    await c.wake_check()
    await asyncio.sleep(0.05)
    assert "light.cab" in [x[2] for x in fake.calls]
    reader.cancel(); await ha.close()


@pytest.mark.asyncio
async def test_quiet_when_occupied(aiohttp_server):
    clock = Clock(datetime.combine(date(2026, 12, 1), time(20, 0), TZ))
    opts = dict(options(), quiet_when_occupied=["light.cab binary_sensor.kitchen"])
    fake, c, ha, reader = await boot(aiohttp_server, opts, clock)
    c.states["binary_sensor.kitchen"] = {"entity_id": "binary_sensor.kitchen", "state": "on", "attributes": {}}
    await c.tick()
    await asyncio.sleep(0.05)
    assert "light.cab" not in [x[2] for x in fake.calls]
    c.states["binary_sensor.kitchen"]["state"] = "off"
    await c.tick()
    await asyncio.sleep(0.05)
    assert "light.cab" in [x[2] for x in fake.calls]
    reader.cancel(); await ha.close()


@pytest.mark.asyncio
async def test_lutron_fresh_context_matched_to_recent_command(aiohttp_server):
    # "Alexa, kitchen brighter" turns on an off Lutron light; Lutron reports it with a new context.
    clock = Clock(datetime.combine(date(2026, 12, 1), time(7, 57), TZ))
    fake, c, ha, reader = await boot(aiohttp_server, options(), clock)
    await fake.event("call_service", {"domain": "light", "service": "turn_on",
                                      "service_data": {"entity_id": "light.chand", "brightness": 160}},
                     context={"id": "alexa-ctx", "user_id": "cloud"})
    await asyncio.sleep(0.02)
    await fake.person_sets("light.chand", brightness=160)    # arrives with an unrelated context
    await asyncio.sleep(0.05)
    assert c.ov.track("light.chand").held
    assert "light.chand" not in [x[2] for x in fake.calls]   # no snap back to the curve
    reader.cancel(); await ha.close()


@pytest.mark.asyncio
async def test_holds_survive_restart(aiohttp_server, tmp_path, monkeypatch):
    monkeypatch.setenv("CIRCADIAN_STATE", str(tmp_path / "state.json"))
    clock = Clock(datetime.combine(date(2026, 12, 1), time(20, 0), TZ))
    fake, c, ha, reader = await boot(aiohttp_server, options(), clock)
    c.ov.track("light.cab").held, c.ov.track("light.cab").held_reason = True, "set by a command"
    c.save_state()
    reader.cancel(); await ha.close()
    fake2, c2, ha2, reader2 = await boot(aiohttp_server, options(), clock)
    c2.load_state()
    assert c2.ov.track("light.cab").held
    await c2.tick()
    await asyncio.sleep(0.05)
    assert "light.cab" not in [x[2] for x in fake2.calls]
    # after the next morning reset, an old hold isn't restored
    clock.dt = datetime.combine(date(2026, 12, 2), time(7, 5), TZ)
    fake3, c3, ha3, reader3 = await boot(aiohttp_server, options(), clock)
    c3.load_state()
    assert not c3.ov.track("light.cab").held
    reader2.cancel(); await ha2.close(); reader3.cancel(); await ha3.close()


@pytest.mark.asyncio
async def test_keepoff_room_on_vs_direct(aiohttp_server):
    clock = Clock(datetime.combine(date(2026, 12, 1), time(8, 0), TZ))
    opts = dict(options(), keyframes=OPTS + [{"name": "km", "at": "07:00", "step": True,
                                              "lights": ["light.chand keepoff"]}])
    fake, c, ha, reader = await boot(aiohttp_server, opts, clock)
    c.states["light.kitchen_group"] = {"entity_id": "light.kitchen_group", "state": "on",
                                       "attributes": {"entity_id": ["light.chand", "light.cab"]}}
    # "Alexa, kitchen on": group call, then the group forwards to members with the same context
    for ents in (["light.kitchen_group"], ["light.chand", "light.cab"]):
        await fake.event("call_service", {"domain": "light", "service": "turn_on",
                                          "service_data": {"entity_id": ents}},
                         context={"id": "room-ctx", "user_id": "cloud"})
    await asyncio.sleep(0.02)
    old = dict(fake.states["light.chand"])
    new = dict(old, state="on", attributes=dict(old["attributes"], brightness=200),
               context={"id": "room-ctx", "user_id": "cloud"})
    fake.states["light.chand"] = new
    await fake.event("state_changed", {"entity_id": "light.chand", "old_state": old, "new_state": new})
    await asyncio.sleep(0.05)
    assert ("light", "turn_off", "light.chand", {"transition": 1}) in fake.calls
    # "Alexa, chandelier on" (direct) is respected
    fake.calls.clear()
    await fake.event("call_service", {"domain": "light", "service": "turn_on",
                                      "service_data": {"entity_id": "light.chand"}},
                     context={"id": "direct-ctx", "user_id": "cloud"})
    await asyncio.sleep(0.02)
    old = dict(fake.states["light.chand"], state="off")
    new = dict(old, state="on", context={"id": "direct-ctx", "user_id": "cloud"})
    await fake.event("state_changed", {"entity_id": "light.chand", "old_state": old, "new_state": new})
    await asyncio.sleep(0.05)
    assert not any(x[1] == "turn_off" for x in fake.calls)
    reader.cancel(); await ha.close()


@pytest.mark.asyncio
async def test_reset_event_releases_and_reapplies(aiohttp_server):
    clock = Clock(datetime.combine(date(2026, 12, 1), time(20, 0), TZ))
    fake, c, ha, reader = await boot(aiohttp_server, options(), clock)
    c.ov.track("light.cab").held = True
    await fake.event("circadian_reset", {})
    await asyncio.sleep(0.1)
    assert not c.ov.track("light.cab").held
    assert "light.cab" in [x[2] for x in fake.calls]
    reader.cancel(); await ha.close()


@pytest.mark.asyncio
async def test_second_call_in_one_automation_run_is_its_own_request(aiohttp_server):
    # An automation turns on one light, then sends a plain "on" to another light
    # that is already on (dimmed and held). Both calls share the run's context.
    clock = Clock(datetime.combine(date(2026, 12, 1), time(20, 0), TZ))
    fake, c, ha, reader = await boot(aiohttp_server, options(), clock)
    c.ov.track("light.nook").held = True
    for ents in (["light.cab"], ["light.nook"]):
        await fake.event("call_service", {"domain": "light", "service": "turn_on",
                                          "service_data": {"entity_id": ents, "transition": 5}},
                         context={"id": "auto-run", "user_id": None})
    await asyncio.sleep(0.05)
    assert not c.ov.track("light.nook").held
    assert any(x[2] == "light.nook" for x in fake.calls)
    reader.cancel(); await ha.close()


def daylight_options():
    return dict(options(), daylight=[{"name": "kitchen", "sensor": "sensor.lux", "off_above": 250,
                                      "on_below": 160, "off_after_minutes": 10,
                                      "on_after_minutes": 3, "lights": ["light.nook", "light.cab"]}])


async def set_lux(fake, value):
    old = fake.states.get("sensor.lux") or st("sensor.lux", "0")
    new = st("sensor.lux", str(value))
    fake.states["sensor.lux"] = new
    await fake.event("state_changed", {"entity_id": "sensor.lux", "old_state": old, "new_state": new})
    await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_daylight_turns_off_then_back_on(aiohttp_server):
    clock = Clock(datetime.combine(date(2026, 12, 1), time(12, 0), TZ))
    fake, c, ha, reader = await boot(aiohttp_server, daylight_options(), clock)
    await set_lux(fake, 400)
    clock.dt += timedelta(minutes=11)
    await c.daylight_step()
    await asyncio.sleep(0.05)
    offs = {x[2] for x in fake.calls if x[1] == "turn_off"}
    assert offs == {"light.nook", "light.cab"}
    assert c.daylight_off == {"light.nook", "light.cab"}
    # the fake doesn't apply turn_off, so do it the way HA would report it (our context)
    for e in ("light.nook", "light.cab"):
        old = fake.states[e]
        new = dict(old, state="off", context={"id": "x", "user_id": ME})
        fake.states[e] = new
        await fake.event("state_changed", {"entity_id": e, "old_state": old, "new_state": new})
    await asyncio.sleep(0.02)
    fake.calls.clear()
    await set_lux(fake, 100)
    clock.dt += timedelta(minutes=4)
    await c.daylight_step()
    await asyncio.sleep(0.05)
    ons = {x[2] for x in fake.calls if x[1] == "turn_on"}
    assert ons == {"light.nook", "light.cab"}
    assert not c.daylight_off
    reader.cancel(); await ha.close()


@pytest.mark.asyncio
async def test_daylight_respects_person_and_goodnight(aiohttp_server):
    clock = Clock(datetime.combine(date(2026, 12, 1), time(12, 0), TZ))
    fake, c, ha, reader = await boot(aiohttp_server, daylight_options(), clock)
    await set_lux(fake, 400)
    clock.dt += timedelta(minutes=11)
    await c.daylight_step()
    await asyncio.sleep(0.05)
    # someone turns the nook back on while it's bright: leave it on today
    old = dict(fake.states["light.nook"], state="off")
    new = dict(old, state="on", context={"id": "p", "user_id": "someone"})
    fake.states["light.nook"] = new
    await fake.event("state_changed", {"entity_id": "light.nook", "old_state": old, "new_state": new})
    await asyncio.sleep(0.02)
    fake.calls.clear()
    await c.daylight_step()
    await asyncio.sleep(0.05)
    assert not any(x[1] == "turn_off" and x[2] == "light.nook" for x in fake.calls)
    # a floor-wide "goodnight" turn_off: nothing comes back on at dusk
    await fake.event("call_service", {"domain": "light", "service": "turn_off",
                                      "service_data": {"floor_id": ["main"]}},
                     context={"id": "gn", "user_id": "someone"})
    await asyncio.sleep(0.02)
    assert not c.daylight_off
    reader.cancel(); await ha.close()


@pytest.mark.asyncio
async def test_daylight_turns_off_unmanaged_light_but_does_not_restore_it(aiohttp_server):
    # light.island is "off" (not yet managed) before 07:30 in OPTS; lit by hand, it can go off in daylight
    clock = Clock(datetime.combine(date(2026, 12, 1), time(7, 10), TZ))
    opts = dict(options(), daylight=[{"name": "k", "sensor": "sensor.lux", "off_above": 250,
                                      "on_below": 160, "lights": ["light.island"]}])
    fake, c, ha, reader = await boot(aiohttp_server, opts, clock)
    fake.states["light.island"] = st("light.island", "on", brightness=100)
    c.states["light.island"] = fake.states["light.island"]
    await set_lux(fake, 400)
    clock.dt += timedelta(minutes=11)
    await c.daylight_step()
    await asyncio.sleep(0.05)
    assert ("light", "turn_off", "light.island", {"transition": 30}) in fake.calls
    old = fake.states["light.island"]
    new = dict(old, state="off", context={"id": "x", "user_id": ME})
    c.states["light.island"] = new
    fake.calls.clear()
    await set_lux(fake, 100)
    clock.dt += timedelta(minutes=4)
    await c.daylight_step()
    await asyncio.sleep(0.05)
    assert not any(x[1] == "turn_on" for x in fake.calls)
    reader.cancel(); await ha.close()


@pytest.mark.asyncio
async def test_evening_keyframe_trades_lights(aiohttp_server):
    # at sunset: chandelier off once, island (off) turned on; runs even if someone turned it off today
    clock = Clock(datetime.combine(date(2026, 12, 1), time(17, 0), TZ))
    opts = dict(options(), keyframes=OPTS + [{"name": "path", "at": "17:00", "wake": True,
                                              "lights": ["light.chand shutoff", "light.island 30 2400"]}])
    fake, c, ha, reader = await boot(aiohttp_server, opts, clock)
    fake.states["light.chand"] = st("light.chand", "on", brightness=200)
    c.states["light.chand"] = fake.states["light.chand"]
    c.ov.track("light.island").user_off_day = clock.dt.date()
    await c.wake_check()
    await asyncio.sleep(0.05)
    assert ("light", "turn_off", "light.chand", {"transition": 60}) in fake.calls
    assert any(x[1] == "turn_on" and x[2] == "light.island" for x in fake.calls)
    # once only: a person turning the chandelier back on is left alone
    fake.calls.clear()
    clock.dt += timedelta(minutes=5)
    await c.wake_check()
    await asyncio.sleep(0.05)
    assert not fake.calls
    reader.cancel(); await ha.close()


def boost_options():
    return dict(options(), boost=["light.cab", "light.nook", "light.chand", "light.island"],
                boost_factor=1.5)


async def set_boost(fake, on):
    e = "input_boolean.circadian_boost"
    old = fake.states.get(e) or st(e, "off")
    new = dict(old, state="on" if on else "off", context={"id": f"b-{on}", "user_id": "someone"})
    fake.states[e] = new
    await fake.event("state_changed", {"entity_id": e, "old_state": old, "new_state": new})
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_boost_rides_the_curve_and_turns_on_scheduled_lights(aiohttp_server):
    clock = Clock(datetime.combine(date(2026, 12, 1), time(12, 0), TZ))
    fake, c, ha, reader = await boot(aiohttp_server, boost_options(), clock)
    await set_boost(fake, True)
    by = {x[2]: x[3] for x in fake.calls if x[1] == "turn_on"}
    # noon: cab 85 -> capped 100, nook gliding 57.5 -> 86 with the curve's colour, chand (off) 35 -> 52.5 on
    assert by["light.cab"]["brightness_pct"] == 100
    assert by["light.nook"]["brightness_pct"] == 86
    assert by["light.nook"]["color_temp_kelvin"] == c.schedule.target_at("light.nook", c.now()).kelvin
    assert by["light.chand"]["brightness_pct"] in (52, 53)
    assert c.boost_lit == {"light.chand", "light.island"}
    # manual off: lights boost turned on go off again, the rest back to the curve
    fake.calls.clear()
    await set_boost(fake, False)
    offs = {x[2] for x in fake.calls if x[1] == "turn_off"}
    assert offs == {"light.chand", "light.island"}
    nook = [x for x in fake.calls if x[2] == "light.nook"][0][3]
    assert nook["brightness_pct"] in (57, 58)
    reader.cancel(); await ha.close()


@pytest.mark.asyncio
async def test_boost_eases_out_after_sunset_and_switches_off(aiohttp_server):
    clock = Clock(datetime.combine(date(2026, 12, 1), time(12, 0), TZ))
    fake, c, ha, reader = await boot(aiohttp_server, boost_options(), clock)
    await set_boost(fake, True)
    start, end = c.boost_window(c.now())
    assert start.hour == 16 and end.hour == 19      # sunset -> next keyframe (eve 19:00)
    clock.dt = start + (end - start) / 2
    assert abs(c.boost_mult(c.now()) - 1.25) < 0.01
    clock.dt = end + timedelta(minutes=1)
    fake.calls.clear()
    await c.tick()
    await asyncio.sleep(0.05)
    assert ("input_boolean", "turn_off", "input_boolean.circadian_boost", {}) in fake.calls
    await set_boost(fake, False)
    assert not [x for x in fake.calls if x[1] == "turn_off" and x[0] == "light"]   # eased out: nothing snaps
    reader.cancel(); await ha.close()


@pytest.mark.asyncio
async def test_day_report_shape(aiohttp_server):
    clock = Clock(datetime.combine(date(2026, 12, 1), time(12, 0), TZ))
    fake, c, ha, reader = await boot(aiohttp_server, boost_options(), clock)
    await set_boost(fake, True)
    r = c.day_report()
    import json as _j
    _j.dumps(r)
    assert len(r["lights"]["light.nook"]["curve"]) == 145
    assert r["boost"]["on"] and r["boost"]["mult_now"] == 1.5
    assert r["lights"]["light.nook"]["target"][0] == 86.2 or r["lights"]["light.nook"]["target"][0] > 80
    reader.cancel(); await ha.close()


@pytest.mark.asyncio
async def test_boost_eases_from_a_set_time_to_the_next_brightness_keyframe(aiohttp_server):
    from test_engine import SPLIT
    clock = Clock(datetime.combine(date(2026, 12, 1), time(12, 0), TZ))
    opts = dict(options(), keyframes=SPLIT, boost=["light.a"], boost_ease_from="18:30")
    fake, c, ha, reader = await boot(aiohttp_server, opts, clock)
    await set_boost(fake, True)
    s, e = c.boost_window(c.now())
    assert (s.hour, s.minute, e.hour, e.minute) == (18, 30, 21, 30)   # sunset's color keyframe doesn't end it
    reader.cancel(); await ha.close()


@pytest.mark.asyncio
async def test_boost_off_in_daylight_lets_dusk_bring_lights_back(aiohttp_server):
    # Bright House on and off at midday must not cost a daylight-dimmed light its return at dusk.
    clock = Clock(datetime.combine(date(2026, 12, 1), time(12, 0), TZ))
    opts = dict(boost_options(), daylight=[{"name": "kitchen", "sensor": "sensor.lux", "off_above": 250,
                                            "on_below": 160, "off_after_minutes": 10,
                                            "on_after_minutes": 3, "lights": ["light.island"]}])
    fake, c, ha, reader = await boot(aiohttp_server, opts, clock)
    await set_lux(fake, 400)
    clock.dt += timedelta(minutes=11)
    await c.daylight_step()                      # bright; the island is already off
    await set_boost(fake, True)
    assert "light.island" in c.boost_lit
    await set_boost(fake, False)
    assert ("light", "turn_off", "light.island", {"transition": 5}) in fake.calls
    assert "light.island" in c.daylight_off
    old = fake.states["light.island"]
    fake.states["light.island"] = dict(old, state="off", context={"id": "x", "user_id": ME})
    await fake.event("state_changed", {"entity_id": "light.island", "old_state": old,
                                       "new_state": fake.states["light.island"]})
    fake.calls.clear()
    await set_lux(fake, 100)
    clock.dt += timedelta(minutes=4)
    await c.daylight_step()
    await asyncio.sleep(0.05)
    assert any(x[1] == "turn_on" and x[2] == "light.island" for x in fake.calls)
    reader.cancel(); await ha.close()
