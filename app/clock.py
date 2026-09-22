"""Single source of 'now'. Tests replace `now` to control time."""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

_now: Callable[[], datetime] = lambda: datetime.now(UTC)  # noqa: E731


def utcnow() -> datetime:
    return _now()


def set_clock(fn: Callable[[], datetime] | None) -> None:
    global _now
    _now = fn if fn is not None else (lambda: datetime.now(UTC))
