"""Circadian engine: pure scheduling and override logic (no I/O).

Keyframes are times with per-light targets. Each light glides between the
keyframes that mention it. See DESIGN.md (kept locally) for the reasoning.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Callable

OFF = "off"   # light should not be on yet (only meaningful for wake)
ON = "on"     # power entity (e.g. a switch) to turn on at wake
KEEP_OFF = "keepoff"  # if a room-wide "on" turns this light on, turn it back off (direct requests still win)
SHUT = "shutoff"     # in a wake keyframe: turn this light off once at that time (people can turn it back on)

_TIME_RE = re.compile(r"^\s*(sunrise|sunset|\d{1,2}:\d{2})\s*(?:([+-])\s*(\d{1,2}):(\d{2}))?\s*$")


@dataclass(frozen=True)
class Target:
    brightness: float | None  # percent 1..100, None for OFF/ON
    kelvin: int | None = None
    kind: str = "level"       # "level", OFF or ON
    via: str | None = None    # entity to turn on (bare) when waking this light


@dataclass(frozen=True)
class Keyframe:
    name: str
    at: str
    lights: dict[str, Target]
    step: bool = False                      # no glide into this keyframe; previous values hold until it
    wake: bool = False                      # may turn lights on at this time
    wake_if_sunrise_after: time | None = None
    channel: str = "both"                   # "both", "brightness" or "color": what this keyframe sets


CHANNELS = ("both", "brightness", "color")


def parse_light(spec: str) -> tuple[str, Target]:
    """'light.x 35', 'light.x 35 2700', 'light.x off', 'switch.x on', '... via light.y'."""
    parts = spec.split()
    if not parts:
        raise ValueError("empty light spec")
    via = None
    if "via" in parts:
        i = parts.index("via")
        via = parts[i + 1]
        parts = parts[:i]
    entity = parts[0]
    if "." not in entity:
        raise ValueError(f"bad entity in {spec!r}")
    if len(parts) < 2:
        raise ValueError(f"missing value in {spec!r}")
    val = parts[1].lower()
    if val == "-":   # color only: 'light.x - 3200'
        if len(parts) < 3:
            raise ValueError(f"missing kelvin in {spec!r}")
        return entity, Target(None, int(parts[2]), "level", via)
    if val == OFF:
        return entity, Target(None, None, OFF, via)
    if val == ON:
        return entity, Target(None, None, ON, via)
    if val == KEEP_OFF:
        return entity, Target(None, None, KEEP_OFF, via)
    if val == SHUT:
        return entity, Target(None, None, SHUT, via)
    b = float(val)
    if not 0 < b <= 100:
        raise ValueError(f"brightness out of range in {spec!r}")
    k = int(parts[2]) if len(parts) > 2 else None
    return entity, Target(b, k, "level", via)


def parse_hhmm(s: str | None) -> time | None:
    if not s:
        return None
    h, m = s.strip().split(":")
    return time(int(h), int(m))


def keyframes_from_options(raw: list[dict]) -> list[Keyframe]:
    out = []
    for k in raw or []:
        lights = dict(parse_light(s) for s in k.get("lights", []))
        if not _TIME_RE.match(k["at"]):
            raise ValueError(f"bad time {k['at']!r} in keyframe {k.get('name')!r}")
        channel = k.get("channel") or "both"
        if channel not in CHANNELS:
            raise ValueError(f"bad channel {channel!r} in keyframe {k.get('name')!r}")
        if channel == "color":
            if k.get("wake"):
                raise ValueError(f"color-only keyframe {k.get('name')!r} can't wake lights")
            bad = [e for e, t in lights.items() if t.kind != "level" or not t.kelvin]
            if bad:
                raise ValueError(f"color-only keyframe {k.get('name')!r} needs a kelvin for {', '.join(bad)}")
        elif any(t.kind == "level" and t.brightness is None for t in lights.values()):
            raise ValueError(f"keyframe {k.get('name')!r}: '-' (no brightness) only belongs in a color keyframe")
        out.append(Keyframe(
            name=k["name"], at=k["at"], lights=lights,
            step=bool(k.get("step", False)), wake=bool(k.get("wake", False)),
            wake_if_sunrise_after=parse_hhmm(k.get("wake_if_sunrise_after")), channel=channel))
    return out


SunFn = Callable[[date], tuple[datetime, datetime]]  # day -> (sunrise, sunset), tz-aware


def resolve(at: str, day: date, sun: SunFn, tz) -> datetime:
    m = _TIME_RE.match(at)
    if not m:
        raise ValueError(f"bad time {at!r}")
    base, sign, hh, mm = m.groups()
    if base == "sunrise":
        t = sun(day)[0]
    elif base == "sunset":
        t = sun(day)[1]
    else:
        h, mi = base.split(":")
        t = datetime.combine(day, time(int(h), int(mi)), tz)
    if sign:
        d = timedelta(hours=int(hh), minutes=int(mm))
        t = t + d if sign == "+" else t - d
    return t


def _mired(k: int) -> float:
    return 1_000_000 / k


class Schedule:
    def __init__(self, keyframes: list[Keyframe], sun: SunFn, tz):
        self.keyframes = keyframes
        self.sun = sun
        self.tz = tz

    def entities(self) -> set[str]:
        return {e for k in self.keyframes for e in k.lights}

    def points(self, entity: str, now: datetime, channels=("both", "brightness", "color")):
        """(time, target, step) for this entity across yesterday..tomorrow, sorted."""
        pts = []
        for offset in (-1, 0, 1):
            day = (now + timedelta(days=offset)).date()
            for k in self.keyframes:
                if entity in k.lights and k.channel in channels:
                    pts.append((resolve(k.at, day, self.sun, self.tz), k.lights[entity], k.step))
        pts.sort(key=lambda p: p[0])
        return pts

    def target_at(self, entity: str, now: datetime) -> Target | None:
        """Brightness follows keyframes that set it; color follows keyframes that set it.
        A keyframe with no channel sets both, so lights without split keyframes behave as before."""
        tb = self._track(self.points(entity, now, ("both", "brightness")), now)
        if tb is None or tb.kind != "level":
            return tb
        if not any(k.channel == "color" and entity in k.lights for k in self.keyframes):
            return tb
        tk = self._track(self.points(entity, now, ("both", "color")), now, color=True)
        k = tk.kelvin if tk is not None and tk.kind == "level" else tb.kelvin
        return Target(tb.brightness, k, "level", tb.via)

    def _track(self, pts, now: datetime, color: bool = False) -> Target | None:
        prev = next_ = None
        for p in pts:
            if p[0] <= now:
                prev = p
            elif next_ is None:
                next_ = p
        if prev is None:
            return None
        pt = prev[1]
        if pt.kind != "level":
            return pt
        if next_ is None or next_[2] or next_[1].kind != "level":
            return pt  # hold until a step keyframe or a non-level value
        nt = next_[1]
        span = (next_[0] - prev[0]).total_seconds()
        f = 0.0 if span <= 0 else (now - prev[0]).total_seconds() / span
        b = None if pt.brightness is None or nt.brightness is None else pt.brightness + (nt.brightness - pt.brightness) * f
        k = None
        if pt.kelvin and nt.kelvin:
            m = _mired(pt.kelvin) + (_mired(nt.kelvin) - _mired(pt.kelvin)) * f
            k = int(round(1_000_000 / m))
        else:
            k = pt.kelvin or nt.kelvin
        return Target(None if b is None else round(b, 1), k, "level", pt.via)

    def wake_events(self, day: date, ignore_gate: bool = False):
        """[(time, keyframe)] for wake keyframes whose gate passes on this day."""
        out = []
        sunrise = self.sun(day)[0]
        for k in self.keyframes:
            if not k.wake:
                continue
            if (not ignore_gate and k.wake_if_sunrise_after
                    and sunrise.timetz().replace(tzinfo=None) <= k.wake_if_sunrise_after):
                continue
            out.append((resolve(k.at, day, self.sun, self.tz), k))
        return sorted(out, key=lambda e: e[0])


# ---------------------------------------------------------------- overrides

@dataclass
class LightTrack:
    held: bool = False
    held_reason: str = ""
    cmd_from: int | None = None     # brightness (0-255) when we last sent
    cmd_to: int | None = None       # brightness (0-255) we asked for
    cmd_kelvin: int | None = None
    cmd_kelvin_from: int | None = None
    cmd_at: datetime | None = None
    cmd_transition: float = 0
    user_off_day: date | None = None
    woken: set = field(default_factory=set)   # (day, keyframe name)
    recent: list = field(default_factory=list)  # [(at, b_from, b_to, k_from, k_to)] last few commands
    mismatch: str = ""


BRIGHT_TOL = 6      # of 255 (Hue rounding + fades that overlap the next step)
KELVIN_TOL = 150


def pct_to_255(p: float) -> int:
    return max(1, min(255, round(p * 255 / 100)))


class Overrides:
    """Decides whether a state change was a person taking control."""

    def __init__(self):
        self.tracks: dict[str, LightTrack] = {}
        self.our_contexts: dict[str, datetime] = {}
        self.grace_until: datetime | None = None   # after a restart, late reports of old fades aren't people

    def track(self, e: str) -> LightTrack:
        return self.tracks.setdefault(e, LightTrack())

    def note_command(self, e: str, ctx: str | None, now: datetime, from_b: int | None,
                     to_pct: float | None, kelvin: int | None, transition: float,
                     from_k: int | None = None):
        t = self.track(e)
        t.cmd_from = from_b
        t.cmd_to = pct_to_255(to_pct) if to_pct is not None else None
        t.cmd_kelvin = kelvin
        t.cmd_kelvin_from = from_k
        t.cmd_at = now
        t.cmd_transition = transition
        t.recent = [r for r in t.recent if (now - r[0]).total_seconds() < 900][-2:]
        t.recent.append((now, from_b, t.cmd_to, from_k, kelvin))
        if ctx:
            self.our_contexts[ctx] = now
        cutoff = now - timedelta(minutes=30)
        for c in [c for c, ts in self.our_contexts.items() if ts < cutoff]:
            del self.our_contexts[c]

    def ours(self, ctx: str | None) -> bool:
        return bool(ctx) and ctx in self.our_contexts

    def _within_expected(self, t: LightTrack, attrs: dict) -> bool:
        """Is this reported state explained by our own recent fades?

        Uses the endpoints of the last few commands, because a new step can start
        before the bridge reports where the previous fade ended."""
        if t.cmd_to is None or not t.recent:
            t.mismatch = "no recent command"
            return False
        bs = [v for r in t.recent for v in (r[1], r[2]) if v is not None]
        ks = [v for r in t.recent for v in (r[3], r[4]) if v]
        b = attrs.get("brightness")
        if b is not None and bs:
            lo, hi = min(bs) - BRIGHT_TOL, max(bs) + BRIGHT_TOL
            if not lo <= b <= hi:
                t.mismatch = f"brightness {b} outside {lo}-{hi}"
                return False
        if ks and attrs.get("color_mode") == "color_temp":
            k = attrs.get("color_temp_kelvin")
            if k is not None:
                lo, hi = min(ks) - KELVIN_TOL, max(ks) + KELVIN_TOL
                if not lo <= k <= hi:
                    t.mismatch = f"kelvin {k} outside {lo}-{hi}"
                    return False
        # Color reported as xy/hs for a light we drove by kelvin is treated as a
        # representation change, not a person (lesson from Adaptive Lighting #1619).
        t.mismatch = ""
        return True

    def on_state(self, e: str, old: str | None, new: str, attrs: dict, ctx: str | None,
                 now: datetime, call: dict | None, ours: bool = False) -> str:
        """Returns an action: 'none', 'snap' (put it on the curve), 'held'.

        `ours` is True when the change came from this app (matched by user or context)."""
        t = self.track(e)
        ours = ours or self.ours(ctx)
        if new == "unavailable" or new == "unknown":
            return "none"
        if new == "off":
            if t.held:
                t.held, t.held_reason = False, ""
            if old == "on" and not ours:
                t.user_off_day = now.date()
            return "none"
        # new == "on"
        if old in (None, "off", "unavailable", "unknown"):
            if ours:
                return "none"
            if call is not None and not call.get("bare", True):
                t.held, t.held_reason = True, "turned on with a level"
                return "held"
            t.held, t.held_reason = False, ""
            return "snap"
        if ours:
            return "none"
        if call is not None:
            if call.get("bare", False):
                # a plain "on" to a light that's already on: give it back to the curve
                t.held, t.held_reason = False, ""
                return "snap"
            # a deliberate command through HA (voice, app, script) always wins
            t.held, t.held_reason = True, "set by a command"
            return "held"
        # No HA command behind it: a bridge report or a physical control.
        # Only treat it as a person if it's outside what our own fade explains.
        if self._within_expected(t, attrs):
            return "none"
        if not t.recent and self.grace_until and now < self.grace_until:
            return "none"
        t.held, t.held_reason = True, f"changed at the device: {t.mismatch}"
        return "held"

    def release_all(self):
        for t in self.tracks.values():
            t.held, t.held_reason = False, ""


# ---------------------------------------------------------------- daylight

@dataclass
class DaylightZone:
    """Turns lights off when a light sensor says daylight is enough, and back on when it isn't.

    Hysteresis keeps passing clouds from flicking lights: the reading has to stay at or above
    `off_above` for `off_after` before lights go off, and at or below `on_below` for `on_after`
    before they come back. A missing or unreadable sensor counts as dark, so lights stay on."""
    name: str
    sensor: str
    off_above: float
    on_below: float
    off_after: timedelta
    on_after: timedelta
    lights: frozenset
    bright: bool = False
    lux: float | None = None
    above_since: datetime | None = None
    below_since: datetime | None = None

    def update(self, value, now: datetime):
        try:
            lux = float(value)
        except (TypeError, ValueError):
            lux = None
        self.lux = lux
        if lux is None:
            self.above_since = None
            self.below_since = self.below_since or now
            return
        if lux >= self.off_above:
            self.above_since = self.above_since or now
        else:
            self.above_since = None
        if lux <= self.on_below:
            self.below_since = self.below_since or now
        else:
            self.below_since = None

    def evaluate(self, now: datetime) -> bool:
        if self.lux is None:
            self.bright = False
        elif not self.bright and self.above_since and now - self.above_since >= self.off_after:
            self.bright = True
        elif self.bright and self.below_since and now - self.below_since >= self.on_after:
            self.bright = False
        return self.bright


def daylight_zones_from_options(raw: list[dict] | None) -> list[DaylightZone]:
    out, seen = [], set()
    for z in raw or []:
        lights = frozenset(z.get("lights", []))
        dup = lights & seen
        if dup:
            raise ValueError(f"light(s) in more than one daylight zone: {', '.join(sorted(dup))}")
        seen |= lights
        if float(z["on_below"]) >= float(z["off_above"]):
            raise ValueError(f"daylight zone {z.get('name')!r}: on_below must be lower than off_above")
        out.append(DaylightZone(
            name=z.get("name") or z["sensor"], sensor=z["sensor"],
            off_above=float(z["off_above"]), on_below=float(z["on_below"]),
            off_after=timedelta(minutes=float(z.get("off_after_minutes", 10))),
            on_after=timedelta(minutes=float(z.get("on_after_minutes", 3))),
            lights=lights))
    return out
