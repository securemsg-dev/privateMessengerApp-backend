from __future__ import annotations
from typing import Optional, Union, Any
"""
alembic/env.py
───────────────
Alembic migration environment — async mode for asyncpg.

When you run `alembic revision --autogenerate`, Alembic imports all
models via `app.db.models` and compares them against the live DB schema.

Usage:
    make migrate message="add_users_table"
    make upgrade
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings
from app.db.base import Base

# Import all models so Alembic finds them for autogenerate
import app.db.models  # noqa: F401

config = context.config
if config.config_file_name is not None:
    try:
        fileConfig(config.config_file_name)
    except KeyError:
        # Logging config is missing in alembic.ini, skipping
        pass

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations without a DB connection (generates SQL script)."""
    context.configure(
        url=settings.DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations against a live async DB connection."""
    connectable = create_async_engine(settings.DATABASE_URL, echo=False)
    async with connectable.begin() as conn:
        await conn.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
