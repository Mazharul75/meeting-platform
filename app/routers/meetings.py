"""Dashboard, scheduling, meeting detail, invites, cancel, guest links, calendar file, lobby."""
from __future__ import annotations

import uuid
import zoneinfo
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import utcnow
from app.config import get_settings
from app.db import get_db
from app.deps import current_user
from app.models import GuestInvite, Meeting, MeetingMember, Recording, User
from app.security import permissions as perm
from app.services import audit, livekit_tokens, participant_names
from app.services import meetings as svc
from app.services.ics import meeting_ics
from app.templating import templates

router = APIRouter()


async def _end_meeting(db: AsyncSession, meeting: Meeting, user: User, request: Request) -> None:
    """Shared by both end-meeting routes: moves the meeting to 'ended' and closes the LiveKit
    room so nobody can stay connected (plan Flow B step 6)."""
    svc.transition(meeting, "ended")
    await audit.log(db, "meeting.end", actor=user.id, target_type="meeting", target_id=meeting.id, request=request)
    await livekit_tokens.close_room(get_settings(), str(meeting.room_name))
    participant_names.forget_room(str(meeting.room_name))


def _tz_names() -> list[str]:
    return sorted(zoneinfo.available_timezones())


async def _staff_choices(db: AsyncSession, exclude: uuid.UUID) -> list[User]:
    stmt = select(User).where(User.is_active.is_(True), User.id != exclude).order_by(User.display_name)
    return list((await db.execute(stmt)).scalars().all())


# ------------------------------------------------------------------------------ dashboard


