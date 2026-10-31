"""Music: every room's music at a glance, the turntable, and a weekly shelf of new records.

Served inside Home Assistant (ingress). Rooms come from the app options; each names its
Music Assistant speaker (what plays Spotify), its Sonos entity (grouping, line-in, TV audio)
and, for the two home theaters, the Apple TV. Only those entities can be touched from here.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import aiohttp
from aiohttp import web

from listen import Listen

log = logging.getLogger("music")
WWW = Path(__file__).parent / "www"
HA = "http://supervisor/core/api"
VERSION = "0.2.1"


def load_options(path: str = "/data/options.json") -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except OSError:
        return {}


class Music:
    def __init__(self, opts: dict):
        import os
        self.opts = opts
        self.token = os.environ.get("SUPERVISOR_TOKEN", "")
        self.zones = [z for z in opts.get("zones", []) if z.get("speaker") and z.get("sonos")]
        self.by_name = {z["name"]: z for z in self.zones}
        self.turntable = opts.get("turntable") or ""
        self.session: aiohttp.ClientSession | None = None

    # --------------------------------------------------- Home Assistant
    def hdr(self):
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    async def ha_call(self, domain: str, service: str, data: dict):
        async with self.session.post(f"{HA}/services/{domain}/{service}", headers=self.hdr(),
                                     data=json.dumps(data), timeout=aiohttp.ClientTimeout(total=30)) as r:
            r.raise_for_status()

    async def template(self, tpl: str):
        async with self.session.post(f"{HA}/template", headers=self.hdr(), data=json.dumps({"template": tpl}),
                                     timeout=aiohttp.ClientTimeout(total=10)) as r:
            r.raise_for_status()
            return await r.text()

    async def states(self) -> dict:
        async with self.session.get(f"{HA}/states", headers=self.hdr(), timeout=aiohttp.ClientTimeout(total=10)) as r:
            r.raise_for_status()
            return {s["entity_id"]: s for s in await r.json()}

    # --------------------------------------------------- the rooms, now
    async def now(self) -> dict:
        st = await self.states()
        sonos_to_zone = {z["sonos"]: z["name"] for z in self.zones}
        rooms = []
        for z in self.zones:
            sp, so = st.get(z["speaker"], {}), st.get(z["sonos"], {})
            spa, soa = sp.get("attributes", {}), so.get("attributes", {})
            tv = st.get(z.get("tv") or "", {})
            on_tv = soa.get("source") == "TV" or spa.get("source") == "TV"
            line_in = soa.get("source") == "Line-in" or so.get("attributes", {}).get("media_title") == "Line-in"
            # what's playing: Music Assistant knows Spotify best; Sonos knows TV and line-in
            meta = spa if sp.get("state") == "playing" else soa
            members = [sonos_to_zone.get(m) for m in soa.get("group_members", []) if m in sonos_to_zone]
            if self.turntable and self.turntable in soa.get("group_members", []):
                members.append("Turntable")
            state = sp.get("state") if sp.get("state") == "playing" else so.get("state", "unavailable")
            pic = meta.get("entity_picture_local") or meta.get("entity_picture")
            rooms.append({
                "name": z["name"], "floor": z.get("floor", ""), "state": state,
                "title": meta.get("media_title"), "artist": meta.get("media_artist"),
                "album": meta.get("media_album_name"),
                "art": pic if pic and pic.startswith("/api/") else None,
                "volume": soa.get("volume_level", spa.get("volume_level")),
                "muted": bool(soa.get("is_volume_muted")),
                "tv": bool(on_tv), "tv_on": tv.get("state") not in (None, "off", "standby", "unavailable"),
                "line_in": bool(line_in),
                "group": [m for m in members if m],
                "leader": bool(soa.get("group_members")) and soa.get("group_members", [None])[0] == z["sonos"],
            })
        tt = st.get(self.turntable, {}) if self.turntable else {}
        return {"rooms": rooms, "turntable": {"available": bool(tt) and tt.get("state") != "unavailable",
                                              "playing": tt.get("state") == "playing"}}

    # --------------------------------------------------- doing things
    async def act(self, b: dict):
        op = b.get("op")
        if op == "turntable":
            return await self.play_turntable(b.get("rooms") or [])
        z = self.by_name.get(b.get("room"))
        if z is None:
            raise web.HTTPForbidden(text="not a music room")
        so = z["sonos"]
        if op in ("play", "pause", "play_pause", "next", "previous", "stop"):
            svc = {"play": "media_play", "pause": "media_pause", "play_pause": "media_play_pause",
                   "next": "media_next_track", "previous": "media_previous_track", "stop": "media_stop"}[op]
            await self.ha_call("media_player", svc, {"entity_id": so})
        elif op == "volume":
            v = max(0.0, min(1.0, float(b.get("value", 0))))
            await self.ha_call("media_player", "volume_set", {"entity_id": so, "volume_level": v})
        elif op == "join":
            to = self.by_name.get(b.get("to"))
            if to is None:
                raise web.HTTPForbidden(text="not a music room")
            await self.ha_call("media_player", "join", {"entity_id": to["sonos"], "group_members": [so]})
        elif op == "leave":
            await self.ha_call("media_player", "unjoin", {"entity_id": so})
        else:
            raise web.HTTPForbidden(text="not a music action")

    async def play_turntable(self, rooms: list[str]):
        if not self.turntable:
            raise web.HTTPForbidden(text="no turntable configured")
        targets = [self.by_name[r]["sonos"] for r in rooms if r in self.by_name]
        if not targets:
            raise web.HTTPBadRequest(text="no rooms")
        await self.ha_call("media_player", "select_source", {"entity_id": self.turntable, "source": "Line-in"})
        await self.ha_call("media_player", "join", {"entity_id": self.turntable, "group_members": targets})
        await self.ha_call("media_player", "media_play", {"entity_id": self.turntable})

    def floors_of(self, place: str) -> list[str]:
        return [z["name"] for z in self.zones if z.get("floor") == place]


def make_app(m: Music) -> web.Application:
    app = web.Application()
    listen = Listen(m)

    async def on_start(_app):
        m.session = aiohttp.ClientSession()
        app["listen_loop"] = asyncio.create_task(listen.loop())

    async def on_stop(_app):
        await m.session.close()

    async def index(_req):
        return web.FileResponse(WWW / "index.html", headers={"Cache-Control": "no-cache"})

    async def api_now(_req):
        try:
            return web.json_response(await m.now())
        except Exception as ex:
            log.exception("now failed")
            return web.json_response({"error": str(ex)}, status=502)

    async def api_act(req):
        b = await req.json()
        if b.get("op") == "turntable" and b.get("place"):
            b["rooms"] = m.floors_of(b["place"])
        await m.act(b)
        return web.json_response({"ok": True})

    async def api_listen(_req):
        return web.json_response(listen.view())

    async def api_listen_feedback(req):
        b = await req.json()
        try:
            listen.feedback(str(b.get("id")), b.get("verdict"))
        except (KeyError, ValueError):
            raise web.HTTPBadRequest(text="unknown pick or verdict")
        return web.json_response(listen.view())

    async def api_listen_play(req):
        b = await req.json()
        try:
            await listen.play(str(b.get("id")), str(b.get("place", "main")))
        except (KeyError, ValueError):
            raise web.HTTPBadRequest(text="unknown pick or place")
        return web.json_response({"ok": True})

    async def api_listen_refresh(_req):
        if not listen.busy:
            asyncio.create_task(listen.refresh())
        await asyncio.sleep(0.1)
        return web.json_response(listen.view())

    async def api_listen_import(req):
        # one-time move of an existing shelf (picks, feedback, history) from another app
        b = await req.json()
        if listen.data.get("picks"):
            raise web.HTTPConflict(text="already has a shelf")
        for k in ("picks", "feedback", "history", "generated", "season"):
            if k in b:
                listen.data[k] = b[k]
        listen.save()
        return web.json_response(listen.view())

    app.on_startup.append(on_start)
    app.on_cleanup.append(on_stop)
    app.router.add_get("/", index)
    app.router.add_get("/api/now", api_now)
    app.router.add_post("/api/act", api_act)
    app.router.add_get("/api/listen", api_listen)
    app.router.add_post("/api/listen/feedback", api_listen_feedback)
    app.router.add_post("/api/listen/play", api_listen_play)
    app.router.add_post("/api/listen/refresh", api_listen_refresh)
    app.router.add_post("/api/listen/import", api_listen_import)
    app.router.add_static("/shared", WWW / "shared")
    return app


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    log.info("Music %s starting", VERSION)
    web.run_app(make_app(Music(load_options())), host="0.0.0.0", port=8097, access_log=None)


if __name__ == "__main__":
    main()
