"""CSRF tokens. Token = HMAC(SECRET_KEY, per-session secret). Anonymous pages use a cookie seed."""
from __future__ import annotations

import hashlib
import hmac
import secrets

from fastapi import HTTPException, Request

from app.config import get_settings

CSRF_HEADER = "x-csrf-token"
CSRF_FIELD = "csrf_token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# Signed-URL endpoints authenticate by signature, not by cookie.
EXEMPT_PREFIXES = ("/_local_storage/",)


def make_token(secret_material: str) -> str:
    key = get_settings().secret_key.encode() or b"dev-only-insecure-key"
    return hmac.new(key, secret_material.encode(), hashlib.sha256).hexdigest()


def new_anon_seed() -> str:
    return secrets.token_urlsafe(32)


def token_for_request(request: Request) -> str:
    secret = getattr(request.state, "csrf_secret", None)
    if secret:
        return make_token("session:" + secret)
    return make_token("anon:" + getattr(request.state, "anon_seed", ""))


def is_valid(request: Request, supplied: str | None) -> bool:
    if not supplied:
        return False
    return hmac.compare_digest(token_for_request(request), supplied)


async def enforce(request: Request) -> None:
    """Raise 403 unless the request carries a valid token (header or form field)."""
    if request.method in SAFE_METHODS or request.url.path.startswith(EXEMPT_PREFIXES):
        return
    supplied = request.headers.get(CSRF_HEADER)
    if not supplied:
        ctype = request.headers.get("content-type", "")
        if ctype.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
            form = await request.form()
            value = form.get(CSRF_FIELD)
            supplied = value if isinstance(value, str) else None
    if not is_valid(request, supplied):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid.")
