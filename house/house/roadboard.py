"""The roads board: our usual routes, now. Pure functions over what Roads gathered from 511 and the
Waze trip sensors, so it can be tested without either.

Rules it keeps:
- A route is one leg unless part of it is slow (no splitting for its own sake).
- Every closure says when: the next time it happens, in plain words ("Tonight, 9 pm – 6 am").
- Calm days stay calm: the headline only leads with a problem when there is one.
"""
from __future__ import annotations

import re
from datetime import datetime, time, timedelta

# 511 travel-time segments that make up our routes, matched by their description
SEGMENTS = {
    "41": "I-41/US 45 SB County Q to Zoo Interchange",
    "94w": "I-94 EB WIS 16 to Zoo Interchange",
    "94e_full": "I-94 EB WIS 16 to Downtown",          # minus 94w gives the Zoo to downtown
    "894": "I-41 SB Burleigh St to Airport via I-94",
}
TRIPS = [("Downtown", "sensor.waze_drive_to_downtown"),
         ("Airport", "sensor.waze_to_mke"),
         ("The Falls", "sensor.waze_drive_to_the_falls")]
ROUTE_ROADS = {"I-41", "WI-164", "I-94", "I-894", "I-794"}
# Where each leg's camera should look, and which roads count as on it
LEG_CAMS = {"41": ("I-41", (43.107, -88.080)), "94w": ("I-94", (43.037, -88.140)),
            "94e": ("I-94", (43.034, -87.975)), "894": ("894", (42.990, -87.990))}
WEST_OF_164 = -88.21            # 94 west of 164 isn't on our way (Oconomowoc and beyond)
DEFAULT_CAMERA = "991"          # 41 at County Q: our on-ramp
DAYS = {"M": 0, "T": 1, "Tu": 1, "W": 2, "Th": 3, "F": 4, "Sat": 5, "Sa": 5, "Sun": 6, "Su": 6}
DIRS = {"S": "south", "N": "north", "E": "east", "W": "west"}

_SCHED = re.compile(r"Daily from (\d\d/\d\d/\d{4}) to (\d\d/\d\d/\d{4}), (\d\d:\d\d [AP]M) - (\d\d:\d\d [AP]M)")


def clock(t: datetime | time) -> str:
    if t.hour == 12 and t.minute == 0:
        return "noon"
    if t.hour == 0 and t.minute == 0:
        return "midnight"
    h, ap = t.hour % 12 or 12, "am" if t.hour < 12 else "pm"
    return f"{h}:{t.minute:02d}\u00a0{ap}" if t.minute else f"{h}\u00a0{ap}"


def schedule(desc: str):
    """(first day, last day, start time, end time) from 511's 'Daily from … to …, 09:00 PM - 06:00 AM'."""
    m = _SCHED.search(desc or "")
    if not m:
        return None
    d0, d1 = (datetime.strptime(x, "%m/%d/%Y").date() for x in m.group(1, 2))
    t0, t1 = (datetime.strptime(x, "%I:%M %p").time() for x in m.group(3, 4))
    return d0, d1, t0, t1


def windows(sched, tz) -> list[tuple[datetime, datetime]]:
    d0, d1, t0, t1 = sched
    overnight = t1 <= t0
    last = d1 - timedelta(days=1) if overnight else d1
    out, d = [], d0
    while d <= last:
        a = datetime.combine(d, t0, tz)
        b = datetime.combine(d + timedelta(days=1) if overnight else d, t1, tz)
        out.append((a, b))
        d += timedelta(days=1)
    return out


def next_window(sched, now: datetime):
    for a, b in windows(sched, now.tzinfo):
        if b > now:
            return a, b
    return None


def when(a: datetime, b: datetime, now: datetime) -> str:
    span = f"{clock(a)} – {clock(b)}"
    if a <= now < b:
        return f"Now, until {clock(b)}"
    night = a.hour >= 17
    days = (a.date() - now.date()).days
    if days == 0:
        return f"{'Tonight' if night else 'Today'}, {span}"
    if days == 1:
        return f"Tomorrow {'night' if night else ''}, {span}".replace(" ,", ",")
    return f"{a.strftime('%A')}{' night' if night else ''}, {span}"


