from __future__ import annotations
"""
tests/test_conversations_and_devices.py
────────────────────────────────────────
Coverage for two pre-launch review fixes:

1. GET /conversations batched hydration — the endpoint was rewritten from
   3-queries-per-conversation to fixed-count batch queries (window function
   for last messages). These tests pin the response shape and correctness.

2. Device push-token claim — a push token identifies a physical phone, so
   registering it under a new account must evict rows held by other
   accounts (privacy: no cross-account notifications), and clear-push must
   detach it on logout.
"""

import uuid
from datetime import datetime, timedelta, timezone

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.message import MessageMetadata, MessageType
from tests.conftest import (
    auth_header,
    create_conversation_with_participants,
    register_and_login,
)


async def _add_message(
    db: AsyncSession,
    conversation_id,
    sender_id,
    payload: str,
    created_at: datetime,
    read: bool = False,
) -> MessageMetadata:
    msg = MessageMetadata(
        conversation_id=conversation_id,
        sender_id=sender_id,
        message_type=MessageType.text,
        encrypted_payload=payload,
        created_at=created_at,
        read_at=created_at if read else None,
    )
    db.add(msg)
    await db.flush()
    return msg


# ── GET /conversations (batched hydration) ───────────────────────────────────

async def test_list_conversations_hydrates_batches(
    client: AsyncClient, db_session: AsyncSession
):
    alice = await register_and_login(client, display_name="Alice")
    bob = await register_and_login(client, display_name="Bob")
    carol = await register_and_login(client, display_name="Carol")

    alice_id = uuid.UUID(alice["user"]["id"])
    bob_id = uuid.UUID(bob["user"]["id"])
    carol_id = uuid.UUID(carol["user"]["id"])

    conv_ab = await create_conversation_with_participants(db_session, [alice_id, bob_id])
    conv_ac = await create_conversation_with_participants(db_session, [alice_id, carol_id])

    now = datetime.now(timezone.utc)
    # conv_ab: two messages from Bob (unread), newest is "b2"
    await _add_message(db_session, conv_ab.id, bob_id, "b1", now - timedelta(minutes=10))
    await _add_message(db_session, conv_ab.id, bob_id, "b2", now - timedelta(minutes=5))
    # conv_ac: newer conversation, one READ message from Carol
    await _add_message(
        db_session, conv_ac.id, carol_id, "c1", now - timedelta(minutes=1), read=True
    )

    resp = await client.get(
        "/api/v1/conversations", headers=auth_header(alice["tokens"]["access_token"])
    )
    assert resp.status_code == 200, resp.text
    convs = resp.json()
    assert len(convs) == 2

    # Sorted by last-message recency: conv_ac (1 min ago) first
    first, second = convs[0], convs[1]
    assert first["id"] == str(conv_ac.id)
    assert second["id"] == str(conv_ab.id)

    # Other participant resolved per conversation
    assert first["other_participant"]["display_name"] == "Carol"
    assert second["other_participant"]["display_name"] == "Bob"

    # Last message is the NEWEST message of each conversation
    assert first["last_message"]["encrypted_payload"] == "c1"
    assert second["last_message"]["encrypted_payload"] == "b2"

    # Unread counts: both Bob messages unread; Carol's message read
    assert second["unread_count"] == 2
    assert first["unread_count"] == 0

    # Prefs default when never touched
    assert second["is_pinned"] is False
    assert second["mute_until"] is None


async def test_list_conversations_includes_prefs(
    client: AsyncClient, db_session: AsyncSession
):
    alice = await register_and_login(client)
    bob = await register_and_login(client)
    conv = await create_conversation_with_participants(
        db_session,
        [uuid.UUID(alice["user"]["id"]), uuid.UUID(bob["user"]["id"])],
    )

    pin = await client.patch(
        f"/api/v1/conversations/{conv.id}/prefs",
        json={"is_pinned": True},
        headers=auth_header(alice["tokens"]["access_token"]),
    )
    assert pin.status_code == 200, pin.text

    resp = await client.get(
        "/api/v1/conversations", headers=auth_header(alice["tokens"]["access_token"])
    )
    assert resp.status_code == 200
    convs = resp.json()
    assert len(convs) == 1
    assert convs[0]["is_pinned"] is True

    # Bob never pinned anything — prefs are per-user
    resp_bob = await client.get(
        "/api/v1/conversations", headers=auth_header(bob["tokens"]["access_token"])
    )
    assert resp_bob.json()[0]["is_pinned"] is False


