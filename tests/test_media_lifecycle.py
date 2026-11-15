from __future__ import annotations
"""
tests/test_media_lifecycle.py
──────────────────────────────
Media blob storage lifecycle: delete-for-everyone and account deletion must
remove the ciphertext file + blob row, not just the message/user rows.
"""

from datetime import datetime, timezone
from unittest.mock import patch
from uuid import UUID

import pytest

from app.db.models.media_blob import MediaBlob
from app.db.models.message import MessageMetadata
from app.services.media_storage import LocalFileStorage
from tests.conftest import (
    auth_header,
    create_conversation_with_participants,
    register_and_login,
)

pytestmark = pytest.mark.asyncio


async def _make_uploaded_blob(db, owner_id: UUID, storage: LocalFileStorage) -> MediaBlob:
    blob = MediaBlob(
        owner_id=owner_id,
        size_bytes=5,
        mime="image/jpeg",
        uploaded_at=datetime.now(timezone.utc),
    )
    db.add(blob)
    await db.flush()
    await storage.write_bytes(blob.id, b"bytes")
    return blob


async def test_delete_for_everyone_removes_blob(client, db_session, tmp_path):
    storage = LocalFileStorage(base_path=str(tmp_path))
    alice = await register_and_login(client)
    bob = await register_and_login(client)
    alice_id = UUID(alice["user"]["id"])
    bob_id = UUID(bob["user"]["id"])

    conv = await create_conversation_with_participants(db_session, [alice_id, bob_id])
    blob = await _make_uploaded_blob(db_session, alice_id, storage)
    msg = MessageMetadata(
        conversation_id=conv.id,
        sender_id=alice_id,
        encrypted_payload="cipher",
        media_blob_id=blob.id,
    )
    db_session.add(msg)
    await db_session.flush()

    with patch("app.api.v1.endpoints.messages.storage_for", return_value=storage):
        resp = await client.post(
            f"/api/v1/messages/{msg.id}/delete",
            json={"scope": "everyone"},
            headers=auth_header(alice["tokens"]["access_token"]),
        )
    assert resp.status_code == 204, resp.text

    assert await storage.read_bytes(blob.id) is None
    assert await db_session.get(MediaBlob, blob.id) is None
    refreshed = await db_session.get(MessageMetadata, msg.id)
    assert refreshed.deleted_at is not None
    assert refreshed.media_blob_id is None


async def test_delete_for_me_keeps_blob(client, db_session, tmp_path):
    storage = LocalFileStorage(base_path=str(tmp_path))
    alice = await register_and_login(client)
    bob = await register_and_login(client)
    alice_id = UUID(alice["user"]["id"])
    bob_id = UUID(bob["user"]["id"])

    conv = await create_conversation_with_participants(db_session, [alice_id, bob_id])
    blob = await _make_uploaded_blob(db_session, alice_id, storage)
    msg = MessageMetadata(
        conversation_id=conv.id,
        sender_id=alice_id,
        encrypted_payload="cipher",
        media_blob_id=blob.id,
    )
    db_session.add(msg)
    await db_session.flush()

    with patch("app.api.v1.endpoints.messages.storage_for", return_value=storage):
        resp = await client.post(
            f"/api/v1/messages/{msg.id}/delete",
            json={"scope": "me"},
            headers=auth_header(bob["tokens"]["access_token"]),
        )
    assert resp.status_code == 204, resp.text

    # Hide-for-me must not touch the shared blob.
    assert await storage.read_bytes(blob.id) is not None
    assert await db_session.get(MediaBlob, blob.id) is not None


