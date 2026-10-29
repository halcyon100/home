"""Circadian app: connects the engine to Home Assistant over its websocket API."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp
from astral import LocationInfo
from astral.sun import sun as astral_sun

from engine import (KEEP_OFF, OFF, ON, SHUT, Overrides, Schedule, daylight_zones_from_options,
                    keyframes_from_options, pct_to_255)

APP_VERSION = "0.3.2"
log = logging.getLogger("circadian")

WAKE_WINDOW = timedelta(minutes=15)   # a wake keyframe can fire up to this long after its time
MIN_STEP_PCT = 1.0                    # skip changes smaller than this
MIN_STEP_K = 25
CALL_MATCH_SECONDS = 6           # a command's effect can be reported this long after it


def load_options(path: str = "/data/options.json") -> dict:
    with open(path) as f:
        return json.load(f)


class HA:
    """Minimal Home Assistant websocket client."""

    def __init__(self, url: str, token: str):
        self.url, self.token = url, token
        self.ws = None
        self.session = None
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self.handlers = {}

    async def connect(self):
        self.session = aiohttp.ClientSession()
        self.ws = await self.session.ws_connect(self.url, heartbeat=30, max_msg_size=0)
        msg = await self.ws.receive_json()
        if msg.get("type") != "auth_required":
            raise RuntimeError(f"unexpected hello: {msg}")
        await self.ws.send_json({"type": "auth", "access_token": self.token})
        msg = await self.ws.receive_json()
        if msg.get("type") != "auth_ok":
            raise RuntimeError(f"auth failed: {msg.get('type')}")

    async def close(self):
        if self.ws is not None:
            await self.ws.close()
        if self.session is not None:
            await self.session.close()

    async def send(self, payload: dict, timeout: float = 15):
        self._id += 1
        payload = dict(payload, id=self._id)
        fut = asyncio.get_running_loop().create_future()
        self._pending[self._id] = fut
        await self.ws.send_json(payload)
        return await asyncio.wait_for(fut, timeout)

    async def subscribe(self, event_type: str, handler):
        self._id += 1
        sub_id = self._id
        self.handlers[sub_id] = handler
        fut = asyncio.get_running_loop().create_future()
        self._pending[sub_id] = fut
        await self.ws.send_json({"id": sub_id, "type": "subscribe_events", "event_type": event_type})
        await asyncio.wait_for(fut, 15)

    async def reader(self):
        async for msg in self.ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            data = json.loads(msg.data)
            items = data if isinstance(data, list) else [data]
            for m in items:
                if m.get("type") == "result":
                    fut = self._pending.pop(m.get("id"), None)
                    if fut and not fut.done():
                        if m.get("success"):
                            fut.set_result(m.get("result"))
                        else:
                            fut.set_exception(RuntimeError(str(m.get("error"))))
                elif m.get("type") == "event":
                    h = self.handlers.get(m.get("id"))
                    if h:
                        try:
                            h(m["event"])
                        except Exception:
                            log.exception("event handler failed")
        raise ConnectionError("websocket closed")


class Circadian:
    def __init__(self, opts: dict, ha: HA):
        self.opts = opts
        self.ha = ha
        self.shadow = bool(opts.get("shadow", True))
        self.interval = int(opts.get("interval_seconds", 120))
        self.enable_entity = opts.get("enable_entity") or "input_boolean.circadian"
        self.morning_reset = opts.get("morning_reset", "07:00")
        self.keyframes = keyframes_from_options(opts.get("keyframes", []))
        self.states: dict[str, dict] = {}
        self.calls: dict[str, dict] = {}     # context id -> {entity: call record}
        self.ov = Overrides()
        self.tz = None
        self.schedule: Schedule | None = None
        self.last_reset_day = None
        self.my_user = None
        self.early_wake = None      # (start, shift, catch-up end)
        self.recent_call_by_entity: dict[str, dict] = {}
        self.state_path = os.environ.get("CIRCADIAN_STATE", "/data/circadian_state.json")
        self._saved = None
        self.force_wake = False
        self.catch_up = opts.get("early_wake_catch_up", "12:00")
        # Lutron dims in visible 1% steps, so some lights only move while nobody's there.
        self.quiet = {}
        for spec in opts.get("quiet_when_occupied", []) or []:
            light, sensor = spec.split()
            self.quiet[light] = sensor
        # Daylight: lights a zone's sensor says aren't needed go off, and come back when it darkens.
        self.zones = daylight_zones_from_options(opts.get("daylight"))
        self.zone_of = {e: z for z in self.zones for e in z.lights}
        self.daylight_off: set[str] = set()          # lights we turned off for daylight
        self.daylight_keep: dict[str, object] = {}   # light -> day someone turned it on in daylight

    # -------------------------------------------------- setup
    async def start(self):
        cfg = await self.ha.send({"type": "get_config"})
        self.tz = ZoneInfo(cfg.get("time_zone") or "America/Chicago")
        loc = LocationInfo("home", "", cfg.get("time_zone"), cfg["latitude"], cfg["longitude"])

        def sunfn(day):
            s = astral_sun(loc.observer, date=day, tzinfo=self.tz)
            return s["sunrise"], s["sunset"]

        self.schedule = Schedule(self.keyframes, sunfn, self.tz)
        try:
            me = await self.ha.send({"type": "auth/current_user"})
            self.my_user = (me or {}).get("id")
        except Exception:
            self.my_user = None
        for st in await self.ha.send({"type": "get_states"}):
            self.states[st["entity_id"]] = st
        self.ov.grace_until = datetime.now(self.tz) + timedelta(seconds=self.interval + 60)
        for z in self.zones:
            z.update((self.states.get(z.sensor) or {}).get("state"), datetime.now(self.tz))
            if z.sensor not in self.states:
                log.warning("Daylight zone %s: sensor %s not found", z.name, z.sensor)
        await self.ha.subscribe("state_changed", self.on_state_changed)
        await self.ha.subscribe("call_service", self.on_call_service)
        await self.ha.subscribe("circadian_wake", self.on_wake_event)
        await self.ha.subscribe("circadian_reset", self.on_reset_event)
        managed = sorted(self.schedule.entities())
        log.info("Circadian %s started: %d keyframes, %d entities, interval %ss, %s",
                 APP_VERSION, len(self.keyframes), len(managed), self.interval,
                 "SHADOW MODE (no lights touched)" if self.shadow else "LIVE")
        missing = [e for e in managed if e not in self.states]
        if missing:
            log.warning("Unknown entities in keyframes: %s", ", ".join(missing))
        for z in self.zones:
            log.info("Daylight zone %s: %s, off above %g lx, on below %g lx, %d lights",
                     z.name, z.sensor, z.off_above, z.on_below, len(z.lights))
        today = datetime.now(self.tz).date()
        for t, k in self.schedule.wake_events(today):
            log.info("Today's wake: %s at %s", k.name, t.strftime("%H:%M"))

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def vnow(self) -> datetime:
        """Schedule time. After an early wake, the morning is stretched so it starts now
        and catches back up to the real clock by `catch_up` (default noon)."""
        now = self.now()
        ew = self.early_wake
        if not ew:
            return now
        t0, shift, end = ew
        if now >= end or now < t0:
            self.early_wake = None if now >= end else self.early_wake
            return now
        return now + shift * ((end - now) / (end - t0))

    def on_reset_event(self, ev):
        """'Natural light': release every hold and put every lit light back on the schedule."""
        self.ov.release_all()
        self.daylight_keep.clear()
        log.info("Reset requested: all holds released, back to the schedule")
        asyncio.get_running_loop().create_task(self.reset_all())

    async def reset_all(self):
        if not self.enabled():
            return
        now = self.vnow()
        for e in sorted(self.schedule.entities()):
            if e.split(".")[0] != "light" or (self.states.get(e) or {}).get("state") != "on":
                continue
            tgt = self.schedule.target_at(e, now)
            try:
                if tgt is not None and tgt.kind == KEEP_OFF:
                    log.info("%s: off (kept off at this time)", e)
                    await self.call("light", "turn_off", e, {"transition": 3})
                else:
                    await self.apply(e, transition=3, why="reset")
            except Exception as ex:
                log.warning("%s: reset failed: %s", e, ex)

    def on_wake_event(self, ev):
        now = self.now()
        h, m = (int(x) for x in self.morning_reset.split(":"))
        seven = now.replace(hour=h, minute=m, second=0, microsecond=0)
        self.ov.release_all()
        if now < seven:
            ch, cm = (int(x) for x in self.catch_up.split(":"))
            end = now.replace(hour=ch, minute=cm, second=0, microsecond=0)
            self.early_wake = (now, seven - now, end)
            log.info("Early wake: the morning starts now (%s early), catching up by %s",
                     str(seven - now).split(".")[0], self.catch_up)
        else:
            log.info("Wake requested after %s: running the morning wake now", self.morning_reset)
        self.force_wake = True

    def enabled(self) -> bool:
        st = self.states.get(self.enable_entity)
        return st is None or st.get("state") != "off"

    # -------------------------------------------------- events
    def _expand(self, ents) -> set[str]:
        out, stack = set(), list(ents)
        while stack:
            e = stack.pop()
            if e in out:
                continue
            out.add(e)
            members = (self.states.get(e) or {}).get("attributes", {}).get("entity_id")
            if isinstance(members, list):
                stack.extend(members)
        return out

    def _from_me(self, context: dict | None) -> bool:
        return bool(self.my_user) and (context or {}).get("user_id") == self.my_user

    def on_call_service(self, ev):
        d = ev.get("data", {})
        if (d.get("domain") == "light" and d.get("service") == "turn_off" and self.daylight_off
                and not self._from_me(ev.get("context"))):
            self._daylight_forget(d.get("service_data") or {})
        if d.get("domain") != "light" or d.get("service") not in ("turn_on", "toggle"):
            return
        sd = dict(d.get("service_data") or {})
        ents = sd.pop("entity_id", None)
        if ents is None:
            return
        ents = [ents] if isinstance(ents, str) else list(ents)
        level_keys = {"brightness", "brightness_pct", "brightness_step", "brightness_step_pct",
                      "color_temp_kelvin", "color_temp", "kelvin", "rgb_color", "xy_color",
                      "hs_color", "rgbw_color", "rgbww_color", "color_name", "profile", "white",
                      "effect", "flash"}
        bare = not (level_keys & set(sd))
        ctx = ev.get("context", {}).get("id")
        if not ctx or self.ov.ours(ctx) or self._from_me(ev.get("context")):
            return
        expanded = self._expand(ents)
        per = self.calls.setdefault(ctx, {})
        if set(ents) <= set(per):
            # A light group forwarding to its members reuses the same context. The
            # members were reached through the group and already have its record.
            return
        if any(r["group"] for r in per.values()):
            # Forwarded members we could not expand up front: they belong to the
            # group's call, not a new direct request.
            first = next(iter(per.values()))
            first["entities"] |= expanded
            for e in expanded:
                per.setdefault(e, first)
                self.recent_call_by_entity[e] = first
            return
        # One automation or script run makes several calls under one context, so
        # each call keeps its own record for the lights it targets.
        rec = {"bare": bare, "entities": expanded, "direct": set(ents), "at": self.now(),
               "transition": sd.get("transition"),
               "group": any(len(self._expand([x])) > 1 for x in ents)}
        expanded = expanded - set(per)
        for e in expanded:
            per[e] = rec
            self.recent_call_by_entity[e] = rec
        if len(self.calls) > 500:
            age = lambda c: min((r["at"] for r in self.calls[c].values()), default=self.now())
            for c in sorted(self.calls, key=age)[:250]:
                del self.calls[c]
        # A plain "on" to a managed light that is already on gives it back to the curve.
        if bare and self.schedule:
            for e in expanded & self.schedule.entities():
                if (self.states.get(e) or {}).get("state") == "on":
                    t = self.ov.track(e)
                    if t.held:
                        t.held, t.held_reason = False, ""
                        log.info("%s: plain 'on' while on, back to the curve", e)
                    asyncio.get_running_loop().create_task(self.apply(e, transition=2, why="plain on"))

    def _daylight_forget(self, sd: dict):
        """Someone turned lights off: those we turned off for daylight stay off."""
        ents = sd.get("entity_id")
        if ents is None:
            # an area or floor command ("goodnight"): forget them all
            if self.daylight_off:
                log.info("Lights turned off by area or floor: %d daylight-off light(s) stay off",
                         len(self.daylight_off))
            self.daylight_off.clear()
            return
        ents = [ents] if isinstance(ents, str) else list(ents)
        for e in self._expand(ents) & self.daylight_off:
            self.daylight_off.discard(e)
            log.info("%s: turned off by someone, stays off when daylight fades", e)

    def on_state_changed(self, ev):
        d = ev.get("data", {})
        e = d.get("entity_id")
        new = d.get("new_state")
        old = d.get("old_state")
        if new is None:
            self.states.pop(e, None)
            return
        self.states[e] = new
        for z in self.zones:
            if z.sensor == e:
                z.update(new.get("state"), self.now())
        z = self.zone_of.get(e)
        if (z is not None and new.get("state") == "on" and (old or {}).get("state") != "on"
                and not self._from_me(new.get("context"))):
            self.daylight_off.discard(e)
            if z.bright:
                self.daylight_keep[e] = self.now().date()
                log.info("%s: turned on in daylight, leaving it on today", e)
        if not self.schedule or e not in self.schedule.entities():
            return
        ctx = (new.get("context") or {}).get("id")
        call = None
        if e in self.calls.get(ctx, {}):
            call = self.calls[ctx][e]
        else:
            # Some integrations (Lutron) report the result of a command with a fresh
            # context, so match a command aimed at this light in the last few seconds.
            rc = self.recent_call_by_entity.get(e)
            if rc and (self.now() - rc["at"]).total_seconds() <= CALL_MATCH_SECONDS:
                call = rc
        action = self.ov.on_state(e, (old or {}).get("state"), new.get("state"),
                                  new.get("attributes", {}), ctx, self.now(), call,
                                  ours=self._from_me(new.get("context")))
        if action == "held":
            log.info("%s: held (%s)", e, self.ov.track(e).held_reason)
        elif action == "snap":
            tgt = self.schedule.target_at(e, self.vnow())
            if (tgt is not None and tgt.kind == KEEP_OFF and call is not None
                    and e not in call.get("direct", set())):
                log.info("%s: kept off (turned on with the whole room)", e)
                asyncio.get_running_loop().create_task(self.call("light", "turn_off", e, {"transition": 1}))
                return
            tr = (call or {}).get("transition")
            tr = float(tr) if isinstance(tr, (int, float)) and tr > 0 else 1
            asyncio.get_running_loop().create_task(self.apply(e, transition=tr, why="turned on"))

    # -------------------------------------------------- commands
    async def call(self, domain: str, service: str, entity: str, data: dict | None = None):
        if self.shadow:
            log.info("[shadow] %s.%s %s %s", domain, service, entity, data or {})
            return None
        res = await self.ha.send({"type": "call_service", "domain": domain, "service": service,
                                  "target": {"entity_id": entity}, "service_data": data or {}})
        return ((res or {}).get("context") or {}).get("id")

    async def apply(self, e: str, transition: float, why: str):
        """Put one light on its curve. Never turns a light on."""
        if not self.enabled():
            return
        st = self.states.get(e) or {}
        if st.get("state") != "on":
            return
        t = self.ov.track(e)
        if t.held:
            return
        tgt = self.schedule.target_at(e, self.vnow())
        if tgt is None or tgt.kind != "level":
            return
        attrs = st.get("attributes", {})
        cur_b = attrs.get("brightness")
        cur_k = attrs.get("color_temp_kelvin")
        data = {"brightness_pct": max(1, min(100, round(tgt.brightness))), "transition": transition}
        k = None
        if tgt.kelvin and "color_temp" in (attrs.get("supported_color_modes") or []):
            lo = attrs.get("min_color_temp_kelvin") or 2000
            hi = attrs.get("max_color_temp_kelvin") or 6500
            k = max(lo, min(hi, tgt.kelvin))
            data["color_temp_kelvin"] = k
        if why == "tick" and cur_b is not None:
            same_b = abs(cur_b - pct_to_255(tgt.brightness)) < pct_to_255(MIN_STEP_PCT)
            same_k = k is None or (cur_k is not None and abs(cur_k - k) < MIN_STEP_K)
            if same_b and same_k:
                return
        # re-check right before sending (lesson: never turn a light back on)
        if (self.states.get(e) or {}).get("state") != "on":
            return
        self.ov.note_command(e, None, self.now(), cur_b, tgt.brightness, k, transition, cur_k)
        ctx = await self.call("light", "turn_on", e, data)
        if ctx:
            self.ov.our_contexts[ctx] = self.now()
        if why != "tick":
            log.info("%s -> %s%%%s (%s)", e, data["brightness_pct"],
                     f" {k}K" if k else "", why)

    async def tick(self):
        if not self.enabled():
            return
        for e in sorted(self.schedule.entities()):
            if e.split(".")[0] != "light":
                continue
            sensor = self.quiet.get(e)
            if sensor and (self.states.get(sensor) or {}).get("state") == "on":
                continue  # someone's there: hold still, catch up once they leave
            try:
                await self.apply(e, transition=self.interval, why="tick")
            except Exception as ex:
                log.warning("%s: update failed: %s", e, ex)
        await self.daylight_step()

    async def daylight_step(self):
        if not self.zones or not self.enabled():
            return
        now = self.now()
        today = now.date()
        for z in self.zones:
            was = z.bright
            bright = z.evaluate(now)
            if bright != was:
                log.info("Daylight %s: %s (%s lx)", z.name, "enough, lights off" if bright else
                         "fading, lights back on", "?" if z.lux is None else round(z.lux))
            for e in sorted(z.lights):
                st = (self.states.get(e) or {}).get("state")
                tgt = self.schedule.target_at(e, self.vnow())
                if bright:
                    # level: on the curve; OFF: lit but unmanaged now (e.g. balcony filaments
                    # after the morning) - it can go off, but only comes back if it has a level
                    if (st != "on" or e in self.daylight_off or self.daylight_keep.get(e) == today
                            or self.ov.track(e).held or tgt is None or tgt.kind not in ("level", OFF)):
                        continue
                    self.daylight_off.add(e)
                    log.info("%s: off, daylight is enough (%s %g lx)", e, z.name, z.lux)
                    try:
                        await self.call("light", "turn_off", e, {"transition": 30})
                    except Exception as ex:
                        log.warning("%s: daylight off failed: %s", e, ex)
                elif e in self.daylight_off:
                    self.daylight_off.discard(e)
                    if st != "off" or tgt is None or tgt.kind != "level":
                        continue
                    try:
                        await self._turn_on_at(e, tgt, 30, now)
                        log.info("%s: back on, daylight faded (%s)", e, z.name)
                    except Exception as ex:
                        log.warning("%s: daylight on failed: %s", e, ex)

    async def _turn_on_at(self, e, tgt, transition, now):
        data = {"brightness_pct": max(1, min(100, round(tgt.brightness))), "transition": transition}
        attrs = (self.states.get(e) or {}).get("attributes", {})
        if tgt.kelvin and "color_temp" in (attrs.get("supported_color_modes") or []):
            lo = attrs.get("min_color_temp_kelvin") or 2000
            hi = attrs.get("max_color_temp_kelvin") or 6500
            data["color_temp_kelvin"] = max(lo, min(hi, tgt.kelvin))
        self.ov.note_command(e, None, now, 0, tgt.brightness, data.get("color_temp_kelvin"), transition)
        ctx = await self.call("light", "turn_on", e, data)
        if ctx:
            self.ov.our_contexts[ctx] = now

    async def wake_check(self):
        if not self.enabled():
            return
        now = self.vnow()
        today = now.date()
        events = self.schedule.wake_events(today)
        force = self.force_wake
        if force:
            self.force_wake = False
            if not events:  # light mornings: the sun gate skipped them, but you asked
                events = [(t, k) for t, k in self.schedule.wake_events(today, ignore_gate=True)]
        for t, k in events:
            due = t <= now < t + WAKE_WINDOW
            if force and not due and t <= now:
                due = True   # asked to wake: bring on everything up to now
            if not due:
                continue
            for e, tgt in k.lights.items():
                if tgt.kind in (OFF, KEEP_OFF):
                    continue
                tr = self.ov.track(e)
                key = (today, k.name)
                # A morning wake respects someone having turned the light off today; an
                # evening keyframe (no sunrise gate) runs every day regardless.
                morning = k.wake_if_sunrise_after is not None
                if key in tr.woken or (morning and tr.user_off_day == today and not force):
                    continue
                st = (self.states.get(e) or {}).get("state")
                tr.woken.add(key)
                if tgt.kind == SHUT:
                    if st == "on" and not tr.held:
                        log.info("%s %s: off", k.name, e)
                        try:
                            await self.call("light", "turn_off", e, {"transition": 60})
                        except Exception as ex:
                            log.warning("%s %s: off failed: %s", k.name, e, ex)
                    continue
                if st == "on":
                    continue
                z = self.zone_of.get(e)
                if z is not None and z.evaluate(self.now()) and tgt.kind == "level" and not tgt.via:
                    self.daylight_off.add(e)   # comes on when the daylight fades
                    log.info("Wake %s: %s waits, daylight is enough", k.name, e)
                    continue
                try:
                    if tgt.kind == ON or tgt.via:
                        target = tgt.via or e
                        dom = target.split(".")[0]
                        log.info("Wake %s: turning on %s", k.name, target)
                        ctx = await self.call(dom, "turn_on", target, None)
                    else:
                        data = {"brightness_pct": max(1, min(100, round(tgt.brightness))), "transition": 3}
                        attrs = (self.states.get(e) or {}).get("attributes", {})
                        if tgt.kelvin and "color_temp" in (attrs.get("supported_color_modes") or []):
                            lo = attrs.get("min_color_temp_kelvin") or 2000
                            hi = attrs.get("max_color_temp_kelvin") or 6500
                            data["color_temp_kelvin"] = max(lo, min(hi, tgt.kelvin))
                        log.info("Wake %s: %s -> %s%%", k.name, e, data["brightness_pct"])
                        self.ov.note_command(e, None, now, 0, tgt.brightness,
                                             data.get("color_temp_kelvin"), 3)
                        ctx = await self.call("light", "turn_on", e, data)
                    if ctx:
                        self.ov.our_contexts[ctx] = now
                except Exception as ex:
                    log.warning("Wake %s: %s failed: %s", k.name, e, ex)

    def reset_check(self):
        now = self.now()
        h, m = (int(x) for x in self.morning_reset.split(":"))
        if self.last_reset_day != now.date() and (now.hour, now.minute) >= (h, m):
            if self.last_reset_day is not None:
                self.ov.release_all()
                log.info("Morning reset: all holds released")
            self.last_reset_day = now.date()

    # -------------------------------------------------- state across restarts
    def _last_reset(self, now: datetime) -> datetime:
        h, m = (int(x) for x in self.morning_reset.split(":"))
        r = now.replace(hour=h, minute=m, second=0, microsecond=0)
        return r if now >= r else r - timedelta(days=1)

    def snapshot(self) -> dict:
        tr = self.ov.tracks
        return {
            "held": {e: t.held_reason for e, t in tr.items() if t.held},
            "woken": {e: [[d.isoformat(), n] for d, n in sorted(t.woken)] for e, t in tr.items() if t.woken},
            "user_off": {e: t.user_off_day.isoformat() for e, t in tr.items() if t.user_off_day},
            "daylight_off": sorted(self.daylight_off),
            "daylight_keep": {e: d.isoformat() for e, d in self.daylight_keep.items()},
        }

    def save_state(self):
        snap = self.snapshot()
        if snap == self._saved:
            return
        try:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(dict(snap, saved=self.now().isoformat()), f)
            os.replace(tmp, self.state_path)
            self._saved = snap
        except OSError as ex:
            log.warning("Could not save state: %s", ex)

    def load_state(self):
        try:
            with open(self.state_path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        now = self.now()
        try:
            saved = datetime.fromisoformat(data["saved"])
        except (KeyError, ValueError):
            return
        today = now.date()
        if saved >= self._last_reset(now):
            for e, reason in data.get("held", {}).items():
                t = self.ov.track(e)
                t.held, t.held_reason = True, reason
        for e, items in data.get("woken", {}).items():
            for d, n in items:
                if d == today.isoformat():
                    self.ov.track(e).woken.add((today, n))
        for e, d in data.get("user_off", {}).items():
            if d == today.isoformat():
                self.ov.track(e).user_off_day = today
        if saved >= self._last_reset(now):
            self.daylight_off = {e for e in data.get("daylight_off", []) if e in self.zone_of}
        for e, d in data.get("daylight_keep", {}).items():
            if d == today.isoformat():
                self.daylight_keep[e] = today
        n = sum(1 for t in self.ov.tracks.values() if t.held)
        if n:
            log.info("Restored %d held light(s) from before the restart", n)
        self._saved = self.snapshot()

    async def run(self):
        await self.start()
        self.load_state()
        last_tick = None
        while True:
            self.reset_check()
            await self.wake_check()
            now = self.now()
            if last_tick is None or (now - last_tick).total_seconds() >= self.interval:
                last_tick = now
                await self.tick()
            self.save_state()
            await asyncio.sleep(15)


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    opts = load_options(os.environ.get("CIRCADIAN_OPTIONS", "/data/options.json"))
    url = os.environ.get("HA_WS", "ws://supervisor/core/websocket")
    token = os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HA_TOKEN", "")
    backoff = 5
    while True:
        ha = HA(url, token)
        try:
            await ha.connect()
            app = Circadian(opts, ha)
            reader = asyncio.create_task(ha.reader())
            runner = asyncio.create_task(app.run())
            done, pending = await asyncio.wait({reader, runner}, return_when=asyncio.FIRST_EXCEPTION)
            for t in pending:
                t.cancel()
            for t in done:
                t.result()
        except Exception as ex:
            log.warning("Disconnected (%s); retrying in %ss", ex, backoff)
        finally:
            await ha.close()
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 120)


if __name__ == "__main__":
    asyncio.run(main())
