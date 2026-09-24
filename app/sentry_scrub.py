"""Strip anything personal from Sentry events before they leave the server."""
from __future__ import annotations

from typing import Any

from app.logging_setup import redact


def scrub_event(event: dict[str, Any], hint: dict[str, Any] | None = None) -> dict[str, Any]:
    request = event.get("request")
    if isinstance(request, dict):
        for key in ("cookies", "headers", "data", "query_string", "env"):
            request.pop(key, None)
        if isinstance(request.get("url"), str):
            request["url"] = redact(request["url"].split("?", 1)[0])
    event.pop("user", None)
    for exc in (event.get("exception") or {}).get("values", []):
        if isinstance(exc.get("value"), str):
            exc["value"] = redact(exc["value"])
    if isinstance(event.get("message"), str):
        event["message"] = redact(event["message"])
    return event