async def test_list_conversations_empty(client: AsyncClient):
    user = await register_and_login(client)
    resp = await client.get(
        "/api/v1/conversations", headers=auth_header(user["tokens"]["access_token"])
    )
    assert resp.status_code == 200
    assert resp.json() == []


# ── Device push-token claim + clear ───────────────────────────────────────────

PUSH_TOKEN = "ExponentPushToken[test-shared-phone]"


async def _register_device(client: AsyncClient, access_token: str) -> dict:
    resp = await client.post(
        "/api/v1/devices/register",
        json={
            "device_name": "Shared Phone",
            "platform": "android",
            "push_token": PUSH_TOKEN,
        },
        headers=auth_header(access_token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _device_tokens(client: AsyncClient, access_token: str) -> list:
    resp = await client.get("/api/v1/devices", headers=auth_header(access_token))
    assert resp.status_code == 200
    return [d["push_token"] for d in resp.json()]


async def test_push_token_claimed_across_accounts(client: AsyncClient):
    """User B logging in on A's phone must strip the token from A's devices."""
    alice = await register_and_login(client)
    bob = await register_and_login(client)

    await _register_device(client, alice["tokens"]["access_token"])
    assert PUSH_TOKEN in await _device_tokens(client, alice["tokens"]["access_token"])

    # Same physical phone, different account
    await _register_device(client, bob["tokens"]["access_token"])

    assert PUSH_TOKEN in await _device_tokens(client, bob["tokens"]["access_token"])
    # Alice must NOT keep a device row holding this phone's token
    assert PUSH_TOKEN not in await _device_tokens(client, alice["tokens"]["access_token"])


async def test_clear_push_token(client: AsyncClient):
    user = await register_and_login(client)
    await _register_device(client, user["tokens"]["access_token"])

    resp = await client.post(
        "/api/v1/devices/clear-push",
        json={"push_token": PUSH_TOKEN},
        headers=auth_header(user["tokens"]["access_token"]),
    )
    assert resp.status_code == 204

    tokens = await _device_tokens(client, user["tokens"]["access_token"])
    # Device row survives (it holds the E2EE key) but the token is detached
    assert tokens == [None]

    # Idempotent: clearing again is a silent no-op
    resp2 = await client.post(
        "/api/v1/devices/clear-push",
        json={"push_token": PUSH_TOKEN},
        headers=auth_header(user["tokens"]["access_token"]),
    )
    assert resp2.status_code == 204


async def test_create_conversation_race_returns_winner(client, db_session):
    """If the unique direct_key insert collides (concurrent create from both
    sides), the endpoint returns the already-created conversation instead of
    erroring or duplicating."""
    from app.db.models.conversation import Conversation

    alice = await register_and_login(client)
    bob = await register_and_login(client)
    alice_id = uuid.UUID(alice["user"]["id"])
    bob_id = uuid.UUID(bob["user"]["id"])

    # Simulate the race winner: a keyed conversation row that the existence
    # check can't find (no participant rows — mimics the loser's view mid-race).
    # Everything is COMMITTED, as the winner's transaction would be in
    # production — the endpoint's rollback must only undo the loser's insert.
    direct_key = ":".join(sorted((str(alice_id), str(bob_id))))
    winner = Conversation(is_group=False, name=None, direct_key=direct_key)
    db_session.add(winner)
    await db_session.commit()
    winner_id = str(winner.id)

    resp = await client.post(
        "/api/v1/conversations",
        json={"other_private_number": bob["private_number"]},
        headers=auth_header(alice["tokens"]["access_token"]),
    )
    assert resp.status_code in (200, 201), resp.text
    assert resp.json()["id"] == winner_id
