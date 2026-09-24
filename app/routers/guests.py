"""Guest join by link: name, consent notice and device check. Guests never get an account."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.models import GuestInvite, Meeting
from app.security import ratelimit
from app.security.permissions import require_guest_access
from app.services import audit, livekit_tokens, participant_names
from app.services import meetings as svc
from app.services.e2ee_keys import meeting_e2ee_key
from app.templating import templates

router = APIRouter()

_guest_limit = Depends(ratelimit.limit_by_ip("guest_join", 30, 60))
_guest_token_limit = Depends(ratelimit.limit_by_ip("guest_livekit_token", 30, 60))


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


# ------------------------------------------------------------------------ online room (guest)


@router.get("/join/{token}/room", response_model=None, dependencies=[_guest_limit])
async def guest_room(request: Request, token: str, name: str = "", db: AsyncSession = Depends(get_db)) -> Response:
    found = await _lookup(db, token)
    if found is None:
        return _invalid(request)
    invite, meeting = found
    state = svc.guest_invite_state(invite, meeting)
    if state != "ok":
        return _invalid(request, state)
    require_guest_access(invite, meeting, "join_room")
    if not get_settings().livekit_configured:
        return templates.TemplateResponse(
            request, "errors/error.html",
            {"status": 503, "title": "Video is not set up yet",
             "message": "The administrator has not configured the online video service yet."},
            status_code=503,
        )
    display_name = (name or "Guest").strip()[:60] or "Guest"
    return templates.TemplateResponse(
        request,
        "room_online.html",
        {
            "meeting": meeting,
            "is_host": False,
            "can_record": False,
            "can_end": False,
            "room_config": {
                "meetingId": str(meeting.id),
                "tokenUrl": f"/join/{token}/livekit-token?name={display_name}",
                "participantsUrl": f"/join/{token}/participants",
                "endUrl": "",
                "isHost": False,
            },
        },
    )


@router.post("/join/{token}/livekit-token", response_model=None, dependencies=[_guest_token_limit])
async def guest_livekit_token(
    request: Request, token: str, name: str = "", db: AsyncSession = Depends(get_db)
) -> Response:
    """Body is optional; the name may also arrive as a query string (see guest_room above),
    since a guest has no account or CSRF-protected session to attach a JSON body to safely."""
    found = await _lookup(db, token)
    if found is None:
        return _invalid(request)
    invite, meeting = found
    state = svc.guest_invite_state(invite, meeting)
    if state != "ok":
        return _invalid(request, state)
    require_guest_access(invite, meeting, "join_room")
    if not get_settings().livekit_configured:
        raise HTTPException(status_code=503, detail="Online video is not configured.")
    if meeting.status not in ("scheduled", "live"):
        raise HTTPException(status_code=409, detail="This meeting is closed.")
    settings = get_settings()
    if meeting.status == "scheduled":
        svc.transition(meeting, "live")
        await audit.log(db, "meeting.live", target_type="meeting", target_id=meeting.id, request=request)

    display_name = (name or "Guest").strip()[:60] or "Guest"
    room_name = str(meeting.room_name)
    identity = livekit_tokens.new_identity()
    participant_names.register(room_name, identity, display_name)
    jwt_token = livekit_tokens.create_join_token(settings, room_name, identity)
    await audit.log(
        db, "livekit.token", target_type="meeting", target_id=meeting.id, request=request,
        details={"identity": identity, "guest": True},
    )
    await db.commit()
    return JSONResponse(
        {
            "url": settings.livekit_url,
            "token": jwt_token,
            "identity": identity,
            "display_name": display_name,
            "e2ee_key": meeting_e2ee_key(settings.master_key, meeting.id),
        }
    )


@router.get("/join/{token}/participants", response_model=None, dependencies=[_guest_limit])
async def guest_participants(request: Request, token: str, db: AsyncSession = Depends(get_db)) -> Response:
    found = await _lookup(db, token)
    if found is None:
        return _invalid(request)
    invite, meeting = found
    require_guest_access(invite, meeting, "join_room")
    return JSONResponse({"participants": participant_names.names_for_room(str(meeting.room_name))})
