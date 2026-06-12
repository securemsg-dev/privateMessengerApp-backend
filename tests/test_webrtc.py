from __future__ import annotations
"""
tests/test_webrtc.py
─────────────────────
Tests for GET /webrtc/config (Phase E ICE server configuration).
"""

import pytest

from tests.conftest import auth_header, register_and_login


@pytest.mark.asyncio
async def test_webrtc_config_requires_auth(client):
    resp = await client.get("/api/v1/webrtc/config")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_webrtc_config_has_no_null_credential_fields(client):
    """
    Regression: react-native-webrtc's native RTCPeerConnection throws
    "Exception in HostFunction: username == null" when an ice_servers entry
    carries `username`/`credential` keys with null values. STUN-only entries
    must omit those keys entirely.
    """
    user = await register_and_login(client)
    resp = await client.get(
        "/api/v1/webrtc/config",
        headers=auth_header(user["tokens"]["access_token"]),
    )
    assert resp.status_code == 200

    servers = resp.json()["ice_servers"]
    assert len(servers) >= 1  # default STUN is always configured

    for server in servers:
        assert server["urls"]
        # Keys must be absent rather than null
        assert server.get("username") is not None or "username" not in server
        assert server.get("credential") is not None or "credential" not in server
