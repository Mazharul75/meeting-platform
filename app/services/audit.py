"""Append-only audit log. Never pass secrets, tokens or signed URLs in `details`."""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog

_FORBIDDEN_KEYS = {"password", "token", "secret", "key", "url", "cookie"}


def _clean(details: dict[str, Any] | None) -> dict[str, Any] | None:
    if not details:
        return None
    return {
        k: v
        for k, v in details.items()
        if not any(bad in k.lower() for bad in _FORBIDDEN_KEYS)
    }


def request_ip(request: Request | None) -> str | None:
    if request is None or request.client is None:
        return None
    return request.client.host


async def log(
    db: AsyncSession,
    action: str,
    *,
    actor: uuid.UUID | None = None,
    target_type: str | None = None,
    target_id: object | None = None,
    request: Request | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    db.add(
        AuditLog(
            actor_user_id=actor,
            action=action,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            ip=request_ip(request),
            details=_clean(details),
        )
    )
    await db.flush()
