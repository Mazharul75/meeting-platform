"""Alembic environment. Uses the session-pooler URL (MIGRATION_DATABASE_URL) when available."""
from __future__ import annotations

import asyncio

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context
from app.config import get_settings
from app.db import normalize_database_url
from app.models import Base

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    explicit = config.get_main_option("sqlalchemy.url")
    if explicit:
        return explicit
    settings = get_settings()
    raw = settings.migration_database_url or settings.database_url
    if not raw:
        raise RuntimeError("Set MIGRATION_DATABASE_URL or DATABASE_URL before running migrations")
    return raw


def run_migrations_offline() -> None:
    url, _ = normalize_database_url(_url())
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def _do_run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    url, connect_args = normalize_database_url(_url())
    engine = create_async_engine(url, connect_args=connect_args, poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(_do_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
