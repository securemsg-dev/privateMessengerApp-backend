from __future__ import annotations
"""
tests/test_health.py
─────────────────────
Tests for the GET /health endpoint.
"""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_health_returns_200(client: AsyncClient):
    """GET /health returns 200 with status ok."""
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_health_response_schema(client: AsyncClient):
    """Response matches exact expected schema."""
    response = await client.get("/health")
    data = response.json()
    assert data == {"status": "ok", "version": "1.0.0"}


@pytest.mark.asyncio
async def test_health_no_auth_required(client: AsyncClient):
    """Health endpoint works without any Authorization header."""
    response = await client.get("/health")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_health_post_not_allowed(client: AsyncClient):
    """POST /health should return 405 Method Not Allowed."""
    response = await client.post("/health")
    assert response.status_code == 405


@pytest.mark.asyncio
async def test_health_put_not_allowed(client: AsyncClient):
    """PUT /health should return 405 Method Not Allowed."""
    response = await client.put("/health")
    assert response.status_code == 405
