"""Shared FastAPI dependencies: session loading, CSRF, current user, admin check."""
from __future__ import annotations

from types import SimpleNamespace

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import Session, User
from app.security import csrf, sessions


class AuthRequired(Exception):
    """Raised when a page or API needs a logged-in user."""


async def resolve_session(request: Request, db: AsyncSession) -> Session | None:
    if getattr(request.state, "session_resolved", False):
        return request.state.session
    session = await sessions.load_session(db, request.cookies.get(sessions.cookie_name()))
    request.state.session = session
    # Plain snapshots: error pages render after the DB session was rolled back and closed.
    request.state.csrf_secret = session.csrf_secret if session else None
    request.state.user = (
        SimpleNamespace(
            id=session.user.id,
            display_name=session.user.display_name,
            role=session.user.role,
            timezone=session.user.timezone,
        )
        if session
        else None
    )
    request.state.session_resolved = True
    return session


async def protect(request: Request, db: AsyncSession = Depends(get_db)) -> None:
    """Global dependency: loads the session, then enforces CSRF on state-changing requests."""
    await resolve_session(request, db)
    await csrf.enforce(request)


async def current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    session = await resolve_session(request, db)
    if session is None:
        raise AuthRequired()
    return session.user


async def admin_user(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Administrators only.")
    return user