@router.get("/", response_model=None)
async def dashboard(
    request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> Response:
    now = utcnow()
    visible = perm.visible_meeting_filter(user)
    upcoming = (
        (
            await db.execute(
                select(Meeting)
                .where(
                    visible,
                    Meeting.status.in_(["scheduled", "live"]),
                    Meeting.scheduled_end >= now - timedelta(hours=1),
                )
                .order_by(Meeting.scheduled_start)
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    recent = (
        (
            await db.execute(
                select(Recording)
                .join(Meeting, Recording.meeting_id == Meeting.id)
                .where(visible, Recording.status != "deleted")
                .order_by(Recording.created_at.desc())
                .limit(5)
            )
        )
        .scalars()
        .all()
    )
    used = (
        await db.execute(
            select(func.coalesce(func.sum(Recording.total_bytes), 0)).where(Recording.status != "deleted")
        )
    ).scalar_one()
    quota = get_settings().storage_quota_bytes
    percent = min(100, int(used * 100 / quota)) if quota else 0
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"upcoming": upcoming, "recent": recent, "used": used, "quota": quota, "percent": percent},
    )


# ------------------------------------------------------------------------------ scheduling


async def _new_form(
    request: Request,
    user: User,
    db: AsyncSession,
    values: dict[str, object],
    errors: dict[str, str],
    status: int = 200,
) -> Response:
    return templates.TemplateResponse(
        request,
        "meeting_new.html",
        {
            "staff": await _staff_choices(db, user.id),
            "timezones": _tz_names(),
            "values": values,
            "errors": errors,
        },
        status_code=status,
    )


@router.get("/meetings/new", response_model=None)
async def meeting_new(
    request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> Response:
    values = {"timezone": user.timezone, "duration": 60, "mode": "online", "invited": []}
    return await _new_form(request, user, db, values, {})


@router.post("/meetings/new", response_model=None)
async def meeting_create(
    request: Request,
    title: Annotated[str, Form(max_length=1000)] = "",
    description: Annotated[str, Form(max_length=10000)] = "",
    mode: Annotated[str, Form(max_length=20)] = "online",
    start: Annotated[str, Form(max_length=40)] = "",
    duration: Annotated[int, Form()] = 60,
    timezone: Annotated[str, Form(max_length=100)] = "",
    user_ids: Annotated[list[uuid.UUID], Form()] = [],  # noqa: B006
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    parsed, errors = svc.parse_meeting_form(title, description, mode, start, duration, timezone.strip())
    invitees: list[User] = []
    if user_ids:
        invitees = list(
            (
                await db.execute(
                    select(User).where(User.id.in_(user_ids), User.is_active.is_(True), User.id != user.id)
                )
            )
            .scalars()
            .all()
        )
        if len(invitees) != len(set(user_ids) - {user.id}):
            errors["invited"] = "One of the invited people is not valid."
    if parsed is None or errors:
        values = {
            "title": title,
            "description": description,
            "mode": mode,
            "start": start,
            "duration": duration,
            "timezone": timezone,
            "invited": [str(i) for i in user_ids],
        }
        return await _new_form(request, user, db, values, errors, status=422)

    meeting = Meeting(
        id=uuid.uuid4(),
        title=parsed.title,
        description=parsed.description,
        mode=parsed.mode,
        scheduled_start=parsed.start_utc,
        scheduled_end=parsed.end_utc,
        timezone=parsed.timezone,
        host_id=user.id,
        status="scheduled",
        room_name=uuid.uuid4(),
    )
    meeting.members.append(MeetingMember(user_id=user.id, role="host"))
    for person in invitees:
        meeting.members.append(MeetingMember(user_id=person.id, role="member"))
    db.add(meeting)
    await db.flush()
    await audit.log(
        db, "meeting.create", actor=user.id, target_type="meeting", target_id=meeting.id, request=request,
        details={"mode": meeting.mode, "invited": len(invitees)},
    )
    await db.commit()
    return RedirectResponse(f"/meetings/{meeting.id}?ok=meeting_created", status_code=303)


# ---------------------------------------------------------------------------------- detail


async def _detail_page(
    request: Request,
    db: AsyncSession,
    user: User,
    meeting: Meeting,
    role: perm.Role,
    new_link: str | None = None,
    status: int = 200,
    error: str | None = None,
) -> Response:
    can = {a: perm.is_allowed(role, a) for a in perm.ALL_ACTIONS}
    invites: list[GuestInvite] = []
    if can["manage_guest_links"]:
        invites = list(
            (await db.execute(select(GuestInvite).where(GuestInvite.meeting_id == meeting.id).order_by(GuestInvite.valid_from)))
            .scalars()
            .all()
        )
    recordings = list(
        (
            await db.execute(
                select(Recording)
                .where(Recording.meeting_id == meeting.id, Recording.status != "deleted")
                .order_by(Recording.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    invited_ids = {m.user_id for m in meeting.members}
    staff = await _staff_choices(db, meeting.host_id)
    return templates.TemplateResponse(
        request,
        "meeting_detail.html",
        {
            "meeting": meeting,
            "role": role,
            "can": can,
            "invites": invites,
            "recordings": recordings,
            "new_link": new_link,
            "error": error,
            "now": utcnow(),
            "uninvited_staff": [s for s in staff if s.id not in invited_ids],
            "invite_state": svc.guest_invite_state,
        },
        status_code=status,
    )


@router.get("/meetings/{meeting_id}", response_model=None)
async def meeting_detail(
    request: Request,
    meeting_id: uuid.UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    meeting, role = await perm.require_meeting_access(db, user, meeting_id, "view_meeting")
    return await _detail_page(request, db, user, meeting, role)


@router.get("/meetings/{meeting_id}/calendar.ics", response_model=None)
async def calendar_file(
    request: Request,
    meeting_id: uuid.UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    meeting, _ = await perm.require_meeting_access(db, user, meeting_id, "view_meeting")
    body = meeting_ics(meeting, get_settings().app_base_url)
    return Response(
        body,
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="meeting.ics"'},
    )


@router.post("/meetings/{meeting_id}/cancel", response_model=None)
async def meeting_cancel(
    request: Request,
    meeting_id: uuid.UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    meeting, role = await perm.require_meeting_access(db, user, meeting_id, "edit_meeting")
    try:
        svc.transition(meeting, "cancelled")
    except svc.IllegalTransition:
        return await _detail_page(
            request, db, user, meeting, role, status=409, error="Only a meeting that has not started can be cancelled."
        )
    await audit.log(db, "meeting.cancel", actor=user.id, target_type="meeting", target_id=meeting.id, request=request)
    await db.commit()
    return RedirectResponse(f"/meetings/{meeting.id}?ok=cancelled", status_code=303)


@router.post("/meetings/{meeting_id}/invite", response_model=None)
async def meeting_invite(
    request: Request,
    meeting_id: uuid.UUID,
    user_ids: Annotated[list[uuid.UUID], Form()] = [],  # noqa: B006
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    meeting, role = await perm.require_meeting_access(db, user, meeting_id, "edit_meeting")
    if meeting.status not in ("scheduled", "live"):
        return await _detail_page(request, db, user, meeting, role, status=409, error="This meeting is closed.")
    existing = {m.user_id for m in meeting.members}
    people = (
        (await db.execute(select(User).where(User.id.in_(user_ids), User.is_active.is_(True))))
        .scalars()
        .all()
    )
    added = 0
    for person in people:
        if person.id not in existing:
            meeting.members.append(MeetingMember(user_id=person.id, role="member"))
            added += 1
    await audit.log(
        db, "meeting.invite", actor=user.id, target_type="meeting", target_id=meeting.id, request=request,
        details={"added": added},
    )
    await db.commit()
    return RedirectResponse(f"/meetings/{meeting.id}?ok=invited", status_code=303)


# ---------------------------------------------------------------------------- guest links


@router.post("/meetings/{meeting_id}/guest-links", response_model=None)
async def guest_link_create(
    request: Request,
    meeting_id: uuid.UUID,
    label: Annotated[str, Form(max_length=100)] = "",
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    meeting, role = await perm.require_meeting_access(db, user, meeting_id, "manage_guest_links")
    if meeting.mode != "online" or meeting.status not in ("scheduled", "live"):
        return await _detail_page(
            request, db, user, meeting, role, status=409, error="Guest links only work for open online meetings."
        )
    token, invite = svc.new_guest_invite(meeting, label)
    db.add(invite)
    await db.flush()
    await audit.log(
        db, "guest_link.create", actor=user.id, target_type="meeting", target_id=meeting.id, request=request,
        details={"invite_id": str(invite.id)},
    )
    await db.commit()
    link = f"{get_settings().app_base_url.rstrip('/')}/join/{token}"
    # Rendered directly (not redirected) so the link is shown exactly once.
    return await _detail_page(request, db, user, meeting, role, new_link=link)


@router.delete("/meetings/{meeting_id}/guest-links/{invite_id}", response_model=None)
async def guest_link_revoke(
    request: Request,
    meeting_id: uuid.UUID,
    invite_id: uuid.UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    meeting, _ = await perm.require_meeting_access(db, user, meeting_id, "manage_guest_links")
    invite = (
        await db.execute(
            select(GuestInvite).where(GuestInvite.id == invite_id, GuestInvite.meeting_id == meeting.id)
        )
    ).scalar_one_or_none()
    if invite is None:
        raise perm.not_found()
    if invite.revoked_at is None:
        invite.revoked_at = utcnow()
    await audit.log(
        db, "guest_link.revoke", actor=user.id, target_type="meeting", target_id=meeting.id, request=request,
        details={"invite_id": str(invite.id)},
    )
    await db.commit()
    return Response(status_code=204, headers={"HX-Refresh": "true"})


# ------------------------------------------------------------------------- lobby and end


@router.get("/meetings/{meeting_id}/lobby", response_model=None)
async def meeting_lobby(
    request: Request,
    meeting_id: uuid.UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    meeting, role = await perm.require_meeting_access(db, user, meeting_id, "join_room")
    if meeting.status not in ("scheduled", "live"):
        raise perm.not_found()
    return templates.TemplateResponse(
        request,
        "lobby.html",
        {"meeting": meeting, "role": role, "guest": False, "display_name": user.display_name},
    )


@router.post("/api/meetings/{meeting_id}/end", response_model=None)
async def meeting_end(
    request: Request,
    meeting_id: uuid.UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    meeting, _ = await perm.require_meeting_access(db, user, meeting_id, "record")
    try:
        await _end_meeting(db, meeting, user, request)
    except svc.IllegalTransition:
        raise HTTPException(status_code=409, detail="Only a live meeting can be ended.") from None
    await db.commit()
    return JSONResponse({"status": "ended"})


@router.post("/meetings/{meeting_id}/end", response_model=None)
async def meeting_end_form(
    request: Request,
    meeting_id: uuid.UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    meeting, role = await perm.require_meeting_access(db, user, meeting_id, "record")
    try:
        await _end_meeting(db, meeting, user, request)
    except svc.IllegalTransition:
        return await _detail_page(
            request, db, user, meeting, role, status=409, error="Only a live meeting can be ended."
        )
    await db.commit()
    return RedirectResponse(f"/meetings/{meeting.id}", status_code=303)


@router.get("/meetings/{meeting_id}/room", response_model=None)
async def meeting_room(
    request: Request,
    meeting_id: uuid.UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    meeting, role = await perm.require_meeting_access(db, user, meeting_id, "join_room")
    if meeting.status not in ("scheduled", "live") or meeting.mode != "online":
        raise perm.not_found()
    if not get_settings().livekit_configured:
        return templates.TemplateResponse(
            request, "errors/error.html",
            {"status": 503, "title": "Video is not set up yet",
             "message": "The administrator has not configured the online video service yet."},
            status_code=503,
        )
    return templates.TemplateResponse(
        request,
        "room_online.html",
        {
            "meeting": meeting,
            "is_host": role == "host",
            "can_record": perm.is_allowed(role, "record"),
            "can_end": perm.is_allowed(role, "record"),
            "room_config": {
                "meetingId": str(meeting.id),
                "tokenUrl": f"/api/meetings/{meeting.id}/livekit-token",
                "participantsUrl": f"/api/meetings/{meeting.id}/participants",
                "endUrl": f"/api/meetings/{meeting.id}/end",
                "isHost": role == "host",
            },
        },
    )
