"""Developer-only recorder test page. Returns 404 unless dev tools are enabled."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.deps import current_user
from app.models import User
from app.security import permissions as perm
from app.templating import templates

router = APIRouter(prefix="/dev")


@router.get("/recorder-test", response_model=None)
async def recorder_test(
    request: Request,
    meeting: uuid.UUID | None = None,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    settings = get_settings()
    if not settings.dev_tools_enabled:
        raise HTTPException(status_code=404, detail="Not found")
    meeting_id: str | None = None
    if meeting is not None:
        row, _ = await perm.require_meeting_access(db, user, meeting, "record")
        meeting_id = str(row.id)
    return templates.TemplateResponse(
        request,
        "recorder_test.html",
        {
            "meeting_id": meeting_id,
            "config": {
                "meetingId": meeting_id,
                "partSeconds": settings.part_seconds,
                "timesliceMs": settings.timeslice_ms,
                "maxPartBytes": settings.max_part_bytes,
            },
        },
    )
