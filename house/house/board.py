"""The kitchen board: a still for the Frame on weekday-and-weekend mornings.

When someone walks into the kitchen in the morning, the Frame shows the weather now, what's notable
today and when, the next three days, and a line about the house. The art comes back a few minutes
after the kitchen empties.

`build()` turns Home Assistant's data into what the page draws; it's pure, so it's tested on its own.
`Board` renders the page with headless Chromium, stages the picture on the Frame ahead of time, and
watches the kitchen sensor.
"""
from __future__ import annotations

import asyncio
import html
import io
import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp

log = logging.getLogger("house.board")
SKY_DIR = Path(__file__).parent / "www" / "sky"

# Home Assistant's weather conditions, as the board's skies and as words
SKY_OF = {"sunny": "clear", "clear-night": "clear", "partlycloudy": "partly", "cloudy": "overcast",
          "rainy": "rain", "pouring": "rain", "lightning": "storm", "lightning-rainy": "storm", "hail": "storm",
          "snowy": "snow", "snowy-rainy": "snow", "fog": "fog", "windy": "partly", "windy-variant": "overcast",
          "exceptional": "overcast"}
WORD = {"sunny": "Clear and bright", "clear-night": "Clear", "partlycloudy": "Sun and cloud", "cloudy": "Gray",
        "rainy": "Wet", "pouring": "Pouring", "lightning": "Stormy", "lightning-rainy": "Stormy", "hail": "Hail",
        "snowy": "Snowy", "snowy-rainy": "Sleet", "fog": "Foggy", "windy": "Windy", "windy-variant": "Gray and windy",
        "exceptional": "Unsettled"}
DAY_WORD = {"sunny": "Sunny", "partlycloudy": "Partly sunny", "cloudy": "Cloudy", "fog": "Foggy",
            "windy": "Windy", "windy-variant": "Cloudy and windy", "snowy": "Snow", "snowy-rainy": "Sleet"}
ICON = {"sunny": "clear-day", "clear-night": "clear-day", "partlycloudy": "partly-cloudy-day", "cloudy": "overcast",
        "rainy": "rain", "pouring": "extreme-rain", "lightning": "thunderstorms-day",
        "lightning-rainy": "thunderstorms-rain", "hail": "hail", "snowy": "snow", "snowy-rainy": "sleet",
        "fog": "fog", "windy": "wind", "windy-variant": "wind", "exceptional": "overcast"}
WET = 35        # a chance of rain worth mentioning, in percent (NWS calls an hour "rainy" from about 10%)


def clock(dt: datetime, exact: bool = False) -> str:
    """7 am, 12:30 pm, noon, midnight (lowercase: they sit mid-sentence; labels capitalize)"""
    if dt.hour == 12 and dt.minute == 0:
        return "noon"
    if dt.hour == 0 and dt.minute == 0:
        return "midnight"
    h = dt.hour % 12 or 12
    ap = "am" if dt.hour < 12 else "pm"
    return f"{h}:{dt.minute:02d} {ap}" if exact and dt.minute else f"{h} {ap}"


def windows(rows: list, test, gap: int = 1) -> list[tuple[int, int]]:
    """Runs of consecutive rows passing test, bridging gaps of up to `gap` rows. (first, last) indexes."""
    out, start, last = [], None, None
    for i, r in enumerate(rows):
        if test(r):
            if start is None:
                start = i
            elif i - last > gap + 1:
                out.append((start, last))
                start = i
            last = i
    if start is not None:
        out.append((start, last))
    return out


def _hours(hourly: list, tz: ZoneInfo) -> list[dict]:
    out = []
    for h in hourly or []:
        try:
            out.append(dict(h, dt=datetime.fromisoformat(h["datetime"]).astimezone(tz)))
        except (KeyError, ValueError, TypeError):
            continue
    return out


def _rain_phrase(rows: list, now_hour: datetime | None = None) -> tuple[str, str, int] | None:
    """('Showers on and off', '7 am – 5 pm, likeliest around 4', 49) for the longest wet stretch."""
    runs = windows(rows, lambda r: (r.get("precipitation_probability") or 0) >= WET
                   and r.get("condition") not in ("snowy",))
    if not runs:
        return None
    a, b = max(runs, key=lambda w: (w[1] - w[0], max(rows[i].get("precipitation_probability") or 0
                                                      for i in range(w[0], w[1] + 1))))
    seg = rows[a:b + 1]
    peak = max(seg, key=lambda r: r.get("precipitation_probability") or 0)
    pmax = peak.get("precipitation_probability") or 0
    start, end = seg[0]["dt"], seg[-1]["dt"] + timedelta(hours=1)
    hours = b - a + 1
    if any(r.get("condition") in ("lightning", "lightning-rainy") for r in seg):
        what = "Storms possible"
    elif pmax >= 60:
        what = "Rain likely"
    elif hours >= 3:
        what = "Showers on and off"
    else:
        what = "A chance of showers"
    if now_hour is not None and start <= now_hour:
        when = f"until {clock(end)}"
    else:
        when = f"{clock(start)} – {clock(end)}"
    if hours >= 3:
        when += f", likeliest around {clock(peak['dt']).replace(' am', '').replace(' pm', '')}"
    return what, when, pmax


