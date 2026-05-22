from __future__ import annotations
"""
tests/test_auth.py
────────────────────
Unit tests for phoneless authentication endpoints.

Tests cover:
  - User registration (success, validation, dual-password constraint)
  - Login (success, wrong password, unknown private_number, delete-intent branch)
  - Confirm-delete (success + cascades, invalid/expired token, wrong token type)
  - Token refresh (success, rotation, reuse)
  - Logout (success, idempotent, access token survives)

Run with: pytest tests/ -v
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from jose import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import DELETE_INTENT_TOKEN_TYPE
from tests.conftest import auth_header, register_and_login


# ── Registration ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_register_new_user(client: AsyncClient):
    """POST /register creates a user, returns tokens + 10-digit private_number."""
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "login_password": "LoginPass123",
            "delete_password": "DeletePass456",
            "display_name": "Alice",
        },
    )
    assert response.status_code == 201
    data = response.json()
    assert data["user"]["display_name"] == "Alice"
    assert data["user"]["is_active"] is True
    pn = data["private_number"]
    assert len(pn) == 10 and pn.isdigit()
    assert data["user"]["private_number"] == pn
    assert "access_token" in data["tokens"]
    assert "refresh_token" in data["tokens"]


@pytest.mark.asyncio
async def test_register_generates_unique_private_numbers(client: AsyncClient):
    """Two registrations produce distinct private_numbers."""
    r1 = await client.post(
        "/api/v1/auth/register",
        json={"login_password": "LoginPass123", "delete_password": "DeletePass456"},
    )
    r2 = await client.post(
        "/api/v1/auth/register",
        json={"login_password": "LoginPass123", "delete_password": "DeletePass456"},
    )
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["private_number"] != r2.json()["private_number"]


@pytest.mark.asyncio
async def test_register_rejects_matching_passwords(client: AsyncClient):
    """login_password == delete_password → 422."""
    response = await client.post(
        "/api/v1/auth/register",
        json={"login_password": "SamePass1234", "delete_password": "SamePass1234"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_register_rejects_short_password(client: AsyncClient):
    """< 8 chars login_password → 422."""
    response = await client.post(
        "/api/v1/auth/register",
        json={"login_password": "short1", "delete_password": "DeletePass456"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_register_rejects_short_delete_password(client: AsyncClient):
    """< 8 chars delete_password → 422."""
    response = await client.post(
        "/api/v1/auth/register",
        json={"login_password": "LoginPass123", "delete_password": "short1"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_register_missing_passwords(client: AsyncClient):
    """Missing login_password field → 422."""
    response = await client.post(
        "/api/v1/auth/register",
        json={"delete_password": "DeletePass456"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_register_no_body(client: AsyncClient):
    """POST /register with no body returns 422."""
    response = await client.post("/api/v1/auth/register", content=b"")
    assert response.status_code == 422


# ── Login ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_login_success(client: AsyncClient):
    """Login with correct private_number + login_password returns tokens."""
    reg = await register_and_login(client)
    pn = reg["private_number"]

    response = await client.post(
        "/api/v1/auth/login",
        json={"private_number": pn, "login_password": reg["login_password"]},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["action"] == "authenticated"
    assert data["user"]["private_number"] == pn
    assert "access_token" in data["tokens"]
    assert "refresh_token" in data["tokens"]


@pytest.mark.asyncio
async def test_login_wrong_password(client: AsyncClient):
    """Wrong login_password returns 401."""
    reg = await register_and_login(client)
    response = await client.post(
        "/api/v1/auth/login",
        json={"private_number": reg["private_number"], "login_password": "wrongpassword"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_login_with_delete_password_returns_confirm_delete(client: AsyncClient):
    """
    New behavior: supplying the delete_password on POST /login returns a
    delete-intent envelope (no user, no session tokens) so the client can
    show a warning dialog and call /confirm-delete.
    """
    reg = await register_and_login(client)
    response = await client.post(
        "/api/v1/auth/login",
        json={
            "private_number": reg["private_number"],
            "login_password": reg["delete_password"],
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["action"] == "confirm_delete"
    assert isinstance(data["delete_token"], str) and len(data["delete_token"]) > 0
    assert isinstance(data["expires_in"], int) and data["expires_in"] > 0
    # Must NOT leak session credentials or user info on the delete branch.
    assert "user" not in data
    assert "tokens" not in data


@pytest.mark.asyncio
async def test_login_unknown_private_number(client: AsyncClient):
    """Login with non-existent private_number returns 401 (no user enumeration)."""
    response = await client.post(
        "/api/v1/auth/login",
        json={"private_number": "9999999999", "login_password": "LoginPass123"},
    )
    assert response.status_code == 401
    # Same generic error as wrong-password to avoid enumeration.
    assert "invalid" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_login_invalid_private_number_format(client: AsyncClient):
    """Non-10-digit private_number returns 422."""
    response = await client.post(
        "/api/v1/auth/login",
        json={"private_number": "123", "login_password": "LoginPass123"},
    )
    assert response.status_code == 422


# ── Confirm delete (login-screen flow) ────────────────────────────────────────

async def _obtain_delete_intent_token(client: AsyncClient, reg: dict) -> str:
    """Helper: log in with the delete_password and return the delete_token."""
    resp = await client.post(
        "/api/v1/auth/login",
        json={
            "private_number": reg["private_number"],
            "login_password": reg["delete_password"],
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["action"] == "confirm_delete"
    return data["delete_token"]


@pytest.mark.asyncio
async def test_confirm_delete_with_intent_token_deletes_account(
    client: AsyncClient,
    db_session: AsyncSession,
):
    """POST /confirm-delete with a valid delete-intent token hard-deletes the user."""
    from app.db.models.user import User
    reg = await register_and_login(client)
    delete_token = await _obtain_delete_intent_token(client, reg)

    response = await client.post(
        "/api/v1/auth/confirm-delete",
        json={"delete_token": delete_token},
    )
    assert response.status_code == 200
    assert "deleted" in response.json()["message"].lower()

    result = await db_session.execute(
        select(User).where(User.private_number == reg["private_number"])
    )
    assert result.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_confirm_delete_cascades_sessions(
    client: AsyncClient,
    db_session: AsyncSession,
):
    """Confirming delete cascades to all of the user's sessions."""
    from app.db.models.session import Session as UserSession
    reg = await register_and_login(client)

    user_id = uuid.UUID(reg["user"]["id"])
    result = await db_session.execute(
        select(UserSession).where(UserSession.user_id == user_id)
    )
    assert result.scalars().first() is not None

    delete_token = await _obtain_delete_intent_token(client, reg)
    confirm = await client.post(
        "/api/v1/auth/confirm-delete",
        json={"delete_token": delete_token},
    )
    assert confirm.status_code == 200

    result = await db_session.execute(
        select(UserSession).where(UserSession.user_id == user_id)
    )
    assert result.scalars().first() is None


