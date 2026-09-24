"""Jinja2 setup (auto-escaping stays on) and template helpers."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Request
from starlette.templating import Jinja2Templates

from app.config import get_settings
from app.security import csrf
from app.services.meetings import to_local

BASE_DIR = Path(__file__).parent


FLASH = {
    "created": "Done - the account was created.",
    "pw_changed": "The password was changed and the user was signed out everywhere.",
    "deactivated": "The account was deactivated.",
    "activated": "The account was activated.",
    "meeting_created": "Your meeting was scheduled.",
    "cancelled": "The meeting was cancelled.",
    "invited": "Staff were invited.",
    "revoked": "The guest link was revoked.",
}


def _context(request: Request) -> dict[str, Any]:
    return {
        "flash": FLASH.get(request.query_params.get("ok", "")),
        "csrf_token": csrf.token_for_request(request),
        "current_user": getattr(request.state, "user", None),
        "app_env": get_settings().app_env,
    }


templates = Jinja2Templates(directory=str(BASE_DIR / "templates"), context_processors=[_context])


def fmt_dt(value: datetime | None, tz_name: str = "UTC", with_zone: bool = True) -> str:
    if value is None:
        return "-"
    local = to_local(value, tz_name)
    text = local.strftime("%a %d %b %Y, %H:%M")
    return f"{text} ({tz_name})" if with_zone else text


def fmt_duration(ms: int | None) -> str:
    total = int((ms or 0) / 1000)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_bytes(n: int | None) -> str:
    size = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


templates.env.filters["dt"] = fmt_dt
templates.env.filters["duration"] = fmt_duration
templates.env.filters["filesize"] = fmt_bytes
