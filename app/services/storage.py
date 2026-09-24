"""Recording storage. Files never pass through this server: the browser uploads/downloads with
short-lived signed links and the server only checks that files exist with the right size.

Three implementations behind one interface:
  * SupabaseStorage  - real private bucket (production)
  * LocalDevStorage  - files under .dev_storage/, signed with HMAC (local development only)
  * MemoryStorage    - in-memory fake used by tests
"""
from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, urlencode

import httpx

from app.config import Settings, get_settings

EXT_BY_MIME = {"video/mp4": "mp4", "video/webm": "webm"}


@dataclass
class SignedUpload:
    url: str
    method: str = "PUT"
    # "multipart": send FormData like supabase-js does; "raw": send the file as the body.
    body_kind: str = "multipart"
    headers: dict[str, str] = field(default_factory=dict)


class StorageError(RuntimeError):
    pass


class Storage(Protocol):
    async def create_upload(self, path: str, content_type: str, upsert: bool) -> SignedUpload: ...

    async def stat(self, path: str) -> int | None:
        """Size in bytes, or None when the file does not exist."""
        ...

    async def create_download_url(
        self, path: str, expires_in: int, download_name: str | None = None
    ) -> str: ...

    async def delete(self, paths: list[str]) -> None: ...


def part_path(meeting_id: object, recording_id: object, part_number: int, mime_type: str) -> str:
    """The server alone decides where a part lives: recordings/{meeting}/{recording}/part-0001.ext"""
    ext = EXT_BY_MIME[mime_type]
    return f"recordings/{meeting_id}/{recording_id}/part-{part_number:04d}.{ext}"


# ---------------------------------------------------------------------------------- Supabase