@pytest.mark.asyncio
async def test_confirm_delete_with_invalid_token_fails(client: AsyncClient):
    """Garbage delete_token → 401."""
    response = await client.post(
        "/api/v1/auth/confirm-delete",
        json={"delete_token": "not.a.valid.jwt"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_confirm_delete_with_access_token_fails(
    client: AsyncClient,
    db_session: AsyncSession,
):
    """
    An access token must NOT be accepted as a delete-intent token.
    verify_delete_intent_token rejects any token whose type != 'delete_intent'.
    """
    from app.db.models.user import User
    reg = await register_and_login(client)

    response = await client.post(
        "/api/v1/auth/confirm-delete",
        json={"delete_token": reg["tokens"]["access_token"]},
    )
    assert response.status_code == 401

    # User still exists.
    result = await db_session.execute(
        select(User).where(User.private_number == reg["private_number"])
    )
    assert result.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_confirm_delete_with_expired_token_fails(
    client: AsyncClient,
    db_session: AsyncSession,
):
    """Expired delete-intent token → 401 (user must re-enter credentials)."""
    from app.db.models.user import User
    reg = await register_and_login(client)

    # Forge an already-expired delete-intent token for this user.
    now = datetime.now(timezone.utc)
    expired_payload = {
        "sub": reg["user"]["id"],
        "type": DELETE_INTENT_TOKEN_TYPE,
        "iat": now - timedelta(minutes=10),
        "exp": now - timedelta(minutes=5),
    }
    expired_token = jwt.encode(
        expired_payload,
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )

    response = await client.post(
        "/api/v1/auth/confirm-delete",
        json={"delete_token": expired_token},
    )
    assert response.status_code == 401

    result = await db_session.execute(
        select(User).where(User.private_number == reg["private_number"])
    )
    assert result.scalar_one_or_none() is not None


# ── Token Management ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_refresh_token_success(client: AsyncClient):
    """POST /refresh with valid refresh token returns new token pair."""
    result = await register_and_login(client)
    tokens = result["tokens"]
    await asyncio.sleep(1.1)  # Ensure iat is different

    response = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": tokens["refresh_token"]},
    )
    assert response.status_code == 200
    new_tokens = response.json()
    assert "access_token" in new_tokens
    assert new_tokens["access_token"] != tokens["access_token"]


@pytest.mark.asyncio
async def test_refresh_invalid_token(client: AsyncClient):
    """POST /refresh with invalid token returns 401."""
    response = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": "not.a.valid.jwt"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_refresh_reuse_after_rotation(client: AsyncClient):
    """After refresh, the OLD refresh token should no longer work."""
    result = await register_and_login(client)
    old_refresh = result["tokens"]["refresh_token"]
    await asyncio.sleep(1.1)

    resp1 = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": old_refresh},
    )
    assert resp1.status_code == 200

    resp2 = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": old_refresh},
    )
    assert resp2.status_code == 401


