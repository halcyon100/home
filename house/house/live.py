"""One live picture of the house.

House subscribes once to Home Assistant's stream of state changes and keeps every entity's current
state in memory. Every screen (Home, Light, Devices, the kitchen board) reads from this one picture,
so they agree, and open pages are told the moment something changes instead of each polling the
whole house.

If the connection drops, readers fall back to asking Home Assistant directly until it's back.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time

import aiohttp

log = logging.getLogger("house.live")
WS = "ws://supervisor/core/websocket"


class Live:
    def __init__(self):
        self.token = os.environ.get("SUPERVISOR_TOKEN", "")
        self.states: dict[str, dict] = {}
        self.ready = False
        self.version = 0                  # goes up on every change worth redrawing for
        self.changed = asyncio.Event()    # set on each change; waiters clear their own copy
        self.since = 0.0

    def snapshot(self) -> list[dict]:
        return list(self.states.values())

    def _bump(self):
        self.version += 1
        self.changed.set()
        self.changed = asyncio.Event()

    async def run(self, session: aiohttp.ClientSession):
        backoff = 5
        while True:
            try:
                async with session.ws_connect(WS, heartbeat=30, max_msg_size=64 * 1024 * 1024) as ws:
                    msg = await ws.receive_json()
                    if msg.get("type") != "auth_required":
                        raise RuntimeError(f"unexpected hello {msg.get('type')}")
                    await ws.send_json({"type": "auth", "access_token": self.token})
                    msg = await ws.receive_json()
                    if msg.get("type") != "auth_ok":
                        raise RuntimeError("Home Assistant refused the connection")
                    await ws.send_json({"id": 1, "type": "get_states"})
                    await ws.send_json({"id": 2, "type": "subscribe_events", "event_type": "state_changed"})
                    backoff = 5
                    async for m in ws:
                        if m.type != aiohttp.WSMsgType.TEXT:
                            if m.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
                            continue
                        data = json.loads(m.data)
                        if data.get("id") == 1 and data.get("type") == "result":
                            self.states = {s["entity_id"]: s for s in data.get("result") or []}
                            self.ready, self.since = True, time.time()
                            log.info("Live: %d entities, following changes", len(self.states))
                            self._bump()
                        elif data.get("type") == "event":
                            ev = (data.get("event") or {}).get("data") or {}
                            e, new = ev.get("entity_id"), ev.get("new_state")
                            if not e:
                                continue
                            old = self.states.get(e)
                            if new is None:
                                self.states.pop(e, None)
                            else:
                                self.states[e] = new
                            # attribute-only chatter (a sensor's last_reported, a media position) doesn't redraw
                            if old is None or new is None or old.get("state") != new.get("state") or _looks(old) != _looks(new):
                                self._bump()
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                log.warning("Live: connection lost (%s); retrying in %ds", ex, backoff)
            self.ready = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)


LOOKS = ("brightness", "color_temp_kelvin", "current_position", "media_title", "media_artist", "temperature",
         "humidity", "volume_level", "source", "group_members", "entity_picture")


def _looks(s: dict) -> tuple:
    a = s.get("attributes") or {}
    return tuple(str(a.get(k)) for k in LOOKS)