def _dst_note(now: datetime, tz: ZoneInfo) -> dict | None:
    """Clocks change within the next day and a half."""
    t, off = now, now.utcoffset()
    for _ in range(36):
        t2 = (t + timedelta(hours=1)).astimezone(tz)
        if t2.utcoffset() != off:
            back = t2.utcoffset() < off
            ahead = (t2.date() - now.date()).days
            if t2.hour < 12 and ahead <= 1:
                when_day = "tonight"
            elif t2.hour < 12 and ahead == 2:
                when_day = "tomorrow night"
            else:
                when_day = t2.strftime("%A")
            return {"what": f"Clocks {'fall back' if back else 'spring forward'} {when_day}",
                    "when": "2 am becomes 1 am" if back else "2 am becomes 3 am"}
        t = t2
    return None


NIGHT_WORD = {"sunny": "Clear", "clear-night": "Clear", "partlycloudy": "Partly cloudy", "cloudy": "Overcast",
              "rainy": "Wet", "pouring": "Pouring", "lightning": "Stormy", "lightning-rainy": "Stormy", "hail": "Hail",
              "snowy": "Snowy", "snowy-rainy": "Sleet", "fog": "Foggy", "windy": "Windy", "windy-variant": "Windy",
              "exceptional": "Unsettled"}


def moon(now: datetime) -> dict:
    """Moon phase from the mean synodic month, counted from the new moon of 6 Jan 2000 18:14 UTC."""
    import math
    syn = 29.530588853
    age = ((now.timestamp() - datetime(2000, 1, 6, 18, 14, tzinfo=ZoneInfo("UTC")).timestamp()) / 86400) % syn
    f = age / syn
    lit = (1 - math.cos(2 * math.pi * f)) / 2
    if age < 1 or age > syn - 1:
        name = "New moon"
    elif abs(age - syn / 2) < 1:
        name = "Full moon"
    elif abs(age - syn / 4) < 1:
        name = "First quarter"
    elif abs(age - 3 * syn / 4) < 1:
        name = "Last quarter"
    else:
        name = ("Waxing crescent", "Waxing gibbous", "Waning gibbous", "Waning crescent")[min(3, int(f * 4))]
    return {"name": name, "pct": round(lit * 100), "f": round(f, 4)}


def sun_elevation(when: datetime, lat: float, lon: float) -> float:
    """Solar elevation in degrees (NOAA's approximation; good to a fraction of a degree)."""
    import math
    rad = math.pi / 180
    jd = when.timestamp() / 86400 + 2440587.5
    T = (jd - 2451545) / 36525
    L0 = (280.46646 + T * (36000.76983 + T * 0.0003032)) % 360
    M = 357.52911 + T * (35999.05029 - 0.0001537 * T)
    C = (math.sin(M * rad) * (1.914602 - T * (0.004817 + 0.000014 * T)) + math.sin(2 * M * rad) * (0.019993 - 0.000101 * T)
         + math.sin(3 * M * rad) * 0.000289)
    lam = L0 + C - 0.00569 - 0.00478 * math.sin((125.04 - 1934.136 * T) * rad)
    eps = 23 + (26 + (21.448 - T * (46.815 + T * (0.00059 - T * 0.001813))) / 60) / 60 + 0.00256 * math.cos((125.04 - 1934.136 * T) * rad)
    dec = math.asin(math.sin(eps * rad) * math.sin(lam * rad))
    y = math.tan(eps * rad / 2) ** 2
    e = 0.016708634 - T * (0.000042037 + 0.0000001267 * T)
    eqt = 4 / rad * (y * math.sin(2 * L0 * rad) - 2 * e * math.sin(M * rad) + 4 * e * y * math.sin(M * rad) * math.cos(2 * L0 * rad)
                     - 0.5 * y * y * math.sin(4 * L0 * rad) - 1.25 * e * e * math.sin(2 * M * rad))
    utc = when.astimezone(ZoneInfo("UTC"))
    mins = utc.hour * 60 + utc.minute + utc.second / 60
    ha = ((mins + eqt + 4 * lon) / 4 - 180) * rad
    return math.asin(math.sin(lat * rad) * math.sin(dec) + math.cos(lat * rad) * math.cos(dec) * math.cos(ha)) / rad


