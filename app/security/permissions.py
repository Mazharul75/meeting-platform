"""The one place that decides who may do what to a meeting or recording (plan Section 6.3).

Rule: never trust the browser. Every route with a meeting/recording id calls
`require_meeting_access`. Someone who cannot even see the meeting gets 404 (so IDs cannot be
discovered); someone who can see it but lacks the action gets 403.
"""
from __future__ import annotations

import uuid
from typing import Literal

from fastapi import HTTPException
from sqlalchemy import ColumnElement, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import GuestInvite, Meeting, MeetingMember, Recording, User

Role = Literal["admin", "host", "invited", "other", "guest"]
Action = Literal[
    "edit_meeting",  # create/edit/cancel
    "view_meeting",
    "join_room",
    "manage_guest_links",
    "record",  # start/stop recording
    "play_recording",
    "download_recording",
    "delete_recording",
]

ALL_ROLES: tuple[Role, ...] = ("admin", "host", "invited", "other", "guest")
ALL_ACTIONS: tuple[Action, ...] = (
    "edit_meeting",
    "view_meeting",
    "join_room",
    "manage_guest_links",
    "record",
    "play_recording",
    "download_recording",
    "delete_recording",
)

# role -> set of allowed actions. An admin who is also the host is treated as the host.
_MATRIX: dict[Role, frozenset[Action]] = {
    "admin": frozenset(
        {
            "edit_meeting",
            "view_meeting",
            "join_room",
            "manage_guest_links",
            "play_recording",
            "download_recording",
            "delete_recording",
        }
    ),
    "host": frozenset(ALL_ACTIONS),
    "invited": frozenset({"view_meeting", "join_room", "play_recording"}),
    "other": frozenset(),
    "guest": frozenset({"join_room"}),
}


def is_allowed(role: Role, action: Action) -> bool:
    return action in _MATRIX[role]


def role_for(user: User, meeting: Meeting) -> Role:
    if meeting.host_id == user.id:
        return "host"
    if user.role == "admin":
        return "admin"
    if any(m.user_id == user.id for m in meeting.members):
        return "invited"
    return "other"


def not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="Not found")


async def get_meeting_or_none(db: AsyncSession, meeting_id: uuid.UUID) -> Meeting | None:
    return (await db.execute(select(Meeting).where(Meeting.id == meeting_id))).scalar_one_or_none()


async def require_meeting_access(
    db: AsyncSession, user: User, meeting_id: uuid.UUID, action: Action
) -> tuple[Meeting, Role]:
    meeting = await get_meeting_or_none(db, meeting_id)
    if meeting is None:
        raise not_found()
    role = role_for(user, meeting)
    if not is_allowed(role, "view_meeting"):
        raise not_found()
    if not is_allowed(role, action):
        raise HTTPException(status_code=403, detail="You are not allowed to do that.")
    return meeting, role


def require_guest_access(invite: GuestInvite, meeting: Meeting, action: Action) -> None:
    """Guests may only join the one online meeting their link belongs to."""
    if invite.meeting_id != meeting.id or not is_allowed("guest", action):
        raise not_found()


def visible_meeting_filter(user: User) -> ColumnElement[bool]:
    """SQL condition: meetings this user may see (admin: all; else host or invited)."""
    if user.role == "admin":
        return true()
    return or_(
        Meeting.host_id == user.id,
        Meeting.id.in_(select(MeetingMember.meeting_id).where(MeetingMember.user_id == user.id)),
    )


async def load_recording_for(
    db: AsyncSession, user: User, recording_id: uuid.UUID, action: Action
) -> tuple[Recording, Meeting, Role]:
    """Load a recording through its meeting's permission check. Deleted recordings are 404."""
    recording = (
        await db.execute(select(Recording).where(Recording.id == recording_id))
    ).scalar_one_or_none()
    if recording is None or recording.status == "deleted":
        raise not_found()
    meeting, role = await require_meeting_access(db, user, recording.meeting_id, action)
    return recording, meeting, role