async def test_account_delete_removes_owned_blobs(client, db_session, tmp_path):
    storage = LocalFileStorage(base_path=str(tmp_path))
    acct = await register_and_login(client)
    user_id = UUID(acct["user"]["id"])
    blob = await _make_uploaded_blob(db_session, user_id, storage)
    blob_id = blob.id

    # Delete flow: login with the delete password → confirm with the token.
    login = await client.post(
        "/api/v1/auth/login",
        json={
            "private_number": acct["private_number"],
            "login_password": acct["delete_password"],
        },
    )
    assert login.status_code == 200
    assert login.json()["action"] == "confirm_delete"

    with patch("app.services.auth_service.storage_for", return_value=storage):
        resp = await client.post(
            "/api/v1/auth/confirm-delete",
            json={"delete_token": login.json()["delete_token"]},
        )
    assert resp.status_code == 200, resp.text

    assert await storage.read_bytes(blob_id) is None
    assert await db_session.get(MediaBlob, blob_id) is None


# ── View-once media ──────────────────────────────────────────────────────────

async def _view_once_message(client, db_session, storage):
    alice = await register_and_login(client)
    bob = await register_and_login(client)
    alice_id = UUID(alice["user"]["id"])
    bob_id = UUID(bob["user"]["id"])
    conv = await create_conversation_with_participants(db_session, [alice_id, bob_id])
    blob = await _make_uploaded_blob(db_session, alice_id, storage)
    msg = MessageMetadata(
        conversation_id=conv.id,
        sender_id=alice_id,
        encrypted_payload="cipher",
        media_blob_id=blob.id,
        self_destruct=True,
    )
    db_session.add(msg)
    await db_session.flush()
    return alice, bob, conv, blob, msg


async def test_view_once_opened_wipes_payload_and_blob(client, db_session, tmp_path):
    storage = LocalFileStorage(base_path=str(tmp_path))
    alice, bob, conv, blob, msg = await _view_once_message(client, db_session, storage)
    blob_id = blob.id

    with patch("app.api.v1.endpoints.messages.storage_for", return_value=storage):
        # The sender can't "open" their own view-once message.
        resp = await client.post(
            f"/api/v1/messages/{msg.id}/opened",
            headers=auth_header(alice["tokens"]["access_token"]),
        )
        assert resp.status_code == 403

        resp = await client.post(
            f"/api/v1/messages/{msg.id}/opened",
            headers=auth_header(bob["tokens"]["access_token"]),
        )
        assert resp.status_code == 204
        # Idempotent.
        resp = await client.post(
            f"/api/v1/messages/{msg.id}/opened",
            headers=auth_header(bob["tokens"]["access_token"]),
        )
        assert resp.status_code == 204

    await db_session.refresh(msg)
    assert msg.opened_at is not None
    assert msg.encrypted_payload == ""
    assert msg.media_blob_id is None
    assert await db_session.get(MediaBlob, blob_id) is None
    assert await storage.read_bytes(blob_id) is None

    # History reports it as opened to both sides.
    resp = await client.get(
        f"/api/v1/conversations/{conv.id}/messages",
        headers=auth_header(alice["tokens"]["access_token"]),
    )
    assert resp.status_code == 200
    row = next(m for m in resp.json()["messages"] if m["id"] == str(msg.id))
    assert row["self_destruct"] is True
    assert row["opened_at"] is not None
    assert row["encrypted_payload"] == ""


async def test_opened_rejects_normal_messages(client, db_session, tmp_path):
    alice = await register_and_login(client)
    bob = await register_and_login(client)
    conv = await create_conversation_with_participants(
        db_session, [UUID(alice["user"]["id"]), UUID(bob["user"]["id"])],
    )
    msg = MessageMetadata(
        conversation_id=conv.id,
        sender_id=UUID(alice["user"]["id"]),
        encrypted_payload="cipher",
    )
    db_session.add(msg)
    await db_session.flush()

    resp = await client.post(
        f"/api/v1/messages/{msg.id}/opened",
        headers=auth_header(bob["tokens"]["access_token"]),
    )
    assert resp.status_code == 409
    await db_session.refresh(msg)
    assert msg.encrypted_payload == "cipher"
