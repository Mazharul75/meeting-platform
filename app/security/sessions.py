"""Server-side sessions. The cookie holds a random token; only its SHA-256 hash is stored."""
from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import timedelta

from fastapi import Request, Response
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import utcnow
from app.config import get_settings
from app.models import Session, User

_TOUCH_INTERVAL = timedelta(seconds=60)


def cookie_name() -> str:
    return "__Host-session" if get_settings().cookie_secure else "session"


def anon_cookie_name() -> str:
    return "__Host-csrf" if get_settings().cookie_secure else "csrf"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def set_cookie(response: Response, name: str, value: str, max_age: int | None) -> None:
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        httponly=True,
        secure=get_settings().cookie_secure,
        samesite="lax",
        path="/",
    )


async def create_session(db: AsyncSession, user: User, request: Request) -> tuple[str, Session]:
    settings = get_settings()
    now = utcnow()
    token = secrets.token_urlsafe(32)  # 256 bits
    row = Session(
        user_id=user.id,
        token_hash=hash_token(token),
        csrf_secret=secrets.token_urlsafe(32),
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(days=settings.session_absolute_days),
        ip=request.client.host if request.client else None,
        user_agent=(request.headers.get("user-agent") or "")[:300],
    )
    db.add(row)
    await db.flush()
    return token, row


async def load_session(db: AsyncSession, token: str | None) -> Session | None:
    """Return the live session for a cookie token, or None. Deletes expired sessions."""
    if not token or len(token) > 200:
        return None
    settings = get_settings()
    row = (
        await db.execute(select(Session).where(Session.token_hash == hash_token(token)))
    ).scalar_one_or_none()
    if row is None:
        return None
    now = utcnow()
    idle = timedelta(hours=settings.session_idle_hours)
    if now >= row.expires_at or now - row.last_seen_at >= idle or not row.user.is_active:
        await db.delete(row)
        await db.commit()  # persist now: the request will usually end in a redirect/rollback
        return None
    if now - row.last_seen_at >= _TOUCH_INTERVAL:
        row.last_seen_at = now
    return row


async def destroy_session(db: AsyncSession, token: str | None) -> None:
    if token:
        await db.execute(delete(Session).where(Session.token_hash == hash_token(token)))


async def destroy_user_sessions(db: AsyncSession, user_id: uuid.UUID) -> None:
    await db.execute(delete(Session).where(Session.user_id == user_id))


__all__ = [
    "User",
    "anon_cookie_name",
    "cookie_name",
    "create_session",
    "destroy_session",
    "destroy_user_sessions",
    "hash_token",
    "load_session",
    "set_cookie",
]
