"""Small in-memory sliding-window rate limiter (per process; fine for one free instance)."""
from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Callable

from fastapi import HTTPException, Request

_buckets: dict[str, deque[float]] = defaultdict(deque)
_clock: Callable[[], float] = time.monotonic


def hit(key: str, limit: int, window_seconds: int) -> bool:
    """Record an event; return False when the limit is exceeded."""
    now = _clock()
    q = _buckets[key]
    while q and q[0] <= now - window_seconds:
        q.popleft()
    if len(q) >= limit:
        return False
    q.append(now)
    return True


def reset_all() -> None:
    _buckets.clear()


def set_clock(fn: Callable[[], float] | None) -> None:
    global _clock
    _clock = fn or time.monotonic


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def enforce(key: str, limit: int, window_seconds: int) -> None:
    if not hit(key, limit, window_seconds):
        raise HTTPException(status_code=429, detail="Too many requests. Please wait a moment.")


def limit_by_ip(name: str, limit: int, window_seconds: int) -> Callable[[Request], None]:
    def dependency(request: Request) -> None:
        enforce(f"{name}:ip:{client_ip(request)}", limit, window_seconds)

    return dependency