def _name(s: str) -> str:
    s = re.sub(r"\s*\(BRIDGE CROSSING\)", "", s.strip())
    s = re.sub(r"^(OFF RAMP TO|ON RAMP FROM)\s+", "", s)
    s = re.sub(r"^WIS \d+ [NSEW]B-", "", s)                   # "WIS 100 SB-SILVER SPRING" is Silver Spring
    s = s.replace("AVENUE", "AVE").replace("'", "")
    s = re.sub(r"\b(RD|ROAD)\b\.?", "", s, flags=re.I).strip()
    s = s.replace("COUNTY ", "County ").replace("WIS ", "WIS ")
    fixed = {"ST": "St", "AVE": "Ave", "DR": "Dr", "S": "S", "N": "N", "E": "E", "W": "W"}
    words = []
    for w in s.split():
        if w in fixed:
            words.append(fixed[w])
        elif any(c.isdigit() for c in w):
            words.append(re.sub(r"(\d+)(ST|ND|RD|TH)\b", lambda m: m.group(1) + m.group(2).lower(), w))
        elif w.isupper() and len(w) <= 3:
            words.append(w)                      # PP, WIS
        else:
            words.append(w.capitalize())
    return " ".join(words)


def what(desc: str) -> tuple[str, str] | None:
    """('41 south', 'Good Hope exit') from a 511 description; None if it isn't a closure we can say plainly."""
    m = re.search(r"on (I-\d+|WIS \d+) (NB|SB|EB|WB) at (OFF|ON) RAMP (?:TO|FROM) ([^.]+?)\.", desc or "")
    if m:
        road = m.group(1).replace("WIS ", "").replace("I-", "")
        return f"{road} {DIRS[m.group(2)[0]]}", f"{_name(m.group(4))} {'exit' if m.group(3) == 'OFF' else 'entrance'}"
    m = re.search(r"Mainline (.+?) Closed on (I-\d+|WIS \d+) (NB|SB|EB|WB) from (.+?) to ([^.]+?)(?:\.|$)", desc or "")
    if m:
        road = m.group(2).replace("WIS ", "").replace("I-", "")
        lanes = m.group(1).lower().replace("various lanes", "lanes")
        frm, to = _name(m.group(4)), _name(re.sub(r"\s+(NB|SB|EB|WB)$", "", m.group(5)))
        return f"{road} {DIRS[m.group(3)[0]]}", f"{lanes} closed, {frm} to {to}"
    return None


def closures(events: list, now: datetime, horizon_days: int = 4, limit: int = 3) -> list[dict]:
    """Closures on our routes, the next time each happens, grouped when they share a window."""
    groups: dict = {}
    for e in events:
        road = (e.get("road") or "").strip()
        if road not in ROUTE_ROADS or "shoulder" in (e.get("what") or "").lower():
            continue
        if road == "I-94" and e.get("lon") is not None and e["lon"] < WEST_OF_164:
            continue
        w = what(e.get("what", ""))
        if not w:
            continue
        sched = schedule(e.get("what", ""))
        if sched:
            nw = next_window(sched, now)
            if not nw or (nw[0] - now).days >= horizon_days:
                continue
            key, label, start = (w[0], nw), when(*nw, now), nw[0]
        else:
            ends = e.get("ends")
            end = datetime.fromtimestamp(ends, now.tzinfo) if ends else None
            if end and end < now:
                continue
            key = (w[0], "ongoing", end.date() if end else None)
            label = f"Until {end.strftime('%b')} {end.day}" if end else "Until further notice"
            start = now + timedelta(days=horizon_days)          # standing closures go after the dated ones
        g = groups.setdefault(key, {"when": label, "road": w[0], "items": [], "start": start})
        if w[1] not in g["items"]:
            g["items"].append(w[1])
    out = sorted(groups.values(), key=lambda g: g["start"])
    for g in out:
        items = g["items"]
        ramps = [i for i in items if i.endswith(("exit", "entrance"))]
        lanes = [i for i in items if i not in ramps]
        parts = []
        if ramps:
            parts.append((ramps[0] if len(ramps) == 1 else ", ".join(ramps[:-1]) + " and " + ramps[-1]) + " closed")
        parts += lanes
        g["text"] = f"{g['road']}: " + "; ".join(parts)
        g["now"] = g["when"].startswith("Now")
        del g["start"]
    return out[:limit]


def segment(times: list, match: str) -> dict | None:
    for t in times:
        if match.lower() in (t.get("what") or "").lower() and t.get("now") is not None:
            return t
    return None


