"""Guest join by link: name, consent notice and device check. Guests never get an account."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import GuestInvite, Meeting
from app.security import ratelimit
from app.security.permissions import require_guest_access
from app.services import audit
from app.services import meetings as svc
from app.templating import templates

router = APIRouter()

_guest_limit = Depends(ratelimit.limit_by_ip("guest_join", 30, 60))


async def _lookup(db: AsyncSession, token: str) -> tuple[GuestInvite, Meeting] | None:
    if not token or len(token) > 100:
        return None
    invite = (
        await db.execute(select(GuestInvite).where(GuestInvite.token_hash == svc.hash_guest_token(token)))
    ).scalar_one_or_none()
    if invite is None:
        return None
    meeting = (await db.execute(select(Meeting).where(Meeting.id == invite.meeting_id))).scalar_one()
    return invite, meeting


def _invalid(request: Request, reason: str = "invalid") -> Response:
    return templates.TemplateResponse(request, "guest_invalid.html", {"reason": reason}, status_code=404)


@router.get("/join/{token}", response_model=None, dependencies=[_guest_limit])
async def guest_join_page(request: Request, token: str, db: AsyncSession = Depends(get_db)) -> Response:
    found = await _lookup(db, token)
    if found is None:
        return _invalid(request)
    invite, meeting = found
    state = svc.guest_invite_state(invite, meeting)
    if state != "ok":
        return _invalid(request, state)
    require_guest_access(invite, meeting, "join_room")
    invite.use_count += 1
    await db.commit()
    return templates.TemplateResponse(request, "guest_join.html", {"meeting": meeting, "token": token, "step": "consent"})


@router.post("/join/{token}", response_model=None, dependencies=[_guest_limit])
async def guest_consent(
    request: Request,
    token: str,
    name: Annotated[str, Form(max_length=200)] = "",
    consent: Annotated[str, Form(max_length=10)] = "",
    db: AsyncSession = Depends(get_db),
) -> Response:
    found = await _lookup(db, token)
    if found is None:
        return _invalid(request)
    invite, meeting = found
    state = svc.guest_invite_state(invite, meeting)
    if state != "ok":
        return _invalid(request, state)
    require_guest_access(invite, meeting, "join_room")
    name = name.strip()[:60]
    errors: dict[str, str] = {}
    if not name:
        errors["name"] = "Please enter your name."
    if consent != "yes":
        errors["consent"] = "Please confirm that you understand this meeting may be recorded."
    if errors:
        return templates.TemplateResponse(
            request,
            "guest_join.html",
            {"meeting": meeting, "token": token, "step": "consent", "errors": errors, "name": name},
            status_code=422,
        )
    await audit.log(
        db, "guest.consent", target_type="meeting", target_id=meeting.id, request=request,
        details={"invite_id": str(invite.id)},
    )
    await db.commit()
    return templates.TemplateResponse(
        request, "guest_join.html", {"meeting": meeting, "token": token, "step": "lobby", "name": name}
    )
