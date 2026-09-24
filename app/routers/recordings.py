"""Recording API (start, upload-url, complete, finish), library, playback, download, delete."""
from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import utcnow
from app.config import get_settings
from app.db import get_db
from app.deps import current_user
from app.models import Meeting, Recording, RecordingPart, User
from app.security import permissions as perm
from app.security import ratelimit
from app.services import audit
from app.services import meetings as meeting_svc
from app.services.storage import EXT_BY_MIME, Storage, StorageError, get_storage, part_path
from app.templating import templates

router = APIRouter()

MAX_PARTS = 500
MAX_PART_BYTES_HARD = 50 * 1024 * 1024  # the free storage limit; the bucket enforces 45 MB
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def normalize_mime(mime: str) -> str | None:
    """Map a MediaRecorder mime string to the two types the bucket accepts."""
    base = mime.split(";")[0].strip().lower()
    if base in ("video/mp4", "audio/mp4"):
        return "video/mp4"
    if base in ("video/webm", "audio/webm"):
        return "video/webm"
    return None


def _json_error(status: int, code: str) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status)


# ------------------------------------------------------------------------------ start


class StartBody(BaseModel):
    mime_type: str = Field(max_length=100)
    consent: bool = False


@router.post("/api/meetings/{meeting_id}/recordings", response_model=None)
async def start_recording(
    request: Request,
    meeting_id: uuid.UUID,
    body: StartBody,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    ratelimit.enforce(f"rec_start:{user.id}", 30, 3600)
    meeting, _ = await perm.require_meeting_access(db, user, meeting_id, "record")
    if not body.consent:  # consent is enforced on the server, not only in the browser
        return _json_error(400, "consent_required")
    mime = normalize_mime(body.mime_type)
    if mime is None:
        return _json_error(400, "unsupported_format")
    if meeting.status not in ("scheduled", "live"):
        return _json_error(409, "meeting_closed")
    if meeting.status == "scheduled":
        meeting_svc.transition(meeting, "live")
    settings = get_settings()
    now = utcnow()
    recording = Recording(
        id=uuid.uuid4(),
        meeting_id=meeting.id,
        status="recording",
        mime_type=mime,
        part_seconds=settings.part_seconds,
        created_by=user.id,
        consent_at=now,
        created_at=now,
    )
    db.add(recording)
    await db.flush()
    await audit.log(
        db, "recording.consent", actor=user.id, target_type="recording", target_id=recording.id, request=request
    )
    await audit.log(
        db, "recording.start", actor=user.id, target_type="recording", target_id=recording.id, request=request,
        details={"meeting_id": str(meeting.id), "mime": mime},
    )
    await db.commit()
    return JSONResponse(
        {
            "recording_id": str(recording.id),
            "mime_type": mime,
            "part_seconds": settings.part_seconds,
            "timeslice_ms": settings.timeslice_ms,
            "max_part_bytes": settings.max_part_bytes,
        },
        status_code=201,
    )


# ----------------------------------------------------------------------- upload + verify


def _valid_part_number(n: int) -> None:
    if not 1 <= n <= MAX_PARTS:
        raise perm.not_found()


async def _part(db: AsyncSession, recording_id: uuid.UUID, n: int) -> RecordingPart | None:
    return (
        await db.execute(
            select(RecordingPart).where(RecordingPart.recording_id == recording_id, RecordingPart.part_number == n)
        )
    ).scalar_one_or_none()


@router.post("/api/recordings/{recording_id}/parts/{n}/upload-url", response_model=None)
async def part_upload_url(
    request: Request,
    recording_id: uuid.UUID,
    n: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> Response:
    ratelimit.enforce(f"upload_url:{user.id}", 240, 60)
    recording, meeting, _ = await perm.load_recording_for(db, user, recording_id, "record")
    _valid_part_number(n)
    part = await _part(db, recording.id, n)
    if part is not None and part.status == "verified":
        return _json_error(409, "already_verified")
    if part is None:
        # The server alone chooses the path; the browser cannot.
        part = RecordingPart(
            recording_id=recording.id,
            part_number=n,
            storage_path=part_path(meeting.id, recording.id, n, recording.mime_type),
            status="pending",
        )
        db.add(part)
        await db.flush()
    try:
        signed = await storage.create_upload(part.storage_path, recording.mime_type, upsert=True)
    except (StorageError, OSError) as exc:
        raise HTTPException(status_code=503, detail="Storage is not available right now.") from exc
    await db.commit()
    return JSONResponse(
        {
            "url": signed.url,
            "method": signed.method,
            "body_kind": signed.body_kind,
            "headers": signed.headers,
            "content_type": recording.mime_type,
            "expires_in": get_settings().upload_url_ttl_seconds,
        }
    )


class CompleteBody(BaseModel):
    size_bytes: int = Field(gt=0, le=MAX_PART_BYTES_HARD)
    duration_ms: int = Field(ge=0, le=24 * 3600 * 1000)
    sha256: str = Field(min_length=64, max_length=64)


@router.post("/api/recordings/{recording_id}/parts/{n}/complete", response_model=None)
async def part_complete(
    request: Request,
    recording_id: uuid.UUID,
    n: int,
    body: CompleteBody,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> Response:
    recording, _, _ = await perm.load_recording_for(db, user, recording_id, "record")
    _valid_part_number(n)
    sha = body.sha256.lower()
    if not _SHA256_RE.match(sha):
        return _json_error(422, "bad_checksum")
    part = await _part(db, recording.id, n)
    if part is None:  # an upload link was never requested for this part
        raise perm.not_found()
    if part.status == "verified" and part.size_bytes == body.size_bytes:
        return JSONResponse({"status": "verified"})  # safe to retry
    try:
        actual = await storage.stat(part.storage_path)
    except (StorageError, OSError) as exc:
        raise HTTPException(status_code=503, detail="Storage is not available right now.") from exc
    if actual is None:
        return _json_error(409, "not_in_storage")
    if actual != body.size_bytes:
        part.status = "uploaded"
        await db.commit()
        return _json_error(409, "size_mismatch")
    part.status = "verified"
    part.size_bytes = actual
    part.duration_ms = body.duration_ms
    part.sha256 = sha
    part.uploaded_at = utcnow()
    await db.commit()
    return JSONResponse({"status": "verified"})


class FinishBody(BaseModel):
    parts_expected: int = Field(ge=0, le=MAX_PARTS)


@router.post("/api/recordings/{recording_id}/finish", response_model=None)
async def finish_recording(
    request: Request,
    recording_id: uuid.UUID,
    body: FinishBody,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Idempotent: recomputes ready / partial / failed from what storage has confirmed."""
    recording, _, _ = await perm.load_recording_for(db, user, recording_id, "record")
    verified = {p.part_number: p for p in recording.parts if p.status == "verified"}
    expected = body.parts_expected
    missing = [n for n in range(1, expected + 1) if n not in verified]
    if expected == 0:
        status = "failed"  # nothing recorded
    elif missing:
        status = "partial"
    else:
        status = "ready"
    recording.parts_expected = expected
    recording.status = status
    recording.total_bytes = sum(p.size_bytes or 0 for p in verified.values())
    recording.total_duration_ms = sum(p.duration_ms or 0 for p in verified.values())
    if recording.finished_at is None:
        recording.finished_at = utcnow()
    await audit.log(
        db, "recording.finish", actor=user.id, target_type="recording", target_id=recording.id, request=request,
        details={"status": status, "missing": len(missing)},
    )
    await db.commit()
    return JSONResponse({"status": status, "missing": missing})


class NoteBody(BaseModel):
    code: str = Field(max_length=40)


_NOTE_CODES = {"tab_hidden", "tab_hidden_stopped", "device_lost", "offline"}


@router.post("/api/recordings/{recording_id}/note", response_model=None)
async def recording_note(
    request: Request,
    recording_id: uuid.UUID,
    body: NoteBody,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """The recorder tells the audit trail about things like a hidden tab or a lost microphone."""
    ratelimit.enforce(f"rec_note:{user.id}", 60, 60)
    recording, _, _ = await perm.load_recording_for(db, user, recording_id, "record")
    if body.code not in _NOTE_CODES:
        return _json_error(422, "bad_code")
    await audit.log(
        db, "recording.note", actor=user.id, target_type="recording", target_id=recording.id, request=request,
        details={"code": body.code},
    )
    await db.commit()
    return JSONResponse({"ok": True})


# ------------------------------------------------------------------------------ library


def _visible_recordings_stmt(user: User):  # type: ignore[no-untyped-def]
    return (
        select(Recording)
        .join(Meeting, Recording.meeting_id == Meeting.id)
        .where(perm.visible_meeting_filter(user), Recording.status != "deleted")
        .order_by(Recording.created_at.desc())
    )


@router.get("/recordings", response_model=None)
async def recordings_library(
    request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> Response:
    rows = (await db.execute(_visible_recordings_stmt(user).limit(200))).scalars().all()
    template = "_recordings_table.html" if request.headers.get("hx-request") else "recordings.html"
    return templates.TemplateResponse(request, template, {"recordings": rows})


# ------------------------------------------------------------------------------- playback


async def _play_links(
    storage: Storage, recording: Recording, download: bool = False
) -> list[dict[str, object]]:
    ttl = get_settings().download_url_ttl_seconds
    links: list[dict[str, object]] = []
    for part in recording.parts:
        if part.status != "verified":
            continue
        url = await storage.create_download_url(part.storage_path, ttl)
        links.append(
            {
                "n": part.part_number,
                "url": url,
                "duration_ms": part.duration_ms or 0,
                "size": part.size_bytes or 0,
            }
        )
    return links


@router.get("/recordings/{recording_id}", response_model=None)
async def recording_view(
    request: Request,
    recording_id: uuid.UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> Response:
    recording, meeting, role = await perm.load_recording_for(db, user, recording_id, "play_recording")
    try:
        links = await _play_links(storage, recording)
    except (StorageError, OSError):
        links = []
    await audit.log(db, "recording.view", actor=user.id, target_type="recording", target_id=recording.id, request=request)
    await db.commit()
    verified = {p.part_number for p in recording.parts if p.status == "verified"}
    missing = [n for n in range(1, (recording.parts_expected or 0) + 1) if n not in verified]
    return templates.TemplateResponse(
        request,
        "recording_view.html",
        {
            "recording": recording,
            "meeting": meeting,
            "links": links,
            "missing": missing,
            "can_download": perm.is_allowed(role, "download_recording"),
            "can_delete": perm.is_allowed(role, "delete_recording"),
            "player_config": {
                "recordingId": str(recording.id),
                "parts": links,
                "refreshUrl": f"/api/recordings/{recording.id}/parts",
                "refreshSeconds": max(60, get_settings().download_url_ttl_seconds - 120),
            },
        },
    )


@router.get("/api/recordings/{recording_id}/parts", response_model=None)
async def recording_part_links(
    recording_id: uuid.UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> Response:
    """Fresh signed links, used by player.js before the old ones expire (after a permission check)."""
    ratelimit.enforce(f"play_links:{user.id}", 60, 60)
    recording, _, _ = await perm.load_recording_for(db, user, recording_id, "play_recording")
    try:
        links = await _play_links(storage, recording)
    except (StorageError, OSError) as exc:
        raise HTTPException(status_code=503, detail="Storage is not available right now.") from exc
    return JSONResponse({"parts": links})


def _download_name(meeting: Meeting, recording: Recording, n: int) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", meeting.title).strip("-")[:40] or "meeting"
    stamp = recording.created_at.strftime("%Y%m%d")
    return f"{safe}-{stamp}-part{n:02d}.{EXT_BY_MIME[recording.mime_type]}"


@router.get("/recordings/{recording_id}/parts/{n}/download", response_model=None)
async def part_download(
    request: Request,
    recording_id: uuid.UUID,
    n: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> Response:
    recording, meeting, _ = await perm.load_recording_for(db, user, recording_id, "download_recording")
    _valid_part_number(n)
    part = await _part(db, recording.id, n)
    if part is None or part.status != "verified":
        raise perm.not_found()
    try:
        url = await storage.create_download_url(
            part.storage_path, get_settings().download_url_ttl_seconds, _download_name(meeting, recording, n)
        )
    except (StorageError, OSError) as exc:
        raise HTTPException(status_code=503, detail="Storage is not available right now.") from exc
    await audit.log(
        db, "recording.download", actor=user.id, target_type="recording", target_id=recording.id, request=request,
        details={"part": n},
    )
    await db.commit()
    return RedirectResponse(url, status_code=303)


@router.delete("/recordings/{recording_id}", response_model=None)
async def recording_delete(
    request: Request,
    recording_id: uuid.UUID,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> Response:
    recording, _, _ = await perm.load_recording_for(db, user, recording_id, "delete_recording")
    paths = [p.storage_path for p in recording.parts]
    try:
        await storage.delete(paths)
    except (StorageError, OSError) as exc:
        raise HTTPException(status_code=503, detail="Storage is not available right now.") from exc
    recording.parts.clear()
    recording.status = "deleted"
    recording.total_bytes = 0
    await audit.log(
        db, "recording.delete", actor=user.id, target_type="recording", target_id=recording.id, request=request,
        details={"files": len(paths)},
    )
    await db.commit()
    return Response(status_code=204, headers={"HX-Redirect": "/recordings"})

