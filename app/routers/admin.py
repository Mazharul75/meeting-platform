"""Admin: create staff, reset passwords, deactivate/reactivate."""
from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import utcnow
from app.config import get_settings
from app.db import get_db
from app.deps import admin_user
from app.models import User
from app.security import sessions
from app.security.passwords import hash_password, validate_new_password
from app.services import audit
from app.services.meetings import valid_timezone
from app.templating import templates

router = APIRouter(prefix="/admin")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


async def _render(
    request: Request, db: AsyncSession, errors: dict[str, str] | None = None, status: int = 200,
    notice: str | None = None,
) -> Response:
    users = (await db.execute(select(User).order_by(User.created_at))).scalars().all()
    return templates.TemplateResponse(
        request,
        "admin_users.html",
        {"users": users, "errors": errors or {}, "notice": notice, "now_utc": utcnow(), "default_tz": get_settings().default_timezone},
        status_code=status,
    )


@router.get("/users", response_model=None)
async def users_page(
    request: Request, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)
) -> Response:
    return await _render(request, db)


@router.post("/users", response_model=None)
async def create_user(
    request: Request,
    email: str = Form("", max_length=254),
    display_name: str = Form("", max_length=100),
    password: str = Form("", max_length=500),
    role: str = Form("member"),
    timezone: str = Form(""),
    admin: User = Depends(admin_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    errors: dict[str, str] = {}
    email = email.strip().lower()
    display_name = display_name.strip()
    timezone = timezone.strip() or get_settings().default_timezone
    if not _EMAIL_RE.match(email):
        errors["email"] = "Please enter a valid email address."
    elif (await db.execute(select(User).where(User.email == email))).scalar_one_or_none():
        errors["email"] = "An account with this email already exists."
    if not display_name:
        errors["display_name"] = "Please enter a name."
    if (msg := validate_new_password(password)) is not None:
        errors["password"] = msg
    if role not in ("admin", "member"):
        errors["role"] = "Invalid role."
    if not valid_timezone(timezone):
        errors["timezone"] = "Please choose a valid time zone."
    if errors:
        return await _render(request, db, errors, status=422)

    user = User(
        email=email,
        display_name=display_name,
        password_hash=hash_password(password),
        role=role,
        timezone=timezone,
    )
    db.add(user)
    await db.flush()
    await audit.log(
        db, "user.create", actor=admin.id, target_type="user", target_id=user.id, request=request,
        details={"role": role},
    )
    await db.commit()
    return RedirectResponse("/admin/users?ok=created", status_code=303)


async def _target(db: AsyncSession, user_id: uuid.UUID) -> User:
    from app.security.permissions import not_found

    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise not_found()
    return user


@router.post("/users/{user_id}/reset-password", response_model=None)
async def reset_password(
    request: Request,
    user_id: uuid.UUID,
    password: str = Form("", max_length=500),
    admin: User = Depends(admin_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    target = await _target(db, user_id)
    if (msg := validate_new_password(password)) is not None:
        return await _render(request, db, {"password": msg}, status=422)
    target.password_hash = hash_password(password)
    target.failed_logins = 0
    target.locked_until = None
    await sessions.destroy_user_sessions(db, target.id)
    await audit.log(db, "user.reset_password", actor=admin.id, target_type="user", target_id=target.id, request=request)
    await db.commit()
    return RedirectResponse("/admin/users?ok=pw_changed", status_code=303)


@router.post("/users/{user_id}/deactivate", response_model=None)
async def deactivate(
    request: Request,
    user_id: uuid.UUID,
    admin: User = Depends(admin_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    target = await _target(db, user_id)
    if target.role == "admin" and target.is_active:
        active_admins = (
            await db.execute(
                select(func.count()).select_from(User).where(User.role == "admin", User.is_active.is_(True))
            )
        ).scalar_one()
        if active_admins <= 1:
            return await _render(request, db, {"general": "The last administrator cannot be deactivated."}, status=409)
    target.is_active = False
    await sessions.destroy_user_sessions(db, target.id)
    await audit.log(db, "user.deactivate", actor=admin.id, target_type="user", target_id=target.id, request=request)
    await db.commit()
    return RedirectResponse("/admin/users?ok=deactivated", status_code=303)


@router.post("/users/{user_id}/activate", response_model=None)
async def activate(
    request: Request,
    user_id: uuid.UUID,
    admin: User = Depends(admin_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    target = await _target(db, user_id)
    target.is_active = True
    await audit.log(db, "user.activate", actor=admin.id, target_type="user", target_id=target.id, request=request)
    await db.commit()
    return RedirectResponse("/admin/users?ok=activated", status_code=303)