@pytest.mark.asyncio
async def test_refresh_with_empty_string(client: AsyncClient):
    """Empty refresh token returns 401."""
    response = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": ""},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_refresh_returns_different_refresh_token(client: AsyncClient):
    """New refresh token should differ from old one (rotation)."""
    result = await register_and_login(client)
    old_refresh = result["tokens"]["refresh_token"]
    await asyncio.sleep(1.1)

    response = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": old_refresh},
    )
    assert response.status_code == 200
    assert response.json()["refresh_token"] != old_refresh


# ── Logout ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_logout_success(client: AsyncClient):
    """POST /logout invalidates the session; subsequent refresh fails."""
    result = await register_and_login(client)
    tokens = result["tokens"]

    logout_resp = await client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": tokens["refresh_token"]},
    )
    assert logout_resp.status_code == 200
    assert "logged out" in logout_resp.json()["message"]

    refresh_resp = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": tokens["refresh_token"]},
    )
    assert refresh_resp.status_code == 401


@pytest.mark.asyncio
async def test_logout_with_garbage_token(client: AsyncClient):
    """Logout with a non-existent token still returns 200 (forgiving)."""
    response = await client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": "this.is.garbage.token"},
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_logout_double_logout(client: AsyncClient):
    """Logging out twice with the same token should both return 200."""
    result = await register_and_login(client)
    refresh = result["tokens"]["refresh_token"]

    resp1 = await client.post("/api/v1/auth/logout", json={"refresh_token": refresh})
    assert resp1.status_code == 200
    resp2 = await client.post("/api/v1/auth/logout", json={"refresh_token": refresh})
    assert resp2.status_code == 200


@pytest.mark.asyncio
async def test_logout_access_token_still_works(client: AsyncClient):
    """After logout, the access token should still work (stateless JWT).
    Logout only invalidates the refresh token session."""
    result = await register_and_login(client)
    tokens = result["tokens"]

    # Register a device first so we have something to list
    await client.post(
        "/api/v1/devices/register",
        json={"device_name": "Test", "platform": "ios"},
        headers=auth_header(tokens["access_token"]),
    )

    await client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": tokens["refresh_token"]},
    )

    response = await client.get(
        "/api/v1/devices",
        headers=auth_header(tokens["access_token"]),
    )
    assert response.status_code == 200
