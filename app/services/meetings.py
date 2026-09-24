"""Meeting rules: form validation, the status machine and guest-link helpers."""
from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.clock import utcnow
from app.models import GuestInvite, Meeting

TITLE_MAX = 200
DESCRIPTION_MAX = 2000
MIN_DURATION_MIN = 5
MAX_DURATION_MIN = 8 * 60
ALLOWED_MODES = ("online",)  # in-person recording is not part of this build

# status machine: only the host moves it, illegal jumps are refused
_TRANSITIONS: dict[str, frozenset[str]] = {
    "scheduled": frozenset({"live", "cancelled"}),
    "live": frozenset({"ended"}),
    "ended": frozenset(),
    "cancelled": frozenset(),
}

GUEST_OPENS_BEFORE = timedelta(minutes=30)
GUEST_CLOSES_AFTER = timedelta(hours=4)


class IllegalTransition(ValueError):
    pass


def transition(meeting: Meeting, new_status: str) -> None:
    if new_status not in _TRANSITIONS.get(meeting.status, frozenset()):
        raise IllegalTransition(f"{meeting.status} -> {new_status}")
    meeting.status = new_status
    now = utcnow()
    if new_status == "live":
        meeting.started_at = now
    elif new_status == "ended":
        meeting.ended_at = now


@dataclass
class MeetingInput:
    title: str
    description: str
    mode: str
    start_utc: datetime
    end_utc: datetime
    timezone: str


def valid_timezone(name: str) -> bool:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return bool(name) and len(name) <= 64


def parse_meeting_form(
    title: str,
    description: str,
    mode: str,
    start_local: str,
    duration_minutes: int,
    timezone: str,
) -> tuple[MeetingInput | None, dict[str, str]]:
    """Validate raw form values. Returns (input, errors); input is None when errors exist."""
    errors: dict[str, str] = {}
    title = title.strip()
    description = description.strip()
    if not title:
        errors["title"] = "Please enter a title."
    elif len(title) > TITLE_MAX:
        errors["title"] = f"The title must be at most {TITLE_MAX} characters."
    if len(description) > DESCRIPTION_MAX:
        errors["description"] = f"The description must be at most {DESCRIPTION_MAX} characters."
    if mode not in ALLOWED_MODES:
        errors["mode"] = "Only online meetings are available right now."
    if not valid_timezone(timezone):
        errors["timezone"] = "Please choose a valid time zone."
    if not (MIN_DURATION_MIN <= duration_minutes <= MAX_DURATION_MIN):
        errors["duration"] = f"Duration must be {MIN_DURATION_MIN}-{MAX_DURATION_MIN} minutes."

    start_utc: datetime | None = None
    if "timezone" not in errors:
        try:
            naive = datetime.strptime(start_local, "%Y-%m-%dT%H:%M")
            start_utc = naive.replace(tzinfo=ZoneInfo(timezone)).astimezone(UTC)
        except ValueError:
            errors["start"] = "Please enter a valid date and time."
        else:
            if start_utc <= utcnow():
                errors["start"] = "The meeting must start in the future."
    else:
        errors.setdefault("start", "Please choose a time zone first.")

    if errors or start_utc is None:
        return None, errors
    return (
        MeetingInput(
            title=title,
            description=description,
            mode=mode,
            start_utc=start_utc,
            end_utc=start_utc + timedelta(minutes=duration_minutes),
            timezone=timezone,
        ),
        {},
    )


def to_local(dt: datetime, tz_name: str) -> datetime:
    try:
        return dt.astimezone(ZoneInfo(tz_name))
    except (ZoneInfoNotFoundError, ValueError):
        return dt.astimezone(UTC)


# ---- guest links -----------------------------------------------------------------------------


def hash_guest_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_guest_invite(meeting: Meeting, label: str) -> tuple[str, GuestInvite]:
    """Create a guest invite. The raw token is returned once; only its hash is stored."""
    token = secrets.token_urlsafe(32)  # 32 random bytes
    invite = GuestInvite(
        id=uuid.uuid4(),
        meeting_id=meeting.id,
        token_hash=hash_guest_token(token),
        label=label.strip()[:100],
        valid_from=meeting.scheduled_start - GUEST_OPENS_BEFORE,
        valid_until=meeting.scheduled_end + GUEST_CLOSES_AFTER,
        use_count=0,
    )
    return token, invite


def guest_invite_state(invite: GuestInvite, meeting: Meeting, now: datetime | None = None) -> str:
    """One of: ok | revoked | not_yet | expired | meeting_closed."""
    now = now or utcnow()
    if invite.revoked_at is not None:
        return "revoked"
    if meeting.status in ("cancelled", "ended"):
        return "meeting_closed"
    if now < invite.valid_from:
        return "not_yet"
    if now > invite.valid_until:
        return "expired"
    return "ok"
