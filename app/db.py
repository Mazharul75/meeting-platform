"""Async database engine. Works with Supabase's transaction pooler (no prepared statements)."""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def normalize_database_url(raw: str) -> tuple[str, dict[str, Any]]:
    """Turn a plain postgres URL into an asyncpg URL and connect args."""
    url = make_url(raw)
    connect_args: dict[str, Any] = {}
    if url.drivername in ("postgres", "postgresql", "postgresql+psycopg2"):
        url = url.set(drivername="postgresql+asyncpg")
    if url.drivername == "postgresql+asyncpg":
        query = dict(url.query)
        sslmode = query.pop("sslmode", None)
        url = url.set(query=query)
        if sslmode in ("require", "verify-ca", "verify-full"):
            connect_args["ssl"] = "require"
        # Required by the Supabase transaction pooler (pgbouncer).
        connect_args["statement_cache_size"] = 0
        connect_args["prepared_statement_cache_size"] = 0
        connect_args["prepared_statement_name_func"] = lambda: f"__asyncpg_{uuid4()}__"
    return url.render_as_string(hide_password=False), connect_args


def get_engine() -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        settings = get_settings()
        if not settings.database_url:
            raise RuntimeError("DATABASE_URL is not set")
        url, connect_args = normalize_database_url(settings.database_url)
        _engine = create_async_engine(url, connect_args=connect_args, pool_pre_ping=True)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    get_engine()
    if _sessionmaker is None:
        raise RuntimeError("database not initialised")
    return _sessionmaker


def set_engine_for_tests(engine: AsyncEngine | None) -> None:
    global _engine, _sessionmaker
    _engine = engine
    _sessionmaker = async_sessionmaker(engine, expire_on_commit=False) if engine else None


async def get_db() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
