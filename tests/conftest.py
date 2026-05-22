from __future__ import annotations
"""
tests/conftest.py
──────────────────
Pytest fixtures shared across all test modules.

Test database:
  - Uses an in-memory SQLite DB via aiosqlite (no Postgres required for CI).
  - Applies all schema create_all at session start.
  - Each test gets a fresh, rolled-back DB transaction for isolation.

Test Redis:
  - Uses fakeredis (in-memory) so no Redis server is required.
"""

import itertools
import uuid
from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from jose import jwt
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.security import hash_password
from app.db.base import Base
from app.db.models.conversation import Conversation, conversation_participants
from app.db.models.user import User
from app.db.session import get_session
from app.main import create_app

# ── Test database engine (SQLite in-memory) ───────────────────────────────────
TEST_DB_URL = "sqlite+aiosqlite:///:memory:"

test_engine = create_async_engine(
    TEST_DB_URL,
    echo=False,
    connect_args={"check_same_thread": False},
)

TestSessionLocal = async_sessionmaker(
    bind=test_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


@pytest_asyncio.fixture(scope="session", autouse=True)
async def create_tables():
    """Create all tables once per test session."""
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """Each test gets its own session that is rolled back after the test."""
    async with TestSessionLocal() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture
async def fake_redis():
    """Fake in-memory Redis using fakeredis."""
    import fakeredis
    redis = fakeredis.FakeAsyncRedis(decode_responses=False)
    yield redis
    await redis.aclose()


@pytest_asyncio.fixture
async def client(db_session: AsyncSession, fake_redis) -> AsyncGenerator[AsyncClient, None]:
    """
    Test HTTP client with overridden DB session and Redis.
    The app is created fresh for each test.
    """
    from app.core.limiter import limiter
    limiter.enabled = False

    app = create_app()

    # Override DB dependency
    async def _override_get_session():
        yield db_session

    app.dependency_overrides[get_session] = _override_get_session

    # Inject fake Redis into app state
    app.state.redis = fake_redis

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac


# ── Shared helpers ───────────────────────────────────────────────────────────

_number_counter = itertools.count(1000000000)


def next_private_number() -> str:
    """Generate a unique 10-digit private number for each call."""
    return f"{next(_number_counter):010d}"


DEFAULT_LOGIN_PW = "LoginPass123"
DEFAULT_DELETE_PW = "DeletePass456"


async def register_and_login(
    client: AsyncClient,
    login_password: str = DEFAULT_LOGIN_PW,
    delete_password: str = DEFAULT_DELETE_PW,
    display_name: str | None = None,
) -> dict:
    """
    Helper: register a new user → returns dict with tokens + private_number.
    Registration auto-logs-in, so no separate login call needed.
    """
    body = {
        "login_password": login_password,
        "delete_password": delete_password,
    }
    if display_name:
        body["display_name"] = display_name
    resp = await client.post("/api/v1/auth/register", json=body)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    return {
        "tokens": data["tokens"],
        "private_number": data["private_number"],
        "user": data["user"],
        "login_password": login_password,
        "delete_password": delete_password,
    }


def auth_header(access_token: str) -> dict[str, str]:
    """Build Authorization header dict."""
    return {"Authorization": f"Bearer {access_token}"}


def forge_expired_token(user_id: str | uuid.UUID) -> str:
    """Create a JWT access token that is already expired."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "type": "access",
        "iat": now - timedelta(hours=1),
        "exp": now - timedelta(seconds=1),
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


async def create_user_directly(
    db: AsyncSession,
    private_number: str | None = None,
    display_name: str | None = None,
    is_active: bool = True,
    login_password: str = DEFAULT_LOGIN_PW,
    delete_password: str = DEFAULT_DELETE_PW,
) -> User:
    """Insert a User row directly into the DB (bypasses HTTP registration)."""
    if private_number is None:
        private_number = next_private_number()
    user = User(
        private_number=private_number,
        login_password_hash=hash_password(login_password),
        delete_password_hash=hash_password(delete_password),
        display_name=display_name,
        is_active=is_active,
    )
    db.add(user)
    await db.flush()
    return user


async def create_conversation_with_participants(
    db: AsyncSession,
    user_ids: list[uuid.UUID],
) -> Conversation:
    """Create a conversation and add participants directly in the DB."""
    conv = Conversation(is_group=len(user_ids) > 2)
    db.add(conv)
    await db.flush()
    for uid in user_ids:
        await db.execute(
            insert(conversation_participants).values(
                conversation_id=conv.id, user_id=uid,
            )
        )
    await db.flush()
    return conv
