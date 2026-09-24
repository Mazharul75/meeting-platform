"""Local-development stand-in for Supabase Storage signed URLs. Registered but inert elsewhere."""
from __future__ import annotations

import mimetypes

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from app.config import get_settings
from app.services.storage import LocalDevStorage, get_storage

router = APIRouter(prefix="/_local_storage")

MAX_UPLOAD_BYTES = 50 * 1024 * 1024


def _local() -> LocalDevStorage:
    storage = get_storage()
    if get_settings().is_prod or not isinstance(storage, LocalDevStorage):
        raise HTTPException(status_code=404, detail="Not found")
    return storage


@router.put("/upload", response_model=None)
async def upload(request: Request, path: str, exp: int, sig: str, u: int = 0) -> Response:
    storage = _local()
    if not storage.verify("put", path, exp, sig, bool(u)):
        raise HTTPException(status_code=403, detail="Bad or expired link")
    target = storage.file_path(path)
    if target.exists() and not u:
        return JSONResponse({"error": "Duplicate"}, status_code=409)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    size = 0
    with tmp.open("wb") as fh:
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                fh.close()
                tmp.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="Too large")
            fh.write(chunk)
    tmp.replace(target)
    return JSONResponse({"Key": path})


@router.get("/download", response_model=None)
async def download(path: str, exp: int, sig: str, name: str | None = None) -> Response:
    storage = _local()
    if not storage.verify("get", path, exp, sig):
        raise HTTPException(status_code=403, detail="Bad or expired link")
    target = storage.file_path(path)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(target, media_type=media_type, filename=name if name else None,
                        content_disposition_type="attachment" if name else "inline")
