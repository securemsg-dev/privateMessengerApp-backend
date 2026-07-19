from __future__ import annotations
"""
tests/test_maintenance.py
──────────────────────────
Tests for the periodic housekeeping sweep (app/services/maintenance.py):
abandoned media blob cleanup + expired session cleanup.
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.core.security import hash_password
from app.db.models.call import Call
from app.db.models.media_blob import MediaBlob
from app.db.models.message import MessageMetadata
from app.db.models.session import Session
from app.db.models.user import User
from app.services.maintenance import sweep_once
from app.services.media_storage import LocalFileStorage
from tests.conftest import TestSessionLocal, create_conversation_with_participants


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _make_user(identifier: str) -> uuid.UUID:
    dummy = hash_password("Placeholder123")
    async with TestSessionLocal() as session:
        user = User(
            private_number=identifier,
            login_password_hash=dummy,
            delete_password_hash=dummy,
        )
        session.add(user)
        await session.flush()
        user_id = user.id
        await session.commit()
    return user_id


@pytest.mark.asyncio
async def test_sweep_removes_abandoned_blobs_and_expired_sessions(tmp_path, db_session):
    user_id = await _make_user("9100000001")
    storage = LocalFileStorage(base_path=str(tmp_path), ttl_seconds=600)

    async with TestSessionLocal() as session:
        # Abandoned: reserved 2 days ago, never uploaded
        abandoned = MediaBlob(
            owner_id=user_id, size_bytes=10, mime="image/jpeg", uploaded_at=None,
        )
        # Fresh reservation: pending but recent — must survive
        pending = MediaBlob(
            owner_id=user_id, size_bytes=10, mime="image/jpeg", uploaded_at=None,
        )
        # Completed upload — young enough to survive the orphan pass too
        uploaded = MediaBlob(
            owner_id=user_id, size_bytes=10, mime="image/jpeg", uploaded_at=_now(),
        )
        session.add_all([abandoned, pending, uploaded])
        await session.flush()
        abandoned.created_at = _now() - timedelta(days=2)
        uploaded.created_at = _now() - timedelta(days=20)

        # One expired session, one live one
        expired_sess = Session(
            user_id=user_id,
            refresh_token_hash="x" * 32,
            expires_at=_now() - timedelta(days=1),
        )
        live_sess = Session(
            user_id=user_id,
            refresh_token_hash="y" * 32,
            expires_at=_now() + timedelta(days=1),
        )
        session.add_all([expired_sess, live_sess])
        await session.commit()

        abandoned_id = abandoned.id
        pending_id = pending.id
        uploaded_id = uploaded.id
        live_sess_id = live_sess.id

    # Give the abandoned blob a stray ciphertext file to confirm file removal
    await storage.write_bytes(abandoned_id, b"orphaned-bytes")
    assert await storage.read_bytes(abandoned_id) is not None

    with patch("app.services.maintenance.AsyncSessionLocal", TestSessionLocal), \
         patch("app.services.maintenance.get_storage", return_value=storage):
        blobs_removed, sessions_removed, _, orphans_removed = await sweep_once()

    assert blobs_removed == 1
    assert sessions_removed == 1
    assert orphans_removed == 0
    assert await storage.read_bytes(abandoned_id) is None

    async with TestSessionLocal() as session:
        assert await session.get(MediaBlob, abandoned_id) is None
        assert await session.get(MediaBlob, pending_id) is not None
        assert await session.get(MediaBlob, uploaded_id) is not None
        assert await session.get(Session, live_sess_id) is not None


@pytest.mark.asyncio
async def test_sweep_closes_stale_calls(tmp_path, db_session):
    caller_id = await _make_user("9100000002")
    callee_id = await _make_user("9100000003")
    storage = LocalFileStorage(base_path=str(tmp_path), ttl_seconds=600)

    async with TestSessionLocal() as session:
        # Stale, never answered → missed
        stale_unanswered = Call(
            caller_id=caller_id, callee_id=callee_id,
            started_at=_now() - timedelta(hours=3),
        )
        # Stale, was connected but never hung up → completed
        stale_connected = Call(
            caller_id=caller_id, callee_id=callee_id,
            started_at=_now() - timedelta(hours=3),
            accepted_at=_now() - timedelta(hours=3),
        )
        # Fresh ringing call — must be left alone
        ringing = Call(
            caller_id=caller_id, callee_id=callee_id,
            started_at=_now(),
        )
        session.add_all([stale_unanswered, stale_connected, ringing])
        await session.commit()
        unanswered_id = stale_unanswered.id
        connected_id = stale_connected.id
        ringing_id = ringing.id

    with patch("app.services.maintenance.AsyncSessionLocal", TestSessionLocal), \
         patch("app.services.maintenance.get_storage", return_value=storage):
        _, _, calls_closed, _ = await sweep_once()

    assert calls_closed == 2
    async with TestSessionLocal() as session:
        unanswered = await session.get(Call, unanswered_id)
        assert unanswered.end_reason == "missed"
        assert unanswered.ended_at is not None

        connected = await session.get(Call, connected_id)
        assert connected.end_reason == "completed"
        assert connected.ended_at is not None

        still_ringing = await session.get(Call, ringing_id)
        assert still_ringing.ended_at is None
        assert still_ringing.end_reason is None


@pytest.mark.asyncio
async def test_sweep_removes_orphaned_uploaded_blobs(tmp_path, db_session):
    """Uploaded blobs past the 30-day grace with no message and no avatar
    reference are removed (file + row); referenced ones survive."""
    user_id = await _make_user("9100000004")
    peer_id = await _make_user("9100000005")
    storage = LocalFileStorage(base_path=str(tmp_path), ttl_seconds=600)
    old = _now() - timedelta(days=31)

    async with TestSessionLocal() as session:
        orphan = MediaBlob(
            owner_id=user_id, size_bytes=10, mime="image/jpeg", uploaded_at=old,
        )
        referenced = MediaBlob(
            owner_id=user_id, size_bytes=10, mime="image/jpeg", uploaded_at=old,
        )
        avatar = MediaBlob(
            owner_id=user_id, size_bytes=10, mime="image/jpeg", uploaded_at=old,
        )
        session.add_all([orphan, referenced, avatar])
        await session.flush()
        for blob in (orphan, referenced, avatar):
            blob.created_at = old

        conv = await create_conversation_with_participants(session, [user_id, peer_id])
        session.add(MessageMetadata(
            conversation_id=conv.id,
            sender_id=user_id,
            encrypted_payload="cipher",
            media_blob_id=referenced.id,
        ))
        user = await session.get(User, user_id)
        user.profile_picture_key = str(avatar.id)
        await session.commit()
        orphan_id, referenced_id, avatar_id = orphan.id, referenced.id, avatar.id

    await storage.write_bytes(orphan_id, b"orphan-bytes")

    with patch("app.services.maintenance.AsyncSessionLocal", TestSessionLocal), \
         patch("app.services.maintenance.get_storage", return_value=storage):
        _, _, _, orphans_removed = await sweep_once()

    assert orphans_removed == 1
    assert await storage.read_bytes(orphan_id) is None
    async with TestSessionLocal() as session:
        assert await session.get(MediaBlob, orphan_id) is None
        assert await session.get(MediaBlob, referenced_id) is not None
        assert await session.get(MediaBlob, avatar_id) is not None
