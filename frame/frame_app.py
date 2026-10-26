#!/usr/bin/env python3
"""
Frame — a small local web app for a Samsung Frame TV hung in portrait.

  python3 frame_app.py

Then open the address it prints, on this computer or your phone (same Wi-Fi).

What it does
  - Shows what's on the wall and everything stored on the TV
  - Click a picture to put it on the wall; select pictures to delete them
  - Add photos: drag to set the portrait crop, then hang them (2160x3840, no matte)
  - Prepare photos: crop, zoom and frame them (mats, bevels, frames) in the browser
  - Collections of pictures, and playing a collection on a timer
  - Optional "Keep it in Art Mode": checks every minute and pulls the TV back
    into Art Mode when Samsung's software switches it to TV. Every pull-back
    is logged to frame_escapes.csv next to this file.

Needs: pip3 install samsungtvws pillow   (optional: pillow-heif for HEIC files)
Works with Python 3.9+.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import socket
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import urllib3
from PIL import Image, ImageOps

try:  # HEIC support is optional
    from pillow_heif import register_heif_opener  # type: ignore

    register_heif_opener()
except Exception:
    pass

from samsungtvws import SamsungTVWS

# ---------------------------------------------------------------- settings
HERE = os.path.dirname(os.path.abspath(__file__))
# Where state, cache and logs live. On Home Assistant this is the app's /data folder,
# which survives updates and restarts; on a laptop it's the folder this file is in.
DATA = os.environ.get("FRAME_DATA", HERE)
os.makedirs(DATA, exist_ok=True)


def _ha_option(key, default):
    """Read a setting from the Home Assistant app's options, if running as an app."""
    try:
        with open(os.path.join(DATA, "options.json")) as f:
            v = json.load(f).get(key)
        return v if v not in (None, "") else default
    except (OSError, ValueError):
        return default


TV_IP = os.environ.get("FRAME_IP") or _ha_option("tv_ip", "192.168.0.60")
PORT = int(os.environ.get("FRAME_PORT", "8800"))
WATCHDOG_EVERY = 60  # seconds
PORTRAIT = (2160, 3840)  # width x height for a portrait-mounted 4K Frame
MY_PHOTOS = "MY-C0002"  # the TV's category for your own uploads

TOKEN_FILE = os.path.join(DATA, "frame-token.txt")
STATE_FILE = os.path.join(DATA, "frame_app_state.json")
ESCAPE_LOG = os.path.join(DATA, "frame_escapes.csv")
CACHE = os.path.join(DATA, ".frame_cache")
SEED_STATE = os.path.join(HERE, "seed_state.json")  # first-run collections/settings, if present
if not os.path.exists(STATE_FILE) and os.path.exists(SEED_STATE):
    import shutil
    shutil.copyfile(SEED_STATE, STATE_FILE)
THUMBS = os.path.join(CACHE, "thumbs")
FULL = os.path.join(CACHE, "full")
# Originals (resized to a sensible size) and the frame/crop recipe used, so any picture
# can be reframed or recropped later without the source photo.
ORIGINALS = os.path.join(DATA, "originals")
RECIPES = os.path.join(DATA, "recipes")
for d in (THUMBS, FULL, ORIGINALS, RECIPES):
    os.makedirs(d, exist_ok=True)

# The TV uses a self-signed certificate on your own network; that's expected.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("frame")
try:
    from logging.handlers import RotatingFileHandler
    _fh = RotatingFileHandler(os.path.join(DATA, "frame_app.log"), maxBytes=1_000_000, backupCount=1)
    _fh.setFormatter(logging.Formatter("%(asctime)s  %(levelname)s  %(message)s"))
    log.addHandler(_fh)
except OSError:
    pass
logging.getLogger("samsungtvws").setLevel(logging.WARNING)

SAFE_ID = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


# ---------------------------------------------------------------- app state
class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.data = {"watchdog": True}
        try:
            with open(STATE_FILE) as f:
                self.data.update(json.load(f))
        except (OSError, ValueError):
            pass
        self.last_check = None  # type: str | None
        self.last_result = "Not checked yet"

    def get(self, key, default=None):
        with self.lock:
            return self.data.get(key, default)

    def set(self, key, value) -> None:
        with self.lock:
            self.data[key] = value
            with open(STATE_FILE, "w") as f:
                json.dump(self.data, f, indent=2)


STATE = State()


def log_escape(note: str) -> None:
    new = not os.path.exists(ESCAPE_LOG)
    with open(ESCAPE_LOG, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "what happened"])
        w.writerow([datetime.now().isoformat(timespec="seconds"), note])


def escape_counts() -> dict:
    today = datetime.now().date().isoformat()
    total = today_n = 0
    last = None
    try:
        with open(ESCAPE_LOG) as f:
            for row in csv.DictReader(f):
                total += 1
                if row["time"].startswith(today):
                    today_n += 1
                last = row["time"]
    except OSError:
        pass
    return {"today": today_n, "total": total, "last": last}


# ---------------------------------------------------------------- TV access
class TV:
    """One shared Art Mode connection, used by one caller at a time.

    The TV only likes a single art connection, so every call goes through a
    lock. If the connection has gone stale (TV slept, Wi-Fi blip) the call is
    retried once on a fresh connection.
    """

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self._art = None

    def _remote(self, timeout: float = 20) -> SamsungTVWS:
        return SamsungTVWS(host=TV_IP, port=8002, token_file=TOKEN_FILE,
                           name="FrameController", timeout=timeout, key_press_delay=0)

    def _reset(self) -> None:
        if self._art is not None:
            try:
                self._art.close()
            except Exception:
                pass
        self._art = None

    def call(self, fn):
        with self.lock:
            for attempt in (1, 2):
                if self._art is None:
                    self._art = self._remote().art()
                try:
                    return fn(self._art)
                except Exception:
                    self._reset()
                    if attempt == 2:
                        raise

    def power(self) -> str:
        """'on', 'standby', or 'unreachable'. Uses plain HTTP, no art socket."""
        try:
            info = self._remote(timeout=4).rest_device_info().get("device", {})
            return str(info.get("PowerState", "on")).lower()
        except Exception:
            return "unreachable"


TVC = TV()


def artmode_is_on(value) -> bool:
    return str(value).lower() == "on"


# ---------------------------------------------------------------- images
def prepare(data: bytes, fx: float, fy: float) -> bytes:
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img).convert("RGB")
    img = ImageOps.fit(img, PORTRAIT, Image.LANCZOS, centering=(fx, fy))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=92)
    return out.getvalue()


def small(jpeg: bytes) -> bytes:
    img = Image.open(io.BytesIO(jpeg))
    img.thumbnail((540, 960), Image.LANCZOS)
    out = io.BytesIO()
    img.convert("RGB").save(out, "JPEG", quality=85)
    return out.getvalue()


def cache_path(folder: str, cid: str) -> str:
    return os.path.join(folder, cid + ".img")


def read_cache(folder: str, cid: str):
    try:
        with open(cache_path(folder, cid), "rb") as f:
            return f.read()
    except OSError:
        return None


def write_cache(folder: str, cid: str, data: bytes) -> None:
    with open(cache_path(folder, cid), "wb") as f:
        f.write(data)


def cache_version(cid: str) -> int:
    """Changes whenever a cached picture changes, so browsers don't keep a stale copy."""
    v = 0
    for folder in (THUMBS, FULL):
        try:
            v = max(v, int(os.path.getmtime(cache_path(folder, cid))))
        except OSError:
            pass
    return v


def import_cache(kind: str, cid: str, data: bytes) -> dict:
    """Accept a cached picture copied from another install (e.g. the laptop version)."""
    if kind not in ("thumbs", "full") or not SAFE_ID.match(cid):
        raise ValueError("Unknown picture")
    try:
        Image.open(io.BytesIO(data)).verify()
    except Exception:
        raise ValueError("Not an image")
    if kind == "full":
        write_cache(FULL, cid, data)
        write_cache(THUMBS, cid, small(data))  # portrait thumbnail made from the original
    elif not os.path.exists(cache_path(FULL, cid)):
        write_cache(THUMBS, cid, data)
    THUMB_FAILS.pop(cid, None)
    return {"ok": True, "id": cid, "kind": kind}


def drop_cache(cid: str) -> None:
    for folder in (THUMBS, FULL):
        try:
            os.remove(cache_path(folder, cid))
        except OSError:
            pass


def original_path(cid: str) -> str:
    return os.path.join(ORIGINALS, cid + ".jpg")


def recipe_path(cid: str) -> str:
    return os.path.join(RECIPES, cid + ".json")


def read_recipe(cid: str):
    try:
        with open(recipe_path(cid)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


RECIPE_KEYS = {"style": str, "mat": float, "fit": str, "zoom": float, "fx": float, "fy": float,
               "turn": int, "tilt": float}


def clean_recipe(r) -> dict:
    out = {}
    if isinstance(r, dict):
        for k, t in RECIPE_KEYS.items():
            if k in r:
                try:
                    out[k] = t(r[k]) if t is not str else str(r[k])[:32]
                except (TypeError, ValueError):
                    pass
    return out


def save_original(cid: str, data: bytes) -> dict:
    """Keep a sensibly sized JPEG of the source photo (long edge up to 4096 px)."""
    if not SAFE_ID.match(cid):
        raise ValueError("Unknown picture")
    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)
    except Exception:
        raise ValueError("Not an image")
    if max(img.size) > 4096:
        img.thumbnail((4096, 4096), Image.LANCZOS)
    img.convert("RGB").save(original_path(cid), "JPEG", quality=90)
    return {"ok": True, "id": cid, "size": list(img.size)}


def forget_original(cid: str) -> None:
    for path in (original_path(cid), recipe_path(cid)):
        try:
            os.remove(path)
        except OSError:
            pass


THUMB_FAILS = {}  # content_id -> time of last failure
THUMB_LOCK = threading.Lock()


def fetch_thumb(cid: str) -> bytes:
    """Thumbnail bytes for one picture, cached on disk.

    Uses its own short-timeout connection so a slow thumbnail can never hold
    up status checks or the watchdog. Failures are remembered for 5 minutes.
    """
    cached = read_cache(THUMBS, cid)
    if cached:
        return cached
    full = read_cache(FULL, cid)
    if full:
        data = small(full)
        write_cache(THUMBS, cid, data)
        return data
    if time.time() - THUMB_FAILS.get(cid, 0) < 300:
        raise RuntimeError("Thumbnail unavailable")
    with THUMB_LOCK:
        cached = read_cache(THUMBS, cid)
        if cached:
            return cached
        box = {}

        def run():
            art = None
            try:
                art = SamsungTVWS(host=TV_IP, port=8002, token_file=TOKEN_FILE,
                                  name="FrameController", timeout=8, key_press_delay=0).art()
                # Art API 4.x only answers get_thumbnail_list; get_thumbnail gets no reply.
                found = art.get_thumbnail_list([cid])
                box["data"] = next(iter(found.values()), None)
            except Exception as e:
                box["err"] = e
            finally:
                if art is not None:
                    try:
                        art.close()
                    except Exception:
                        pass

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(15)
        data = box.get("data")
        if worker.is_alive() or not data:
            THUMB_FAILS[cid] = time.time()
            why = "no answer from the TV" if worker.is_alive() else box.get("err", "empty reply")
            log.info("Thumbnail %s unavailable: %s", cid, why)
            raise RuntimeError("Thumbnail unavailable")
        data = bytes(data)
    write_cache(THUMBS, cid, data)
    return data


def image_type(data: bytes) -> str:
    return "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"


# ---------------------------------------------------------------- watchdog
def watchdog_loop() -> None:
    while True:
        time.sleep(WATCHDOG_EVERY)
        if not STATE.get("watchdog"):
            continue
        STATE.last_check = datetime.now().isoformat(timespec="seconds")
        power = TVC.power()
        if power != "on":
            # Turned off on purpose, asleep, or off the network: leave it alone.
            STATE.last_result = "TV is %s" % power
            continue
        try:
            mode = TVC.call(lambda a: a.get_artmode())
            if artmode_is_on(mode):
                STATE.last_result = "In Art Mode"
                continue
            TVC.call(lambda a: a.set_artmode(True))
            log_escape("TV left Art Mode; switched it back")
            STATE.last_result = "Pulled back into Art Mode"
            log.info("Watchdog: TV had left Art Mode, switched it back")
        except Exception as e:
            STATE.last_result = "Check failed: %s" % e
            log.info("Watchdog check failed: %s", e)


# ---------------------------------------------------------------- API
def api_status() -> dict:
    power = TVC.power()
    out = {
        "power": power,
        "artmode": None,
        "current": None,
        "watchdog": bool(STATE.get("watchdog")),
        "last_check": STATE.last_check,
        "last_result": STATE.last_result,
        "escapes": escape_counts(),
    }
    if power == "on":
        def read(art):
            return art.get_artmode(), art.get_current()

        mode, current = TVC.call(read)
        out["artmode"] = artmode_is_on(mode)
        out["current"] = (current or {}).get("content_id")
    return out


def api_library() -> dict:
    items = TVC.call(lambda a: a.available())
    LIB_CACHE["mine"] = list(dict.fromkeys(i["content_id"] for i in items
                                           if str(i.get("content_id", "")).upper().startswith("MY")))
    LIB_CACHE["t"] = time.time()
    mine, samsung, seen = [], [], set()
    for it in items:
        cid = it.get("content_id")
        if not cid or not SAFE_ID.match(cid) or cid in seen:
            continue  # the TV lists a picture once per group it's in
        seen.add(cid)
        entry = {
            "id": cid,
            "date": it.get("image_date"),
            "w": it.get("width"),
            "h": it.get("height"),
            "full": os.path.exists(cache_path(FULL, cid)),
            "v": cache_version(cid),
            "orig": os.path.exists(original_path(cid)),
        }
        is_mine = it.get("category_id") == MY_PHOTOS or cid.upper().startswith("MY")
        (mine if is_mine else samsung).append(entry)
    mine.sort(key=lambda e: e["id"], reverse=True)  # newest uploads first
    return {"mine": mine, "samsung": samsung, "collections": collections()}


def api_show(cid: str) -> dict:
    def go(art):
        art.select_image(cid, show=True)
        if not artmode_is_on(art.get_artmode()):
            art.set_artmode(True)

    TVC.call(go)
    return {"ok": True}


