"""House: a small portal served inside Home Assistant (ingress).

Reads Home Assistant through the Supervisor proxy and Circadian through its read-only API.
Only the actions listed in the app options can be run from the page.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
from aiohttp import web

from board import Board, observed
from live import Live

log = logging.getLogger("house")
WWW = Path(__file__).parent / "www"
HA = "http://supervisor/core/api"
VERSION = "0.15.0"


def load_options(path: str = "/data/options.json") -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except OSError:
        return {}


class House:
    def __init__(self, opts: dict):
        self.opts = opts
        self.token = os.environ.get("SUPERVISOR_TOKEN", "")
        self.circadian = opts.get("circadian_url", "http://3dd8a0cc-circadian:8810")
        self.shortcuts = opts.get("shortcuts", []) or []
        self.shades = opts.get("shades", []) or []
        self.motion = opts.get("motion", []) or []
        self.activity = opts.get("activity", []) or []
        self.exclude = set(opts.get("exclude", []) or [])
        self.switches = list(opts.get("switches", []) or [])
        self.floor_order = opts.get("floors", []) or []
        # Rooms, as people think of them: each control is one thing you switch, which may be
        # a group or a Hue-behind-Lutron wrapper in Home Assistant ("entity|Label").
        self.rooms = opts.get("rooms", []) or []
        self.device_ids: set[str] = set()
        self._cache: dict = {}
        self.live = Live()
        self._refreshing: set = set()
        self.session: aiohttp.ClientSession | None = None

    def hdr(self):
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    async def ha_get(self, path: str):
        async with self.session.get(f"{HA}{path}", headers=self.hdr(), timeout=aiohttp.ClientTimeout(total=10)) as r:
            r.raise_for_status()
            return await r.json()

    async def all_states(self) -> list[dict]:
        """Every entity, from the live picture when it's connected, otherwise straight from HA."""
        if self.live.ready:
            return self.live.snapshot()
        return await self.ha_get("/states")

    async def ha_call(self, domain: str, service: str, data: dict):
        async with self.session.post(f"{HA}/services/{domain}/{service}", headers=self.hdr(),
                                     data=json.dumps(data), timeout=aiohttp.ClientTimeout(total=10)) as r:
            r.raise_for_status()

    async def template(self, tpl: str):
        async with self.session.post(f"{HA}/template", headers=self.hdr(), data=json.dumps({"template": tpl}),
                                     timeout=aiohttp.ClientTimeout(total=10)) as r:
            r.raise_for_status()
            return await r.text()

    AREAS_TPL = ("{% set ns = namespace(o=[]) %}{% for f in floors() %}{% for a in floor_areas(f) %}"
                 "{% for e in area_entities(a) %}{% if e.startswith('light.') or e.startswith('switch.') %}"
                 "{% set ns.o = ns.o + [[f, floor_name(f), a, area_name(a), e]] %}{% endif %}"
                 "{% endfor %}{% endfor %}{% endfor %}{{ ns.o | tojson }}")

    @staticmethod
    def _item(e: str, label: str | None, st: dict) -> dict:
        a = st.get("attributes", {})
        modes = a.get("supported_color_modes") or []
        return {"entity": e, "name": label or a.get("friendly_name", e), "state": st.get("state"),
                "brightness": round(a["brightness"] / 2.55) if a.get("brightness") else None,
                "dimmable": e.startswith("light.") and any(m != "onoff" for m in modes),
                "kelvin": a.get("color_temp_kelvin"),
                "parts": len(a["entity_id"]) if isinstance(a.get("entity_id"), list) else 1}

    async def curated(self):
        states = await self.all_states()
        by = {s["entity_id"]: s for s in states}
        floors, ids = {}, set()
        for room in self.rooms:
            fid = room.get("floor", "")
            f = floors.setdefault(fid, {"id": fid, "name": fid, "areas": []})
            items = []
            for spec in room.get("controls", []):
                e, _, label = (x.strip() for x in spec.partition("|"))
                if e in by and e not in self.exclude:
                    items.append(self._item(e, label or None, by[e]))
                    ids.add(e)
            if items:
                f["areas"].append({"id": room["name"].lower(), "name": room["name"], "items": items})
        self.device_ids = ids
        order = {f: i for i, f in enumerate(self.floor_order)}
        return by, [f for f in sorted(floors.values(), key=lambda f: order.get(f["id"], 99)) if f["areas"]]

    def lights_on(self, by: dict) -> dict:
        """Lights on, counted the way a person switches them: one per control in the rooms list (a
        group or a Hue-behind-Lutron wrapper counts once), across the whole house. Every screen uses this."""
        seen, on = set(), 0
        for room in self.rooms:
            for spec in room.get("controls", []):
                e = spec.partition("|")[0].strip()
                if e in by and e not in self.exclude and e not in seen and e.startswith("light."):
                    seen.add(e)
                    on += by[e].get("state") == "on"
        return {"on": on, "of": len(seen)}

    def _covers(self, by):
        def cover(e):
            st = by.get(e) or {}
            a = st.get("attributes", {})
            return {"entity": e, "name": a.get("friendly_name", e), "state": st.get("state"),
                    "position": a.get("current_position")}
        return [cover(e) for e in self.shades if e in by]

    async def devices(self):
        if self.rooms:
            by, floors = await self.curated()
            return {"floors": floors, "shades": self._covers(by)}
        rows, states = await asyncio.gather(self.template(self.AREAS_TPL), self.all_states())
        by = {s["entity_id"]: s for s in states}
        floors: dict[str, dict] = {}
        ids = set()
        for fid, fname, aid, aname, e in json.loads(rows):
            st = by.get(e)
            if not st or e in self.exclude:
                continue
            a = st.get("attributes", {})
            if isinstance(a.get("entity_id"), list):
                continue          # light groups: the room already shows its lights
            if e.startswith("switch.") and e not in self.switches:
                continue          # most switches are settings, not things to switch
            modes = a.get("supported_color_modes") or []
            f = floors.setdefault(fid, {"id": fid, "name": fname, "areas": {}})
            area = f["areas"].setdefault(aid, {"id": aid, "name": aname, "items": []})
            area["items"].append({
                "entity": e, "name": a.get("friendly_name", e), "state": st["state"],
                "brightness": round(a["brightness"] / 2.55) if a.get("brightness") else None,
                "dimmable": any(m != "onoff" for m in modes) and e.startswith("light."),
                "kelvin": a.get("color_temp_kelvin")})
            ids.add(e)
        self.device_ids = ids
        order = {f: i for i, f in enumerate(self.floor_order)}
        out = []
        for f in sorted(floors.values(), key=lambda f: order.get(f["id"], 99)):
            areas = []
            for ar in sorted(f["areas"].values(), key=lambda x: x["name"]):
                ar["items"].sort(key=lambda x: x["name"])
                areas.append(ar)
            out.append({"id": f["id"], "name": f["name"], "areas": areas})

        def cover(e):
            st = by.get(e) or {}
            a = st.get("attributes", {})
            return {"entity": e, "name": a.get("friendly_name", e), "state": st.get("state"),
                    "position": a.get("current_position")}
        return {"floors": out, "shades": self._covers(by)}

    async def circadian_day(self):
        try:
            async with self.session.get(f"{self.circadian}/api/day", timeout=aiohttp.ClientTimeout(total=5)) as r:
                r.raise_for_status()
                return await r.json()
        except Exception as ex:
            log.warning("Circadian unreachable: %s", ex)
            return None

    async def logbook(self):
        start = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        try:
            rows = await self.ha_get(f"/logbook/{start}")
        except Exception as ex:
            log.warning("Logbook: %s", ex)
            return []
        want = set(self.activity) | set(self.motion) | set(self.shades) | {s["entity"] for s in self.shortcuts}
        out, last_motion, via = [], {}, {}
        for r in sorted(rows, key=lambda x: x.get("when", "")):
            e = r.get("entity_id")
            if e not in want:
                continue
            state = r.get("state")
            if state is None:
                # "Amazon Alexa sent command ..." / "started": note the source, keep the state row
                msg = (r.get("name") or "") + " " + (r.get("message") or "")
                if "Alexa" in msg and "ReportState" not in msg:
                    prev = next((o for o in reversed(out[-3:]) if o["entity"] == e), None)
                    if prev and abs((datetime.fromisoformat(r["when"]) -
                                     datetime.fromisoformat(prev["when"])).total_seconds()) < 3:
                        prev["via"] = "Alexa"      # logged just after the change it caused
                    else:
                        via[e] = "Alexa"
                continue
            if state in ("opening", "closing"):
                continue
            if e in self.motion:
                # one line per visit: an "on" more than 15 minutes after the last one
                if state not in ("on",):
                    continue
                t = datetime.fromisoformat(r["when"])
                prev = last_motion.get(e)
                last_motion[e] = t
                if prev and (t - prev) < timedelta(minutes=15):
                    continue
            elif e.startswith("script.") and state != "on":
                continue
            elif state in ("unavailable", "unknown"):
                continue
            out.append({"when": r.get("when"), "entity": e, "name": r.get("name"), "state": state,
                        "via": via.pop(e, None)})
        return out[-40:][::-1]

    async def cached(self, key: str, ttl: float, fn):
        """Serve the last answer while a fresh one is fetched in the background: pages open at once,
        and a slow source (the logbook, the TV) never holds up the rest."""
        import time
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        if hit:
            if key not in self._refreshing:
                self._refreshing.add(key)

                async def bg():
                    try:
                        self._cache[key] = (time.time(), await fn())
                    except Exception as ex:
                        log.info("Refreshing %s: %s", key, ex)
                    finally:
                        self._refreshing.discard(key)
                asyncio.create_task(bg())
            return hit[1]
        val = await fn()
        self._cache[key] = (time.time(), val)
        return val

    async def state(self):
        states, day, log_rows, cfg = await asyncio.gather(
            self.all_states(), self.circadian_day(),
            self.cached("logbook", 60, self.logbook), self.cached("config", 3600, lambda: self.ha_get("/config")))
        by = {s["entity_id"]: s for s in states}

        def pick(e):
            s = by.get(e)
            if not s:
                return None
            a = s.get("attributes", {})
            return {"entity": e, "state": s["state"], "name": a.get("friendly_name", e),
                    "changed": s.get("last_changed"), "position": a.get("current_position"),
                    "last_triggered": a.get("last_triggered")}

        for row in log_rows:
            if not row["name"]:
                row["name"] = by.get(row["entity"], {}).get("attributes", {}).get("friendly_name", row["entity"])
        sun = by.get("sun.sun", {}).get("attributes", {})
        wx = observed(by.get(self.opts.get("forecast", "weather.nws")),
                      by.get(self.opts.get("weather", "weather.forecast_home")))
        wa = wx.get("attributes", {})
        al = by.get(self.opts.get("allergy", "sensor.allergy_index_today"), {})
        pollen = None
        if al.get("state") not in (None, "unknown", "unavailable"):
            aa = al.get("attributes", {})
            pollen = {"index": al["state"], "rating": aa.get("rating"),
                      "top": [aa[k] for k in ("allergen_name_1", "allergen_name_2", "allergen_name_3") if aa.get(k)]}
        return {
            "now": datetime.now(timezone.utc).isoformat(),
            "weather": {"state": wx.get("state"), "temperature": wa.get("temperature"),
                        "unit": wa.get("temperature_unit"), "uv": wa.get("uv_index"),
                        "clouds": wa.get("cloud_coverage"), "humidity": wa.get("humidity"),
                        "wind": wa.get("wind_speed"), "wind_unit": wa.get("wind_speed_unit"),
                        "pollen": pollen} if wx else None,
            "sun": {"next_rising": sun.get("next_rising"), "next_setting": sun.get("next_setting"),
                    "elevation": sun.get("elevation"), "up": by.get("sun.sun", {}).get("state") == "above_horizon"},
            "circadian": day,
            "lights": self.lights_on(by),
            "home": {"lat": cfg.get("latitude"), "lon": cfg.get("longitude")},
            "shortcuts": [dict(pick(s["entity"]) or {}, **s) for s in self.shortcuts],
            "shades": [x for x in (pick(e) for e in self.shades) if x],
            "motion": [x for x in (pick(e) for e in self.motion) if x],
            "activity": log_rows,
        }

    async def _json(self, url: str, timeout: float = 4):
        try:
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                r.raise_for_status()
                return await r.json()
        except Exception as ex:
            log.info("Home: %s didn't answer (%s)", url, ex)
            return None

    async def home(self):
        """What the other apps are doing, for their tiles on Home."""
        music_url = self.opts.get("music_url", "http://3dd8a0cc-music:8097")
        frame_url = self.opts.get("frame_url", "http://172.30.32.1:8800").rstrip("/")
        music, frame = await asyncio.gather(self._json(f"{music_url}/api/now"), self._json(f"{frame_url}/api/status"))
        playing = []
        for r in (music or {}).get("rooms", []):
            if r.get("state") != "playing" or (r.get("group") and not r.get("leader")):
                continue
            playing.append({"rooms": [r["name"]] + [g for g in r.get("group", []) if g != r["name"]],
                            "title": r.get("title"), "artist": r.get("artist"), "art": r.get("art"),
                            "tv": r.get("tv"), "line_in": r.get("line_in")})
        out = {"music": {"ok": music is not None, "playing": playing}, "frame": None}
        if frame:
            out["frame"] = {"power": frame.get("power"), "current": frame.get("current"),
                            "board": bool((frame.get("board") or {}).get("showing"))}
        return out

    async def frame_thumb(self, cid: str) -> bytes | None:
        frame_url = self.opts.get("frame_url", "http://172.30.32.1:8800").rstrip("/")
        try:
            async with self.session.get(f"{frame_url}/thumb/{cid}", timeout=aiohttp.ClientTimeout(total=8)) as r:
                r.raise_for_status()
                return await r.read()
        except Exception:
            return None

    async def do(self, body: dict):
        entity, op = body.get("entity"), body.get("op")
        sc = {s["entity"]: s for s in self.shortcuts}
        domain = (entity or "").split(".")[0]
        if entity in sc:
            if domain in ("script", "scene"):
                await self.ha_call(domain, "turn_on", {"entity_id": entity})
            elif domain == "input_boolean" and op in ("on", "off"):
                await self.ha_call("input_boolean", f"turn_{op}", {"entity_id": entity})
            else:
                raise web.HTTPBadRequest(text="unsupported")
        elif entity in self.shades and op in ("open", "close", "stop"):
            await self.ha_call("cover", f"{op}_cover", {"entity_id": entity})
        elif domain in ("light", "switch") and entity not in self.exclude:
            if entity not in self.device_ids:
                await self.devices()
            if entity not in self.device_ids:
                raise web.HTTPForbidden(text="not a portal device")
            if op in ("on", "off"):
                await self.ha_call(domain, f"turn_{op}", {"entity_id": entity})
            elif op == "level" and domain == "light":
                pct = max(1, min(100, int(body.get("value", 50))))
                await self.ha_call("light", "turn_on", {"entity_id": entity, "brightness_pct": pct})
            else:
                raise web.HTTPBadRequest(text="unsupported")
        else:
            raise web.HTTPForbidden(text="not a portal action")


