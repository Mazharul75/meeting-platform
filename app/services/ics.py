"""Calendar (.ics) files for meetings."""
from __future__ import annotations

from datetime import UTC

from icalendar import Calendar, Event

from app.clock import utcnow
from app.models import Meeting


def meeting_ics(meeting: Meeting, base_url: str) -> bytes:
    cal = Calendar()
    cal.add("prodid", "-//Company Meeting Platform//EN")
    cal.add("version", "2.0")
    cal.add("method", "PUBLISH")
    event = Event()
    event.add("uid", f"{meeting.id}@meeting-platform")
    event.add("summary", meeting.title)
    event.add("dtstamp", utcnow())
    event.add("dtstart", meeting.scheduled_start.astimezone(UTC))
    event.add("dtend", meeting.scheduled_end.astimezone(UTC))
    description = meeting.description or ""
    link = f"{base_url.rstrip('/')}/meetings/{meeting.id}"
    event.add("description", f"{description}\n\nOpen in the platform: {link}".strip())
    event.add("url", link)
    event.add("location", "Online meeting")
    if meeting.status == "cancelled":
        event.add("status", "CANCELLED")
    cal.add_component(event)
    return bytes(cal.to_ical())