def api_upload(body: bytes, fx: float, fy: float, show: bool, prepared: bool = False,
               collection: str = "", recipe=None, replace: str = "") -> dict:
    jpeg = body
    if prepared:
        img = Image.open(io.BytesIO(body))
        if img.size != PORTRAIT or img.format != "JPEG":
            jpeg = prepare(body, 0.5, 0.5)
    else:
        jpeg = prepare(body, fx, fy)

    def go(art):
        cid = art.upload(jpeg, file_type="JPEG", matte="none", portrait_matte="none")
        if show:
            art.select_image(cid, show=True)
        return cid

    cid = TVC.call(go)
    if cid and SAFE_ID.match(cid):
        write_cache(FULL, cid, jpeg)
        write_cache(THUMBS, cid, small(jpeg))
    if cid and SAFE_ID.match(cid):
        if recipe:
            with open(recipe_path(cid), "w") as f:
                json.dump(clean_recipe(recipe), f)
        if replace and SAFE_ID.match(replace) and replace != cid:
            swap_picture(replace, cid)
    if collection and cid and collection in collections():
        api_collections({"action": "add", "id": collection, "ids": [cid]})
    LIB_CACHE["t"] = 0
    log.info("Hung %s (%d KB)%s", cid, len(jpeg) // 1024, " replacing %s" % replace if replace else "")
    return {"ok": True, "id": cid}


def swap_picture(old: str, new: str) -> None:
    """A reframed picture replaces the old one: same original, same place in collections."""
    if os.path.exists(original_path(old)) and not os.path.exists(original_path(new)):
        os.replace(original_path(old), original_path(new))
    c = collections()
    for col in c.values():
        if old in col["items"]:
            col["items"] = list(dict.fromkeys(new if i == old else i for i in col["items"]))
    save_collections(c)
    try:
        TVC.call(lambda a: a.delete_list([old]))
    except Exception as e:
        log.info("Couldn't remove the old version %s from the TV: %s", old, e)
    drop_cache(old)
    forget_original(old)


def api_convert(body: bytes) -> bytes:
    """Turn a photo the browser can't open (e.g. HEIC in Chrome) into a JPEG."""
    try:
        img = Image.open(io.BytesIO(body))
    except Exception:
        raise ValueError("This app can't open that file. For iPhone HEIC photos run: "
                         "pip3 install pillow-heif   then restart the app.")
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((5000, 5000), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=94)
    return out.getvalue()


def api_delete(ids: list) -> dict:
    ids = [i for i in ids if isinstance(i, str) and SAFE_ID.match(i)]
    if not ids:
        return {"ok": True, "deleted": 0}
    TVC.call(lambda a: a.delete_list(ids))
    for cid in ids:
        drop_cache(cid)
        forget_original(cid)
    forget_pictures(ids)
    LIB_CACHE["t"] = 0
    log.info("Deleted %s", ", ".join(ids))
    return {"ok": True, "deleted": len(ids)}


def api_artmode(on: bool) -> dict:
    TVC.call(lambda a: a.set_artmode(on))
    return {"ok": True}


# ---------------------------------------------------------------- one-off calls
def oneshot(fn, limit: float = 10):
    """Run fn(art) on its own short-lived connection with a hard time limit.

    Some requests get no reply at all on Art API 4.x; this keeps those from
    freezing anything else.
    """
    box = {}

    def run():
        art = None
        try:
            art = SamsungTVWS(host=TV_IP, port=8002, token_file=TOKEN_FILE,
                              name="FrameController", timeout=8, key_press_delay=0).art()
            box["r"] = fn(art)
        except Exception as e:
            box["e"] = e
        finally:
            if art is not None:
                try:
                    art.close()
                except Exception:
                    pass

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(limit)
    if t.is_alive():
        raise TimeoutError("The Frame didn't answer")
    if "e" in box:
        raise box["e"]
    return box.get("r")


# ---------------------------------------------------------------- collections
def collections() -> dict:
    return STATE.get("collections", {}) or {}


def save_collections(c: dict) -> None:
    STATE.set("collections", c)


def api_collections(p: dict) -> dict:
    c = collections()
    action = p.get("action")
    cid = str(p.get("id", ""))
    ids = [i for i in p.get("ids", []) if isinstance(i, str) and SAFE_ID.match(i)]
    if action == "create":
        name = str(p.get("name", "")).strip()[:60] or "Untitled"
        cid = "c%d" % int(time.time() * 1000)
        c[cid] = {"name": name, "items": ids}
    elif cid not in c:
        raise ValueError("That collection doesn't exist anymore.")
    elif action == "rename":
        c[cid]["name"] = str(p.get("name", "")).strip()[:60] or c[cid]["name"]
    elif action == "delete":
        c.pop(cid)
        playing = STATE.get("playing")
        if playing and playing.get("target") == cid:
            stop_playing()
    elif action == "add":
        have = c[cid]["items"]
        c[cid]["items"] = have + [i for i in ids if i not in have]
    elif action == "remove":
        c[cid]["items"] = [i for i in c[cid]["items"] if i not in ids]
    save_collections(c)
    return {"ok": True, "id": cid, "collections": c}


def forget_pictures(ids: list) -> None:
    c = collections()
    for col in c.values():
        col["items"] = [i for i in col["items"] if i not in ids]
    save_collections(c)


# ---------------------------------------------------------------- playing
TV_INTERVALS = [3, 15, 60, 720, 1440, 10080]  # what the Frame's own rotation offers
ROT_LOGGED = {"done": False}


def read_rotation():
    """What the TV says it's rotating, or None if it won't say."""
    for name in (STATE.get("rotation_get"), "get_slideshow_status", "get_auto_rotation_status"):
        if not name:
            continue
        try:
            r = oneshot(lambda a, n=name: getattr(a, n)(), 8)
        except Exception as e:
            log.info("Rotation: %s gave no answer (%s)", name, e)
            continue
        if not ROT_LOGGED["done"]:
            log.info("Rotation: %s replied %s", name, json.dumps(r)[:300])
            ROT_LOGGED["done"] = True
        if isinstance(r, dict) and ("value" in r or "status" in r):
            if STATE.get("rotation_get") != name:
                STATE.set("rotation_get", name)
            return {"value": str(r.get("value", r.get("status", "off"))),
                    "category": r.get("category_id"), "type": r.get("type")}
    return None


def set_rotation(minutes: int, category_id: str, shuffle: bool) -> None:
    getter = STATE.get("rotation_get") or "get_slideshow_status"
    setter = getter.replace("get_", "set_", 1)
    oneshot(lambda a: getattr(a, setter)(duration=minutes, type=shuffle, category_id=category_id), 12)


def sync_favourites(ids: list) -> None:
    """Make the TV's Favourites match ids, so the TV can rotate them by itself."""
    before = set(STATE.get("favs_set", []) or [])
    want = set(ids)

    def go(art):
        for cid in before - want:
            art.set_favourite(cid, "off")
        for cid in ids:
            if cid not in before:
                art.set_favourite(cid, "on")

    oneshot(go, 30)
    STATE.set("favs_set", list(want))


def label_for(target: str) -> str:
    if target == "all":
        return "All your photos"
    col = collections().get(target)
    return col["name"] if col else "A deleted collection"


def api_play(p: dict) -> dict:
    target = str(p.get("target", "all"))
    minutes = int(p.get("minutes", 15))
    shuffle = bool(p.get("shuffle", True))
    if minutes < 1:
        raise ValueError("Pick how often the picture should change.")
    if target != "all":
        col = collections().get(target)
        if not col or not col["items"]:
            raise ValueError("That collection is empty. Add some pictures first.")
    on_tv = minutes in TV_INTERVALS
    note = ""
    if on_tv:
        try:
            if target == "all":
                set_rotation(minutes, MY_PHOTOS, shuffle)
            else:
                ids = collections()[target]["items"]
                sync_favourites(ids)
                set_rotation(minutes, "MY-C0004", shuffle)
                try:
                    oneshot(lambda a: a.select_image(ids[0], show=True), 10)
                except Exception:
                    pass
        except Exception as e:
            log.error("The TV wouldn't take the rotation for %s; playing from this app instead",
                      label_for(target), exc_info=e)
            on_tv = False
            note = "The TV wouldn't run this by itself, so it's playing from this app. Keep the app running."
    if not on_tv:
        try:
            set_rotation(0, MY_PHOTOS, shuffle)  # the app takes over; TV rotation off
        except Exception as e:
            log.info("Couldn't switch off the TV's own rotation: %s", e)
    STATE.set("playing", {"target": target, "minutes": minutes, "shuffle": shuffle,
                          "mode": "tv" if on_tv else "app", "next_at": 0, "last": None})
    log.info("Playing %s every %d min (%s)", label_for(target), minutes, "on the TV" if on_tv else "from this app")
    return {"ok": True, "mode": "tv" if on_tv else "app", "note": note}


def stop_playing() -> None:
    STATE.set("playing", None)
    set_rotation(0, MY_PHOTOS, True)


def api_rotation() -> dict:
    playing = STATE.get("playing")
    out = {"playing": None, "tv": read_rotation(), "intervals": TV_INTERVALS}
    if playing:
        out["playing"] = dict(playing, label=label_for(playing["target"]))
    return out


LIB_CACHE = {"t": 0, "mine": []}


def my_photo_ids() -> list:
    if time.time() - LIB_CACHE["t"] > 300:
        items = TVC.call(lambda a: a.available())
        LIB_CACHE["mine"] = list(dict.fromkeys(i["content_id"] for i in items
                                               if str(i.get("content_id", "")).upper().startswith("MY")))
        LIB_CACHE["t"] = time.time()
    return LIB_CACHE["mine"]


def player_loop() -> None:
    import random
    while True:
        time.sleep(10)
        p = STATE.get("playing")
        if not p or p.get("mode") != "app" or time.time() < p.get("next_at", 0):
            continue
        try:
            if TVC.power() != "on":
                continue
            pool = my_photo_ids() if p["target"] == "all" else collections().get(p["target"], {}).get("items", [])
            if not pool:
                continue
            last = p.get("last")
            if p.get("shuffle"):
                choices = [i for i in pool if i != last] or pool
                nxt = random.choice(choices)
            else:
                nxt = pool[(pool.index(last) + 1) % len(pool)] if last in pool else pool[0]

            def go(art):
                art.select_image(nxt, show=artmode_is_on(art.get_artmode()))

            TVC.call(go)
            p.update(last=nxt, next_at=time.time() + p["minutes"] * 60)
            STATE.set("playing", p)
            log.info("Now showing %s (%s)", nxt, label_for(p["target"]))
        except Exception as e:
            log.info("Player: couldn't change the picture (%s)", e)


# ---------------------------------------------------------------- display (brightness etc.)
def read_display() -> dict:
    """The Frame's Art Mode picture settings, as {item: {value, min, max}}."""
    r = oneshot(lambda a: a.get_artmode_settings(), 10)
    items = r.get("data") if isinstance(r, dict) else None
    if isinstance(items, str):
        items = json.loads(items)
    out = {}
    for it in items or []:
        if isinstance(it, dict) and it.get("item"):
            out[it["item"]] = {k: it.get(k) for k in ("value", "min", "max")}
    return out


def api_display() -> dict:
    d = read_display()
    num = lambda k, lo, hi: {"value": int(d[k]["value"]), "min": int(d[k].get("min") or lo),
                             "max": int(d[k].get("max") or hi)} if k in d else None
    sensor = d.get("brightness_sensor_setting", {}).get("value")
    return {"brightness": num("brightness", 0, 10),
            "warmth": num("color_temperature", -5, 5),
            "sensor": None if sensor is None else str(sensor).lower() == "on"}


def api_display_set(p: dict) -> dict:
    """Apply changes, then read back what the TV actually has."""
    wanted = {}
    if "brightness" in p:
        v = int(p["brightness"]); wanted["brightness"] = v
        try:
            oneshot(lambda a: a.set_brightness(str(v)), 8)
        except Exception as e:
            log.info("Brightness: no confirmation from the TV (%s)", e)
    if "warmth" in p:
        v = int(p["warmth"]); wanted["warmth"] = v
        try:
            oneshot(lambda a: a.set_color_temperature(str(v)), 8)
        except Exception as e:
            log.info("Warmth: no confirmation from the TV (%s)", e)
    if "sensor" in p:
        v = bool(p["sensor"]); wanted["sensor"] = v
        try:
            oneshot(lambda a: a.set_brightness_sensor_setting(v), 8)
        except Exception as e:
            log.info("Light sensor: no confirmation from the TV (%s)", e)
    now = api_display()
    def has(k):
        cur = now.get(k)
        return cur.get("value") if isinstance(cur, dict) else cur
    missed = [k for k, v in wanted.items() if has(k) != v]
    if missed:
        log.error("The Frame didn't take: %s (wanted %s, has %s)", missed, wanted, now)
    return dict(now, missed=missed)


# ---------------------------------------------------------------- sunrise / sunset
HOME_LAT, HOME_LON = 43.19306, -88.20667  # Colgate, WI 53017


def sun_times(day=None, lat=HOME_LAT, lon=HOME_LON):
    """Local (sunrise, sunset) datetimes for a date, computed offline.

    Standard almanac algorithm (zenith 90.833 deg: sun's upper edge at the
    horizon, with refraction). Accurate to about a minute.
    """
    import math
    from datetime import date as _date, datetime as _dt, timedelta, timezone
    day = day or _date.today()
    rad, deg = math.radians, math.degrees
    n = day.timetuple().tm_yday
    lng_hour = lon / 15.0
    out = []
    for rising in (True, False):
        t = n + ((6 if rising else 18) - lng_hour) / 24
        m = 0.9856 * t - 3.289
        l = (m + 1.916 * math.sin(rad(m)) + 0.020 * math.sin(rad(2 * m)) + 282.634) % 360
        ra = deg(math.atan(0.91764 * math.tan(rad(l)))) % 360
        ra = (ra + (math.floor(l / 90) * 90 - math.floor(ra / 90) * 90)) / 15
        sin_dec = 0.39782 * math.sin(rad(l))
        cos_dec = math.cos(math.asin(sin_dec))
        cos_h = (math.cos(rad(90.833)) - sin_dec * math.sin(rad(lat))) / (cos_dec * math.cos(rad(lat)))
        cos_h = max(-1.0, min(1.0, cos_h))
        h = (360 - deg(math.acos(cos_h))) if rising else deg(math.acos(cos_h))
        h /= 15
        local_mean = h + ra - 0.06571 * t - 6.622
        ut = (local_mean - lng_hour) % 24
        moment = _dt(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(hours=ut)
        out.append(moment.astimezone().replace(tzinfo=None))
    return out[0], out[1]


# ---------------------------------------------------------------- brightness schedule (editable time slots)
# Each slot says: from this moment of the day, set the Frame like this. Slots are anchored
# to sunrise, sunset (with an offset) or a clock time, so they move with the seasons.
SLOT_ANCHORS = ("sunrise", "sunset", "time")
MAX_SLOTS = 12
SCHED_STATE = {"applied": None}
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _new_slot_id() -> str:
    return "s%d" % int(time.time() * 1000) + "%03d" % (int(time.perf_counter() * 1000) % 1000)


def _migrate_dim(d: dict) -> list:
    """Turn the old fixed day/evening/late-night settings into editable slots."""
    ev = d.get("evening_start", "sunset")
    off = int(ev.split("+")[1]) if "+" in ev else 0
    return [
        {"id": "s1", "at": "time", "time": d.get("quiet_end", "07:00"), "offset": 0,
         "brightness": int(d.get("day", 7)), "sensor": bool(d.get("sensor", True)), "warmth": None},
        {"id": "s2", "at": "sunset", "time": "18:00", "offset": off,
         "brightness": int(d.get("evening", 5)), "sensor": bool(d.get("sensor", True)), "warmth": None},
        {"id": "s3", "at": "time", "time": d.get("quiet_start", "22:00"), "offset": 0,
         "brightness": int(d.get("night", 1)), "sensor": False, "warmth": None},
    ]


def _clean_slot(s: dict) -> dict:
    at = s.get("at") if s.get("at") in SLOT_ANCHORS else "time"
    t = str(s.get("time") or "07:00")
    if not TIME_RE.match(t):
        t = "07:00"
    hh, mm = t.split(":")
    warmth = s.get("warmth")
    warmth = None if warmth in (None, "", "keep") else max(-5, min(5, int(warmth)))
    sid = str(s.get("id") or _new_slot_id())
    return {"id": sid if SAFE_ID.match(sid) else _new_slot_id(), "at": at, "time": "%02d:%s" % (int(hh), mm),
            "offset": max(-240, min(240, int(s.get("offset") or 0))),
            "brightness": max(0, min(10, int(s.get("brightness", 5)))),
            "sensor": bool(s.get("sensor", False)), "warmth": warmth}


def sched_settings() -> dict:
    saved = STATE.get("schedule")
    if not saved:  # first run after the update: carry the old schedule over
        old = STATE.get("dim") or {}
        saved = {"on": bool(old.get("on", False)), "slots": _migrate_dim(old)}
    slots = [_clean_slot(s) for s in (saved.get("slots") or [])][:MAX_SLOTS]
    return {"on": bool(saved.get("on")), "slots": slots}


def slot_moment(slot: dict, day, sun=None):
    from datetime import datetime as _dt, timedelta
    if slot["at"] == "time":
        hh, mm = slot["time"].split(":")
        return _dt(day.year, day.month, day.day, int(hh), int(mm))
    rise, sett = sun or sun_times(day)
    return (rise if slot["at"] == "sunrise" else sett) + timedelta(minutes=slot["offset"])


def sched_now(now=None) -> dict:
    """Which slot is in effect now, and when the next one starts."""
    from datetime import datetime as _dt, timedelta
    d = sched_settings()
    now = now or _dt.now()
    today = now.date()
    sun = sun_times(today)
    timed = sorted(((slot_moment(s, today, sun), s) for s in d["slots"]), key=lambda x: x[0])
    current = nxt = None
    for t, s in timed:
        if t <= now:
            current = s
        elif nxt is None:
            nxt = t
    if timed and current is None:
        current = timed[-1][1]  # before the first slot: yesterday's last slot is still running
    if timed and nxt is None:
        tomorrow = today + timedelta(days=1)
        nxt = min(slot_moment(s, tomorrow) for s in d["slots"])
    return {"current": current, "next": nxt, "timed": timed, "sunrise": sun[0], "sunset": sun[1]}


def apply_schedule(force: bool = False) -> None:
    d = sched_settings()
    if not d["on"] or not d["slots"]:
        SCHED_STATE["applied"] = None
        return
    s = sched_now()["current"]
    target = (s["id"], s["brightness"], s["sensor"], s["warmth"])
    if not force and SCHED_STATE["applied"] == target:
        return
    if TVC.power() != "on":
        SCHED_STATE["applied"] = None
        return  # try again when the TV is back
    steps = [("light sensor", lambda a: a.set_brightness_sensor_setting(s["sensor"])),
             ("brightness", lambda a: a.set_brightness(str(s["brightness"])))]
    if s["warmth"] is not None:
        steps.append(("warmth", lambda a: a.set_color_temperature(str(s["warmth"]))))
    for name, fn in steps:
        try:
            oneshot(fn, 8)
        except Exception as e:
            log.info("Schedule: no confirmation for %s (%s)", name, e)
    try:
        has = api_display()
        ok = (has.get("brightness") or {}).get("value") == s["brightness"] and has.get("sensor") in (s["sensor"], None)
        if ok:
            SCHED_STATE["applied"] = target
            log.info("Schedule: %s, brightness %d, light sensor %s%s", slot_label(s), s["brightness"],
                     "on" if s["sensor"] else "off",
                     "" if s["warmth"] is None else ", warmth %d" % s["warmth"])
        else:
            log.error("Schedule: wanted %s, the Frame has %s", target, has)
    except Exception as e:
        log.info("Schedule: couldn't confirm (%s)", e)


def sched_loop() -> None:
    while True:
        time.sleep(60)
        try:
            apply_schedule()
        except Exception as e:
            log.info("Schedule check failed: %s", e)


def _clock(t) -> str:
    return t.strftime("%I:%M %p").lstrip("0")


def slot_label(s: dict) -> str:
    if s["at"] == "time":
        hh, mm = s["time"].split(":")
        from datetime import datetime as _dt
        return _clock(_dt(2000, 1, 1, int(hh), int(mm)))
    base = "Sunrise" if s["at"] == "sunrise" else "Sunset"
    if not s["offset"]:
        return base
    m = abs(s["offset"])
    span = "%d hr" % (m // 60) if m >= 60 and m % 60 == 0 else "%d min" % m
    return "%s %s %s" % (span, "before" if s["offset"] < 0 else "after", base.lower())


def api_schedule_get() -> dict:
    d = sched_settings()
    sc = sched_now()
    cur = sc["current"]["id"] if sc["current"] else None
    slots = [dict(s, label=slot_label(s), today=_clock(t), current=(s["id"] == cur)) for t, s in sc["timed"]]
    return {"on": d["on"], "slots": slots, "current": cur,
            "next": _clock(sc["next"]) if sc["next"] else None,
            "sunrise": _clock(sc["sunrise"]), "sunset": _clock(sc["sunset"]), "max": MAX_SLOTS}


def api_schedule_set(p: dict) -> dict:
    d = sched_settings()
    if "on" in p:
        d["on"] = bool(p["on"])
    if isinstance(p.get("slots"), list):
        if len(p["slots"]) > MAX_SLOTS:
            raise ValueError("Up to %d times, please." % MAX_SLOTS)
        d["slots"] = [_clean_slot(s) for s in p["slots"] if isinstance(s, dict)]
    STATE.set("schedule", d)
    apply_schedule(force=True)
    return api_schedule_get()


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "Frame/1"

    def log_message(self, fmt, *args):  # keep the terminal quiet
        pass

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def send_json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, data: bytes, ctype: str, cache: bool = False) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def fail(self, e: Exception) -> None:
        if isinstance(e, (BrokenPipeError, ConnectionResetError)):
            return  # the browser went away (reload or closed tab); nothing to report
        msg = str(e) or e.__class__.__name__
        if isinstance(e, TimeoutError) or "Time out" in msg or "timed out" in msg:
            msg = "The Frame didn't answer in time. Try again in a moment."
        elif isinstance(e, (OSError, ConnectionError)):
            msg = "Can't reach the Frame at %s. Check that it's plugged in and on Wi-Fi." % TV_IP
        log.error("Error on %s: %s", self.path, msg, exc_info=e)
        self.send_json({"error": msg}, 502)

    def read_body(self, limit: int = 80 * 1024 * 1024) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ValueError("That file is too large (over 80 MB).")
        return self.rfile.read(n)

    def do_GET(self):
        url = urlparse(self.path)
        try:
            if url.path in ("/", "/index.html"):
                return self.send_bytes(PAGE.encode(), "text/html; charset=utf-8")
            if url.path == "/api/status":
                return self.send_json(api_status())
            if url.path == "/api/library":
                return self.send_json(api_library())
            if url.path == "/api/rotation":
                return self.send_json(api_rotation())
            if url.path == "/api/display":
                return self.send_json(api_display())
            if url.path == "/api/schedule":
                return self.send_json(api_schedule_get())
            m = re.match(r"^/api/recipe/([A-Za-z0-9_\-]{1,64})$", url.path)
            if m:
                return self.send_json({"id": m.group(1), "recipe": read_recipe(m.group(1))})
            m = re.match(r"^/original/([A-Za-z0-9_\-]{1,64})$", url.path)
            if m:
                try:
                    with open(original_path(m.group(1)), "rb") as f:
                        return self.send_bytes(f.read(), "image/jpeg")
                except OSError:
                    return self.send_json({"error": "No original kept for that picture"}, 404)
            m = re.match(r"^/(thumb|full)/([A-Za-z0-9_\-]{1,64})$", url.path)
            if m:
                kind, cid = m.groups()
                data = read_cache(FULL, cid) if kind == "full" else None
                if data is None:
                    try:
                        data = fetch_thumb(cid)
                    except Exception:
                        return self.send_json({"error": "Thumbnail unavailable"}, 404)
                return self.send_bytes(data, image_type(data), cache=True)
            self.send_json({"error": "Not found"}, 404)
        except Exception as e:
            self.fail(e)

    def do_POST(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        try:
            if url.path == "/api/upload":
                fx = min(max(float(q.get("fx", ["0.5"])[0]), 0.0), 1.0)
                fy = min(max(float(q.get("fy", ["0.5"])[0]), 0.0), 1.0)
                show = q.get("show", ["0"])[0] == "1"
                prepared = q.get("prepared", ["0"])[0] == "1"
                collection = q.get("collection", [""])[0]
                replace = q.get("replace", [""])[0]
                try:
                    recipe = json.loads(q.get("recipe", [""])[0] or "null")
                except ValueError:
                    recipe = None
                body = self.read_body()
                try:
                    Image.open(io.BytesIO(body))
                except Exception:
                    return self.send_json({"error": "That file isn't an image this app can read. "
                                           "For iPhone HEIC photos, run: pip3 install pillow-heif"}, 400)
                return self.send_json(api_upload(body, fx, fy, show, prepared, collection, recipe, replace))
            if url.path == "/api/original":
                try:
                    return self.send_json(save_original(q.get("id", [""])[0], self.read_body(40 * 1024 * 1024)))
                except ValueError as e:
                    return self.send_json({"error": str(e)}, 400)
            if url.path == "/api/cache/import":
                kind = q.get("kind", [""])[0]
                cid = q.get("id", [""])[0]
                try:
                    return self.send_json(import_cache(kind, cid, self.read_body()))
                except ValueError as e:
                    return self.send_json({"error": str(e)}, 400)
            if url.path == "/api/convert":
                try:
                    return self.send_bytes(api_convert(self.read_body()), "image/jpeg")
                except ValueError as e:
                    return self.send_json({"error": str(e)}, 400)
            payload = json.loads(self.read_body(1024 * 1024) or b"{}")
            if url.path == "/api/show":
                cid = str(payload.get("id", ""))
                if not SAFE_ID.match(cid):
                    return self.send_json({"error": "Unknown picture"}, 400)
                return self.send_json(api_show(cid))
            if url.path == "/api/delete":
                return self.send_json(api_delete(list(payload.get("ids", []))))
            if url.path == "/api/collections":
                try:
                    return self.send_json(api_collections(payload))
                except ValueError as e:
                    return self.send_json({"error": str(e)}, 400)
            if url.path == "/api/play":
                try:
                    return self.send_json(api_play(payload))
                except ValueError as e:
                    return self.send_json({"error": str(e)}, 400)
            if url.path == "/api/display":
                return self.send_json(api_display_set(payload))
            if url.path == "/api/schedule":
                try:
                    return self.send_json(api_schedule_set(payload))
                except ValueError as e:
                    return self.send_json({"error": str(e)}, 400)
            if url.path == "/api/stop":
                stop_playing()
                return self.send_json({"ok": True})
            if url.path == "/api/artmode":
                return self.send_json(api_artmode(bool(payload.get("on", True))))
            if url.path == "/api/watchdog":
                STATE.set("watchdog", bool(payload.get("on")))
                return self.send_json({"ok": True})
            self.send_json({"error": "Not found"}, 404)
        except Exception as e:
            self.fail(e)


def lan_address() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((TV_IP, 9))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "localhost"


def main() -> None:
    if not os.path.exists(TOKEN_FILE):
        print("First connection: watch the TV and choose Allow when it asks.\n")
    threading.Thread(target=watchdog_loop, daemon=True).start()
    threading.Thread(target=player_loop, daemon=True).start()
    threading.Thread(target=sched_loop, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("Frame is running.")
    print("  On this computer:  http://localhost:%d" % PORT)
    print("  On your phone:     http://%s:%d" % (lan_address(), PORT))
    print("Keep this window open. Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


# ---------------------------------------------------------------- page
PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#23352f">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Frame">
<title>Frame</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Atkinson+Hyperlegible:wght@400;700&family=Young+Serif&display=swap" rel="stylesheet">
<style>
:root{
  --wall:#23352f; --wall-deep:#1b2925; --wall-lift:#2c423b;
  --plaster:#eceeea; --sage:#a6b8af; --brass:#cfab64; --brass-deep:#8f733a;
  --bezel:#141614; --danger:#f19a8f;
  --serif:"Young Serif",Georgia,serif;
  --sans:"Atkinson Hyperlegible",system-ui,-apple-system,sans-serif;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--wall);color:var(--plaster);font:16px/1.5 var(--sans);
  min-height:100vh;min-height:100dvh;padding:0 0 env(safe-area-inset-bottom)}
button{font:inherit;color:inherit;cursor:pointer}
:focus-visible{outline:2px solid var(--brass);outline-offset:3px}
.layout>*,.controls>*,details.panel,.play-row>*{min-width:0}
html,body{overflow-x:clip}
select,input{max-width:100%}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}

.layout{display:grid;grid-template-columns:minmax(260px,340px) 1fr;gap:56px;
  max-width:1240px;margin:0 auto;padding:40px 32px 64px}
.side{position:sticky;top:0;align-self:start;max-height:100vh;max-height:100dvh;overflow-y:auto;
  padding:32px 6px 32px 0;margin-top:-32px;scrollbar-width:thin}

/* the wall: picture light, bezel, matte screen */
.wall{position:relative;padding-top:34px;display:flex;justify-content:center}
.lamp{position:absolute;top:0;left:50%;transform:translateX(-50%);width:46%;height:8px;
  border-radius:4px 4px 8px 8px;background:var(--brass);box-shadow:0 2px 0 var(--brass-deep)}
.lamp::after{content:"";position:absolute;left:-30%;right:-30%;top:8px;height:230px;
  background:radial-gradient(ellipse 50% 100% at 50% 0,rgba(255,232,180,.17),rgba(255,232,180,0) 70%);
  pointer-events:none;transition:opacity .6s}
.wall.dark .lamp::after{opacity:0}
.tv{width:100%;max-width:300px;aspect-ratio:9/16;background:var(--bezel);padding:7px;
  box-shadow:0 18px 30px -12px rgba(0,0,0,.55)}
.screen{width:100%;height:100%;background:#2a2d2a;overflow:hidden;position:relative}
.screen img{width:100%;height:100%;object-fit:cover;display:block;transition:opacity .5s}
.screen .msg{position:absolute;inset:0;display:grid;place-items:center;padding:24px;
  text-align:center;color:var(--sage);font-size:14px}

.status{margin:22px 0 0;text-align:center}
.status h1{font:400 26px/1.2 var(--serif);margin:0;letter-spacing:.005em}
.status p{margin:6px 0 0;color:var(--sage);font-size:14px}
.status .act{margin-top:12px}

.controls{margin-top:30px;display:grid;gap:22px}
.row-label{font-size:14px;color:var(--sage);margin:0 0 8px}
.seg{display:flex;border:1px solid var(--wall-lift);border-radius:999px;padding:3px;gap:2px}
.seg button{flex:1;border:0;background:none;border-radius:999px;padding:7px 0;font-size:14px;color:var(--sage)}
.seg button[aria-pressed=true]{background:var(--plaster);color:var(--wall-deep);font-weight:700}
.switch{display:flex;gap:14px;align-items:flex-start;background:none;border:0;padding:0;text-align:left;width:100%}
.knob{flex:none;width:42px;height:24px;border-radius:12px;background:var(--wall-lift);position:relative;
  margin-top:1px;transition:background .2s}
.knob::after{content:"";position:absolute;top:3px;left:3px;width:18px;height:18px;border-radius:50%;
  background:var(--sage);transition:transform .2s,background .2s}
.switch[aria-checked=true] .knob{background:var(--brass)}
.switch[aria-checked=true] .knob::after{transform:translateX(18px);background:var(--wall-deep)}
.switch strong{display:block;font-weight:700}
.switch small{display:block;color:var(--sage);font-size:14px;margin-top:2px}

.btn{border:0;border-radius:999px;padding:10px 20px;font-weight:700;background:var(--plaster);color:var(--wall-deep)}
.btn.brass{background:var(--brass);color:#2a2110}
.btn.ghost{background:none;color:var(--plaster);box-shadow:inset 0 0 0 1px var(--wall-lift)}
.btn.danger{background:var(--danger);color:#3a120d}
.btn:disabled{opacity:.5;cursor:default}

/* library */
.head{display:flex;align-items:center;gap:16px;flex-wrap:wrap;margin-bottom:26px}
.tabs{display:flex;gap:26px;margin-right:auto}
.tabs button{background:none;border:0;padding:4px 0;font:400 22px/1.2 var(--serif);color:var(--sage);
  border-bottom:2px solid transparent}
.tabs button[aria-selected=true]{color:var(--plaster);border-bottom-color:var(--brass)}
.tabs .n{font:14px var(--sans);margin-left:6px;vertical-align:2px}
.drophint{color:var(--sage);font-size:14px}

.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(132px,1fr));gap:30px 22px;
  list-style:none;margin:0;padding:0}
.grid li{position:relative}
.pic{display:block;width:100%;aspect-ratio:9/16;border:0;padding:0;background:none;position:relative}
.pic img{width:100%;height:100%;object-fit:contain;display:block;filter:drop-shadow(0 8px 10px rgba(0,0,0,.35));
  opacity:0;transition:opacity .4s}
.pic img.in{opacity:1}
.pic.noprev{background:var(--wall-lift)}
.pic.noprev::after{content:attr(data-label);position:absolute;inset:0;display:grid;place-items:center;
  font-size:13px;color:var(--sage)}
.pic:hover img{filter:drop-shadow(0 12px 16px rgba(0,0,0,.45))}
.label{margin-top:10px;height:2px;background:transparent}
li.current .label{background:var(--brass)}
li.current .cap{display:block}
.cap{display:none;font-size:13px;color:var(--brass);margin-top:6px;text-align:center}
.tick{display:none;position:absolute;top:8px;right:8px;width:26px;height:26px;border-radius:50%;
  border:2px solid var(--plaster);background:rgba(27,41,37,.6);pointer-events:none}
.selecting .tick{display:block}
li.sel .tick{background:var(--brass);border-color:var(--brass)}
li.sel .tick::after{content:"";position:absolute;left:7px;top:3px;width:7px;height:12px;
  border:solid var(--wall-deep);border-width:0 2.5px 2.5px 0;transform:rotate(45deg)}
li.sel .pic img{opacity:.55}
.empty{color:var(--sage);padding:40px 0;max-width:36ch}

/* editor: prepare photos before hanging */
.editor{position:fixed;inset:0;z-index:20;background:var(--wall-deep);display:none;
  grid-template-columns:1fr minmax(320px,380px);overflow:hidden}
.editor.open{display:grid}
.ed-stage{position:relative;display:flex;flex-direction:column;align-items:center;justify-content:center;
  padding:28px 24px 18px;min-height:0}
.ed-wrap{height:min(calc(100vh - 110px),1100px);aspect-ratio:9/16;max-width:100%;background:var(--bezel);
  padding:9px;box-shadow:0 24px 40px -18px rgba(0,0,0,.7);position:relative}
.ed-wrap canvas{width:100%;height:100%;display:block;touch-action:none;cursor:grab}
.ed-wrap canvas:active{cursor:grabbing}
.ed-wrap .loading{position:absolute;inset:9px;display:grid;place-items:center;color:var(--sage);font-size:14px;
  background:#2a2d2a;text-align:center;padding:20px}
.ed-hint{margin:14px 0 0;color:var(--sage);font-size:14px;text-align:center}
.ed-panel{background:var(--wall);border-left:1px solid var(--wall-lift);display:flex;flex-direction:column;min-height:0}
.ed-scroll{flex:1;overflow-y:auto;padding:28px 24px 12px;display:grid;gap:26px;align-content:start}
.ed-panel h2{font:400 26px/1.2 var(--serif);margin:0}
.ed-panel h2 small{font:14px var(--sans);color:var(--sage);margin-left:8px}
.styles{display:grid;grid-template-columns:repeat(4,1fr);gap:14px 10px}
.style{background:none;border:0;padding:0;text-align:center;font-size:13px;color:var(--sage);
  display:flex;flex-direction:column;justify-content:flex-start}
.style canvas{width:100%;aspect-ratio:9/16;display:block;background:#2a2d2a;
  box-shadow:0 0 0 1px var(--wall-lift);transition:box-shadow .15s}
.style span{display:block;margin-top:6px;line-height:1.25}
.style[aria-checked=true]{color:var(--plaster)}
.style[aria-checked=true] canvas{box-shadow:0 0 0 2px var(--brass)}
.field label{display:flex;justify-content:space-between;font-size:14px;color:var(--sage);margin-bottom:8px}
.field output{color:var(--plaster)}
input[type=range]{width:100%;accent-color:var(--brass)}
.field[hidden]{display:none}
.queue{display:flex;gap:10px;overflow-x:auto;padding:4px 2px 6px}
.queue button{flex:none;width:52px;border:0;padding:0;background:none;position:relative}
.queue canvas{width:52px;aspect-ratio:9/16;display:block;box-shadow:0 0 0 1px var(--wall-lift)}
.queue button[aria-current=true] canvas{box-shadow:0 0 0 2px var(--brass)}
.queue button.done::after{content:"";position:absolute;inset:0;background:rgba(207,171,100,.35)}
.ed-bar{display:flex;gap:10px;align-items:center;padding:16px 24px calc(18px + env(safe-area-inset-bottom));
  border-top:1px solid var(--wall-lift)}
.ed-bar .note{margin-right:auto;color:var(--sage);font-size:14px}
.ed-bar .btn{white-space:nowrap}
.rot-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
.rot-actions:first-child{margin-top:0}
.queue-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
.btn.small{padding:7px 14px;font-size:14px}
.rot-now{margin:8px 0 0;font-size:13px;color:var(--sage)}
@media (max-width:860px){
  .editor{grid-template-columns:1fr;grid-template-rows:auto 1fr;overflow-y:auto}
  .ed-stage{padding:16px 16px 6px}
  .ed-wrap{height:auto;width:min(72vw,340px)}
  .ed-panel{border-left:0;border-top:1px solid var(--wall-lift)}
  .ed-scroll{overflow:visible}
  .ed-bar{position:sticky;bottom:0;background:var(--wall)}
}

.play{display:grid;gap:12px}
.display{display:grid;gap:16px}
.display .field label{margin-bottom:4px}
.night{display:grid;gap:14px;padding-left:56px}
.night[hidden]{display:none}
.switch:disabled{opacity:.5;cursor:default}
.switch[hidden],.field[hidden]{display:none}
.play-row{display:grid;grid-template-columns:auto minmax(0,1fr);gap:10px 12px;align-items:center;font-size:14px;color:var(--sage)}
select{font:inherit;color:var(--plaster);background:var(--wall-lift);border:0;border-radius:8px;padding:8px 10px;width:100%}
.check{display:flex;gap:8px;align-items:center;font-size:14px;color:var(--sage)}
.check input{accent-color:var(--brass);width:16px;height:16px}
.play-actions{display:flex;gap:10px}
.play-now{font-size:14px;color:var(--plaster);margin:0}
.play-now small{display:block;color:var(--sage);font-size:13px;margin-top:2px}
.tabs{flex-wrap:wrap;row-gap:6px}
.tab-new{background:none;border:0;color:var(--sage);font-size:14px;padding:4px 0}
.tab-new:hover{color:var(--plaster)}
.droppad{display:flex;align-items:center;gap:6px 14px;flex-wrap:wrap;width:100%;margin:0 0 28px;
  padding:22px 24px;border-radius:14px;border:1.5px dashed var(--sage);background:rgba(166,184,175,.05);
  color:var(--plaster);text-align:left;transition:background .15s,border-color .15s}
.droppad:hover{background:rgba(166,184,175,.1)}
.droppad.over{border-color:var(--brass);border-style:solid;background:rgba(207,171,100,.14)}
.droppad strong{font:400 22px/1.2 var(--serif)}
.droppad span{color:var(--sage);font-size:14px}
.droppad .dp-icon{width:34px;height:34px;flex:none;border-radius:50%;background:var(--wall-lift);position:relative}
.droppad .dp-icon::before,.droppad .dp-icon::after{content:"";position:absolute;left:50%;top:50%;
  background:var(--brass);transform:translate(-50%,-50%)}
.droppad .dp-icon::before{width:14px;height:2px}
.droppad .dp-icon::after{width:2px;height:14px}
.col-tools{display:flex;gap:8px;margin:-12px 0 22px;align-items:center;color:var(--sage);font-size:14px}
.col-tools[hidden]{display:none}
.picklist{display:grid;gap:6px;margin:0 0 18px}
.picklist button{text-align:left;background:var(--wall-lift);border:0;border-radius:8px;padding:10px 12px}
.picklist button:hover{background:#355047}
dialog input[type=text]{width:100%;font:inherit;padding:10px 12px;border-radius:8px;border:0;background:var(--wall-lift);color:var(--plaster);margin:0 0 18px}

.dropping .droppad{border-color:var(--brass);border-style:solid;background:rgba(207,171,100,.14)}

.toast{position:fixed;left:50%;bottom:calc(24px + env(safe-area-inset-bottom));transform:translate(-50%,20px);
  background:var(--plaster);color:var(--wall-deep);padding:10px 18px;border-radius:999px;font-weight:700;
  opacity:0;transition:opacity .25s,transform .25s;z-index:40;max-width:calc(100vw - 32px)}
.toast.show{opacity:1;transform:translate(-50%,0)}
.toast.err{background:var(--danger);color:#3a120d}

dialog{border:0;border-radius:14px;background:var(--wall-deep);color:var(--plaster);padding:24px;max-width:360px}
dialog::backdrop{background:rgba(10,16,14,.6)}
dialog h2{font:400 22px var(--serif);margin:0 0 8px}
dialog p{margin:0 0 20px;color:var(--sage)}
dialog .bar{display:flex;gap:10px;justify-content:flex-end}

/* Layout responds to the space it actually has (container queries), not the device. */
.page{container:page / inline-size}
main{container:lib / inline-size}
@container page (max-width:860px){
  .layout{grid-template-columns:minmax(0,1fr);gap:36px;padding:20px 16px 48px}
  .side{position:static;max-height:none;overflow:visible;margin-top:0;padding:0}
  .tv{max-width:min(56cqi,260px)}
}
@container lib (max-width:640px){
  .head{gap:10px}
  .tabs{gap:18px;width:100%;overflow-x:auto;flex-wrap:nowrap;padding-bottom:4px;scrollbar-width:none;
    overscroll-behavior-x:contain}
  .tabs::-webkit-scrollbar{display:none}
  .tabs button{font-size:19px;white-space:nowrap;flex:none}
  .head .btn{padding:8px 16px}
  .droppad{padding:16px 18px;margin-bottom:22px}
  .droppad strong{font-size:19px}
  .grid{grid-template-columns:repeat(auto-fill,minmax(min(104px,28cqi),1fr));gap:22px 14px}
  .drophint{display:none}
}

/* collapsible side panels */
details.panel>summary{list-style:none;cursor:pointer;display:flex;align-items:center;justify-content:space-between}
details.panel>summary::-webkit-details-marker{display:none}
details.panel>summary::after{content:"";width:8px;height:8px;border:solid var(--sage);border-width:0 2px 2px 0;
  transform:rotate(45deg);margin:0 4px 4px 0;transition:transform .2s}
details.panel:not([open])>summary::after{transform:rotate(-45deg);margin:0 6px 0 0}
details.panel:not([open])>summary{margin-bottom:0}
/* schedule slots */
.tools{display:grid;gap:10px}
.tool-note{margin:0;font-size:14px;color:var(--sage)}
.slots{list-style:none;margin:0;padding:0;display:grid;gap:12px}
.slot{background:var(--wall-lift);border-radius:12px;padding:12px 12px 14px;display:grid;gap:8px;border:1px solid transparent}
.slot.now{border-color:var(--brass)}
.slot-top{display:flex;gap:8px;align-items:center}
.slot-top select,.slot-top input{flex:1;min-width:0;background:var(--wall-deep)}
.slot-top input[type=time]{font:inherit;color:var(--plaster);border:0;border-radius:8px;padding:7px 10px;color-scheme:dark}
.slot-del{flex:none;width:32px;height:32px;border-radius:50%;border:0;background:none;color:var(--sage);font-size:20px;line-height:1}
.slot-del:hover{color:var(--danger)}
.slot-del:disabled{opacity:.3;cursor:default}
.slot-meta{margin:-2px 0 0;font-size:13px;color:var(--sage)}
.slot.now .slot-meta{color:var(--brass)}
.slot-opts{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.slot-opts select{flex:1;min-width:150px;background:var(--wall-deep)}
.night{padding-left:0}
@media (max-height:620px){
  .side{position:static;max-height:none;overflow:visible;margin-top:0;padding:0}
}
@media (max-height:520px){ .tv{max-width:150px} }
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
</style>
</head>
<body>
<div class="page">
<div class="layout">
  <aside class="side">
    <div class="wall" id="wall">
      <div class="lamp" aria-hidden="true"></div>
      <div class="tv"><div class="screen" id="screen"><div class="msg">Connecting…</div></div></div>
    </div>
    <div class="status" aria-live="polite">
      <h1 id="s-title">Connecting</h1>
      <p id="s-sub">&nbsp;</p>
      <div class="act" id="s-act"></div>
    </div>

    <div class="controls">
      <details class="play panel" id="play-box" open>
        <summary class="row-label" id="play-l">Play</summary>
        <p class="play-now" id="play-now">Checking the Frame…</p>
        <div class="play-row">
          <label for="play-what">What</label><select id="play-what"></select>
          <label for="play-every">Every</label><select id="play-every"></select>
        </div>
        <label class="check"><input type="checkbox" id="play-shuffle" checked> Shuffle</label>
        <div class="play-actions">
          <button class="btn brass small" id="play-go">Play</button>
          <button class="btn ghost small" id="play-stop">Stop</button>
        </div>
      </details>
      <details class="display panel" id="disp-box" open>
        <summary class="row-label" id="disp-l">Display and schedule</summary>
        <button class="switch" role="switch" aria-checked="false" id="d-sensor" disabled>
          <span class="knob" aria-hidden="true"></span>
          <span><strong>Adjust to room light</strong><small id="d-sensor-sub">Uses the Frame's light sensor.</small></span>
        </button>
        <div class="field" id="d-bright-field">
          <label for="d-bright">Brightness <output id="d-bright-out">…</output></label>
          <input type="range" id="d-bright" min="0" max="10" step="1" disabled>
        </div>
        <div class="field">
          <label for="d-warm">Warmth <output id="d-warm-out">…</output></label>
          <input type="range" id="d-warm" min="-5" max="5" step="1" disabled>
        </div>
        <button class="switch" role="switch" aria-checked="false" id="n-on">
          <span class="knob" aria-hidden="true"></span>
          <span><strong>Brightness schedule</strong><small id="n-sub">Times can follow sunrise and sunset in Colgate.</small></span>
        </button>
        <div class="night" id="n-box" hidden>
          <ol class="slots" id="slots" aria-label="Schedule times"></ol>
          <button class="btn ghost small" id="slot-add" type="button">+ Add a time</button>
        </div>
      </details>
      <button class="switch" role="switch" aria-checked="false" id="watch">
        <span class="knob" aria-hidden="true"></span>
        <span><strong>Keep it in Art Mode</strong><small id="watch-sub">Checks every minute.</small></span>
      </button>
      <details class="panel tools" id="tools-box">
        <summary class="row-label">Tools</summary>
        <p class="tool-note">Bring over sharp, uncropped previews from the laptop version: choose its
          “Frame cache for Home Assistant” folder.</p>
        <button class="btn ghost small" id="import-btn" type="button">Import previews…</button>
        <input type="file" id="import-file" webkitdirectory multiple class="sr" tabindex="-1">
      </details>
    </div>
  </aside>

  <main>
    <div class="head">
      <div class="tabs" role="tablist" aria-label="Pictures"></div>
      <button class="btn ghost" id="select-btn">Select</button>
      <button class="btn ghost" id="addto-btn" hidden>Add to collection</button>
      <button class="btn ghost" id="takeout-btn" hidden>Remove from collection</button>
      <button class="btn ghost" id="reframe-btn" hidden>Reframe</button>
      <button class="btn danger" id="delete-btn" hidden>Delete</button>
      <input type="file" id="file" accept="image/*,.heic,.heif" multiple class="sr" tabindex="-1">
    </div>
    <button class="droppad" id="droppad" type="button">
      <span class="dp-icon" aria-hidden="true"></span>
      <strong id="dp-title">Drop photos here</strong>
      <span id="dp-sub">Drag from Photos or Finder, or click to choose</span>
    </button>
    <div class="col-tools" id="col-tools" hidden>
      <button class="btn ghost small" id="col-play">Play this collection</button>
      <button class="btn ghost small" id="col-rename">Rename</button>
      <button class="btn ghost small" id="col-delete">Delete collection</button>
    </div>
    <ul class="grid" id="grid" aria-label="Pictures on the Frame"></ul>
    <p class="empty" id="empty" hidden></p>
  </main>
</div>
</div>

<section class="editor" id="editor" aria-label="Prepare photos">
  <div class="ed-stage">
    <div class="ed-wrap" id="ed-wrap"><canvas id="ed-canvas" aria-label="Preview of the photo as it will hang"></canvas></div>
    <p class="ed-hint" id="ed-hint">Drag to reposition. Scroll or pinch to zoom.</p>
  </div>
  <div class="ed-panel">
    <div class="ed-scroll">
      <h2>Prepare<small id="ed-count"></small></h2>
      <div>
        <p class="row-label" id="style-l">Frame</p>
        <div class="styles" id="styles" role="radiogroup" aria-labelledby="style-l"></div>
      </div>
      <div class="field" id="mat-field">
        <label for="mat">Mat width <output id="mat-out"></output></label>
        <input type="range" id="mat" min="3" max="22" step="0.5">
      </div>
      <div>
        <p class="row-label" id="fit-l">Photo</p>
        <div class="seg" role="group" aria-labelledby="fit-l" id="fitseg">
          <button data-fit="fill">Fill the opening</button>
          <button data-fit="whole">Show whole photo</button>
        </div>
      </div>
      <div>
        <p class="row-label" id="turn-l">Rotate</p>
        <div class="rot-actions" role="group" aria-labelledby="turn-l">
          <button class="btn ghost small" id="turn-left" aria-label="Turn a quarter turn left">↺ Turn left</button>
          <button class="btn ghost small" id="turn-right" aria-label="Turn a quarter turn right">↻ Turn right</button>
        </div>
      </div>
      <div class="field" id="tilt-field">
        <label for="tilt">Straighten <output id="tilt-out"></output></label>
        <input type="range" id="tilt" min="-10" max="10" step="0.1" value="0">
        <div class="rot-actions">
          <button class="btn ghost small" id="tilt-minus" aria-label="Tilt 0.1 degrees left">−0.1°</button>
          <button class="btn ghost small" id="tilt-plus" aria-label="Tilt 0.1 degrees right">+0.1°</button>
          <button class="btn ghost small" id="tilt-reset">Level</button>
        </div>
      </div>
      <div class="field" id="zoom-field">
        <label for="zoom">Zoom <output id="zoom-out"></output></label>
        <input type="range" id="zoom" min="100" max="400" step="1">
      </div>
      <div class="field">
        <label for="ed-col">Add to collection</label>
        <select id="ed-col"></select>
      </div>
      <div id="queue-block">
        <p class="row-label">Photos to hang</p>
        <div class="queue" id="queue"></div>
        <div class="queue-actions">
          <button class="btn ghost small" id="apply-all">Use this frame for all</button>
          <button class="btn ghost small" id="ed-remove">Leave this one out</button>
        </div>
      </div>
    </div>
    <div class="ed-bar">
      <span class="note" id="ed-note"></span>
      <button class="btn ghost" id="ed-cancel">Cancel</button>
      <button class="btn brass" id="ed-go">Hang</button>
    </div>
  </div>
</section>

<dialog id="confirm">
  <h2 id="c-title">Delete photos?</h2>
  <p>They'll be removed from the Frame. This can't be undone.</p>
  <div class="bar">
    <button class="btn ghost" value="keep" id="c-keep">Keep</button>
    <button class="btn danger" value="delete" id="c-del">Delete</button>
  </div>
</dialog>

<dialog id="namer">
  <h2 id="namer-title">New collection</h2>
  <input type="text" id="namer-input" maxlength="60" placeholder="Name, like Winter or Family">
  <div class="bar">
    <button class="btn ghost" id="namer-cancel">Cancel</button>
    <button class="btn brass" id="namer-ok">Create</button>
  </div>
</dialog>

<dialog id="picker">
  <h2>Add to collection</h2>
  <div class="picklist" id="picklist"></div>
  <div class="bar">
    <button class="btn ghost" id="picker-cancel">Cancel</button>
  </div>
</dialog>

<div class="toast" id="toast" role="status"></div>

<script>
const $ = s => document.querySelector(s);
const plural = (n, w) => n + " " + w + (n === 1 ? "" : "s");
let lib = {mine: [], samsung: [], collections: {}};
let status = {};
let tab = "mine", selecting = false, selected = new Set();

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  let data = {};
  try { data = await r.json(); } catch (e) {}
  if (!r.ok) throw new Error(data.error || "Something went wrong (" + r.status + ")");
  return data;
}
const post = (path, body) => api(path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});

let toastTimer;
function toast(msg, err) {
  const t = $("#toast");
  t.textContent = msg; t.className = "toast show" + (err ? " err" : "");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.className = "toast", err ? 6000 : 2600);
}

function imgSrc(id) {
  const all = lib.mine.concat(lib.samsung);
  const it = all.find(x => x.id === id);
  return (it && it.full ? "/full/" : "/thumb/") + encodeURIComponent(id) + (it && it.v ? "?v=" + it.v : "");
}

/* ---------- status ---------- */
function renderStatus() {
  const s = status, title = $("#s-title"), sub = $("#s-sub"), act = $("#s-act"), screen = $("#screen");
  act.innerHTML = "";
  $("#wall").classList.toggle("dark", s.power !== "on" || !s.artmode);
  if (s.power === "unreachable") {
    title.textContent = "Can't reach the Frame";
    sub.textContent = "Check that it's plugged in and on Wi-Fi.";
    screen.innerHTML = '<div class="msg">Not connected</div>';
  } else if (s.power !== "on") {
    title.textContent = "The Frame is off";
    sub.textContent = "It won't be switched back on while it's off.";
    screen.innerHTML = '<div class="msg">Off</div>';
    const b = button("Turn on Art Mode", "btn", () => setArt(true)); act.append(b);
  } else if (!s.artmode) {
    title.textContent = "Showing TV, not art";
    sub.textContent = "Samsung's software switched it out of Art Mode.";
    screen.innerHTML = '<div class="msg">TV mode</div>';
    act.append(button("Return to art", "btn brass", () => setArt(true)));
  } else {
    title.textContent = "In Art Mode";
    sub.textContent = s.current ? "Now on the wall" : "";
    const want = s.current ? imgSrc(s.current) : null;
    const cur = screen.querySelector("img");
    if (want && (!cur || cur.dataset.src !== want)) {
      const im = new Image(); im.alt = "Picture on the wall"; im.dataset.src = want; im.style.opacity = 0;
      im.onload = () => { screen.innerHTML = ""; screen.append(im); requestAnimationFrame(() => im.style.opacity = 1); };
      im.onerror = () => { screen.innerHTML = '<div class="msg">' + s.current + '<br>No preview from the TV</div>'; };
      im.src = want;
    }
  }
  const w = $("#watch");
  w.setAttribute("aria-checked", s.watchdog ? "true" : "false");
  const e = s.escapes || {};
  $("#watch-sub").textContent = !s.watchdog ? "Off. Samsung's software can switch the TV away from art."
    : e.today ? "On. Pulled it back " + plural(e.today, "time") + " today."
    : e.total ? "On. No escapes today, " + e.total + " in total."
    : "On. Checks every minute.";
  document.querySelectorAll("#grid li").forEach(li => li.classList.toggle("current", li.dataset.id === s.current));
}
function button(text, cls, fn) { const b = document.createElement("button"); b.className = cls; b.textContent = text; b.onclick = fn; return b; }

async function refreshStatus() {
  try { status = await api("/api/status"); }
  catch (e) { status = {power: "unreachable"}; }
  renderStatus();
}
async function setArt(on) {
  try { await post("/api/artmode", {on}); await refreshStatus(); } catch (e) { toast(e.message, true); }
}

/* ---------- library + collections ---------- */
async function refreshLibrary() {
  try { lib = await api("/api/library"); }
  catch (e) { toast(e.message, true); }
  lib.collections = lib.collections || {};
  if (tab.startsWith("c:") && !lib.collections[tab.slice(2)]) tab = "mine";
  renderTabs(); renderLibrary(); fillPlayWhat(); fillEditorCollections();
}
const byId = id => lib.mine.concat(lib.samsung).find(x => x.id === id);
function itemsFor(t) {
  if (t === "mine" || t === "samsung") return lib[t];
  const col = lib.collections[t.slice(2)];
  return col ? col.items.map(id => byId(id) || {id}) : [];
}
function renderTabs() {
  const box = $(".tabs"); box.innerHTML = "";
  const add = (key, name, n) => {
    const b = document.createElement("button");
    b.setAttribute("role", "tab"); b.dataset.tab = key;
    b.setAttribute("aria-selected", key === tab ? "true" : "false");
    b.textContent = name;
    b.insertAdjacentHTML("beforeend", '<span class="n">' + n + "</span>");
    b.onclick = () => selectTab(key);
    box.append(b);
  };
  add("mine", "Your photos", lib.mine.length);
  add("samsung", "Samsung art", lib.samsung.length);
  Object.entries(lib.collections).forEach(([id, c]) => add("c:" + id, c.name, c.items.length));
  const nb = document.createElement("button"); nb.className = "tab-new"; nb.textContent = "+ New collection";
  nb.onclick = () => nameCollection(null); box.append(nb);
}
function selectTab(key) {
  tab = key;
  if (selecting) setSelecting(false); else { renderTabs(); renderLibrary(); }
}
function renderLibrary() {
  const items = itemsFor(tab), grid = $("#grid");
  grid.innerHTML = ""; grid.classList.toggle("selecting", selecting);
  $("#col-tools").hidden = !tab.startsWith("c:");
  const colName = tab.startsWith("c:") && lib.collections[tab.slice(2)] ? lib.collections[tab.slice(2)].name : "";
  if (!matchMedia("(pointer: coarse)").matches) {
    $("#dp-title").textContent = colName ? "Drop photos into " + colName : "Drop photos here";
  }
  items.forEach(it => {
    const li = document.createElement("li"); li.dataset.id = it.id;
    if (it.id === status.current) li.classList.add("current");
    if (selected.has(it.id)) li.classList.add("sel");
    const b = document.createElement("button"); b.className = "pic";
    b.setAttribute("aria-label", selecting ? "Select " + it.id : "Put " + it.id + " on the wall");
    const im = new Image(); im.loading = "lazy"; im.alt = ""; im.onload = () => im.classList.add("in");
    im.onerror = () => { b.classList.add("noprev"); b.dataset.label = it.id; };
    im.src = "/thumb/" + encodeURIComponent(it.id) + (it.v ? "?v=" + it.v : "");
    b.append(im); b.onclick = () => selecting ? toggleSel(it.id, li) : show(it.id);
    li.innerHTML = '<span class="tick"></span>';
    li.prepend(b);
    li.insertAdjacentHTML("beforeend", '<div class="label"></div><div class="cap">On the wall</div>');
    grid.append(li);
  });
  const empty = $("#empty");
  empty.hidden = items.length > 0;
  empty.textContent = tab === "mine" ? "No photos on the Frame yet. Add some to hang them."
    : tab === "samsung" ? "No Samsung art on this TV."
    : "This collection is empty. Choose Select on Your photos or Samsung art, then Add to collection.";
}
async function show(id) {
  try {
    await post("/api/show", {id});
    status.current = id; status.artmode = true; renderStatus(); toast("Now on the wall");
  } catch (e) { toast(e.message, true); }
}

/* ---------- select, delete, add to collection ---------- */
function setSelecting(on) {
  selecting = on; selected.clear();
  $("#select-btn").textContent = on ? "Done" : "Select";
  updateDelete(); renderTabs(); renderLibrary();
}
function toggleSel(id, li) {
  selected.has(id) ? selected.delete(id) : selected.add(id);
  li.classList.toggle("sel", selected.has(id)); updateDelete();
}
function updateDelete() {
  const n = selected.size, inCol = tab.startsWith("c:");
  const d = $("#delete-btn"), a = $("#addto-btn"), t = $("#takeout-btn");
  d.hidden = !selecting; d.disabled = n === 0; d.textContent = n ? "Delete " + n : "Delete";
  a.hidden = !selecting || inCol; a.disabled = n === 0;
  t.hidden = !selecting || !inCol; t.disabled = n === 0;
  const r = $("#reframe-btn"), ids = [...selected], canFrame = ids.filter(id => (byId(id) || {}).orig);
  r.hidden = !selecting;
  r.disabled = canFrame.length === 0;
  r.textContent = canFrame.length ? "Reframe " + canFrame.length : "Reframe";
  r.title = ids.length && canFrame.length < ids.length
    ? "Only pictures hung from this version keep an original to reframe from." : "";
}
$("#reframe-btn").onclick = async () => {
  const ids = [...selected].filter(id => (byId(id) || {}).orig);
  if (!ids.length) return toast("Those pictures don't have an original kept. Re-add them to make them reframable.", true);
  setSelecting(false);
  items = ids.map(id => ({name: id, replaces: id, fromBox: true, s: defaults(), img: null, loading: true}));
  cur = 0; openEditor(); $("#ed-col").value = "";
  for (const it of items) {
    try {
      const [img, rec] = await Promise.all([
        fetch("/original/" + encodeURIComponent(it.replaces)).then(r => { if (!r.ok) throw new Error("Original missing"); return r.blob(); }).then(loadInBrowser),
        api("/api/recipe/" + encodeURIComponent(it.replaces))]);
      Object.assign(it, img);
      if (rec.recipe) it.s = Object.assign(defaults(), rec.recipe);
    } catch (e) { it.error = e.message; toast(it.name + ": " + e.message, true); }
    it.loading = false;
    if (items[cur] === it) refreshEditor(); else renderQueue();
  }
  items = items.filter(it => !it.error);
  if (!items.length) closeEditor(); else { cur = Math.min(cur, items.length - 1); refreshEditor(); }
};
$("#select-btn").onclick = () => setSelecting(!selecting);
$("#delete-btn").onclick = () => {
  const n = selected.size; if (!n) return;
  $("#c-title").textContent = "Delete " + plural(n, "picture") + "?";
  $("#confirm").showModal();
};
$("#c-keep").onclick = () => $("#confirm").close();
$("#c-del").onclick = async () => {
  $("#confirm").close();
  const ids = [...selected], n = ids.length;
  try {
    await post("/api/delete", {ids});
    toast("Deleted " + plural(n, "picture")); setSelecting(false); await refreshLibrary(); refreshStatus();
  } catch (e) { toast(e.message, true); }
};
async function collectionAction(body, done) {
  try { const r = await post("/api/collections", body); lib.collections = r.collections; if (done) done(r); }
  catch (e) { toast(e.message, true); }
  renderTabs(); renderLibrary(); fillPlayWhat(); fillEditorCollections();
}
$("#addto-btn").onclick = () => {
  if (!selected.size) return;
  const list = $("#picklist"); list.innerHTML = "";
  Object.entries(lib.collections).forEach(([id, c]) => {
    list.append(button(c.name + " (" + c.items.length + ")", "", async () => {
      $("#picker").close(); const n = selected.size;
      await collectionAction({action: "add", id, ids: [...selected]}, () => toast("Added " + plural(n, "picture") + " to " + c.name));
      setSelecting(false);
    }));
  });
  list.append(button("+ New collection", "", () => { $("#picker").close(); nameCollection(null, [...selected]); }));
  $("#picker").showModal();
};
$("#picker-cancel").onclick = () => $("#picker").close();
$("#takeout-btn").onclick = async () => {
  const id = tab.slice(2), n = selected.size; if (!n) return;
  await collectionAction({action: "remove", id, ids: [...selected]}, () => toast("Removed " + plural(n, "picture") + " from the collection"));
  setSelecting(false);
};
let naming = null;
function nameCollection(id, ids) {
  naming = {id, ids: ids || []};
  $("#namer-title").textContent = id ? "Rename collection" : "New collection";
  $("#namer-ok").textContent = id ? "Rename" : "Create";
  $("#namer-input").value = id ? lib.collections[id].name : "";
  $("#namer").showModal(); $("#namer-input").focus();
}
$("#namer-cancel").onclick = () => $("#namer").close();
$("#namer-input").onkeydown = e => { if (e.key === "Enter") { e.preventDefault(); $("#namer-ok").click(); } };
$("#namer-ok").onclick = async () => {
  const name = $("#namer-input").value.trim();
  if (!name) { $("#namer-input").focus(); return; }
  $("#namer").close();
  const n = naming;
  if (n.id) return collectionAction({action: "rename", id: n.id, name});
  await collectionAction({action: "create", name, ids: n.ids}, r => {
    tab = "c:" + r.id; toast(n.ids.length ? "Created " + name + " with " + plural(n.ids.length, "picture") : "Created " + name);
  });
  if (selecting) setSelecting(false);
};
$("#col-rename").onclick = () => nameCollection(tab.slice(2));
$("#col-delete").onclick = () => {
  const id = tab.slice(2), c = lib.collections[id]; if (!c) return;
  $("#c-title").textContent = "Delete the collection " + c.name + "?";
  const keep = $("#c-del").onclick;
  $("#c-del").onclick = async () => {
    $("#confirm").close(); $("#c-del").onclick = keep;
    tab = "mine"; await collectionAction({action: "delete", id}, () => toast("Deleted " + c.name + ". The pictures stay on the Frame."));
  };
  $("#confirm").querySelector("p").textContent = "The pictures stay on the Frame. Only the collection goes.";
  $("#confirm").showModal();
  $("#confirm").addEventListener("close", () => {
    $("#c-del").onclick = keep;
    $("#confirm").querySelector("p").textContent = "They'll be removed from the Frame. This can't be undone.";
  }, {once: true});
};
$("#col-play").onclick = () => { $("#play-what").value = tab.slice(2); $("#play-go").click(); };

/* ---------- play ---------- */
const EVERY = [
  [3, "3 minutes"], [15, "15 minutes"], [60, "1 hour"], [720, "12 hours"], [1440, "1 day"], [10080, "7 days"],
  [1, "1 minute (app)"], [5, "5 minutes (app)"], [30, "30 minutes (app)"], [120, "2 hours (app)"],
  [240, "4 hours (app)"], [360, "6 hours (app)"],
];
const everyLabel = m => { const e = EVERY.find(x => x[0] === m); return e ? e[1].replace(" (app)", "") : m + " minutes"; };
let rotation = null;
function fillPlayWhat() {
  const sel = $("#play-what"), was = sel.value;
  sel.innerHTML = "";
  const opt = (v, t) => { const o = document.createElement("option"); o.value = v; o.textContent = t; sel.append(o); };
  opt("all", "All your photos");
  Object.entries(lib.collections).forEach(([id, c]) => opt(id, c.name + " (" + c.items.length + ")"));
  if ([...sel.options].some(o => o.value === was)) sel.value = was;
}
function fillEvery() {
  const sel = $("#play-every"); sel.innerHTML = "";
  const g1 = document.createElement("optgroup"); g1.label = "Runs on the TV";
  const g2 = document.createElement("optgroup"); g2.label = "Needs this app running";
  EVERY.forEach(([m, t]) => { const o = document.createElement("option"); o.value = m; o.textContent = t; (t.includes("(app)") ? g2 : g1).append(o); });
  sel.append(g1, g2); sel.value = "60";
}
function renderPlay() {
  const now = $("#play-now"), r = rotation;
  if (!r) { now.textContent = "Checking the Frame…"; return; }
  const p = r.playing;
  if (p) {
    now.innerHTML = "";
    now.append("Playing " + p.label + ", every " + everyLabel(p.minutes) + (p.shuffle ? ", shuffled" : ""));
    const sm = document.createElement("small");
    sm.textContent = p.mode === "tv" ? "Runs on the TV." : "Changed by this app on Home Assistant.";
    now.append(sm);
    $("#play-what").value = p.target; $("#play-every").value = String(p.minutes); $("#play-shuffle").checked = !!p.shuffle;
  } else if (r.tv && r.tv.value && r.tv.value !== "off") {
    const m = parseInt(r.tv.value, 10);
    now.textContent = "The TV is changing pictures every " + (isNaN(m) ? r.tv.value : everyLabel(m)) + ".";
  } else if (r.tv) {
    now.textContent = "Not playing. The picture stays put.";
  } else {
    now.textContent = "Not playing from this app. The TV didn't report its own setting.";
  }
  $("#play-stop").disabled = !p && !(r.tv && r.tv.value && r.tv.value !== "off");
}
async function refreshRotation() {
  try { rotation = await api("/api/rotation"); } catch (e) { rotation = {playing: null, tv: null}; }
  renderPlay();
}
$("#play-go").onclick = async () => {
  const body = {target: $("#play-what").value, minutes: +$("#play-every").value, shuffle: $("#play-shuffle").checked};
  const b = $("#play-go"); b.disabled = true; b.textContent = "Starting…";
  try {
    const r = await post("/api/play", body);
    toast(r.note || (r.mode === "tv" ? "Playing on the TV" : "Playing from this app"), !!r.note);
    await refreshRotation();
  } catch (e) { toast(e.message, true); }
  b.disabled = false; b.textContent = "Play";
};
$("#play-stop").onclick = async () => {
  try { await post("/api/stop", {}); toast("Stopped. The picture stays put."); await refreshRotation(); }
  catch (e) { toast(e.message, true); }
};
fillEvery();

$("#watch").onclick = async () => {
  const on = $("#watch").getAttribute("aria-checked") !== "true";
  try { await post("/api/watchdog", {on}); status.watchdog = on; renderStatus(); } catch (e) { toast(e.message, true); }
};

function fillEditorCollections() {
  const sel = $("#ed-col"); if (!sel) return;
  const was = sel.value || recall("col", "");
  sel.innerHTML = "";
  const opt = (v, t) => { const o = document.createElement("option"); o.value = v; o.textContent = t; sel.append(o); };
  opt("", "None");
  Object.entries(lib.collections).forEach(([id, c]) => opt(id, c.name));
  sel.value = [...sel.options].some(o => o.value === was) ? was : "";
}

/* ---------- adding photos: the editor ---------- */
const OUT_W = 2160, OUT_H = 3840;
const STYLES = [
  {id: "bleed",    name: "Edge to edge"},
  {id: "gallery",  name: "Gallery",      mat: "#f3f1ec", core: "#fcfbf8"},
  {id: "museum",   name: "Museum",       mat: "#eeebe3", core: "#faf8f2", bottom: 1.4},
  {id: "linen",    name: "Linen",        mat: "#e4d9c6", core: "#f5efe3", texture: .07},
  {id: "charcoal", name: "Charcoal",     mat: "#2f2f2d", core: "#efede7", texture: .04},
  {id: "float",    name: "Float",        mat: "#ebe9e4", float: true},
  {id: "black",    name: "Black frame",  mat: "#f3f1ec", core: "#fcfbf8", frame: "#151515"},
  {id: "walnut",   name: "Walnut frame", mat: "#efeae0", core: "#faf7f0", frame: "#4a3223", grain: true},
];
const styleOf = id => STYLES.find(s => s.id === id) || STYLES[0];

function remember(k, v) { try { localStorage.setItem("frame." + k, JSON.stringify(v)); } catch (e) {} }
function recall(k, d) { try { const v = localStorage.getItem("frame." + k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } }

let items = [], cur = 0, busy = false;

let noise = null;
function noisePattern(ctx) {
  if (!noise) {
    noise = document.createElement("canvas"); noise.width = noise.height = 192;
    const n = noise.getContext("2d"), d = n.createImageData(192, 192);
    for (let i = 0; i < d.data.length; i += 4) {
      const v = Math.random() * 255; d.data[i] = d.data[i + 1] = d.data[i + 2] = v; d.data[i + 3] = 255;
    }
    n.putImageData(d, 0, 0);
  }
  return ctx.createPattern(noise, "repeat");
}

/* Geometry shared by drawing and dragging. Everything scales with W, so a
   small preview and the full 2160x3840 output are the same picture. */
function source(it) {
  const turn = ((it.s.turn || 0) % 4 + 4) % 4;
  if (!turn) return {src: it.img, w: it.w, h: it.h};
  if (!it._rot || it._rot.turn !== turn) {
    const k = Math.min(1, 4800 / Math.max(it.w, it.h)), w = Math.round(it.w * k), h = Math.round(it.h * k);
    const c = document.createElement("canvas"), x = c.getContext("2d");
    c.width = turn % 2 ? h : w; c.height = turn % 2 ? w : h;
    if (turn === 1) { x.translate(c.width, 0); x.rotate(Math.PI / 2); }
    if (turn === 2) { x.translate(c.width, c.height); x.rotate(Math.PI); }
    if (turn === 3) { x.translate(0, c.height); x.rotate(-Math.PI / 2); }
    x.drawImage(it.img, 0, 0, w, h);
    it._rot = {turn, src: c, w: c.width, h: c.height};
  }
  return it._rot;
}

function layout(W, H, it) {
  const S = source(it), s = it.s, st = styleOf(s.style), iw = S.w, ih = S.h;
  const fw = st.frame ? W * 0.032 : 0;
  const x0 = fw, y0 = fw, w0 = W - 2 * fw, h0 = H - 2 * fw;
  const m = st.mat ? W * s.mat / 100 : 0;
  const bottom = st.bottom || 1;
  let ax = x0 + m, ay = y0 + m, aw = w0 - 2 * m, ah = h0 - m - m * bottom;
  const whole = st.mat && (s.fit === "whole");
  if (whole) {
    const r = Math.min(aw / iw, ah / ih), nw = iw * r, nh = ih * r;
    ax += (aw - nw) / 2; ay += (ah - nh) / 2; aw = nw; ah = nh;
  }
  const zoom = whole ? 1 : s.zoom;
  const angle = whole ? 0 : (s.tilt || 0) * Math.PI / 180;
  const ca = Math.abs(Math.cos(angle)), sa = Math.abs(Math.sin(angle));
  const bw = aw * ca + ah * sa, bh = aw * sa + ah * ca;     /* box the tilted opening needs */
  const scale = Math.max(bw / iw, bh / ih) * zoom;
  const sw = bw / scale, sh = bh / scale;
  const sx = (iw - sw) * s.fx, sy = (ih - sh) * s.fy;
  return {st, fw, x0, y0, w0, h0, ax, ay, aw, ah, scale, sw, sh, sx, sy, whole, angle, bw, bh, iw, ih, src: S.src};
}

function render(ctx, W, H, it) {
  const L = layout(W, H, it), st = L.st;
  ctx.save();
  ctx.clearRect(0, 0, W, H);
  if (st.frame) {
    ctx.fillStyle = st.frame; ctx.fillRect(0, 0, W, H);
    if (st.grain) {
      ctx.globalAlpha = .1; ctx.fillStyle = noisePattern(ctx); ctx.fillRect(0, 0, W, H); ctx.globalAlpha = 1;
      for (let i = 0; i < 28; i++) {
        const t = (i / 28) * L.fw, a = .05 + .05 * Math.sin(i * 1.7);
        ctx.strokeStyle = "rgba(0,0,0," + a + ")"; ctx.lineWidth = Math.max(1, W * 0.0007);
        ctx.strokeRect(t, t, W - 2 * t, H - 2 * t);
      }
    }
    ctx.strokeStyle = "rgba(255,255,255,.14)"; ctx.lineWidth = W * 0.0025;          /* lip highlight */
    ctx.strokeRect(L.fw - W * 0.00125, L.fw - W * 0.00125, L.w0 + W * 0.0025, L.h0 + W * 0.0025);
  }
  if (st.mat) {
    ctx.fillStyle = st.mat; ctx.fillRect(L.x0, L.y0, L.w0, L.h0);
    if (st.texture) {
      ctx.globalAlpha = st.texture; ctx.globalCompositeOperation = "multiply";
      ctx.fillStyle = noisePattern(ctx); ctx.fillRect(L.x0, L.y0, L.w0, L.h0);
      ctx.globalAlpha = 1; ctx.globalCompositeOperation = "source-over";
    }
    if (st.frame) {                                     /* frame casts a shadow onto the mat */
      const sh = W * 0.014;
      let g = ctx.createLinearGradient(0, L.y0, 0, L.y0 + sh);
      g.addColorStop(0, "rgba(0,0,0,.22)"); g.addColorStop(1, "rgba(0,0,0,0)");
      ctx.fillStyle = g; ctx.fillRect(L.x0, L.y0, L.w0, sh);
      g = ctx.createLinearGradient(L.x0, 0, L.x0 + sh * .6, 0);
      g.addColorStop(0, "rgba(0,0,0,.12)"); g.addColorStop(1, "rgba(0,0,0,0)");
      ctx.fillStyle = g; ctx.fillRect(L.x0, L.y0, sh * .6, L.h0);
    }
  }
  const draw = () => {
    if (!L.angle) return ctx.drawImage(L.src, L.sx, L.sy, L.sw, L.sh, L.ax, L.ay, L.aw, L.ah);
    ctx.save();
    ctx.beginPath(); ctx.rect(L.ax, L.ay, L.aw, L.ah); ctx.clip();
    ctx.translate(L.ax + L.aw / 2, L.ay + L.ah / 2); ctx.rotate(L.angle);
    ctx.drawImage(L.src, L.sx, L.sy, L.sw, L.sh, -L.bw / 2, -L.bh / 2, L.bw, L.bh);
    ctx.restore();
  };
  if (st.float) {
    ctx.save();
    ctx.shadowColor = "rgba(0,0,0,.32)"; ctx.shadowBlur = W * 0.03; ctx.shadowOffsetY = W * 0.011;
    ctx.fillStyle = st.mat; ctx.fillRect(L.ax, L.ay, L.aw, L.ah);
    ctx.restore();
    draw();
  } else if (st.core) {
    const b = W * 0.0055;                              /* the bevel cut of the mat */
    ctx.fillStyle = st.core; ctx.fillRect(L.ax - b, L.ay - b, L.aw + 2 * b, L.ah + 2 * b);
    ctx.fillStyle = "rgba(0,0,0,.07)"; ctx.fillRect(L.ax - b, L.ay + L.ah, L.aw + 2 * b, b);
    ctx.fillStyle = "rgba(0,0,0,.035)"; ctx.fillRect(L.ax + L.aw, L.ay - b, b, L.ah + 2 * b);
    draw();
    const d = W * 0.009;                               /* mat edge shades the top of the photo */
    let g = ctx.createLinearGradient(0, L.ay, 0, L.ay + d);
    g.addColorStop(0, "rgba(0,0,0,.26)"); g.addColorStop(1, "rgba(0,0,0,0)");
    ctx.fillStyle = g; ctx.fillRect(L.ax, L.ay, L.aw, d);
    g = ctx.createLinearGradient(L.ax, 0, L.ax + d * .6, 0);
    g.addColorStop(0, "rgba(0,0,0,.14)"); g.addColorStop(1, "rgba(0,0,0,0)");
    ctx.fillStyle = g; ctx.fillRect(L.ax, L.ay, d * .6, L.ah);
  } else {
    draw();
  }
  ctx.restore();
}

/* Loading: the browser decodes what it can (Safari handles iPhone HEIC);
   anything else goes to the app on your computer to convert. */
function loadInBrowser(blob) {
  return new Promise((res, rej) => {
    const url = URL.createObjectURL(blob), im = new Image();
    im.onload = () => res({img: im, w: im.naturalWidth, h: im.naturalHeight, url});
    im.onerror = () => { URL.revokeObjectURL(url); rej(new Error("decode")); };
    im.src = url;
  });
}
async function loadPhoto(file) {
  try { return await loadInBrowser(file); }
  catch (e) {
    const r = await fetch("/api/convert", {method: "POST", body: file});
    if (!r.ok) { let m = "Couldn't open " + file.name; try { m = (await r.json()).error; } catch (x) {} throw new Error(m); }
    return await loadInBrowser(await r.blob());
  }
}

function defaults() {
  return {style: recall("style", "gallery"), mat: recall("mat", 9), fit: recall("fit", "fill"), zoom: 1, fx: .5, fy: .5, turn: 0, tilt: 0};
}

(() => {
  const pad = $("#droppad"); let depth = 0;
  pad.onclick = () => $("#file").click();
  pad.addEventListener("dragenter", e => { e.preventDefault(); depth++; pad.classList.add("over"); });
  pad.addEventListener("dragleave", () => { if (--depth <= 0) { depth = 0; pad.classList.remove("over"); } });
  pad.addEventListener("drop", () => { depth = 0; pad.classList.remove("over"); });
  if (matchMedia("(pointer: coarse)").matches) {
    $("#dp-title").textContent = "Add photos"; $("#dp-sub").textContent = "Tap to choose from your photo library";
  }
})();
$("#file").onchange = e => { addFiles(e.target.files); e.target.value = ""; };
let dragDepth = 0;
addEventListener("dragenter", e => { if ([...e.dataTransfer.types].includes("Files")) { dragDepth++; document.body.classList.add("dropping"); } });
addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; document.body.classList.remove("dropping"); } });
addEventListener("dragover", e => e.preventDefault());
addEventListener("drop", e => { e.preventDefault(); dragDepth = 0; document.body.classList.remove("dropping"); addFiles(e.dataTransfer.files); });

async function addFiles(list) {
  const files = [...list].filter(f => f.type.startsWith("image/") || /\.(heic|heif|jpe?g|png|tiff?)$/i.test(f.name));
  if (!files.length) return toast("Those aren't image files.", true);
  const wasOpen = $("#editor").classList.contains("open");
  const start = items.length;
  files.forEach(f => items.push({file: f, name: f.name, s: defaults(), img: null, loading: true}));
  if (!wasOpen) {
    cur = 0; openEditor();
    if (tab.startsWith("c:")) $("#ed-col").value = tab.slice(2);
  } else { renderQueue(); }
  for (const it of items.slice(start)) {
    try { Object.assign(it, await loadPhoto(it.file)); }
    catch (e) { it.error = e.message; toast(e.message, true); }
    it.loading = false;
    if (items[cur] === it) refreshEditor(); else renderQueue();
  }
  items = items.filter(it => !it.error);
  if (!items.length) closeEditor(); else { cur = Math.min(cur, items.length - 1); refreshEditor(); }
}

function openEditor() {
  busy = false; $("#ed-go").disabled = false; $("#ed-cancel").disabled = false; $("#ed-note").textContent = "";
  $("#editor").classList.add("open"); document.body.style.overflow = "hidden";
  buildStyles(); refreshEditor();
}
function closeEditor() {
  items.forEach(it => it.url && URL.revokeObjectURL(it.url));
  items = []; cur = 0;
  $("#editor").classList.remove("open"); document.body.style.overflow = "";
}

function buildStyles() {
  const box = $("#styles"); box.innerHTML = "";
  STYLES.forEach(st => {
    const b = document.createElement("button");
    b.className = "style"; b.setAttribute("role", "radio"); b.dataset.id = st.id;
    b.innerHTML = '<canvas width="135" height="240"></canvas><span></span>';
    b.querySelector("span").textContent = st.name;
    b.onclick = () => { const it = items[cur]; if (!it) return; it.s.style = st.id; remember("style", st.id); refreshEditor(); };
    box.append(b);
  });
}

let raf = 0;
function refreshEditor() { cancelAnimationFrame(raf); raf = requestAnimationFrame(paintEditor); }
function paintEditor() {
  const it = items[cur], c = $("#ed-canvas"), wrap = $("#ed-wrap");
  $("#ed-count").textContent = items.length > 1 ? (cur + 1) + " of " + items.length : "";
  $("#ed-go").textContent = items.length > 1 ? "Hang " + items.length + " photos" : "Hang";
  $("#queue-block").hidden = items.length < 2;
  $("#ed-remove").hidden = items.length < 2;
  const old = wrap.querySelector(".loading"); if (old) old.remove();
  if (!it) return;
  if (!it.img) {
    wrap.insertAdjacentHTML("beforeend", '<div class="loading">Opening ' + escapeHtml(it.name) + '…</div>');
  } else {
    const dpr = Math.min(devicePixelRatio || 1, 2), cw = c.clientWidth || 300;
    c.width = Math.round(cw * dpr); c.height = Math.round(cw * dpr * 16 / 9);
    const ctx = c.getContext("2d");
    render(ctx, c.width, c.height, it);
    if (showGrid) drawGrid(ctx, layout(c.width, c.height, it));
  }
  const st = styleOf(it.s.style);
  document.querySelectorAll("#styles .style").forEach(b => {
    b.setAttribute("aria-checked", b.dataset.id === it.s.style ? "true" : "false");
    const cv = b.querySelector("canvas"), x = cv.getContext("2d");
    if (it.img) render(x, cv.width, cv.height, {...it, s: {...it.s, style: b.dataset.id}});
    else x.clearRect(0, 0, cv.width, cv.height);
  });
  $("#mat-field").hidden = !st.mat;
  $("#mat").value = it.s.mat; $("#mat-out").textContent = Math.round(it.s.mat) + "%";
  const whole = st.mat && it.s.fit === "whole";
  $("#fitseg").parentElement.hidden = !st.mat;
  document.querySelectorAll("#fitseg button").forEach(b => b.setAttribute("aria-pressed", b.dataset.fit === (st.mat ? it.s.fit : "fill") ? "true" : "false"));
  $("#zoom-field").hidden = whole;
  $("#tilt-field").hidden = whole;
  $("#tilt").value = it.s.tilt || 0;
  const t = it.s.tilt || 0;
  $("#tilt-out").textContent = (t > 0 ? "+" : t < 0 ? "−" : "") + Math.abs(t).toFixed(1) + "°";
  $("#tilt-reset").disabled = !t;
  $("#zoom").value = Math.round(it.s.zoom * 100); $("#zoom-out").textContent = it.s.zoom.toFixed(1) + "×";
  $("#ed-hint").textContent = whole ? "The whole photo is shown inside the mat."
    : matchMedia("(pointer: coarse)").matches ? "Drag to reposition. Pinch to zoom."
    : "Drag to reposition. Scroll to zoom. Option + scroll to straighten.";
  renderQueue();
}
let showGrid = false, gridTimer = 0;
function flashGrid() { showGrid = true; clearTimeout(gridTimer); gridTimer = setTimeout(() => { showGrid = false; refreshEditor(); }, 1200); }
function drawGrid(ctx, L) {
  /* square grid over the opening; the middle lines are brighter, for leveling horizons */
  ctx.save();
  ctx.beginPath(); ctx.rect(L.ax, L.ay, L.aw, L.ah); ctx.clip();
  const step = L.aw / 6, lw = Math.max(1, ctx.canvas.width / 500);
  const cx = L.ax + L.aw / 2, cy = L.ay + L.ah / 2;
  ctx.lineWidth = lw;
  for (let k = -6; k <= 6; k++) {
    const x = cx + k * step;
    ctx.strokeStyle = k === 0 ? "rgba(255,255,255,.7)" : "rgba(255,255,255,.28)";
    ctx.beginPath(); ctx.moveTo(x, L.ay); ctx.lineTo(x, L.ay + L.ah); ctx.stroke();
  }
  for (let k = -12; k <= 12; k++) {
    const y = cy + k * step;
    ctx.strokeStyle = k === 0 ? "rgba(255,255,255,.7)" : "rgba(255,255,255,.28)";
    ctx.beginPath(); ctx.moveTo(L.ax, y); ctx.lineTo(L.ax + L.aw, y); ctx.stroke();
  }
  ctx.restore();
}
function escapeHtml(s) { return String(s).replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c])); }

