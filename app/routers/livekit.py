"""LiveKit token issuing for staff and admins (plan Section 10.1). Guests use the token routes
in routers/guests.py instead, since a guest has no account or server-side session."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.deps import current_user
from app.models import User
from app.security import permissions as perm
from app.security import ratelimit
from app.services import audit, livekit_tokens, participant_names
from app.services.e2ee_keys import meeting_e2ee_key
from app.services.meetings import transition

router = APIRouter(prefix="/api/meetings/{meeting_id}")


def _require_livekit() -> None:
    if not get_settings().livekit_configured:
        raise HTTPException(status_code=503, detail="Online video is not configured.")


@router.post("/livekit-token", response_model=None)
async def issue_token(
    request: Request,
    meeting_id: uuid.UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    ratelimit.enforce(f"lk_token:{user.id}", 30, 60)
    meeting, _ = await perm.require_meeting_access(db, user, meeting_id, "join_room")
    _require_livekit()
    if meeting.status not in ("scheduled", "live"):
        raise HTTPException(status_code=409, detail="This meeting is closed.")
    settings = get_settings()
    if meeting.status == "scheduled":
        transition(meeting, "live")
        await audit.log(db, "meeting.live", actor=user.id, target_type="meeting", target_id=meeting.id, request=request)

    room_name = str(meeting.room_name)
    identity = livekit_tokens.new_identity()
    participant_names.register(room_name, identity, user.display_name)
    token = livekit_tokens.create_join_token(settings, room_name, identity)
    await audit.log(
        db, "livekit.token", actor=user.id, target_type="meeting", target_id=meeting.id, request=request,
        details={"identity": identity},
    )
    await db.commit()
    return JSONResponse(
        {
            "url": settings.livekit_url,
            "token": token,
            "identity": identity,
            "display_name": user.display_name,
            "e2ee_key": meeting_e2ee_key(settings.master_key, meeting.id),
        }
    )


@router.get("/participants", response_model=None)
async def participants(
    meeting_id: uuid.UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    ratelimit.enforce(f"lk_participants:{user.id}", 120, 60)
    meeting, _ = await perm.require_meeting_access(db, user, meeting_id, "join_room")
    return JSONResponse({"participants": participant_names.names_for_room(str(meeting.room_name))})