def status(now_min, normal) -> str:
    if now_min is None or not normal:
        return "unknown"
    r = now_min / normal
    return "bad" if r >= 1.5 else "slow" if r >= 1.2 else "ok"


def legs(times: list) -> dict:
    out = {}
    for k in ("41", "94w", "894"):
        s = segment(times, SEGMENTS[k])
        out[k] = {"now": s and round(s["now"]), "normal": s and round(s["normal"] or 0),
                  "status": status(s and s["now"], s and s["normal"]),
                  "mid": s and s.get("mid")}
    full, w = segment(times, SEGMENTS["94e_full"]), segment(times, SEGMENTS["94w"])
    if full and w and full.get("normal") and w.get("normal"):
        n, u = full["now"] - w["now"], full["normal"] - w["normal"]
        out["94e"] = {"now": round(n), "normal": round(u), "status": status(n, u), "mid": full.get("mid")}
    else:
        out["94e"] = {"now": None, "normal": None, "status": "unknown", "mid": None}
    return out


LEG_NAME = {"41": ("41 south", "the Zoo"), "94w": ("94 east", "the Zoo"), "94e": ("94 east", "downtown"),
            "894": ("894", "the airport")}


def headline(lg: dict, cl: list, trips: list) -> tuple[str, str | None]:
    worst = max(lg.items(), key=lambda kv: (kv[1]["status"] == "bad", kv[1]["status"] == "slow",
                                             (kv[1]["now"] or 0) - (kv[1]["normal"] or 0)))
    k, v = worst
    if v["status"] in ("bad", "slow"):
        road, to = LEG_NAME[k]
        return (f"{road[0].upper()}{road[1:]} is {'slow' if v['status'] == 'slow' else 'backed up'}: "
                f"{v['now']} min to {to}, usually {v['normal']}.", k)
    now_cl = [c for c in cl if c["now"]]
    if now_cl:
        return f"Clear, but {now_cl[0]['text'][0].lower()}{now_cl[0]['text'][1:]} right now.", None
    return "Clear to downtown and the airport.", None


def nearest_camera(cameras: list, lat: float, lon: float) -> dict | None:
    best = None
    for c in cameras:
        d = (c["lat"] - lat) ** 2 + (c["lon"] - lon) ** 2
        if best is None or d < best[0]:
            best = (d, c)
    return best and best[1]


def pick_camera(cameras: list, lg: dict, worst_key: str | None) -> dict | None:
    def live(c):
        return c and any((v.get("status") or "").lower() == "enabled" for v in c["views"])
    if worst_key in LEG_CAMS:
        road, (lat, lon) = LEG_CAMS[worst_key]
        on_leg = [c for c in cameras if road in (c.get("road") or "") and live(c)]
        c = nearest_camera(on_leg, lat, lon)
        if c:
            return c
    for c in cameras:
        if any(str(v["id"]) == DEFAULT_CAMERA for v in c["views"]):
            return c
    return next((c for c in cameras if live(c)), None)


def short_route(r: str | None) -> str | None:
    """Waze's 'County Line Rd Lisbon Twp (Waukesha); Appleton Ave Menomonee Falls' is 'Appleton Ave'."""
    if not r:
        return None
    last = r.split(";")[-1].strip()
    m = re.match(r"(.*?\b(?:Rd|Ave|St|Dr|Blvd|Pkwy|Way|Hwy|[NSEW]))\b", last)
    return (m.group(1) if m else last).strip()


def build(report: dict, states: dict, now: datetime) -> dict:
    times, events, cameras = report.get("times") or [], report.get("events") or [], report.get("cameras") or []
    lg = legs(times)
    cl = closures(events, now)
    trips = []
    for name, ent in TRIPS:
        st = states.get(ent) or {}
        try:
            m = round(float(st.get("state")))
        except (TypeError, ValueError):
            m = None
        trips.append({"name": name, "min": m, "route": short_route((st.get("attributes") or {}).get("route"))})
    say, worst = headline(lg, cl, trips)
    cam = pick_camera(cameras, lg, worst)
    view = cam and next((v for v in cam["views"] if (v.get("status") or "").lower() == "enabled"), None)
    return {"asof": clock(now), "say": say, "trips": trips, "legs": lg, "closures": cl,
            "camera": {"view": view["id"], "where": cam["where"]} if view else None,
            "error": report.get("error")}