function renderQueue() {
  const q = $("#queue");
  if (q.children.length !== items.length) {
    q.innerHTML = "";
    items.forEach((_, i) => {
      const b = document.createElement("button");
      b.innerHTML = '<canvas width="104" height="185"></canvas>';
      b.onclick = () => { if (!busy) { cur = i; refreshEditor(); } };
      q.append(b);
    });
  }
  [...q.children].forEach((b, i) => {
    const it = items[i]; if (!it) return;
    b.setAttribute("aria-label", "Photo " + (i + 1) + ": " + it.name);
    b.setAttribute("aria-current", i === cur ? "true" : "false");
    b.classList.toggle("done", !!it.done);
    const cv = b.querySelector("canvas"), x = cv.getContext("2d");
    if (it.img) render(x, cv.width, cv.height, it); else x.clearRect(0, 0, cv.width, cv.height);
  });
}

$("#mat").oninput = e => { const it = items[cur]; if (!it) return; it.s.mat = +e.target.value; remember("mat", it.s.mat); refreshEditor(); };
function setTilt(v) {
  const it = items[cur]; if (!it) return;
  it.s.tilt = Math.round(Math.min(10, Math.max(-10, v)) * 10) / 10;
  flashGrid(); refreshEditor();
}
$("#tilt").oninput = e => setTilt(+e.target.value);
$("#tilt-minus").onclick = () => setTilt((items[cur] ? items[cur].s.tilt || 0 : 0) - 0.1);
$("#tilt-plus").onclick = () => setTilt((items[cur] ? items[cur].s.tilt || 0 : 0) + 0.1);
$("#tilt-reset").onclick = () => setTilt(0);
$("#tilt").ondblclick = () => setTilt(0);
const turn = d => { const it = items[cur]; if (!it || !it.img) return; it.s.turn = ((it.s.turn || 0) + d + 4) % 4; it.s.fx = it.s.fy = .5; refreshEditor(); };
$("#turn-left").onclick = () => turn(-1);
$("#turn-right").onclick = () => turn(1);
$("#zoom").oninput = e => { const it = items[cur]; if (!it) return; it.s.zoom = +e.target.value / 100; refreshEditor(); };
document.querySelectorAll("#fitseg button").forEach(b => b.onclick = () => {
  const it = items[cur]; if (!it) return; it.s.fit = b.dataset.fit; remember("fit", it.s.fit); refreshEditor();
});
$("#apply-all").onclick = () => {
  const s = items[cur] && items[cur].s; if (!s) return;
  items.forEach(it => { it.s.style = s.style; it.s.mat = s.mat; it.s.fit = s.fit; });
  refreshEditor(); toast("Frame applied to all " + items.length);
};
$("#ed-remove").onclick = () => {
  if (busy || items.length < 2) return;
  const [gone] = items.splice(cur, 1); if (gone.url) URL.revokeObjectURL(gone.url);
  cur = Math.min(cur, items.length - 1); $("#queue").innerHTML = ""; refreshEditor();
};
$("#ed-cancel").onclick = () => { if (!busy) closeEditor(); };
$("#ed-col").onchange = e => remember("col", e.target.value);
addEventListener("keydown", e => {
  if (!$("#editor").classList.contains("open") || busy) return;
  if (e.key === "Escape") closeEditor();
  if (e.key === "ArrowRight" && e.target.type !== "range" && cur < items.length - 1) { cur++; refreshEditor(); }
  if (e.key === "ArrowLeft" && e.target.type !== "range" && cur > 0) { cur--; refreshEditor(); }
});
addEventListener("resize", () => { if ($("#editor").classList.contains("open")) refreshEditor(); });

