from __future__ import annotations
"""
tests/test_devices.py
──────────────────────
Tests for device registration and listing endpoints.

Endpoints:
  POST /api/v1/devices/register — Register or update a device
  GET  /api/v1/devices          — List devices for authenticated user
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import (
    auth_header,
    create_user_directly,
    forge_expired_token,
    register_and_login,
)


# ── Device Registration ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_register_device_success(client: AsyncClient):
    """POST /devices/register returns 201 with correct fields."""
    result = await register_and_login(client)
    response = await client.post(
        "/api/v1/devices/register",
        json={
            "device_name": "iPhone 15 Pro",
            "platform": "ios",
            "push_token": "fake-apns-token-abc123",
            "public_key": "base64encodedpublickey==",
        },
        headers=auth_header(result["tokens"]["access_token"]),
    )
    assert response.status_code == 201
    data = response.json()
    assert data["device_name"] == "iPhone 15 Pro"
    assert data["platform"] == "ios"
    assert "id" in data


@pytest.mark.asyncio
async def test_register_device_without_auth(client: AsyncClient):
    """POST /devices/register without token returns 401 or 403."""
    response = await client.post(
        "/api/v1/devices/register",
        json={"device_name": "Phone", "platform": "ios"},
    )
    assert response.status_code in (401, 403)


@pytest.mark.asyncio
async def test_register_device_expired_token(client: AsyncClient):
    """POST /devices/register with expired token returns 401."""
    token = forge_expired_token(uuid.uuid4())
    response = await client.post(
        "/api/v1/devices/register",
        json={"device_name": "Phone", "platform": "ios"},
        headers=auth_header(token),
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_register_device_malformed_token(client: AsyncClient):
    """POST /devices/register with garbage token returns 401."""
    response = await client.post(
        "/api/v1/devices/register",
        json={"device_name": "Phone", "platform": "ios"},
        headers=auth_header("not.a.jwt"),
    )
    assert response.status_code in (401, 403)


@pytest.mark.asyncio
async def test_register_device_minimal_fields(client: AsyncClient):
    """Only device_name and platform required — no push_token or public_key."""
    result = await register_and_login(client)
    response = await client.post(
        "/api/v1/devices/register",
        json={"device_name": "Pixel 8", "platform": "android"},
        headers=auth_header(result["tokens"]["access_token"]),
    )
    assert response.status_code == 201
    data = response.json()
    assert data["device_name"] == "Pixel 8"
    assert data["platform"] == "android"
    assert data["push_token"] is None
    assert data["public_key"] is None


@pytest.mark.asyncio
async def test_register_device_invalid_platform(client: AsyncClient):
    """Invalid platform value returns 422."""
    result = await register_and_login(client)
    response = await client.post(
        "/api/v1/devices/register",
        json={"device_name": "Surface", "platform": "windows"},
        headers=auth_header(result["tokens"]["access_token"]),
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_register_device_missing_name(client: AsyncClient):
    """Missing device_name returns 422."""
    result = await register_and_login(client)
    response = await client.post(
        "/api/v1/devices/register",
        json={"platform": "ios"},
        headers=auth_header(result["tokens"]["access_token"]),
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_register_device_missing_platform(client: AsyncClient):
    """Missing platform returns 422."""
    result = await register_and_login(client)
    response = await client.post(
        "/api/v1/devices/register",
        json={"device_name": "Phone"},
        headers=auth_header(result["tokens"]["access_token"]),
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_register_device_empty_body(client: AsyncClient):
    """Empty JSON body returns 422."""
    result = await register_and_login(client)
    response = await client.post(
        "/api/v1/devices/register",
        json={},
        headers=auth_header(result["tokens"]["access_token"]),
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_register_device_upsert_by_push_token(client: AsyncClient):
    """Same push_token should update existing device, not create a duplicate."""
    result = await register_and_login(client)
    headers = auth_header(result["tokens"]["access_token"])
    push_token = "unique-push-token-upsert"

    # Register first time
    resp1 = await client.post(
        "/api/v1/devices/register",
        json={"device_name": "Phone v1", "platform": "ios", "push_token": push_token},
        headers=headers,
    )
    assert resp1.status_code == 201

    # Register second time with same push_token but different name
    resp2 = await client.post(
        "/api/v1/devices/register",
        json={"device_name": "Phone v2", "platform": "android", "push_token": push_token},
        headers=headers,
    )
    assert resp2.status_code == 201
    # Same device ID (upserted)
    assert resp2.json()["id"] == resp1.json()["id"]
    assert resp2.json()["device_name"] == "Phone v2"

    # List should show only 1 device
    list_resp = await client.get("/api/v1/devices", headers=headers)
    assert len(list_resp.json()) == 1


@pytest.mark.asyncio
async def test_register_device_different_push_tokens(client: AsyncClient):
    """Different push_tokens should create separate devices."""
    result = await register_and_login(client)
    headers = auth_header(result["tokens"]["access_token"])

    await client.post(
        "/api/v1/devices/register",
        json={"device_name": "Phone", "platform": "ios", "push_token": "token-a"},
        headers=headers,
    )
    await client.post(
        "/api/v1/devices/register",
        json={"device_name": "Tablet", "platform": "android", "push_token": "token-b"},
        headers=headers,
    )

    list_resp = await client.get("/api/v1/devices", headers=headers)
    assert len(list_resp.json()) == 2


@pytest.mark.asyncio
async def test_register_device_no_push_token_creates_new(client: AsyncClient):
    """Without push_token, each registration creates a new device (no upsert key)."""
    result = await register_and_login(client)
    headers = auth_header(result["tokens"]["access_token"])

    await client.post(
        "/api/v1/devices/register",
        json={"device_name": "Phone A", "platform": "ios"},
        headers=headers,
    )
    await client.post(
        "/api/v1/devices/register",
        json={"device_name": "Phone B", "platform": "ios"},
        headers=headers,
    )

    list_resp = await client.get("/api/v1/devices", headers=headers)
    assert len(list_resp.json()) == 2


@pytest.mark.asyncio
async def test_register_device_returns_uuid_id(client: AsyncClient):
    """Response id field should be a valid UUID."""
    result = await register_and_login(client)
    response = await client.post(
        "/api/v1/devices/register",
        json={"device_name": "Phone", "platform": "ios"},
        headers=auth_header(result["tokens"]["access_token"]),
    )
    assert response.status_code == 201
    uuid.UUID(response.json()["id"])  # Raises ValueError if invalid


# ── Device Listing ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_devices_empty(client: AsyncClient):
    """Authenticated user with no devices gets an empty list."""
    result = await register_and_login(client)
    response = await client.get(
        "/api/v1/devices",
        headers=auth_header(result["tokens"]["access_token"]),
    )
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_list_devices_requires_auth(client: AsyncClient):
    """GET /devices without token returns 401 or 403."""
    response = await client.get("/api/v1/devices")
    assert response.status_code in (401, 403)


@pytest.mark.asyncio
async def test_list_devices_ordered_desc(client: AsyncClient):
    """Devices should be ordered by created_at descending (newest first)."""
    result = await register_and_login(client)
    headers = auth_header(result["tokens"]["access_token"])

    await client.post(
        "/api/v1/devices/register",
        json={"device_name": "First", "platform": "ios"},
        headers=headers,
    )
    await client.post(
        "/api/v1/devices/register",
        json={"device_name": "Second", "platform": "android"},
        headers=headers,
    )

    response = await client.get("/api/v1/devices", headers=headers)
    devices = response.json()
    assert len(devices) == 2
    assert devices[0]["device_name"] == "Second"
    assert devices[1]["device_name"] == "First"


@pytest.mark.asyncio
async def test_list_devices_cross_user_isolation(client: AsyncClient):
    """User A's devices should not appear in User B's list."""
    # User A
    result_a = await register_and_login(client)
    headers_a = auth_header(result_a["tokens"]["access_token"])
    await client.post(
        "/api/v1/devices/register",
        json={"device_name": "A-Phone", "platform": "ios"},
        headers=headers_a,
    )

    # User B
    result_b = await register_and_login(client)
    headers_b = auth_header(result_b["tokens"]["access_token"])

    response = await client.get("/api/v1/devices", headers=headers_b)
    assert response.status_code == 200
    assert len(response.json()) == 0


@pytest.mark.asyncio
async def test_list_devices_expired_token(client: AsyncClient):
    """GET /devices with expired token returns 401."""
    token = forge_expired_token(uuid.uuid4())
    response = await client.get(
        "/api/v1/devices",
        headers=auth_header(token),
    )
    assert response.status_code == 401


# ── Auth Edge Cases ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_device_endpoints_deactivated_user(client: AsyncClient, db_session: AsyncSession):
    """Deactivated user gets 403 from get_current_user dependency."""
    result = await register_and_login(client)
    headers = auth_header(result["tokens"]["access_token"])

    # Deactivate the user directly in DB
    from sqlalchemy import select, update
    from app.db.models.user import User

    stmt = update(User).where(User.private_number == result["private_number"]).values(is_active=False)
    await db_session.execute(stmt)
    await db_session.flush()

    response = await client.get("/api/v1/devices", headers=headers)
    assert response.status_code == 403
    assert "deactivated" in response.json()["detail"].lower()
