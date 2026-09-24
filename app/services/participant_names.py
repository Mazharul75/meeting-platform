"""Maps a room's random LiveKit identities to display names, kept only on this server.

LiveKit itself never learns real names (plan Section 10.1): the browser asks this server,
by room, for the name that belongs to each identity it sees connected.
"""
from __future__ import annotations

import time
from collections import defaultdict

_TTL_SECONDS = 15 * 60
# room_name -> identity -> (name, expires_at)
_registry: dict[str, dict[str, tuple[str, float]]] = defaultdict(dict)


def register(room_name: str, identity: str, name: str) -> None:
    _registry[room_name][identity] = (name[:60], time.monotonic() + _TTL_SECONDS)


def names_for_room(room_name: str) -> dict[str, str]:
    now = time.monotonic()
    live = {k: v for k, v in _registry[room_name].items() if v[1] > now}
    _registry[room_name] = live
    return {identity: name for identity, (name, _exp) in live.items()}


def forget_room(room_name: str) -> None:
    _registry.pop(room_name, None)


def reset_all() -> None:
    _registry.clear()