/* drag to reposition, pinch or scroll to zoom */
(() => {
  const c = $("#ed-canvas"), pts = new Map();
  let start = null, pinch = null;
  const geo = it => { const dpr = c.width / c.clientWidth; return {dpr, L: layout(c.width, c.height, it)}; };
  c.addEventListener("pointerdown", e => {
    const it = items[cur]; if (!it || !it.img || busy) return;
    c.setPointerCapture(e.pointerId); pts.set(e.pointerId, {x: e.clientX, y: e.clientY});
    if (pts.size === 2) {
      const [a, b] = [...pts.values()];
      pinch = {d: Math.hypot(a.x - b.x, a.y - b.y), zoom: it.s.zoom}; start = null;
    } else {
      start = {x: e.clientX, y: e.clientY, fx: it.s.fx, fy: it.s.fy};
    }
  });
  c.addEventListener("pointermove", e => {
    const it = items[cur]; if (!it || !pts.has(e.pointerId)) return;
    pts.set(e.pointerId, {x: e.clientX, y: e.clientY});
    if (pinch && pts.size === 2) {
      const [a, b] = [...pts.values()];
      it.s.zoom = Math.min(4, Math.max(1, pinch.zoom * Math.hypot(a.x - b.x, a.y - b.y) / pinch.d));
      return refreshEditor();
    }
    if (!start) return;
    const {dpr, L} = geo(it); if (L.whole) return;
    const ox = L.iw - L.sw, oy = L.ih - L.sh;
    const dx = e.clientX - start.x, dy = e.clientY - start.y, ca = Math.cos(L.angle), sa = Math.sin(L.angle);
    const ux = dx * ca + dy * sa, uy = -dx * sa + dy * ca;
    if (ox > 1) it.s.fx = Math.min(1, Math.max(0, start.fx - ux * dpr / L.scale / ox));
    if (oy > 1) it.s.fy = Math.min(1, Math.max(0, start.fy - uy * dpr / L.scale / oy));
    refreshEditor();
  });
  const end = e => {
    pts.delete(e.pointerId); if (pts.size < 2) pinch = null;
    const it = items[cur];
    if (pts.size === 1 && it) { const p = [...pts.values()][0]; start = {x: p.x, y: p.y, fx: it.s.fx, fy: it.s.fy}; }
    if (!pts.size) start = null;
  };
  c.addEventListener("pointerup", end); c.addEventListener("pointercancel", end);
  c.addEventListener("wheel", e => {
    const it = items[cur]; if (!it || !it.img) return; e.preventDefault();
    if (layout(c.width, c.height, it).whole) return;
    if (e.altKey) return setTilt((it.s.tilt || 0) + (e.deltaY > 0 ? 0.1 : -0.1));
    it.s.zoom = Math.min(4, Math.max(1, it.s.zoom * Math.exp(-e.deltaY * 0.0015)));
    refreshEditor();
  }, {passive: false});
})();

