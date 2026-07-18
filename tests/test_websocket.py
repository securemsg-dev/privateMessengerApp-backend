from __future__ import annotations
"""
tests/test_websocket.py
────────────────────────
Tests for the WebSocket endpoint: /ws/{conversation_id}?token=<JWT>

Covers:
  - Authentication (missing, invalid, expired tokens)
  - Authorization (non-participant, non-existent conversation)
  - Messaging (valid text/voice/image, invalid JSON, unknown type, self_destruct)

Note: WebSocket router uses AsyncSessionLocal directly (not DI), so we patch it
with TestSessionLocal. Setup data is committed via a separate session so the
patched WS sessions can read it.
"""

import json
import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.security import create_access_token, hash_password
from app.db.models.conversation import Conversation, conversation_participants
from app.db.models.message import MessageMetadata
from app.db.models.user import User
from app.db.session import get_session
from app.main import create_app
from tests.conftest import TestSessionLocal, forge_expired_token


def _to_private_number(identifier: str) -> str:
    """Convert any test identifier string to a unique 10-digit private_number.
    Keeps test fixture arguments stable without needing to touch call sites."""
    digits = "".join(c for c in identifier if c.isdigit())
    digits = digits[-10:] if len(digits) >= 10 else digits.rjust(10, "1")
    return digits


def _make_user() -> User:
    """User factory with dummy password hashes for WS tests."""
    dummy = hash_password("Placeholder123")
    return User(
        private_number="",  # filled by caller
        login_password_hash=dummy,
        delete_password_hash=dummy,
    )