def moon_elevation(when: datetime, lat: float, lon: float) -> float:
    """The moon's height in degrees, by Paul Schlyter's low-precision method (good to a degree or two,
    which is plenty for drawing its arc). Includes the parallax that lowers it near the horizon."""
    import math
    rad = math.pi / 180
    utc = when.astimezone(ZoneInfo("UTC"))
    d = (utc - datetime(1999, 12, 31, tzinfo=ZoneInfo("UTC"))).total_seconds() / 86400
    N = (125.1228 - 0.0529538083 * d) * rad
    inc = 5.1454 * rad
    w = (318.0634 + 0.1643573223 * d) * rad
    a, e = 60.2666, 0.0549
    M = ((115.3654 + 13.0649929509 * d) % 360) * rad
    E = M + e * math.sin(M) * (1 + e * math.cos(M))
    for _ in range(5):
        E = E - (E - e * math.sin(E) - M) / (1 - e * math.cos(E))
    xv, yv = a * (math.cos(E) - e), a * math.sqrt(1 - e * e) * math.sin(E)
    v, r = math.atan2(yv, xv), math.hypot(xv, yv)
    xh = r * (math.cos(N) * math.cos(v + w) - math.sin(N) * math.sin(v + w) * math.cos(inc))
    yh = r * (math.sin(N) * math.cos(v + w) + math.cos(N) * math.sin(v + w) * math.cos(inc))
    zh = r * math.sin(v + w) * math.sin(inc)
    lon_e, lat_e = math.atan2(yh, xh), math.atan2(zh, math.hypot(xh, yh))
    ecl = (23.4393 - 3.563e-7 * d) * rad
    xe, ye, ze = math.cos(lon_e) * math.cos(lat_e), math.sin(lon_e) * math.cos(lat_e), math.sin(lat_e)
    yq, zq = ye * math.cos(ecl) - ze * math.sin(ecl), ye * math.sin(ecl) + ze * math.cos(ecl)
    ra, dec = math.atan2(yq, xe), math.atan2(zq, math.hypot(xe, yq))
    ls = (282.9404 + 4.70935e-5 * d) + (356.0470 + 0.9856002585 * d)
    ut = utc.hour + utc.minute / 60 + utc.second / 3600
    lst = ((ls + 180) / 15 + ut + lon / 15) % 24
    ha = lst * 15 * rad - ra
    alt = math.asin(math.sin(lat * rad) * math.sin(dec) + math.cos(lat * rad) * math.cos(dec) * math.cos(ha))
    alt -= math.asin(1 / r) * math.cos(alt)
    return alt / rad


