"""House: a small portal served inside Home Assistant (ingress).

Reads Home Assistant through the Supervisor proxy and Circadian through its read-only API.
Only the actions listed in the app options can be run from the page.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
from aiohttp import web

log = logging.getLogger("house")
WWW = Path(__file__).parent / "www"
HA = "http://supervisor/core/api"
VERSION = "0.1.0"


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
        self.session: aiohttp.ClientSession | None = None

    def hdr(self):
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    async def ha_get(self, path: str):
        async with self.session.get(f"{HA}{path}", headers=self.hdr(), timeout=aiohttp.ClientTimeout(total=10)) as r:
            r.raise_for_status()
            return await r.json()

    async def ha_call(self, domain: str, service: str, data: dict):
        async with self.session.post(f"{HA}/services/{domain}/{service}", headers=self.hdr(),
                                     data=json.dumps(data), timeout=aiohttp.ClientTimeout(total=10)) as r:
            r.raise_for_status()

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
        out, last_motion = [], {}
        for r in sorted(rows, key=lambda x: x.get("when", "")):
            e = r.get("entity_id")
            if e not in want:
                continue
            state = r.get("state")
            if e in self.motion:
                # one line per visit: an "on" more than 15 minutes after the last one
                if state not in ("on",):
                    continue
                t = datetime.fromisoformat(r["when"])
                prev = last_motion.get(e)
                last_motion[e] = t
                if prev and (t - prev) < timedelta(minutes=15):
                    continue
            elif e.startswith("script.") and state not in (None, "on"):
                continue
            elif state in ("unavailable", "unknown"):
                continue
            out.append({"when": r.get("when"), "entity": e, "name": r.get("name"), "state": state,
                        "message": r.get("message"),
                        "by": (r.get("context_user_id") is not None) or r.get("context_event_type") == "call_service"})
        return out[-40:][::-1]

    async def state(self):
        states, day, log_rows = await asyncio.gather(self.ha_get("/states"), self.circadian_day(), self.logbook())
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
        wx = by.get(self.opts.get("weather", "weather.forecast_home"), {})
        return {
            "now": datetime.now(timezone.utc).isoformat(),
            "weather": {"state": wx.get("state"), "temperature": wx.get("attributes", {}).get("temperature"),
                        "unit": wx.get("attributes", {}).get("temperature_unit")} if wx else None,
            "sun": {"next_rising": sun.get("next_rising"), "next_setting": sun.get("next_setting"),
                    "elevation": sun.get("elevation"), "up": by.get("sun.sun", {}).get("state") == "above_horizon"},
            "circadian": day,
            "shortcuts": [dict(pick(s["entity"]) or {}, **s) for s in self.shortcuts],
            "shades": [x for x in (pick(e) for e in self.shades) if x],
            "motion": [x for x in (pick(e) for e in self.motion) if x],
            "activity": log_rows,
        }

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
        else:
            raise web.HTTPForbidden(text="not a portal action")


def make_app(house: House) -> web.Application:
    app = web.Application()

    async def on_start(_app):
        house.session = aiohttp.ClientSession()

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

    async def api_do(req):
        await house.do(await req.json())
        return web.json_response({"ok": True})

    app.on_startup.append(on_start)
    app.on_cleanup.append(on_stop)
    app.router.add_get("/", index)
    app.router.add_get("/api/state", api_state)
    app.router.add_post("/api/do", api_do)
    app.router.add_static("/fonts", WWW / "fonts")
    return app


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    opts = load_options()
    log.info("House %s starting", VERSION)
    web.run_app(make_app(House(opts)), host="0.0.0.0", port=8099, access_log=None)


if __name__ == "__main__":
    main()