async def _setup_user_and_conversation(
    identifier: str,
    extra_identifier: str | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Create a user (and optionally a second) + conversation with committed data.

    Uses a separate session that commits, so the patched WS AsyncSessionLocal
    can read the data from the shared in-memory SQLite DB.

    Returns (user_id, conversation_id).
    """
    async with TestSessionLocal() as session:
        user = _make_user()
        user.private_number = _to_private_number(identifier)
        session.add(user)
        await session.flush()
        user_id = user.id

        participant_ids = [user_id]

        if extra_identifier:
            other = _make_user()
            other.private_number = _to_private_number(extra_identifier)
            session.add(other)
            await session.flush()
            participant_ids.append(other.id)

        conv = Conversation(is_group=False)
        session.add(conv)
        await session.flush()
        conv_id = conv.id

        for uid in participant_ids:
            await session.execute(
                insert(conversation_participants).values(
                    conversation_id=conv_id, user_id=uid,
                )
            )
        await session.commit()
    return user_id, conv_id


async def _setup_user_only(identifier: str) -> uuid.UUID:
    """Create a user (committed) without adding to any conversation."""
    async with TestSessionLocal() as session:
        user = _make_user()
        user.private_number = _to_private_number(identifier)
        session.add(user)
        await session.flush()
        user_id = user.id
        await session.commit()
    return user_id


@pytest.fixture
def ws_app():
    """Create a test app configured for WebSocket testing.

    Patches AsyncSessionLocal in the websocket router to use the test DB,
    swaps Redis for an in-memory fake (created inside the app's own event
    loop when the lifespan runs), and parks the maintenance sweeper so it
    doesn't touch the real database during tests.
    """
    import asyncio

    import fakeredis

    from app.core.limiter import limiter
    limiter.enabled = False

    app = create_app()

    # We don't need DI override for WS (it doesn't use DI), but set it for
    # any HTTP endpoints used indirectly.
    async def _override_get_session():
        async with TestSessionLocal() as session:
            yield session

    app.dependency_overrides[get_session] = _override_get_session

    with patch(
        "app.main.aioredis.from_url",
        side_effect=lambda *a, **k: fakeredis.FakeAsyncRedis(decode_responses=False),
    ), patch(
        "app.main.run_maintenance_loop",
        new=lambda: asyncio.sleep(3600),
    ), patch(
        "app.websocket.router.AsyncSessionLocal", TestSessionLocal,
    ), patch(
        "app.websocket.user_router.AsyncSessionLocal", TestSessionLocal,
    ):
        yield app


@pytest.fixture
def ws_client(ws_app):
    """TestClient with the lifespan running — this starts the Redis pub/sub
    subscriber, which fans published events back out to local sockets (the
    sender's echo, peers' messages, receipts)."""
    with TestClient(ws_app) as client:
        yield client


# ── WS Authentication ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ws_no_token(ws_client):
    """Connecting without a token query param should fail."""
    conv_id = uuid.uuid4()
    with pytest.raises(Exception):
        with ws_client.websocket_connect(f"/ws/{conv_id}"):
            pass


def _assert_closed_1008(ws) -> None:
    """The server accepts then closes with 1008 so the client can see the
    policy-violation code (a pre-accept rejection surfaces as a generic 1006
    and the app's token-refresh reconnect path would never fire)."""
    with pytest.raises(WebSocketDisconnect) as exc_info:
        ws.receive_text()
    assert exc_info.value.code == 1008


@pytest.mark.asyncio
async def test_ws_invalid_token(ws_client):
    """Connecting with an invalid JWT should close with 1008."""
    conv_id = uuid.uuid4()
    with ws_client.websocket_connect(f"/ws/{conv_id}?token=garbage") as ws:
        _assert_closed_1008(ws)


@pytest.mark.asyncio
async def test_ws_expired_token(ws_client):
    """Connecting with an expired JWT should close with 1008."""
    conv_id = uuid.uuid4()
    expired = forge_expired_token(uuid.uuid4())
    with ws_client.websocket_connect(f"/ws/{conv_id}?token={expired}") as ws:
        _assert_closed_1008(ws)


# ── WS Authorization ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ws_user_not_participant(ws_client):
    """User with valid token but not a conversation participant should be disconnected."""
    # Create user A (NOT in conversation) and user B (in conversation)
    user_a_id = await _setup_user_only("+60300000001")
    _, conv_id = await _setup_user_and_conversation("+60300000002")

    token = create_access_token(user_a_id)
    with ws_client.websocket_connect(f"/ws/{conv_id}?token={token}") as ws:
        _assert_closed_1008(ws)


@pytest.mark.asyncio
async def test_ws_nonexistent_conversation(ws_client):
    """Connecting to a non-existent conversation should close with 1008."""
    user_id = await _setup_user_only("+60300000003")
    token = create_access_token(user_id)
    fake_conv_id = uuid.uuid4()

    with ws_client.websocket_connect(f"/ws/{fake_conv_id}?token={token}") as ws:
        _assert_closed_1008(ws)


@pytest.mark.asyncio
async def test_ws_connect_success(ws_client):
    """Valid user who is a participant should connect successfully."""
    user_id, conv_id = await _setup_user_and_conversation("+60300000004")
    token = create_access_token(user_id)

    with ws_client.websocket_connect(f"/ws/{conv_id}?token={token}") as ws:
        # Connection succeeded — just close cleanly
        pass


# ── Per-user signaling WS (call offers) — route-collision regression ───────────

@pytest.mark.asyncio
async def test_user_ws_connect_success(ws_client):
    """
    Regression: /ws/user must NOT collide with /ws/{conversation_id}.

    If the parametrized route is registered first, "/ws/user" matches it with
    conversation_id="user", fails UUID coercion, and is rejected with 403 —
    silently breaking ALL call signaling. A valid token must connect here.
    """
    user_id = await _setup_user_only("+60300000020")
    token = create_access_token(user_id)

    with ws_client.websocket_connect(f"/ws/user?token={token}") as ws:
        # Connected — the route resolved to the user signaling endpoint, not
        # the conversation endpoint. Sending garbage gets a JSON error back,
        # proving we're talking to the user_router receive loop.
        ws.send_text("not json")
        resp = json.loads(ws.receive_text())
        assert resp["type"] == "error"
        assert resp["detail"] == "Invalid JSON"


@pytest.mark.asyncio
async def test_user_ws_invalid_token_rejected(ws_client):
    """A bad token on /ws/user must be closed with 1008."""
    with ws_client.websocket_connect("/ws/user?token=garbage") as ws:
        _assert_closed_1008(ws)


@pytest.mark.asyncio
async def test_user_ws_call_offer_forwarded_to_callee(ws_client):
    """
    End-to-end signaling: a call_offer from the caller is delivered to the
    callee's /ws/user socket (enriched with from_user_id + caller identity).
    """
    caller_id, conv_id = await _setup_user_and_conversation("+60300000021")
    # Add a callee to the SAME conversation so _share_a_conversation passes.
    callee_id = await _setup_user_only("+60300000022")
    async with TestSessionLocal() as session:
        await session.execute(
            conversation_participants.insert().values(
                conversation_id=conv_id, user_id=callee_id
            )
        )
        await session.commit()

    caller_token = create_access_token(caller_id)
    callee_token = create_access_token(callee_id)

    with ws_client.websocket_connect(f"/ws/user?token={callee_token}") as callee_ws, \
         ws_client.websocket_connect(f"/ws/user?token={caller_token}") as caller_ws:
        caller_ws.send_text(json.dumps({
            "type": "call_offer",
            "to_user_id": str(callee_id),
            "conversation_id": str(conv_id),
            "call_id": str(uuid.uuid4()),
            "sdp": "v=0...",
        }))
        offer = json.loads(callee_ws.receive_text())
        assert offer["type"] == "call_offer"
        assert offer["from_user_id"] == str(caller_id)
        assert offer["sdp"] == "v=0..."


# ── WS Messaging ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ws_send_invalid_json(ws_client):
    """Sending non-JSON text should return an error message."""
    user_id, conv_id = await _setup_user_and_conversation("+60300000005")
    token = create_access_token(user_id)

    with ws_client.websocket_connect(f"/ws/{conv_id}?token={token}") as ws:
        ws.send_text("not json")
        resp = json.loads(ws.receive_text())
        assert resp["type"] == "error"
        assert resp["detail"] == "Invalid JSON"


@pytest.mark.asyncio
async def test_ws_send_unknown_message_type(ws_client):
    """Unknown message_type should return an error."""
    user_id, conv_id = await _setup_user_and_conversation("+60300000006")
    token = create_access_token(user_id)

    with ws_client.websocket_connect(f"/ws/{conv_id}?token={token}") as ws:
        ws.send_text(json.dumps({
            "encrypted_payload": "cipher",
            "message_type": "video",
            "self_destruct": False,
        }))
        resp = json.loads(ws.receive_text())
        assert resp["type"] == "error"
        assert "Unknown message_type" in resp["detail"]


def _send_and_sync(ws, payload: dict) -> dict:
    """Send a valid message, then send invalid JSON as a sync barrier.

    The server processes inbound frames sequentially, so the "Invalid JSON"
    error reply guarantees the real message was committed. The sender also
    receives its own message echo via pub/sub, racing with the error frame —
    so drain frames until the barrier error arrives, collecting the echo.

    Returns the echoed message event (asserted to exist and match).
    """
    ws.send_text(json.dumps(payload))
    ws.send_text("__sync__")

    echo: dict | None = None
    for _ in range(10):  # bounded: echo + error are the only expected frames
        resp = json.loads(ws.receive_text())
        if resp.get("type") == "error":
            assert resp["detail"] == "Invalid JSON"
            break
        if resp.get("type") == "message":
            echo = resp
    else:
        raise AssertionError("Never received the sync-barrier error frame")

    # The echo may lag the barrier error (pub/sub round-trip) — wait for it.
    if echo is None:
        resp = json.loads(ws.receive_text())
        assert resp.get("type") == "message"
        echo = resp

    assert echo["encrypted_payload"] == payload["encrypted_payload"]
    assert echo["message_type"] == payload["message_type"]
    assert echo["message_id"]
    return echo


@pytest.mark.asyncio
async def test_ws_send_text_message(ws_client):
    """Sending a valid text message should not return an error."""
    user_id, conv_id = await _setup_user_and_conversation("+60300000007")
    token = create_access_token(user_id)

    with ws_client.websocket_connect(f"/ws/{conv_id}?token={token}") as ws:
        _send_and_sync(ws, {
            "encrypted_payload": "encrypted-text-here",
            "message_type": "text",
            "self_destruct": False,
        })


@pytest.mark.asyncio
async def test_ws_send_voice_message(ws_client):
    """Voice message type should be accepted."""
    user_id, conv_id = await _setup_user_and_conversation("+60300000008")
    token = create_access_token(user_id)

    with ws_client.websocket_connect(f"/ws/{conv_id}?token={token}") as ws:
        _send_and_sync(ws, {
            "encrypted_payload": "encrypted-voice-data",
            "message_type": "voice",
            "self_destruct": False,
        })


@pytest.mark.asyncio
async def test_ws_send_image_message(ws_client):
    """Image message type should be accepted."""
    user_id, conv_id = await _setup_user_and_conversation("+60300000009")
    token = create_access_token(user_id)

    with ws_client.websocket_connect(f"/ws/{conv_id}?token={token}") as ws:
        _send_and_sync(ws, {
            "encrypted_payload": "encrypted-image-data",
            "message_type": "image",
            "self_destruct": False,
        })


# ── WS Two-user flow (send → echo → delivery → read) ────────────────────────

async def _user_id_by_identifier(identifier: str) -> uuid.UUID:
    """Look up a user created by `_setup_user_and_conversation`'s extra arg."""
    async with TestSessionLocal() as session:
        result = await session.execute(
            select(User).where(User.private_number == _to_private_number(identifier))
        )
        return result.scalar_one().id


@pytest.mark.asyncio
async def test_ws_full_message_flow_two_users(ws_client):
    """End-to-end happy path: A sends, B receives, B acks delivery + read."""
    user_a_id, conv_id = await _setup_user_and_conversation(
        "+60300000010", extra_identifier="+60300000011",
    )
    user_b_id = await _user_id_by_identifier("+60300000011")

    token_a = create_access_token(user_a_id)
    token_b = create_access_token(user_b_id)

    with ws_client.websocket_connect(f"/ws/{conv_id}?token={token_a}") as ws_a, \
         ws_client.websocket_connect(f"/ws/{conv_id}?token={token_b}") as ws_b:

        # A sends a message
        ws_a.send_text(json.dumps({
            "type": "message",
            "encrypted_payload": "cipher-blob",
            "message_type": "text",
            "self_destruct": False,
            "client_temp_id": "tmp-1",
        }))

        # B receives it
        evt_b = json.loads(ws_b.receive_text())
        assert evt_b["type"] == "message"
        assert evt_b["conversation_id"] == str(conv_id)
        assert evt_b["sender_id"] == str(user_a_id)
        assert evt_b["encrypted_payload"] == "cipher-blob"
        msg_id = evt_b["message_id"]

        # A receives its own echo, carrying the client_temp_id for
        # optimistic-row reconciliation
        echo_a = json.loads(ws_a.receive_text())
        assert echo_a["type"] == "message"
        assert echo_a["message_id"] == msg_id
        assert echo_a["client_temp_id"] == "tmp-1"

        # B acks delivery → A sees the delivery receipt
        ws_b.send_text(json.dumps({"type": "delivery", "message_id": msg_id}))
        receipt = json.loads(ws_a.receive_text())
        assert receipt["type"] == "delivery"
        assert receipt["message_id"] == msg_id
        assert receipt["by_user_id"] == str(user_b_id)

        # B reads → A sees the read receipt
        ws_b.send_text(json.dumps({"type": "read", "message_id": msg_id}))
        receipt = json.loads(ws_a.receive_text())
        assert receipt["type"] == "read"
        assert receipt["message_id"] == msg_id

    # Receipts are persisted, not just broadcast
    async with TestSessionLocal() as session:
        msg = (await session.execute(
            select(MessageMetadata).where(MessageMetadata.id == uuid.UUID(msg_id))
        )).scalar_one()
        assert msg.delivered_at is not None
        assert msg.read_at is not None


@pytest.mark.asyncio
async def test_ws_batched_read_receipts(ws_client):
    """B acks several messages in ONE `read` frame; A gets one event with all
    the ids and every row is persisted as read. (Batching keeps a chat-open
    burst under the per-connection throttle.)"""
    user_a_id, conv_id = await _setup_user_and_conversation(
        "+60300000012", extra_identifier="+60300000013",
    )
    user_b_id = await _user_id_by_identifier("+60300000013")

    token_a = create_access_token(user_a_id)
    token_b = create_access_token(user_b_id)

    with ws_client.websocket_connect(f"/ws/{conv_id}?token={token_a}") as ws_a, \
         ws_client.websocket_connect(f"/ws/{conv_id}?token={token_b}") as ws_b:

        msg_ids: list[str] = []
        for i in range(3):
            ws_a.send_text(json.dumps({
                "type": "message",
                "encrypted_payload": f"cipher-{i}",
                "message_type": "text",
            }))
            evt_b = json.loads(ws_b.receive_text())
            assert evt_b["type"] == "message"
            msg_ids.append(evt_b["message_id"])
            # Drain A's own echo so the receipt below is A's next frame.
            echo_a = json.loads(ws_a.receive_text())
            assert echo_a["type"] == "message"

        ws_b.send_text(json.dumps({"type": "read", "message_ids": msg_ids}))
        receipt = json.loads(ws_a.receive_text())
        assert receipt["type"] == "read"
        assert set(receipt["message_ids"]) == set(msg_ids)
        assert receipt["by_user_id"] == str(user_b_id)

    async with TestSessionLocal() as session:
        for mid in msg_ids:
            msg = (await session.execute(
                select(MessageMetadata).where(MessageMetadata.id == uuid.UUID(mid))
            )).scalar_one()
            assert msg.read_at is not None
            assert msg.delivered_at is not None


# ── Real-time spine: live message_notification over the user channel ───────────

@pytest.mark.asyncio
async def test_message_notification_delivered_to_user_socket(ws_client):
    """
    When a recipient is online but NOT in the conversation (only their /ws/user
    channel is open), a new message reaches them as a `message_notification`
    so their chat list can update live — without an Expo push.
    """
    user_a_id, conv_id = await _setup_user_and_conversation(
        "+60300000030", extra_identifier="+60300000031",
    )
    user_b_id = await _user_id_by_identifier("+60300000031")
    token_a = create_access_token(user_a_id)
    token_b = create_access_token(user_b_id)

    # B is online on their user channel only (NOT in the conversation WS).
    with ws_client.websocket_connect(f"/ws/user?token={token_b}") as ws_b_user, \
         ws_client.websocket_connect(f"/ws/{conv_id}?token={token_a}") as ws_a:
        ws_a.send_text(json.dumps({
            "encrypted_payload": "cipher-xyz",
            "message_type": "text",
            "client_temp_id": "tmp-n1",
        }))
        # A still gets its own echo over the conversation socket.
        echo_a = json.loads(ws_a.receive_text())
        assert echo_a["type"] == "message"

        # B receives a live notification over the user channel.
        note = json.loads(ws_b_user.receive_text())
        assert note["type"] == "message_notification"
        assert note["conversation_id"] == str(conv_id)
        assert note["sender_id"] == str(user_a_id)
        assert note["encrypted_payload"] == "cipher-xyz"
        assert "sender_name" in note


@pytest.mark.asyncio
async def test_is_user_connected_presence():
    """Presence helper reflects user-channel and conversation connections."""
    from app.websocket.manager import manager

    uid = str(uuid.uuid4())
    assert manager.is_user_connected(uid) is False

    class _FakeWS:
        async def accept(self):  # connect_user calls accept()
            return None

    ws = _FakeWS()
    await manager.connect_user(ws, uid)  # type: ignore[arg-type]
    assert manager.is_user_connected(uid) is True

    await manager.disconnect_user(ws, uid)  # type: ignore[arg-type]
    assert manager.is_user_connected(uid) is False