/* The original we keep on the box: the photo as opened, long edge at most 4096 px. */
function sourceJpeg(it) {
  const k = Math.min(1, 4096 / Math.max(it.w, it.h));
  const c = document.createElement("canvas"); c.width = Math.round(it.w * k); c.height = Math.round(it.h * k);
  c.getContext("2d").drawImage(it.img, 0, 0, c.width, c.height);
  return new Promise((res, rej) => c.toBlob(b => b ? res(b) : rej(new Error("Couldn't keep the original")), "image/jpeg", 0.9));
}
function recipeOf(it) {
  const s = it.s;
  return {style: s.style, mat: s.mat, fit: s.fit, zoom: s.zoom, fx: s.fx, fy: s.fy, turn: s.turn || 0, tilt: s.tilt || 0};
}
function finalJpeg(it) {
  const c = document.createElement("canvas"); c.width = OUT_W; c.height = OUT_H;
  render(c.getContext("2d"), OUT_W, OUT_H, it);
  return new Promise((res, rej) => c.toBlob(b => b ? res(b) : rej(new Error("Couldn't render the photo")), "image/jpeg", 0.93));
}

$("#ed-go").onclick = async () => {
  if (busy) return;
  if (items.some(it => !it.img)) return toast("Still opening photos. Try again in a moment.", true);
  busy = true;
  const go = $("#ed-go"), cancel = $("#ed-cancel"), note = $("#ed-note");
  go.disabled = cancel.disabled = true;
  let ok = 0;
  for (let i = 0; i < items.length; i++) {
    const it = items[i]; cur = i; refreshEditor();
    note.textContent = items.length > 1 ? "Hanging " + (i + 1) + " of " + items.length + "…" : "Hanging…";
    try {
      const blob = await finalJpeg(it);
      const last = i === items.length - 1 ? 1 : 0;
      const col = $("#ed-col").value;
      const q = "/api/upload?prepared=1&show=" + last + (col ? "&collection=" + encodeURIComponent(col) : "")
        + "&recipe=" + encodeURIComponent(JSON.stringify(recipeOf(it)))
        + (it.replaces ? "&replace=" + encodeURIComponent(it.replaces) : "");
      const r = await api(q, {method: "POST", body: blob});
      if (!it.fromBox && r.id) {
        try { await fetch("/api/original?id=" + encodeURIComponent(r.id), {method: "POST", body: await sourceJpeg(it)}); }
        catch (e) { toast(it.name + ": hung, but the original wasn't kept", true); }
      }
      it.done = true; ok++; renderQueue();
    } catch (e) { toast(it.name + ": " + e.message, true); }
  }
  const col = $("#ed-col").value, reframing = items.some(it => it.replaces), backTo = tab;
  closeEditor();
  if (ok) toast((reframing ? "Reframed " : "Hung ") + plural(ok, "photo"));
  tab = col ? "c:" + col : reframing ? backTo : "mine";
  await refreshLibrary(); refreshStatus();
};