def house_light(day: dict | None, when: datetime) -> tuple[float, int] | None:
    """What the house's light is doing at a moment, from Circadian's day: the average level of the
    main-floor lights and their median colour, the same curve the House app draws."""
    if not day or not day.get("day_start"):
        return None
    i = round((when - datetime.fromisoformat(day["day_start"])).total_seconds() / 60 / (day.get("step_minutes") or 10))
    ids = (day.get("boost") or {}).get("lights") or list((day.get("lights") or {}).keys())
    levels, kelvins = [], []
    for e in ids:
        curve = ((day.get("lights") or {}).get(e) or {}).get("curve") or []
        if 0 <= i < len(curve) and curve[i]:
            levels.append(curve[i][0] or 0)
            if curve[i][1]:
                kelvins.append(curve[i][1])
    if not levels:
        return None
    b = sum(levels) / len(levels)
    mult = (day.get("boost") or {}).get("mult")
    if (day.get("boost") or {}).get("on") and mult and i < len(mult) and mult[i]:
        b = min(100.0, b * mult[i])
    kelvins.sort()
    return round(b, 1), (kelvins[len(kelvins) // 2] if kelvins else 2700)


def light_of(now: datetime, sunrise: datetime | None, sunset: datetime | None) -> str:
    """night, dawn or day. Night runs from half an hour after sunset to 45 minutes before sunrise."""
    if sunset and now >= sunset + timedelta(minutes=30):
        return "night"
    if sunrise and now < sunrise - timedelta(minutes=45):
        return "night"
    if sunrise and now < sunrise + timedelta(minutes=20):
        return "dawn"
    return "day"


def build(now: datetime, tz: ZoneInfo, current: dict, hourly: list, daily: list, sunrise: datetime | None,
          pollen: dict | None, house: list[str], place: str = "", sunset: datetime | None = None) -> dict:
    """Everything the board draws. `current` is the weather entity's state; hourly/daily are NWS forecasts."""
    now = now.astimezone(tz)
    light = light_of(now, sunrise, sunset)
    cond = current.get("state") or "cloudy"
    attrs = current.get("attributes") or {}
    hrs = _hours(hourly, tz)
    hour0 = now.replace(minute=0, second=0, microsecond=0)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    today = [h for h in hrs if hour0 <= h["dt"] <= midnight]
    if light == "night":   # after dark, the line runs through the night to the morning
        today = [h for h in hrs if hour0 <= h["dt"] <= hour0 + timedelta(hours=12)]
    temp = attrs.get("temperature")
    if temp is None and today:
        temp = today[0].get("temperature")
    temps_today = [h["temperature"] for h in today if h.get("temperature") is not None] + ([temp] if temp is not None else [])
    # The low from now through tomorrow morning, counting what it is right now: on a night that
    # warms (a front coming through), the low is now, not some hour of the forecast above it
    tonight = [h["temperature"] for h in hrs if now <= h["dt"] <= midnight + timedelta(hours=9)
               and h.get("temperature") is not None] + ([temp] if temp is not None else [])
    high = round(max(temps_today)) if temps_today else None
    low = round(min(tonight)) if tonight else None

    # What's worth saying, with when
    notes = []
    rain = _rain_phrase(today, hour0)
    if rain:
        notes.append({"what": rain[0], "when": rain[1]})
    snow = windows(today, lambda r: r.get("condition") in ("snowy", "snowy-rainy")
                   and (r.get("precipitation_probability") or 0) >= 30)
    if snow:
        a, b = snow[0]
        notes.append({"what": "Snow", "when": f"{clock(today[a]['dt'])} – {clock(today[b]['dt'] + timedelta(hours=1))}"})
    windy = windows(today, lambda r: (r.get("wind_speed") or 0) >= 20)
    if windy:
        a, b = windy[0]
        top = max(round(today[i].get("wind_speed") or 0) for i in range(a, b + 1))
        notes.append({"what": "Windy", "when": f"{clock(today[a]['dt'])} – {clock(today[b]['dt'] + timedelta(hours=1))}, "
                                              f"up to {top} mph"})
    night = [h for h in hrs if now <= h["dt"] <= midnight + timedelta(hours=9) and h.get("temperature") is not None]
    if night and temp is not None and temp > 32:
        cold = min(night, key=lambda h: h["temperature"])
        if cold["temperature"] <= 32:
            notes.append({"what": "Frost tonight", "when": f"down to {round(cold['temperature'])}° by {clock(cold['dt'])}"})
    if high is not None and high >= 90:
        notes.append({"what": "Hot", "when": f"up to {high}°"})
    dst = _dst_note(now, tz)
    if dst:
        notes.append(dst)
    if pollen and pollen.get("index") is not None:
        try:
            idx = float(pollen["index"])
        except (TypeError, ValueError):
            idx = 0
        if idx >= 4.9:
            notes.append({"what": f"Pollen {str(pollen.get('rating') or '').lower()}",
                          "when": ", ".join(pollen.get("top") or []).lower()})

    # The next three days, from the NWS day/night periods
    days = []
    by_date: dict = {}
    for p in daily or []:
        try:
            dt = datetime.fromisoformat(p["datetime"]).astimezone(tz)
        except (KeyError, ValueError, TypeError):
            continue
        d = by_date.setdefault(dt.date(), {})
        d["day" if p.get("is_daytime") else "night"] = p
    tomorrow_hi = None
    for i in range(1, 4):
        date = (now + timedelta(days=i)).date()
        d = by_date.get(date, {})
        dp, np_ = d.get("day"), d.get("night")
        if not dp:
            continue
        rows = [h for h in hrs if h["dt"].date() == date and 6 <= h["dt"].hour]
        pmax = max((r.get("precipitation_probability") or 0 for r in rows), default=dp.get("precipitation_probability") or 0)
        c = dp.get("condition") or "cloudy"
        if c in ("rainy", "pouring") and pmax < 30:
            c = "cloudy"                     # NWS says "rainy" at any chance at all
        rain_d = _rain_phrase(rows)
        wind = max((r.get("wind_speed") or 0 for r in rows), default=0)
        if rain_d:
            first = next(r for r in rows if (r.get("precipitation_probability") or 0) >= WET)
            peak = max(rows, key=lambda r: r.get("precipitation_probability") or 0)
            if first["dt"].hour <= 8 and peak["dt"].hour < 13:
                note = "<b>Wet morning</b>"
            elif first["dt"].hour <= 8:
                note = "<b>Rain on and off</b> all day"
            else:
                note = f"Rain from {clock(first['dt']).lower()}"
            if peak["dt"].hour >= 12 and not note.startswith("<b>Wet") and peak["dt"] != first["dt"]:
                note += f", <b>likeliest {'after' if peak['dt'] > first['dt'] else 'around'} {clock(peak['dt'])}</b>"
            note += f" ({round(pmax)}%)"
        else:
            note = DAY_WORD.get(c, "Cloudy") + " and dry" if c not in ("sunny",) else "Sunny and dry"
        if wind >= 20:
            note += f". <b>Windy</b>, to {round(wind)} mph"
        elif wind >= 15:
            note += ", <b>breezy</b>"
        hi = round(dp.get("temperature")) if dp.get("temperature") is not None else None
        lo = round(np_.get("temperature")) if np_ and np_.get("temperature") is not None else None
        if i == 1:
            tomorrow_hi = hi
        icon = ICON.get(c, "overcast")
        days.append({"name": date.strftime("%A"), "icon": icon, "hi": hi, "lo": lo, "note": note + "."})

    if tomorrow_hi is not None and high is not None and abs(tomorrow_hi - high) >= 8 and len(notes) < 4:
        word = "Warmer" if tomorrow_hi > high else "Colder"
        if word == "Warmer" and tomorrow_hi <= 70:
            word = "Milder"
        dry = days and "dry" in days[0]["note"]
        notes.append({"what": f"{word} tomorrow", "when": f"{tomorrow_hi}°{' and dry' if dry else ''}"})

    # One sentence
    base = WORD.get(cond, "Gray")
    if cond in ("rainy", "cloudy", "fog") and (attrs.get("humidity") or 0) >= 85:
        base = "Gray and damp"
    if light == "night":
        base = NIGHT_WORD.get(cond, "Overcast") + " tonight"
    if light == "night" and not rain:
        say = f"{base}, down to {low}°." if low is not None else f"{base}."
    elif rain:
        clause = rain[1].split(",")[0]
        if clause.startswith("until"):
            tail = f"with {'showers on and off' if rain[0].startswith('Showers') else 'rain'} {clause.replace(' pm', '').replace(' am', '')}"
        else:
            tail = f"{'showers' if rain[0] != 'Rain likely' else 'rain'} from {clause.split(' – ')[0]}"
        say = f"{base}, {tail}."
    elif high is not None:
        say = f"{base}, up to {high}°." if "damp" in base else f"{base}, dry all day, up to {high}°."
    else:
        say = f"{base}."
    say = say.replace(" am", "\u00a0am").replace(" pm", "\u00a0pm")   # "6 pm" never breaks across lines

    # Today, hour by hour, from now to midnight
    chart = [{"h": "Now" if i == 0 else clock(h["dt"]).replace(" am", "").replace(" pm", "")
              if h["dt"].hour not in (0, 12, 6, 18) else clock(h["dt"]).capitalize(),
              # Now is what the station measured, the same number as the big one
              "t": round(temp if i == 0 and temp is not None else h["temperature"]), "p": round(h.get("precipitation_probability") or 0),
              "at": h["dt"].isoformat()}
             for i, h in enumerate(today) if h.get("temperature") is not None]

    sky = SKY_OF.get(cond, "overcast")
    name = {"day": "morning"}.get(light, light)
    image = f"sky/{sky}-{name}.jpg"
    night_photo = light == "night" and (SKY_DIR / f"{sky}-night.jpg").exists()
    if not (SKY_DIR / f"{sky}-{name}.jpg").exists():
        image = f"sky/{sky}-morning.jpg"   # no photo for this light yet: the day one, dimmed at night
    return {
        "day": now.strftime("%A"), "date": now.strftime("%B ") + str(now.day), "place": place,
        "asof": clock(now, exact=True), "temp": round(temp) if temp is not None else "–",
        "high": high, "low": low, "humidity": round(attrs["humidity"]) if attrs.get("humidity") is not None else None,
        "say": say, "notes": notes[:4], "hours": chart, "nowIdx": 0,
        "days": days, "house": house, "sky": sky, "image": image,
        "moon": moon(now), "light": light, "dim": light == "night" and not night_photo, "label": "Tonight" if light == "night" else "Today",
    }


def observed(nws: dict | None, model: dict | None) -> dict:
    """The sky and temperature as measured: the Weather Service's latest station report, with the
    forecast model (met.no) filling in what the station doesn't say. The model's "now" is a forecast
    for this hour, so on a sunny afternoon it can still say cloudy."""
    nws, model = nws or {}, model or {}
    if nws.get("state") in (None, "unknown", "unavailable"):
        return model
    attrs = dict(model.get("attributes") or {})
    na = nws.get("attributes") or {}
    for k in ("temperature", "humidity", "wind_speed", "wind_bearing"):
        if na.get(k) is not None:
            attrs[k] = na[k]
    return {"state": nws["state"], "attributes": attrs}


def inside_block(inside: float | None, damp: float | None, day: dict | None, lights_on: int | None = None) -> dict:
    notes = []
    if day:
        if not day.get("enabled", True):
            notes.append("Natural light paused")
        elif (day.get("boost") or {}).get("on"):
            notes.append("Bright House on")
        else:
            notes.append("Natural light on")
    if lights_on is not None:
        notes.append(f"<b>{lights_on}</b> light{'s' if lights_on != 1 else ''} on")
    return {"t": round(inside) if inside is not None else None, "rh": round(damp) if damp is not None else None,
            "notes": notes}


def house_line(inside: float | None, day: dict | None, pollen: dict | None, damp: float | None = None,
               lights_on: int | None = None) -> list[str]:
    out = []
    if inside is not None:
        out.append(f"<b>{round(inside)}°</b>" + (f" · <b>{round(damp)}%</b>" if damp is not None else "") + " inside")
    if day:
        # The house's own count (one per control, like every other screen), not Circadian's bulbs
        n = lights_on if lights_on is not None else len(
            [x for x in (day.get("lights") or {}).values() if x.get("state") == "on"])
        if not day.get("enabled", True):
            out.append("Natural light paused")
        elif (day.get("boost") or {}).get("on"):
            out.append("Bright House on")
        else:
            out.append("Natural light on")
        out.append(f"<b>{n}</b> light{'s' if n != 1 else ''} on")
    if pollen and pollen.get("rating"):
        top = ", ".join(pollen.get("top") or [])[:40]
        out.append(f"Pollen <b>{html.escape(str(pollen['rating']).lower())}</b>" + (f" · {html.escape(top.lower())}" if top else ""))
    return out


def _hhmm(v: str, fallback: str) -> tuple[int, int]:
    try:
        h, m = (int(x) for x in (v or fallback).split(":"))
        return h, m
    except ValueError:
        h, m = (int(x) for x in fallback.split(":"))
        return h, m


class Board:
    """Runs the Frame's one board slot. Three boards can want it:
      weather  mornings, when someone's in the kitchen
      roads    any time someone's in the kitchen (when 511 is set up)
      music    whenever the kitchen plays a record
    The ones wanted take turns (`alternate` seconds each, in that order). News jumps the queue: a new
    track, or a new problem on the roads. Nobody in the kitchen and no record: the art comes back."""

    ORDER = ("weather", "roads", "music")

    def __init__(self, house, opts: dict):
        self.house = house
        self.sensor = opts.get("board_presence", "binary_sensor.kitchen_presence_presence")
        self.inside = opts.get("board_inside", "sensor.kitchen_presence_temperature")
        self.inside_humidity = opts.get("board_inside_humidity", "sensor.kitchen_presence_humidity")
        self.frame = opts.get("frame_url", "http://172.30.32.1:8800").rstrip("/")
        self.start = _hhmm(opts.get("board_from"), "05:00")
        self.end = _hhmm(opts.get("board_until"), "11:00")
        self.linger = int(opts.get("board_linger_minutes", 5)) * 60
        self.every = int(opts.get("board_refresh_minutes", 30)) * 60       # at the latest, for the clock
        self.gap_showing = int(opts.get("board_redraw_showing_seconds", 180))   # soonest redraw while it's up
        self.gap_waiting = int(opts.get("board_redraw_waiting_seconds", 300))   # ... while it waits on the TV
        self.staged_looks = ""
        self.checked = -1
        self.enabled = bool(opts.get("board", True))
        self.staged_at = 0.0
        self.showing = False
        self.last_seen = 0.0
        self.last_error = ""
        self.staging: asyncio.Task | None = None
        self.has_picture = False
        self.try_until = 0.0     # "show it now", from the House page, for a few minutes
        self.tz = ZoneInfo("America/Chicago")
        # Now playing: whenever the kitchen plays a record, the Frame shows its sleeve; with the morning
        # board up too, the two take turns.
        self.music_on = bool(opts.get("board_music", True))
        self.music_room = opts.get("board_music_room", "Kitchen")
        self.alternate = int(opts.get("board_alternate_seconds", 180))
        self.music_grace = int(opts.get("board_music_grace_seconds", 120))   # a pause between albums isn't the end
        self.music: dict | None = None
        self.music_seen = 0.0
        self.np: dict | None = None        # the track on the staged Now playing picture
        self.staged_kind: str | None = None
        self.turn: str | None = None       # what's on screen, and since when
        self.turn_at = 0.0
        self.shown_track = ""              # the last track that made it onto the screen
        self.roads = None                  # Roads (511), set by the server when it's configured
        self.roads_from = _hhmm(opts.get("roads_from"), "05:00")
        self.roads_until = _hhmm(opts.get("roads_until"), "21:00")   # after 9 pm nobody's heading out
        self.road_news = ""                # the roads' problem, as last staged / shown
        self.shown_road_news = ""

    # ------------------------------------------------------------ data
    async def forecasts(self, entity: str, kind: str) -> list:
        url = "http://supervisor/core/api/services/weather/get_forecasts?return_response"
        async with self.house.session.post(url, headers=self.house.hdr(),
                                           data=json.dumps({"entity_id": entity, "type": kind}),
                                           timeout=aiohttp.ClientTimeout(total=15)) as r:
            r.raise_for_status()
            body = await r.json()
        return ((body.get("service_response") or {}).get(entity) or {}).get("forecast") or []

    async def data(self) -> dict:
        h = self.house
        nws = h.opts.get("forecast", "weather.nws")
        states, day, cfg, hourly, daily = await asyncio.gather(
            h.all_states(), h.circadian_day(), h.cached("config", 3600, lambda: h.ha_get("/config")),
            h.cached("fc-hourly", 600, lambda: self.forecasts(nws, "hourly")),
            h.cached("fc-daily", 600, lambda: self.forecasts(nws, "twice_daily")))
        by = {s["entity_id"]: s for s in states}
        self.tz = ZoneInfo(cfg.get("time_zone") or "America/Chicago")
        current = observed(by.get(nws), by.get(h.opts.get("weather", "weather.forecast_home")))
        al = by.get(h.opts.get("allergy", "sensor.allergy_index_today"), {})
        pollen = None
        if al.get("state") not in (None, "unknown", "unavailable"):
            aa = al.get("attributes", {})
            pollen = {"index": al["state"], "rating": aa.get("rating"),
                      "top": [aa[k] for k in ("allergen_name_1", "allergen_name_2") if aa.get(k)]}
        try:
            inside = float(by.get(self.inside, {}).get("state"))
        except (TypeError, ValueError):
            inside = None
        try:
            damp = float(by.get(self.inside_humidity, {}).get("state"))
        except (TypeError, ValueError):
            damp = None
        sun = by.get("sun.sun", {}).get("attributes", {})
        sunrise = sunset = None
        if day and day.get("sunrise"):
            sunrise = datetime.fromisoformat(day["sunrise"])
            sunset = datetime.fromisoformat(day["sunset"]) if day.get("sunset") else None
        elif sun.get("next_rising"):
            sunrise = datetime.fromisoformat(sun["next_rising"])
        place = cfg.get("location_name") or ""
        place = h.opts.get("board_place") or ("" if place in ("", "Home") else place)   # set in the app's options
        on = h.lights_on(by)["on"]
        b = build(datetime.now(self.tz), self.tz, current, hourly, daily, sunrise, pollen,
                  house_line(inside, day, pollen, damp, on), place, sunset)
        # The day, together: outside temperature, the sun's height and the house's light on one clock
        lat, lon = cfg.get("latitude"), cfg.get("longitude")
        for p in b["hours"]:
            at = datetime.fromisoformat(p["at"])
            if lat is not None and lon is not None:
                p["sun"] = round(sun_elevation(at, lat, lon), 1)
                p["moon"] = round(moon_elevation(at, lat, lon), 1)
            hl = house_light(day, at)
            if hl:
                p["light"], p["k"] = hl
        b["inside"] = inside_block(inside, damp, day, on)
        b["pollen"] = pollen
        return b

    # ------------------------------------------------------------ what's playing in the kitchen
    async def music_now(self) -> dict | None:
        """The kitchen's record, when it's playing one with a sleeve (not TV sound, not a bare line-in)."""
        if not self.music_on:
            return None
        url = self.house.opts.get("music_url", "http://3dd8a0cc-music:8097").rstrip("/")
        now = await self.house.cached("board-music", 4, lambda: self.house._json(f"{url}/api/now"))
        for r in (now or {}).get("rooms", []):
            if r.get("name") != self.music_room:
                continue
            if r.get("state") != "playing" or r.get("tv") or not r.get("art") or not r.get("title"):
                return None
            others = [g for g in r.get("group") or [] if g not in (r["name"], "Turntable")]
            return {"title": r["title"], "artist": r.get("artist") or "", "album": r.get("album") or "",
                    "art": r["art"], "rooms": [r["name"]] + others}
        return None

    @staticmethod
    def track(m: dict | None) -> str:
        return json.dumps([m.get(k) for k in ("title", "artist", "album", "art")]) if m else ""

    # ------------------------------------------------------------ picture
    async def render(self, page: str = "board") -> bytes:
        """A page, as the Frame's 2160 × 3840 JPEG."""
        from PIL import Image
        out = f"/tmp/{page}.png"
        proc = await asyncio.create_subprocess_exec(
            "chromium", "--headless", "--no-sandbox", "--disable-gpu", "--hide-scrollbars", "--mute-audio",
            "--force-device-scale-factor=2", "--window-size=1080,1920", "--virtual-time-budget=10000",
            f"--screenshot={out}", f"http://127.0.0.1:8099/{page}?frame=1",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, err = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode != 0:
            raise RuntimeError(f"Chromium failed: {err.decode(errors='replace')[-300:]}")
        img = Image.open(out).convert("RGB")
        if img.size != (2160, 3840):
            img = img.resize((2160, 3840), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=90, optimize=True)
        return buf.getvalue()

    async def frame_post(self, path: str, body: bytes = b"{}") -> dict:
        async with self.house.session.post(f"{self.frame}{path}", data=body,
                                           timeout=aiohttp.ClientTimeout(total=90)) as r:
            r.raise_for_status()
            return await r.json()

    @staticmethod
    def looks(b: dict) -> str:
        """What the board would show, minus its clock: two boards that look the same give the same string."""
        return json.dumps({k: v for k, v in b.items() if k != "asof"}, sort_keys=True, default=str)

    def on_screen(self, kind: str):
        if kind != self.turn:
            self.turn, self.turn_at = kind, time.time()
        if kind == "music":
            self.shown_track = self.track(self.np)
        elif not self.shown_track:
            self.shown_track = self.track(self.music)   # already playing when the board went up: not new
        if kind == "roads":
            self.shown_road_news = self.road_news

    async def stage(self, kind: str = "weather") -> dict:
        """Put a fresh picture of `kind` in the Frame's one board slot (on screen at once if the slot is showing)."""
        if kind == "music":
            self.np = self.music
            looks, page, label = self.track(self.np), "nowplaying", "Now playing"
        elif kind == "roads":
            rb = self.roads_board()
            if rb is None:
                return {"ok": False, "reason": "no road data yet"}
            looks, page, label = self.roads_looks(rb), "roads", "Roads"
            self.road_news = self.roads_problem(rb)
        else:
            looks, page, label = self.looks(await self.data()), "board", "Kitchen board"
            self.checked = self.house.live.version
        jpeg = await self.render(page)
        r = await self.frame_post(f"/api/board/stage?label={label.replace(' ', '%20')}", jpeg)
        if r.get("ok"):
            self.staged_kind, self.staged_looks = kind, looks
            self.staged_at = time.time()
            self.has_picture = True
            if self.showing:
                self.on_screen(kind)
        return r

    async def _stage_bg(self, kind: str):
        try:
            r = await self.stage(kind)
            if not r.get("ok"):
                log.info("Board not staged: %s", r.get("reason"))
                self.staged_at = time.time() - self.every + 60   # try again in a minute
        except Exception as ex:
            log.warning("Board: staging %s failed: %s", kind, ex)
            self.staged_at = time.time() - self.every + 60
        finally:
            self.staging = None

    # ------------------------------------------------------------ when
    def in_window(self, now: datetime) -> bool:
        m = now.hour * 60 + now.minute
        return self.start[0] * 60 + self.start[1] <= m < self.end[0] * 60 + self.end[1]

    def roads_hours(self, now: datetime) -> bool:
        m = now.hour * 60 + now.minute
        return self.roads_from[0] * 60 + self.roads_from[1] <= m < self.roads_until[0] * 60 + self.roads_until[1]

    async def present(self) -> bool:
        if self.house.live.ready:
            return (self.house.live.states.get(self.sensor) or {}).get("state") == "on"
        st = await self.house.ha_get(f"/states/{self.sensor}")
        return st.get("state") == "on"

    def choose(self, wanted: list[str], t: float, news: str | None = None) -> str | None:
        """What the Frame's board slot shows now, from the boards that want it (in ORDER).
        One wanted: that one. Several: they take turns; `news` (a board with something new) goes next."""
        wanted = [k for k in self.ORDER if k in wanted]
        if not wanted:
            return None
        if not self.showing or self.turn not in wanted:
            return wanted[0]
        if news in wanted and news != self.turn:
            return news
        if len(wanted) > 1 and t - self.turn_at >= self.alternate:
            return wanted[(wanted.index(self.turn) + 1) % len(wanted)]
        return self.turn

    def news(self, wanted: list[str]) -> str | None:
        """Which board has something new to say: a new track, or a new problem on the roads."""
        if "music" in wanted and self.turn != "music" and self.track(self.music) != self.shown_track:
            return "music"
        if "roads" in wanted and self.turn != "roads" and self.road_news and self.road_news != self.shown_road_news:
            return "roads"
        return None

    def roads_board(self) -> dict | None:
        if not self.roads or not self.roads.configured or not self.roads.updated:
            return None
        import roadboard
        by = self.house.live.states if self.house.live.ready else {}
        return roadboard.build(self.roads.report(), by, datetime.now(self.tz))

    @staticmethod
    def roads_looks(b: dict) -> str:
        """What the roads board says, coarsely (status of each leg, closures, headline), so it's redrawn
        when the story changes, not every time a minute ticks."""
        return json.dumps({"say": b["say"], "legs": {k: v["status"] for k, v in b["legs"].items()},
                           "closures": [c["text"] + c["when"] for c in b["closures"]],
                           "trips": [round((x["min"] or 0) / 5) for x in b["trips"]],
                           "camera": (b.get("camera") or {}).get("view")}, sort_keys=True)

    @staticmethod
    def roads_problem(b: dict | None) -> str:
        if not b:
            return ""
        return "|".join(k for k, v in sorted(b["legs"].items()) if v["status"] in ("slow", "bad"))

    async def tick(self):
        t = time.time()
        trying = t < self.try_until
        if await self.present() or trying:
            self.last_seen = t
        window = self.in_window(datetime.now(self.tz)) or trying
        grace = 5 if self.try_until else self.linger
        here = self.last_seen > 0 and t - self.last_seen <= grace
        m = await self.music_now()
        if m:
            self.music, self.music_seen = m, t
        want_music = self.music is not None and t - self.music_seen <= self.music_grace
        if not want_music:
            self.music = None
        rb = self.roads_board() if not trying and self.roads_hours(datetime.now(self.tz)) else None
        wanted = ([("weather")] if window and here else []) + (["roads"] if rb and here else []) + \
                 (["music"] if want_music else [])
        if rb and self.turn != "roads":
            self.road_news = self.roads_problem(rb)           # a problem that appears while away is news
        target = self.choose(wanted, t, self.news(wanted))

        # Keep the right picture staged: what's wanted now, or what arrival would show first, ready for it
        prep = target or ("weather" if window else "roads" if rb else None)
        age = t - self.staged_at
        if prep and not self.staging:
            if self.staged_kind != prep or not self.has_picture:
                self.staging = asyncio.create_task(self._stage_bg(prep))
            elif prep == "music" and self.staged_looks != self.track(self.music):
                self.staging = asyncio.create_task(self._stage_bg("music"))      # the next track
            elif prep == "roads":
                if age > self.every or (age > (self.gap_showing if self.showing else self.every)
                                        and self.roads_looks(rb) != self.staged_looks):
                    self.staging = asyncio.create_task(self._stage_bg("roads"))
            elif prep == "weather":
                if age > self.every:
                    self.staging = asyncio.create_task(self._stage_bg("weather"))
                elif (self.house.live.version != self.checked
                      and age > (self.gap_showing if self.showing else self.gap_waiting)):
                    # Something changed in the house: redraw only if the board would look different.
                    self.checked = self.house.live.version
                    if self.looks(await self.data()) != self.staged_looks:
                        self.staging = asyncio.create_task(self._stage_bg("weather"))

        if target and not self.showing and self.has_picture and self.staged_kind == target:
            r = await self.frame_post("/api/board/show")
            self.showing = bool(r.get("ok"))
            if self.showing:
                self.on_screen(target)
            log.info("Frame: %s %s", {"music": "now playing", "roads": "roads"}.get(target, "kitchen board"),
                     "on" if self.showing else f"not shown ({r.get('reason')})")
        if self.showing and target is None:
            await self.frame_post("/api/board/hide")
            self.showing, self.turn, self.shown_track = False, None, ""
            self.staged_kind, self.staged_at, self.has_picture = None, 0.0, False   # the Frame took it off the TV
            self.try_until = 0.0
            log.info("Frame: nothing to show; art's back")
        if prep is None and not self.showing and self.has_picture and not self.staging:
            await self.frame_post("/api/board/hide")   # take the staged picture off the TV for the day
            self.staged_kind, self.staged_at, self.has_picture = None, 0.0, False

    async def try_now(self, minutes: int = 3) -> dict:
        """Put the weather board up now, whatever the time, and take it down after a few minutes."""
        r = await self.stage("weather")
        if not r.get("ok"):
            return r
        self.try_until = time.time() + minutes * 60
        self.last_seen = time.time()
        r = await self.frame_post("/api/board/show")
        self.showing = bool(r.get("ok"))
        if self.showing:
            self.on_screen("weather")
        return r

    async def loop(self):
        if not self.enabled:
            return
        await asyncio.sleep(10)
        try:
            # Start clean: if a board was up when this app last stopped, the art comes back first.
            await self.frame_post("/api/board/hide")
        except Exception as ex:
            log.info("Board: couldn't reach the Frame app yet (%s)", ex)
        while True:
            try:
                await self.tick()
                self.last_error = ""
            except Exception as ex:
                if str(ex) != self.last_error:
                    log.warning("Board: %s", ex)
                self.last_error = str(ex)
            await asyncio.sleep(5)
