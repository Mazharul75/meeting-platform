"""Login and logout with lockout, rate limiting and a generic error message."""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import utcnow
from app.config import get_settings
from app.db import get_db
from app.deps import resolve_session
from app.models import User
from app.security import ratelimit, sessions
from app.security.passwords import verify_password
from app.services import audit
from app.templating import templates

router = APIRouter()


def _login_rate_limit(request: Request) -> None:
    ratelimit.enforce(f"login:ip:{ratelimit.client_ip(request)}", get_settings().login_rate_per_minute, 60)

LOGIN_ERROR = (
    "Invalid email or password. After 5 wrong attempts in a row, "
    "the account is locked for 15 minutes."
)


@router.get("/login", response_model=None)
async def login_form(request: Request, db: AsyncSession = Depends(get_db)) -> Response:
    if await resolve_session(request, db):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post(
    "/login",
    response_model=None,
    dependencies=[Depends(_login_rate_limit)],
)
async def login(
    request: Request,
    email: str = Form("", max_length=254),
    password: str = Form("", max_length=500),
    db: AsyncSession = Depends(get_db),
) -> Response:
    settings = get_settings()
    now = utcnow()
    user = (
        await db.execute(select(User).where(User.email == email.strip().lower()))
    ).scalar_one_or_none()
    if user is not None and not user.is_active:
        user = None
    locked = user is not None and user.locked_until is not None and now < user.locked_until
    # Always verify (against a dummy hash for unknown users) so timing does not reveal accounts.
    password_ok = verify_password(user.password_hash if user else None, password)

    if user is not None and password_ok and not locked:
        user.failed_logins = 0
        user.locked_until = None
        user.last_login_at = now
        token, _ = await sessions.create_session(db, user, request)
        await audit.log(db, "login.ok", actor=user.id, target_type="user", target_id=user.id, request=request)
        await db.commit()
        response = RedirectResponse("/", status_code=303)
        sessions.set_cookie(
            response, sessions.cookie_name(), token, max_age=settings.session_absolute_days * 86400
        )
        return response

    if user is not None and locked:
        await audit.log(db, "login.blocked", actor=user.id, target_type="user", target_id=user.id, request=request)
    elif user is not None:
        user.failed_logins += 1
        if user.failed_logins >= settings.login_max_failures:
            user.locked_until = now + timedelta(minutes=settings.login_lock_minutes)
            user.failed_logins = 0
            await audit.log(db, "login.locked", actor=user.id, target_type="user", target_id=user.id, request=request)
        await audit.log(db, "login.failed", actor=user.id, target_type="user", target_id=user.id, request=request)
    else:
        await audit.log(db, "login.failed", request=request, details={"reason": "unknown_or_inactive"})
    await db.commit()
    return templates.TemplateResponse(request, "login.html", {"error": LOGIN_ERROR}, status_code=401)


@router.post("/logout", response_model=None)
async def logout(request: Request, db: AsyncSession = Depends(get_db)) -> Response:
    token = request.cookies.get(sessions.cookie_name())
    session = await resolve_session(request, db)
    if session is not None:
        await audit.log(db, "logout", actor=session.user_id, request=request)
    await sessions.destroy_session(db, token)
    await db.commit()
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(sessions.cookie_name(), path="/")
    return response
