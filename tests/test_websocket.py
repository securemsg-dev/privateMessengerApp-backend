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
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.testclient import TestClient

from app.core.security import create_access_token, hash_password
from app.db.models.conversation import Conversation, conversation_participants
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
def ws_app(fake_redis):
    """Create a test app configured for WebSocket testing.

    Patches AsyncSessionLocal in the websocket router to use the test DB.
    """
    from app.core.limiter import limiter
    limiter.enabled = False

    app = create_app()

    # We don't need DI override for WS (it doesn't use DI), but set it for
    # any HTTP endpoints used indirectly.
    async def _override_get_session():
        async with TestSessionLocal() as session:
            yield session

    app.dependency_overrides[get_session] = _override_get_session
    app.state.redis = fake_redis

    with patch("app.websocket.router.AsyncSessionLocal", TestSessionLocal):
        yield app


@pytest.fixture
def ws_client(ws_app):
    """Synchronous TestClient for WebSocket testing."""
    return TestClient(ws_app)


# ── WS Authentication ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ws_no_token(ws_client):
    """Connecting without a token query param should fail."""
    conv_id = uuid.uuid4()
    with pytest.raises(Exception):
        with ws_client.websocket_connect(f"/ws/{conv_id}"):
            pass


@pytest.mark.asyncio
async def test_ws_invalid_token(ws_client):
    """Connecting with an invalid JWT should disconnect with 1008."""
    conv_id = uuid.uuid4()
    with pytest.raises(Exception):
        with ws_client.websocket_connect(f"/ws/{conv_id}?token=garbage"):
            pass


@pytest.mark.asyncio
async def test_ws_expired_token(ws_client):
    """Connecting with an expired JWT should disconnect."""
    conv_id = uuid.uuid4()
    expired = forge_expired_token(uuid.uuid4())
    with pytest.raises(Exception):
        with ws_client.websocket_connect(f"/ws/{conv_id}?token={expired}"):
            pass


# ── WS Authorization ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ws_user_not_participant(ws_client):
    """User with valid token but not a conversation participant should be disconnected."""
    # Create user A (NOT in conversation) and user B (in conversation)
    user_a_id = await _setup_user_only("+60300000001")
    _, conv_id = await _setup_user_and_conversation("+60300000002")

    token = create_access_token(user_a_id)
    with pytest.raises(Exception):
        with ws_client.websocket_connect(f"/ws/{conv_id}?token={token}"):
            pass


@pytest.mark.asyncio
async def test_ws_nonexistent_conversation(ws_client):
    """Connecting to a non-existent conversation should disconnect."""
    user_id = await _setup_user_only("+60300000003")
    token = create_access_token(user_id)
    fake_conv_id = uuid.uuid4()

    with pytest.raises(Exception):
        with ws_client.websocket_connect(f"/ws/{fake_conv_id}?token={token}"):
            pass


@pytest.mark.asyncio
async def test_ws_connect_success(ws_client):
    """Valid user who is a participant should connect successfully."""
    user_id, conv_id = await _setup_user_and_conversation("+60300000004")
    token = create_access_token(user_id)

    with ws_client.websocket_connect(f"/ws/{conv_id}?token={token}") as ws:
        # Connection succeeded — just close cleanly
        pass


# ── WS Messaging ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ws_send_invalid_json(ws_client):
    """Sending non-JSON text should return an error message."""
    user_id, conv_id = await _setup_user_and_conversation("+60300000005")
    token = create_access_token(user_id)

    with ws_client.websocket_connect(f"/ws/{conv_id}?token={token}") as ws:
        ws.send_text("not json")
        resp = json.loads(ws.receive_text())
        assert resp["error"] == "Invalid JSON"


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
        assert "Unknown message_type" in resp["error"]


def _send_and_sync(ws, payload: dict) -> None:
    """Send a valid message, then send invalid JSON as a sync barrier.

    The server processes messages sequentially. By sending invalid JSON after
    the real message and waiting for the error response, we ensure the DB
    commit from the real message has completed before we close the connection.
    """
    ws.send_text(json.dumps(payload))
    ws.send_text("__sync__")
    resp = json.loads(ws.receive_text())
    assert resp["error"] == "Invalid JSON"


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
