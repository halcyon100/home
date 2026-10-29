"""Listen: a weekly handful of new records, picked from the scene's own writing.

Runs on the house box. Claude (through Home Assistant's AI Task, with web search) reads the
editorial roundups and label pages, and picks albums by artists you don't already follow, plus
one record from your own library worth a second listen. Each pick is matched on Spotify through
Music Assistant so it can play in the house or open on a phone.

Feedback: thumbs up and down steer the next picks; dismiss only hides a card.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta

import aiohttp

log = logging.getLogger("house.listen")

SOURCES = ("Bandcamp Daily (Best Electronic, Best Ambient, Essential Releases), Resident Advisor, "
           "The Quietus, Pitchfork, PopMatters, Boomkat, Mixmag, The Wire, A Closer Listen, and label "
           "pages: Warp, Ninja Tune, Ghostly, Erased Tapes, Modern Love, Hyperdub, Planet Mu, Kompakt, "
           "Dekmantel, Smalltown Supersound, AD 93, Phantasy, Ki, Mute, Domino")

PLACES = {"main": "Main level", "upper": "Upstairs", "basement": "Downstairs", "outside": "Outside"}


def season(now: datetime) -> str:
    part = "Early" if now.day <= 10 else "Late" if now.day > 20 else "Mid"
    return f"{part} {now:%B}"


def spotify_url(uri: str | None, artist: str, album: str) -> str:
    m = re.search(r"spotify[^:]*://album/([A-Za-z0-9]+)", uri or "")
    if m:
        return f"https://open.spotify.com/album/{m.group(1)}"
    q = re.sub(r"\s+", "%20", f"{artist} {album}".strip())
    return f"https://open.spotify.com/search/{q}"


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").casefold()).strip()


def artist_matches(want: str, got: list[str]) -> bool:
    w = _norm(want)
    return any(w and (w == _norm(g) or w in _norm(g) or _norm(g) in w) for g in got if g)


class Listen:
    def __init__(self, house, path: str = "/data/listen.json"):
        self.house = house
        self.path = path
        self.taste = house.opts.get("listen_taste", "")
        self.ai = house.opts.get("listen_ai_task", "ai_task.claude_ai_task")
        self.data = {"picks": [], "feedback": {}, "history": [], "generated": None}
        self.busy = False
        self.error = None
        try:
            with open(path) as f:
                self.data.update(json.load(f))
        except (OSError, ValueError):
            pass

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f)
        os.replace(tmp, self.path)

    # ------------------------------------------------------------ public
    def view(self) -> dict:
        fb = self.data["feedback"]
        picks = [dict(p, verdict=(fb.get(p["id"]) or {}).get("verdict")) for p in self.data["picks"]]
        return {"generated": self.data.get("generated"), "season": self.data.get("season"),
                "picks": [p for p in picks if p["verdict"] != "dismiss"],
                "busy": self.busy, "error": self.error, "places": PLACES}

    def feedback(self, pid: str, verdict: str | None):
        p = self._pick(pid)
        if verdict is None:
            self.data["feedback"].pop(pid, None)
        else:
            if verdict not in ("up", "down", "dismiss"):
                raise ValueError("verdict")
            self.data["feedback"][pid] = {"verdict": verdict, "artist": p["artist"], "album": p["album"],
                                          "at": datetime.now().isoformat(timespec="minutes")}
        self.save()

    async def play(self, pid: str, place: str):
        p = self._pick(pid)
        if place not in PLACES or not p.get("uri"):
            raise ValueError("place")
        await self.house.ha_call("script", "play_music",
                                 {"media": p["uri"], "media_type": "album", "levels": [place]})

    def due(self, now: datetime) -> bool:
        """New picks every Friday morning (new-release day), or whenever there are none."""
        if not self.data["picks"]:
            return True
        last = datetime.fromisoformat(self.data["generated"]) if self.data.get("generated") else None
        friday = (now - timedelta(days=(now.weekday() - 4) % 7)).replace(hour=7, minute=0, second=0, microsecond=0)
        if friday > now:
            friday -= timedelta(days=7)
        return last is None or last < friday

    async def refresh(self):
        if self.busy:
            return
        self.busy, self.error = True, None
        try:
            await self._refresh()
        except Exception:
            log.exception("listen refresh failed")
            self.error = "Couldn't put together new picks this time."
        finally:
            self.busy = False

    async def loop(self):
        await asyncio.sleep(60)
        while True:
            try:
                if self.due(datetime.now()) and not self.busy:
                    await self.refresh()
            except Exception:
                log.exception("listen loop")
            await asyncio.sleep(3600)

    # ------------------------------------------------------------ internals
    def _pick(self, pid: str) -> dict:
        for p in self.data["picks"]:
            if p["id"] == pid:
                return p
        raise KeyError(pid)

    async def _service(self, domain: str, service: str, data: dict, timeout: float = 30):
        h = self.house
        async with h.session.post(f"http://supervisor/core/api/services/{domain}/{service}?return_response",
                                  headers=h.hdr(), data=json.dumps(data),
                                  timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            r.raise_for_status()
            return (await r.json()).get("service_response") or {}

    async def _ma_entry(self) -> str:
        return (await self.house.template(
            "{{ config_entry_id(integration_entities('music_assistant') | select('match','media_player') | first) }}")).strip()

    async def _library(self, entry: str):
        arts = await self._service("music_assistant", "get_library",
                                   {"config_entry_id": entry, "media_type": "artist", "limit": 1000})
        albums = await self._service("music_assistant", "get_library",
                                     {"config_entry_id": entry, "media_type": "album", "limit": 40,
                                      "order_by": "random"})
        known = sorted({a["name"] for a in arts.get("items", []) if a.get("name")})
        sample = []
        for a in albums.get("items", []):
            names = ", ".join(x.get("name", "") for x in a.get("artists", []) if x.get("name"))
            sample.append({"artist": names, "album": a.get("name"), "uri": a.get("uri"),
                           "image": a.get("image")})
        return known, sample

    def _prompt(self, known, sample, now) -> str:
        fb = self.data["feedback"].values()
        liked = [f"{f['artist']} - {f['album']}" for f in fb if f["verdict"] == "up"][-30:]
        disliked = [f"{f['artist']} - {f['album']}" for f in fb if f["verdict"] == "down"][-30:]
        recent = [f"{h['artist']} - {h['album']}" for h in self.data["history"][-60:]]
        return f"""You are the record buyer for a two-person household of serious electronic music listeners. It is {now:%A, %B %-d, %Y} ({season(now)}).