/* ---------- display ---------- */
let display = null, dispTimer = 0;
function renderDisplay() {
  const d = display, s = $("#d-sensor"), b = $("#d-bright"), w = $("#d-warm");
  if (!d) return;
  s.disabled = d.sensor === null; s.setAttribute("aria-checked", d.sensor ? "true" : "false");
  $("#d-sensor-sub").textContent = d.sensor === null ? "The Frame didn't report this setting."
    : d.sensor ? "On. The art follows the room's light." : "Off. Brightness stays where you set it.";
  if (d.brightness) { b.disabled = false; b.min = d.brightness.min; b.max = d.brightness.max; b.value = d.brightness.value;
    $("#d-bright-out").textContent = d.brightness.value + " of " + d.brightness.max; }
  if (d.warmth) { w.disabled = false; w.min = d.warmth.min; w.max = d.warmth.max; w.value = d.warmth.value;
    const v = d.warmth.value; $("#d-warm-out").textContent = v === 0 ? "Neutral" : v > 0 ? "Warmer +" + v : "Cooler " + v; }
}
async function refreshDisplay() {
  try { display = await api("/api/display"); renderDisplay(); }
  catch (e) { $("#d-bright-out").textContent = "unavailable"; $("#d-warm-out").textContent = "unavailable"; }
}
async function sendDisplay(change) {
  try {
    const r = await post("/api/display", change);
    display = r; renderDisplay();
    if (r.missed && r.missed.length) toast("The Frame didn't take that change.", true);
  } catch (e) { toast(e.message, true); refreshDisplay(); }
}
$("#d-sensor").onclick = () => { if (!display) return; display.sensor = !display.sensor; renderDisplay(); sendDisplay({sensor: display.sensor}); };
$("#d-bright").oninput = e => {
  $("#d-bright-out").textContent = e.target.value + " of " + e.target.max;
  clearTimeout(dispTimer); dispTimer = setTimeout(() => sendDisplay({brightness: +e.target.value}), 350);
};
$("#d-warm").oninput = e => {
  const v = +e.target.value; $("#d-warm-out").textContent = v === 0 ? "Neutral" : v > 0 ? "Warmer +" + v : "Cooler " + v;
  clearTimeout(dispTimer); dispTimer = setTimeout(() => sendDisplay({warmth: v}), 350);
};

