from __future__ import annotations
"""
tests/test_profile.py
──────────────────────
Profile surface: bio on PATCH /users/me, and POST /users/me/password
(login-password change + session invalidation).
"""

import pytest

from tests.conftest import (
    DEFAULT_DELETE_PW,
    DEFAULT_LOGIN_PW,
    auth_header,
    register_and_login,
)

pytestmark = pytest.mark.asyncio


# ── Bio ───────────────────────────────────────────────────────────────────────

async def test_bio_roundtrip(client):
    acct = await register_and_login(client)
    headers = auth_header(acct["tokens"]["access_token"])

    resp = await client.patch("/api/v1/users/me", json={"bio": "  hello world  "}, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["bio"] == "hello world"

    resp = await client.get("/api/v1/users/me", headers=headers)
    assert resp.json()["bio"] == "hello world"


async def test_bio_too_long_rejected(client):
    acct = await register_and_login(client)
    headers = auth_header(acct["tokens"]["access_token"])

    resp = await client.patch("/api/v1/users/me", json={"bio": "x" * 129}, headers=headers)
    assert resp.status_code == 422


async def test_bio_empty_string_clears(client):
    acct = await register_and_login(client)
    headers = auth_header(acct["tokens"]["access_token"])

    await client.patch("/api/v1/users/me", json={"bio": "something"}, headers=headers)
    resp = await client.patch("/api/v1/users/me", json={"bio": ""}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["bio"] is None


async def test_bio_null_leaves_unchanged(client):
    acct = await register_and_login(client)
    headers = auth_header(acct["tokens"]["access_token"])

    await client.patch("/api/v1/users/me", json={"bio": "keep me"}, headers=headers)
    resp = await client.patch("/api/v1/users/me", json={"display_name": "New Name"}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["bio"] == "keep me"
    assert resp.json()["display_name"] == "New Name"


async def test_bio_visible_via_contact_lookup(client):
    alice = await register_and_login(client)
    bob = await register_and_login(client)
    await client.patch(
        "/api/v1/users/me",
        json={"bio": "bob's bio"},
        headers=auth_header(bob["tokens"]["access_token"]),
    )

    resp = await client.post(
        "/api/v1/contacts/lookup",
        json={"private_number": bob["private_number"]},
        headers=auth_header(alice["tokens"]["access_token"]),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["found"] is True
    assert body["user"]["bio"] == "bob's bio"


# ── Change password ───────────────────────────────────────────────────────────

NEW_PW = "BrandNewPass789"


async def test_change_password_success(client):
    acct = await register_and_login(client)
    headers = auth_header(acct["tokens"]["access_token"])

    resp = await client.post(
        "/api/v1/users/me/password",
        json={
            "current_password": DEFAULT_LOGIN_PW,
            "new_password": NEW_PW,
            "refresh_token": acct["tokens"]["refresh_token"],
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    # Old password no longer works, new one does.
    old_login = await client.post(
        "/api/v1/auth/login",
        json={"private_number": acct["private_number"], "login_password": DEFAULT_LOGIN_PW},
    )
    assert old_login.status_code == 401

    new_login = await client.post(
        "/api/v1/auth/login",
        json={"private_number": acct["private_number"], "login_password": NEW_PW},
    )
    assert new_login.status_code == 200
    assert new_login.json()["action"] == "authenticated"


async def test_change_password_wrong_current(client):
    acct = await register_and_login(client)
    resp = await client.post(
        "/api/v1/users/me/password",
        json={"current_password": "WrongPass999", "new_password": NEW_PW},
        headers=auth_header(acct["tokens"]["access_token"]),
    )
    assert resp.status_code == 403


async def test_change_password_collides_with_delete_password(client):
    acct = await register_and_login(client)
    resp = await client.post(
        "/api/v1/users/me/password",
        json={"current_password": DEFAULT_LOGIN_PW, "new_password": DEFAULT_DELETE_PW},
        headers=auth_header(acct["tokens"]["access_token"]),
    )
    assert resp.status_code == 400
    # Generic message — must not confirm the collision to a shoulder-surfer.
    assert "delete" not in resp.json()["detail"].lower()


async def test_change_password_too_short(client):
    acct = await register_and_login(client)
    resp = await client.post(
        "/api/v1/users/me/password",
        json={"current_password": DEFAULT_LOGIN_PW, "new_password": "short"},
        headers=auth_header(acct["tokens"]["access_token"]),
    )
    assert resp.status_code == 422


async def test_change_password_invalidates_other_sessions(client):
    acct = await register_and_login(client)
    # Second session for the same account (another device).
    other_login = await client.post(
        "/api/v1/auth/login",
        json={"private_number": acct["private_number"], "login_password": DEFAULT_LOGIN_PW},
    )
    assert other_login.status_code == 200
    other_tokens = other_login.json()["tokens"]

    resp = await client.post(
        "/api/v1/users/me/password",
        json={
            "current_password": DEFAULT_LOGIN_PW,
            "new_password": NEW_PW,
            "refresh_token": acct["tokens"]["refresh_token"],
        },
        headers=auth_header(acct["tokens"]["access_token"]),
    )
    assert resp.status_code == 200

    # The other device's refresh token is dead.
    other_refresh = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": other_tokens["refresh_token"]},
    )
    assert other_refresh.status_code == 401

    # The caller's own session survives.
    own_refresh = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": acct["tokens"]["refresh_token"]},
    )
    assert own_refresh.status_code == 200


async def test_change_password_without_refresh_token_kills_all_sessions(client):
    acct = await register_and_login(client)
    resp = await client.post(
        "/api/v1/users/me/password",
        json={"current_password": DEFAULT_LOGIN_PW, "new_password": NEW_PW},
        headers=auth_header(acct["tokens"]["access_token"]),
    )
    assert resp.status_code == 200

    own_refresh = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": acct["tokens"]["refresh_token"]},
    )
    assert own_refresh.status_code == 401
