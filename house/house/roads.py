"""Roads near home, from 511 Wisconsin: incidents and closures, and highway travel times.

The developer key lives in the House app's options (a password field); it's never logged, never
sent anywhere but 511wi.gov, and never returned by the API. 511 allows 10 calls a minute; this makes
two every five minutes.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time

import aiohttp

log = logging.getLogger("house.roads")

BASE = "https://511wi.gov/api/v2/get"
EVERY = 300             # seconds between refreshes
NEAR_MILES = 25         # incidents this close to home are worth knowing about


def miles(lat1, lon1, lat2, lon2) -> float:
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def nearby_events(events: list, home: tuple[float, float], radius: float = NEAR_MILES) -> list[dict]:
    out = []
    for e in events or []:
        lat, lon = _f(e.get("Latitude")), _f(e.get("Longitude"))
        if lat is None or lon is None:
            continue
        d = miles(home[0], home[1], lat, lon)
        if d > radius:
            continue
        out.append({"road": e.get("RoadwayName") or "", "direction": e.get("DirectionOfTravel") or "",
                    "type": e.get("EventType") or "", "subtype": e.get("EventSubType") or "",
                    "what": (e.get("Description") or "").strip(), "closed": bool(e.get("IsFullClosure")),
                    "severity": e.get("Severity") or "", "lanes": e.get("LanesAffected") or "",
                    "miles": round(d, 1), "updated": e.get("LastUpdated"), "ends": e.get("PlannedEndDate"),
                    "county": e.get("County") or "", "lat": lat, "lon": lon})
    # Closures and crashes first, then the closest
    rank = {"accidentsAndIncidents": 0, "closures": 1, "roadwork": 2}
    out.sort(key=lambda x: (not x["closed"], rank.get(x["type"], 3), x["miles"]))
    return out


def nearby_cameras(rows: list, home: tuple[float, float], radius: float = NEAR_MILES) -> list[dict]:
    out = []
    for c in rows or []:
        lat, lon = _f(c.get("Latitude")), _f(c.get("Longitude"))
        if lat is None or lon is None:
            continue
        d = miles(home[0], home[1], lat, lon)
        if d > radius:
            continue
        views = [{"id": v.get("Id"), "url": v.get("Url"), "status": v.get("Status"), "what": v.get("Description"),
                  "video": v.get("VideoUrl")} for v in (c.get("Views") or [])]
        out.append({"id": c.get("Id"), "road": (c.get("Roadway") or "").strip(), "direction": c.get("Direction") or "",
                    "where": c.get("Location") or "", "lat": lat, "lon": lon, "miles": round(d, 1), "views": views})
    out.sort(key=lambda x: x["miles"])
    return out


def nearby_times(rows: list, home: tuple[float, float], radius: float = NEAR_MILES) -> list[dict]:
    out = []
    for t in rows or []:
        pts = [(_f(t.get("StartLatitude")), _f(t.get("StartLongitude"))),
               (_f(t.get("EndLatitude")), _f(t.get("EndLongitude")))]
        ds = [miles(home[0], home[1], a, b) for a, b in pts if a is not None and b is not None]
        if not ds or min(ds) > radius:
            continue
        now, normal = _f(t.get("CurrentTime")), _f(t.get("NormalTime"))
        out.append({"road": t.get("RoadwayName") or "", "what": t.get("Description") or "",
                    "now": now, "normal": normal, "delay": _f(t.get("Delay")),
                    "length": _f(t.get("Distance")), "miles": round(min(ds), 1),
                    "mid": [sum(a for a, _ in pts if a is not None) / 2, sum(b for _, b in pts if b is not None) / 2]
                    if all(a is not None and b is not None for a, b in pts) else None})
    out.sort(key=lambda x: (-(x["delay"] or 0), x["miles"]))
    return out


class Roads:
    def __init__(self, house, key: str | None):
        self.house = house
        self.key = (key or "").strip()
        self.events: list = []
        self.times: list = []
        self.cameras: list = []
        self.cameras_at = 0.0
        self.updated = 0.0
        self.error: str | None = None
        self.task = None

    @property
    def configured(self) -> bool:
        return bool(self.key)

    def start(self):
        if self.configured and self.task is None:
            self.task = asyncio.create_task(self.loop())

    async def _get(self, what: str):
        # The key travels in the query string, so errors are reported without the URL
        try:
            async with self.house.session.get(f"{BASE}/{what}", params={"key": self.key, "format": "json"},
                                              timeout=aiohttp.ClientTimeout(total=20)) as r:
                if r.status in (401, 403):
                    raise RuntimeError("511 didn't accept the key")
                if r.status == 429:
                    raise RuntimeError("511 asked us to slow down")
                if r.status != 200:
                    raise RuntimeError(f"511 answered {r.status}")
                return await r.json(content_type=None)
        except aiohttp.ClientError as ex:
            raise RuntimeError(f"couldn't reach 511 ({type(ex).__name__})") from None

    async def home(self) -> tuple[float, float] | None:
        cfg = await self.house.ha_get("/config")
        lat, lon = _f(cfg.get("latitude")), _f(cfg.get("longitude"))
        return (lat, lon) if lat is not None and lon is not None else None

    async def refresh(self):
        here = await self.home()
        if here is None:
            self.error = "Home Assistant has no home location"
            return
        ev, tt = await self._get("event"), await self._get("traveltimes")
        self.events, self.times = nearby_events(ev, here), nearby_times(tt, here)
        if time.time() - self.cameras_at > 3600:          # cameras rarely move: once an hour
            self.cameras, self.cameras_at = nearby_cameras(await self._get("cameras"), here), time.time()
        self.updated, self.error = time.time(), None
        log.info("Roads: %d incidents within %d miles, %d travel times", len(self.events), NEAR_MILES, len(self.times))

    async def loop(self):
        while True:
            try:
                await self.refresh()
            except Exception as ex:
                self.error = str(ex)
                log.warning("Roads: %s", ex)
            await asyncio.sleep(EVERY)

    def view_url(self, view_id) -> str | None:
        for c in self.cameras:
            for v in c["views"]:
                if str(v["id"]) == str(view_id):
                    return v["url"]
        return None

    async def snapshot(self, view_id) -> tuple[bytes, str] | None:
        """One camera's current picture, fetched by the box (511's own URL; no key needed for images)."""
        url = self.view_url(view_id)
        if not url or not url.startswith("https://511wi.gov/"):
            return None
        async with self.house.session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                return None
            return await r.read(), r.content_type or "application/octet-stream"

    def report(self) -> dict:
        return {"configured": self.configured, "updated": self.updated or None, "error": self.error,
                "near_miles": NEAR_MILES, "events": self.events, "times": self.times,
                "cameras": self.cameras}