Their taste, in their own library's terms:
{self.taste or "Electronic music across IDM, ambient, modern classical, experimental, melodic techno and downtempo."}

Pick SIX new albums or EPs released in roughly the last ten weeks (or due this week), found by searching the web for what these sources have recently reviewed or featured: {SOURCES}.
Rules for the six:
- None by an artist in the FOLLOWED list below (they already know them).
- Spread them across the taste lanes; at least one should suit the season and an evening at home.
- Prefer records a critic or label made a real case for. No generic "chill" compilations, no EDM-festival fare, and never call anything "house music" unless it literally is.
- Each must be streamable on Spotify.
- Skip anything in RECENTLY PICKED. Lean toward what LIKED suggests and away from what DISLIKED suggests.

Then pick ONE "second listen" from SAVED SAMPLE: a record they saved but may not have given a fair shot, that fits right now. Say why now.

FOLLOWED: {"; ".join(known)}
SAVED SAMPLE: {"; ".join(f"{s['artist']} - {s['album']}" for s in sample)}
RECENTLY PICKED: {"; ".join(recent) or "none"}
LIKED: {"; ".join(liked) or "none yet"}
DISLIKED: {"; ".join(disliked) or "none yet"}

Answer with ONLY a JSON array of 7 objects, the second listen last:
{{"kind": "new" | "second", "artist": str, "album": str, "label": str, "released": "YYYY-MM" or "", "lane": short lane name, "why": one sentence of at most 22 words in a critic's voice, specific about the sound, "source": publication or label that featured it, "source_url": a URL you actually visited or found}}"""

    async def _refresh(self):
        now = datetime.now()
        entry = await self._ma_entry()
        known, sample = await self._library(entry)
        res = await self._service("ai_task", "generate_data", {
            "task_name": "listen picks", "entity_id": self.ai,
            "instructions": self._prompt(known, sample, now),
            "structure": {"picks_json": {"description": "The JSON array, nothing else", "required": True,
                                         "selector": {"text": {"multiline": True}}}},
        }, timeout=300)
        raw = (res.get("data") or {}).get("picks_json", "")
        m = re.search(r"\[.*\]", raw, re.S)
        items = json.loads(m.group(0)) if m else []
        picks = []
        for it in items:
            if not isinstance(it, dict) or not it.get("artist") or not it.get("album"):
                continue
            p = {k: str(it.get(k) or "")[:300] for k in
                 ("kind", "artist", "album", "label", "released", "lane", "why", "source", "source_url")}
            if not p["source_url"].startswith("http"):
                p["source_url"] = ""
            p["kind"] = "second" if p["kind"] == "second" else "new"
            if p["kind"] == "new" and any(_norm(p["artist"]) == _norm(k) for k in known):
                continue
            await self._match(entry, p, sample)
            if not p.get("uri"):
                log.info("not on Spotify, skipped: %s - %s", p["artist"], p["album"])
                continue
            p["id"] = uuid.uuid4().hex[:10]
            p["spotify_url"] = spotify_url(p["uri"], p["artist"], p["album"])
            picks.append(p)
        if not [p for p in picks if p["kind"] == "new"]:
            raise RuntimeError("no playable picks")
        self.data["picks"] = picks
        self.data["generated"] = now.isoformat(timespec="minutes")
        self.data["season"] = season(now)
        self.data["history"] = (self.data["history"] + [
            {"artist": p["artist"], "album": p["album"], "at": now.date().isoformat()} for p in picks])[-200:]
        self.save()
        log.info("listen: %d picks", len(picks))

    async def _match(self, entry: str, p: dict, sample: list[dict]):
        if p["kind"] == "second":
            for s in sample:
                if _norm(s["album"]) == _norm(p["album"]):
                    p["uri"], p["image"] = s["uri"], s["image"]
                    return
        res = await self._service("music_assistant", "search", {
            "config_entry_id": entry, "name": p["album"], "artist": p["artist"],
            "media_type": ["album"], "limit": 10})
        for a in res.get("albums", []):
            names = [x.get("name", "") for x in a.get("artists", [])]
            if artist_matches(p["artist"], names) and (_norm(p["album"]) in _norm(a.get("name", ""))
                                                       or _norm(a.get("name", "")) in _norm(p["album"])):
                p["uri"], p["image"] = a.get("uri"), a.get("image")
                return
