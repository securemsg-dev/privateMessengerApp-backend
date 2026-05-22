from __future__ import annotations

"""
app/db/session.py
──────────────────
Async SQLAlchemy engine and session factory.
Use `get_session` as a FastAPI dependency to open a DB session per request.
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

# Create the async engine.
# pool_pre_ping=True avoids "connection lost" errors on long-idle connections.
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,  # Log SQL in development; disable in production
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
)

# Session factory - produces AsyncSession instances
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,  # Avoid lazy-load errors after commit
    autoflush=False,
    autocommit=False,
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency that yields an AsyncSession for each request.
    Automatically commits on success and rolls back on exception.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
