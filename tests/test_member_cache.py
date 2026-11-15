"""
Conversation member lists cached in Redis (moderation_service.cached_participant_ids).

The cache must never make a send behave differently from Postgres:
  • every membership write drops the entry after its commit;
  • a stale entry can't bounce a valid send (rejections are re-checked
    against Postgres), and that re-check repairs the entry.
"""
from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from sqlalchemy import insert

from app.core.cache import KEY_PREFIX
from app.core.security import create_access_token
from app.db.models.conversation import Conversation, conversation_participants
from app.db.models.user import User
from app.services.moderation_service import cached_participant_ids
from tests.conftest import (
    TestSessionLocal,
    auth_header,
    create_conversation_with_participants,
    register_and_login,
)
from tests.test_groups import _envelope, _key, _recv_until, _ws_group
from tests.test_websocket import ws_app, ws_client  # noqa: F401 — fixtures

pytestmark = pytest.mark.asyncio


def _cache_key(conversation_id) -> str:
    return f"{KEY_PREFIX}conv:{conversation_id}:members"


def _send(ws, member_ids, temp_id: str) -> None:
    ws.send_text(json.dumps({
        "type": "message", "message_type": "text",
        "encrypted_payload": _envelope(member_ids), "client_temp_id": temp_id,
    }))


def _reply(ws, temp_id: str) -> dict:
    """The server's answer to one send: its echo OR its error (both carry the
    temp id), so a wrong outcome fails the assertion instead of hanging."""
    return _recv_until(
        ws,
        lambda f: f.get("type") in ("message", "error") and f.get("client_temp_id") == temp_id,
        limit=8,
    )


def _echo(ws, temp_id: str) -> dict:
    frame = _reply(ws, temp_id)
    assert frame["type"] == "message", frame
    return frame


def _error_code(ws, temp_id: str) -> str:
    frame = _reply(ws, temp_id)
    assert frame["type"] == "error", frame
    return frame["code"]


async def _contact_of(admin_id: UUID) -> UUID:
    """A groups-ready user who already chats 1:1 with `admin_id` (addable)."""
    async with TestSessionLocal() as s:
        u = User(
            private_number=f"07{os.urandom(4).hex()[:8]}",
            login_password_hash="x", delete_password_hash="x",
            display_name="Newcomer", public_key=_key(), supports_groups=True,
        )
        s.add(u)
        await s.flush()
        direct = Conversation(is_group=False)
        s.add(direct)
        await s.flush()
        await s.execute(insert(conversation_participants), [
            {"conversation_id": direct.id, "user_id": admin_id},
            {"conversation_id": direct.id, "user_id": u.id},
        ])
        await s.commit()
        return u.id


# ── Send path (WebSocket) ─────────────────────────────────────────────────────

async def test_stale_member_cache_never_bounces_a_valid_send(ws_client):  # noqa: F811
    ids, gid = await _ws_group(3)
    newcomer = await _contact_of(ids[0])
    admin = {"Authorization": f"Bearer {create_access_token(ids[0])}"}

    with ws_client.websocket_connect(f"/ws/{gid}?token={create_access_token(ids[0])}") as ws:
        _send(ws, ids, "t1")
        _echo(ws, "t1")  # the cache now holds the 3 original members

        # Someone joins but the invalidation is lost: the cache is stale.
        with patch("app.services.group_service.forget_participants", new=AsyncMock()):
            r = ws_client.post(
                f"/api/v1/conversations/{gid}/members",
                json={"member_ids": [str(newcomer)]}, headers=admin,
            )
        assert r.status_code == 200, r.text

        # Correctly encrypted for all 4 → accepted despite the stale cache.
        _send(ws, ids + [newcomer], "t2")
        assert _echo(ws, "t2")["client_temp_id"] == "t2"

        # And the entry was repaired: a 3-member envelope is now refused.
        _send(ws, ids, "t3")
        assert _error_code(ws, "t3") == "group_members_changed"


async def test_removing_a_member_invalidates_the_cached_list(ws_client):  # noqa: F811
    ids, gid = await _ws_group(3)
    admin = {"Authorization": f"Bearer {create_access_token(ids[0])}"}

    with ws_client.websocket_connect(f"/ws/{gid}?token={create_access_token(ids[0])}") as ws:
        _send(ws, ids, "t1")
        _echo(ws, "t1")  # primes the cache with all 3

        r = ws_client.delete(f"/api/v1/conversations/{gid}/members/{ids[2]}", headers=admin)
        assert r.status_code == 204, r.text

        # Still wrapped for the removed member → must be refused, which only
        # happens if the cached 3-member list was dropped.
        _send(ws, ids, "t2")
        assert _error_code(ws, "t2") == "group_members_changed"
        _send(ws, ids[:2], "t3")
        assert _echo(ws, "t3")["client_temp_id"] == "t3"


# ── Other membership writes (REST) ────────────────────────────────────────────

async def test_leaving_a_chat_drops_its_cached_members(client, db_session, fake_redis):
    alice = await register_and_login(client)
    bob = await register_and_login(client)
    alice_id, bob_id = UUID(alice["user"]["id"]), UUID(bob["user"]["id"])
    conv = await create_conversation_with_participants(db_session, [alice_id, bob_id])

    assert await cached_participant_ids(db_session, fake_redis, conv.id) == {alice_id, bob_id}
    assert await fake_redis.exists(_cache_key(conv.id))

    r = await client.delete(
        f"/api/v1/conversations/{conv.id}", headers=auth_header(bob["tokens"]["access_token"]),
    )
    assert r.status_code == 204

    assert not await fake_redis.exists(_cache_key(conv.id))
    assert await cached_participant_ids(db_session, fake_redis, conv.id) == {alice_id}


async def test_account_deletion_drops_cached_members_of_its_chats(client, db_session, fake_redis):
    alice = await register_and_login(client)
    bob = await register_and_login(client)
    alice_id, bob_id = UUID(alice["user"]["id"]), UUID(bob["user"]["id"])
    conv = await create_conversation_with_participants(db_session, [alice_id, bob_id])
    await cached_participant_ids(db_session, fake_redis, conv.id)

    r = await client.delete("/api/v1/users/me", headers=auth_header(bob["tokens"]["access_token"]))
    assert r.status_code == 200, r.text

    assert not await fake_redis.exists(_cache_key(conv.id))