/* ---------- brightness schedule: editable times ---------- */
let sched = null, schedTimer = 0;
const OFFSETS = [-120, -90, -60, -45, -30, -15, 0, 15, 30, 45, 60, 90, 120, 180];
const offLabel = m => m === 0 ? "exactly" : (Math.abs(m) >= 60 && Math.abs(m) % 60 === 0 ? Math.abs(m) / 60 + " hr" : Math.abs(m) + " min") + (m < 0 ? " before" : " after");
const WARMTH = [["keep", "Leave warmth as is"], [-5, "Cooler 5"], [-4, "Cooler 4"], [-3, "Cooler 3"], [-2, "Cooler 2"], [-1, "Cooler 1"],
  [0, "Neutral"], [1, "Warmer 1"], [2, "Warmer 2"], [3, "Warmer 3"], [4, "Warmer 4"], [5, "Warmer 5"]];
function opt(sel, value, text, current) {
  const o = document.createElement("option"); o.value = value; o.textContent = text;
  if (String(value) === String(current)) o.selected = true; sel.append(o);
}
function slotRow(s) {
  const li = document.createElement("li"); li.className = "slot" + (s.current ? " now" : ""); li.dataset.id = s.id;
  const top = document.createElement("div"); top.className = "slot-top";
  const when = document.createElement("select"); when.setAttribute("aria-label", "Starts at");
  opt(when, "sunrise", "Sunrise", s.at); opt(when, "sunset", "Sunset", s.at); opt(when, "time", "A set time", s.at);
  top.append(when);
  if (s.at === "time") {
    const t = document.createElement("input"); t.type = "time"; t.value = s.time; t.setAttribute("aria-label", "Time");
    t.onchange = () => { if (t.value) editSlot(s.id, {time: t.value}); };
    top.append(t);
  } else {
    const o = document.createElement("select"); o.setAttribute("aria-label", "Offset");
    const list = OFFSETS.includes(s.offset) ? OFFSETS : OFFSETS.concat([s.offset]).sort((x, y) => x - y);
    list.forEach(m => opt(o, m, offLabel(m), s.offset));
    o.onchange = () => editSlot(s.id, {offset: +o.value});
    top.append(o);
  }
  when.onchange = () => editSlot(s.id, {at: when.value});
  const del = document.createElement("button"); del.className = "slot-del"; del.type = "button";
  del.textContent = "×"; del.setAttribute("aria-label", "Remove this time");
  del.disabled = sched.slots.length <= 1;
  del.onclick = () => saveSlots(sched.slots.filter(x => x.id !== s.id));
  top.append(del);
  const meta = document.createElement("p"); meta.className = "slot-meta";
  meta.textContent = (s.current ? "Now · " : "") + "today " + s.today;
  const f = document.createElement("div"); f.className = "field";
  f.innerHTML = '<label>Brightness <output></output></label><input type="range" min="0" max="10" step="1">';
  const r = f.querySelector("input"), out = f.querySelector("output");
  r.value = s.brightness; out.textContent = lvl(s.brightness); r.setAttribute("aria-label", "Brightness");
  r.oninput = () => { out.textContent = lvl(r.value); };
  r.onchange = () => editSlot(s.id, {brightness: +r.value});
  const opts = document.createElement("div"); opts.className = "slot-opts";
  const sen = document.createElement("label"); sen.className = "check";
  sen.innerHTML = '<input type="checkbox"> Light sensor';
  const cb = sen.querySelector("input"); cb.checked = !!s.sensor; cb.onchange = () => editSlot(s.id, {sensor: cb.checked});
  const w = document.createElement("select"); w.setAttribute("aria-label", "Warmth");
  WARMTH.forEach(([v, t]) => opt(w, v, t, s.warmth === null || s.warmth === undefined ? "keep" : s.warmth));
  w.onchange = () => editSlot(s.id, {warmth: w.value === "keep" ? null : +w.value});
  opts.append(sen, w);
  li.append(top, meta, f, opts);
  return li;
}
const lvl = v => v + " of 10";
function renderSched() {
  const d = sched; if (!d) return;
  $("#n-on").setAttribute("aria-checked", d.on ? "true" : "false");
  $("#n-box").hidden = !d.on;
  $("#d-bright-field").hidden = !!d.on;
  $("#d-sensor").hidden = !!d.on;
  const cur = d.slots.find(s => s.current);
  $("#n-sub").textContent = d.on && cur
    ? "Brightness " + cur.brightness + " now, until " + d.next + ". Sunrise " + d.sunrise + ", sunset " + d.sunset + "."
    : "Times can follow sunrise and sunset in Colgate. Today: sunrise " + d.sunrise + ", sunset " + d.sunset + ".";
  const box = $("#slots"); box.innerHTML = "";
  d.slots.forEach(s => box.append(slotRow(s)));
  $("#slot-add").disabled = d.slots.length >= d.max;
}
async function refreshSched() { try { sched = await api("/api/schedule"); renderSched(); } catch (e) {} }
async function sendSched(change) {
  try { sched = await post("/api/schedule", change); renderSched(); refreshDisplay(); }
  catch (e) { toast(e.message, true); refreshSched(); }
}
const plain = s => ({id: s.id, at: s.at, time: s.time, offset: s.offset, brightness: s.brightness, sensor: s.sensor, warmth: s.warmth});
function saveSlots(list) { sendSched({slots: list.map(plain)}); }
function editSlot(id, change) { saveSlots(sched.slots.map(s => s.id === id ? Object.assign({}, s, change) : s)); }
$("#n-on").onclick = () => { if (!sched) return; sendSched({on: !sched.on}); };
$("#slot-add").onclick = () => {
  if (!sched) return;
  const base = sched.slots.find(s => s.current) || sched.slots[0] || {brightness: 5, sensor: false};
  saveSlots(sched.slots.concat([{id: "", at: "time", time: "12:00", offset: 0, brightness: base.brightness, sensor: base.sensor, warmth: null}]));
};