class SupabaseStorage:
    def __init__(
        self, base_url: str, service_key: str, bucket: str, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._transport = transport
        self._base = base_url.rstrip("/") + "/storage/v1"
        self._bucket = bucket
        self._auth = {"Authorization": f"Bearer {service_key}", "apikey": service_key}

    def _obj(self, path: str) -> str:
        return f"{self._bucket}/{quote(path, safe='/')}"

    def _absolute(self, relative: str) -> str:
        return relative if relative.startswith("http") else self._base + relative

    async def create_upload(self, path: str, content_type: str, upsert: bool) -> SignedUpload:
        async with httpx.AsyncClient(timeout=15, transport=self._transport) as client:
            resp = await client.post(
                f"{self._base}/object/upload/sign/{self._obj(path)}",
                headers={**self._auth, "x-upsert": "true" if upsert else "false"},
            )
        if resp.status_code >= 300:
            raise StorageError(f"could not sign upload ({resp.status_code})")
        return SignedUpload(
            url=self._absolute(resp.json()["url"]),
            method="PUT",
            body_kind="multipart",
            headers={"x-upsert": "true" if upsert else "false"},
        )

    async def stat(self, path: str) -> int | None:
        url = f"{self._base}/object/authenticated/{self._obj(path)}"
        async with httpx.AsyncClient(timeout=15, transport=self._transport) as client:
            resp = await client.head(url, headers=self._auth)
            if resp.status_code in (400, 404):
                return None
            if resp.status_code == 200 and "content-length" in resp.headers:
                return int(resp.headers["content-length"])
            # Fallback: ask for one byte and read the total from Content-Range.
            resp = await client.get(url, headers={**self._auth, "Range": "bytes=0-0"})
        if resp.status_code in (400, 404):
            return None
        if resp.status_code == 206 and "content-range" in resp.headers:
            return int(resp.headers["content-range"].rsplit("/", 1)[1])
        if resp.status_code == 200 and "content-length" in resp.headers:
            return int(resp.headers["content-length"])
        raise StorageError(f"could not read object info ({resp.status_code})")

    async def create_download_url(
        self, path: str, expires_in: int, download_name: str | None = None
    ) -> str:
        async with httpx.AsyncClient(timeout=15, transport=self._transport) as client:
            resp = await client.post(
                f"{self._base}/object/sign/{self._obj(path)}",
                headers=self._auth,
                json={"expiresIn": expires_in},
            )
        if resp.status_code >= 300:
            raise StorageError(f"could not sign download ({resp.status_code})")
        url = self._absolute(resp.json()["signedURL"])
        if download_name:
            url += "&" + urlencode({"download": download_name})
        return url

    async def delete(self, paths: list[str]) -> None:
        if not paths:
            return
        async with httpx.AsyncClient(timeout=30, transport=self._transport) as client:
            resp = await client.request(
                "DELETE",
                f"{self._base}/object/{self._bucket}",
                headers=self._auth,
                json={"prefixes": paths},
            )
        if resp.status_code >= 300:
            raise StorageError(f"could not delete files ({resp.status_code})")


# ------------------------------------------------------------------------------ local (dev only)


class LocalDevStorage:
    """Stores files on disk and signs same-origin URLs. For local development only."""

    def __init__(self, root: Path, secret: str) -> None:
        self._root = root
        self._secret = secret.encode() or b"dev-only-insecure-key"

    def sign(self, op: str, path: str, exp: int, upsert: bool = False) -> str:
        msg = f"{op}|{path}|{exp}|{int(upsert)}".encode()
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()

    def verify(self, op: str, path: str, exp: int, sig: str, upsert: bool = False) -> bool:
        if exp < time.time():
            return False
        return hmac.compare_digest(self.sign(op, path, exp, upsert), sig)

    def file_path(self, path: str) -> Path:
        target = (self._root / path).resolve()
        if self._root.resolve() not in target.parents:
            raise StorageError("bad path")
        return target

    async def create_upload(self, path: str, content_type: str, upsert: bool) -> SignedUpload:
        exp = int(time.time()) + 600
        q = urlencode({"path": path, "exp": exp, "u": int(upsert), "sig": self.sign("put", path, exp, upsert)})
        return SignedUpload(url=f"/_local_storage/upload?{q}", method="PUT", body_kind="raw")

    async def stat(self, path: str) -> int | None:
        target = self.file_path(path)
        return target.stat().st_size if target.is_file() else None

    async def create_download_url(
        self, path: str, expires_in: int, download_name: str | None = None
    ) -> str:
        exp = int(time.time()) + expires_in
        params = {"path": path, "exp": exp, "sig": self.sign("get", path, exp)}
        if download_name:
            params["name"] = download_name
        return "/_local_storage/download?" + urlencode(params)

    async def delete(self, paths: list[str]) -> None:
        for p in paths:
            self.file_path(p).unlink(missing_ok=True)


# ------------------------------------------------------------------------------------- memory


class MemoryStorage:
    """In-memory fake for tests. `put` plays the part of the browser uploading a file."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.upload_requests: list[tuple[str, bool]] = []
        self.download_requests: list[tuple[str, int]] = []

    def put(self, path: str, data: bytes) -> None:
        self.files[path] = data

    async def create_upload(self, path: str, content_type: str, upsert: bool) -> SignedUpload:
        self.upload_requests.append((path, upsert))
        return SignedUpload(url=f"https://storage.test/upload/{path}?token=fake", body_kind="raw")

    async def stat(self, path: str) -> int | None:
        data = self.files.get(path)
        return None if data is None else len(data)

    async def create_download_url(
        self, path: str, expires_in: int, download_name: str | None = None
    ) -> str:
        self.download_requests.append((path, expires_in))
        suffix = f"&download={download_name}" if download_name else ""
        return f"https://storage.test/download/{path}?token=fake{suffix}"

    async def delete(self, paths: list[str]) -> None:
        for p in paths:
            self.files.pop(p, None)


# ------------------------------------------------------------------------------------ factory


def build_storage(settings: Settings) -> Storage:
    backend = settings.resolved_storage_backend
    if backend == "supabase":
        return SupabaseStorage(settings.supabase_url, settings.supabase_service_key, settings.storage_bucket)
    if backend == "memory":
        return MemoryStorage()
    if backend == "local":
        if settings.is_prod:
            raise RuntimeError("Local storage is not allowed in production")
        return LocalDevStorage(Path(".dev_storage"), settings.secret_key)
    raise RuntimeError(f"Unknown STORAGE_BACKEND: {backend}")


@lru_cache
def _cached_storage() -> Storage:
    return build_storage(get_settings())


def get_storage() -> Storage:
    """FastAPI dependency. Tests override it with a MemoryStorage."""
    return _cached_storage()
