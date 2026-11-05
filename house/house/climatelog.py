"""A climate log for learning how the house heats and cools.

Every 5 minutes, one line per sample: each zone's temperature, setpoint, mode, what the HVAC is doing
(heating, cooling, idle) and the fan, with the outdoor temperature, humidity, cloud cover, wind, the
sun's height and direction, and every light (lux) and temperature sensor in the house. Home Assistant keeps detailed history for only 10 days; this keeps it for as long
as there's room, one file a month in the app's /data (so it's in every backup and never in the repo).

From it we'll learn, per level: how fast it warms with the heat on, cools with the AC on, drifts with
both off, and how much the sun adds; then predict the day ahead (see the guide's Climate section).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("house.climatelog")

EVERY = 300


def _num(v):
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def sample(by: dict, zones: list, mode_entity: str, nws: str, model: str) -> dict:
    """One row, from a snapshot of Home Assistant's states."""
    def attrs(e):
        return (by.get(e) or {}).get("attributes") or {}
    wn, wm, sun = attrs(nws), attrs(model), attrs("sun.sun")
    row = {
        "t": int(time.time()),
        "mode": (by.get(mode_entity) or {}).get("state"),
        "out": _num(wn.get("temperature", wm.get("temperature"))),
        "out_rh": _num(wn.get("humidity", wm.get("humidity"))),
        "cloud": _num(wm.get("cloud_coverage")),
        "wind": _num(wn.get("wind_speed", wm.get("wind_speed"))),
        "sun": _num(sun.get("elevation")),
        "az": _num(sun.get("azimuth")),
        "zones": {},
        # Every light and temperature sensor in the house, found by kind, so new ones (the FP300s)
        # join the log without anyone listing them: light outside and in, and temperatures at the
        # house itself (the Hue sensors outside read the house's own air, not the station's)
        "lux": {}, "temps": {},
    }
    for e, st in by.items():
        if not e.startswith("sensor."):
            continue
        dc = (st.get("attributes") or {}).get("device_class")
        v = _num(st.get("state"))
        if v is None:
            continue
        if dc == "illuminance":
            row["lux"][e[7:]] = v
        elif dc == "temperature" and not e.endswith(("_cpu_temperature", "_battery_temperature")):
            row["temps"][e[7:]] = v
    for z in zones:
        s = by.get(z["entity"]) or {}
        a = s.get("attributes") or {}
        row["zones"][z.get("name") or z["entity"]] = {
            "now": _num(a.get("current_temperature")), "set": _num(a.get("temperature")),
            "mode": s.get("state"), "act": a.get("hvac_action"), "fan": a.get("fan_mode"),
            "rh": _num(a.get("current_humidity")),
        }
    return row


class ClimateLog:
    def __init__(self, house, root: str | Path = "/data/climate"):
        self.house = house
        self.root = Path(root)
        self.task = None
        self.rows = 0

    def path(self, when: datetime | None = None) -> Path:
        return self.root / f"{(when or datetime.now()).strftime('%Y-%m')}.jsonl"

    def write(self, row: dict):
        self.root.mkdir(parents=True, exist_ok=True)
        with self.path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
        self.rows += 1

    def start(self):
        if self.house.climate and self.task is None:
            self.task = asyncio.create_task(self.loop())

    async def loop(self):
        await asyncio.sleep(30)
        while True:
            try:
                by = {s["entity_id"]: s for s in await self.house.all_states()}
                h = self.house
                self.write(sample(by, h.climate, h.climate_mode, h.opts.get("forecast", "weather.nws"),
                                  h.opts.get("weather", "weather.forecast_home")))
            except Exception as ex:
                log.warning("Climate log: %s", ex)
            await asyncio.sleep(EVERY)

    def recent(self, days: float = 1) -> list[dict]:
        cut, out = time.time() - days * 86400, []
        for p in sorted(self.root.glob("*.jsonl"))[-3:]:
            for line in p.read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("t", 0) >= cut:
                    out.append(r)
        return out