/* ---------- import previews from the laptop version ---------- */
$("#import-btn").onclick = () => $("#import-file").click();
$("#import-file").onchange = async e => {
  const files = Array.from(e.target.files || []).filter(f => /^[A-Za-z0-9_\-]{1,64}\.(img|jpe?g)$/i.test(f.name));
  e.target.value = "";
  if (!files.length) { toast("No previews found in that folder.", true); return; }
  // thumbnails first, originals last, so the sharper original-made preview wins
  files.sort((a, b) => /\/full\//.test(a.webkitRelativePath) - /\/full\//.test(b.webkitRelativePath));
  let ok = 0, n = 0;
  for (const f of files) {
    n++;
    if (n % 5 === 1) toast("Importing " + n + " of " + files.length + "…");
    const kind = /(^|\/)full\//.test(f.webkitRelativePath) ? "full" : "thumbs";
    const id = f.name.replace(/\.[^.]+$/, "");
    try {
      const r = await fetch("/api/cache/import?kind=" + kind + "&id=" + encodeURIComponent(id), {method: "POST", body: f});
      if (r.ok) ok++;
    } catch (err) {}
  }
  toast("Imported " + ok + " of " + files.length + " previews.");
  refreshLibrary();
};

/* ---------- collapsible panels ---------- */
document.querySelectorAll("details.panel").forEach(d => {
  const saved = recall("open." + d.id, null);
  d.open = saved === null ? !(matchMedia("(max-width:860px)").matches && d.id === "disp-box") : saved;
  d.addEventListener("toggle", () => remember("open." + d.id, d.open));
});

/* ---------- start ---------- */
refreshStatus().then(refreshLibrary).then(refreshRotation).then(refreshDisplay).then(refreshSched);
setInterval(refreshSched, 300000);
setInterval(refreshRotation, 120000);
setInterval(refreshStatus, 20000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshStatus(); });
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