def make_app(house: House) -> web.Application:
    app = web.Application()
    board = Board(house, house.opts)

    async def on_start(_app):
        house.session = aiohttp.ClientSession()
        app["live"] = asyncio.create_task(house.live.run(house.session))
        app["board_loop"] = asyncio.create_task(board.loop())

    async def on_stop(_app):
        await house.session.close()

    async def index(_req):
        return web.FileResponse(WWW / "index.html", headers={"Cache-Control": "no-cache"})

    async def api_state(_req):
        try:
            return web.json_response(await house.state())
        except Exception as ex:
            log.exception("state failed")
            return web.json_response({"error": str(ex)}, status=502)

    async def api_devices(_req):
        try:
            return web.json_response(await house.devices())
        except Exception as ex:
            log.exception("devices failed")
            return web.json_response({"error": str(ex)}, status=502)

    async def api_do(req):
        await house.do(await req.json())
        return web.json_response({"ok": True})

    async def api_stream(req):
        """Tells open pages the moment the house changes (server-sent events), so they don't poll."""
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache",
                                           "X-Accel-Buffering": "no"})
        await resp.prepare(req)
        await resp.write(b": hello\n\n")
        try:
            while True:
                ev = house.live.changed
                try:
                    await asyncio.wait_for(ev.wait(), 25)
                except asyncio.TimeoutError:
                    await resp.write(b": ping\n\n")
                    continue
                await asyncio.sleep(1.2)          # let a burst of changes land together
                await resp.write(f"data: {house.live.version}\n\n".encode())
        except (ConnectionResetError, asyncio.CancelledError, RuntimeError):
            pass
        return resp

    async def api_home(_req):
        return web.json_response(await house.cached("home", 15, house.home))

    async def frame_thumb(req):
        cid = req.match_info["cid"]
        if not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", cid):
            raise web.HTTPBadRequest()
        data = await house.frame_thumb(cid)
        if data is None:
            raise web.HTTPNotFound()
        return web.Response(body=data, content_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})

    async def board_page(_req):
        return web.FileResponse(WWW / "board.html", headers={"Cache-Control": "no-cache"})

    async def api_board(_req):
        try:
            return web.json_response(await board.data())
        except Exception as ex:
            log.exception("board failed")
            return web.json_response({"error": str(ex)}, status=502)

    async def api_board_status(_req):
        return web.json_response({"enabled": board.enabled, "showing": board.showing,
                                  "staged_at": board.staged_at, "has_picture": board.has_picture,
                                  "window": board.in_window(datetime.now(board.tz)), "error": board.last_error})

    async def api_board_try(req):
        b = await req.json() if req.can_read_body else {}
        try:
            return web.json_response(await board.try_now(int(b.get("minutes", 3))))
        except Exception as ex:
            log.exception("board try failed")
            return web.json_response({"ok": False, "reason": str(ex)}, status=502)

    async def board_jpeg(_req):
        # what the Frame would get right now, for looking at
        return web.Response(body=await board.render(), content_type="image/jpeg")

    app.on_startup.append(on_start)
    app.on_cleanup.append(on_stop)
    app.router.add_get("/", index)
    app.router.add_get("/api/state", api_state)
    app.router.add_get("/api/devices", api_devices)
    app.router.add_post("/api/do", api_do)
    app.router.add_static("/shared", WWW / "shared")
    app.router.add_static("/wx", WWW / "wx")
    app.router.add_static("/sky", WWW / "sky")
    app.router.add_get("/board", board_page)
    app.router.add_get("/board.jpg", board_jpeg)
    app.router.add_get("/api/board", api_board)
    app.router.add_get("/api/home", api_home)
    app.router.add_get("/api/stream", api_stream)
    app.router.add_get("/frame/thumb/{cid}", frame_thumb)
    app.router.add_get("/api/board/status", api_board_status)
    app.router.add_post("/api/board/try", api_board_try)
    return app


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    opts = load_options()
    log.info("House %s starting", VERSION)
    web.run_app(make_app(House(opts)), host="0.0.0.0", port=8099, access_log=None)


if __name__ == "__main__":
    main()
