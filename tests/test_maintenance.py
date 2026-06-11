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
from app.db.models.media_blob import MediaBlob
from app.db.models.session import Session
from app.db.models.user import User
from app.services.maintenance import sweep_once
from app.services.media_storage import LocalFileStorage
from tests.conftest import TestSessionLocal


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
        # Completed upload — must survive regardless of age
        uploaded = MediaBlob(
            owner_id=user_id, size_bytes=10, mime="image/jpeg", uploaded_at=_now(),
        )
        session.add_all([abandoned, pending, uploaded])
        await session.flush()
        abandoned.created_at = _now() - timedelta(days=2)
        uploaded.created_at = _now() - timedelta(days=30)

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
        blobs_removed, sessions_removed = await sweep_once()

    assert blobs_removed == 1
    assert sessions_removed == 1
    assert await storage.read_bytes(abandoned_id) is None

    async with TestSessionLocal() as session:
        assert await session.get(MediaBlob, abandoned_id) is None
        assert await session.get(MediaBlob, pending_id) is not None
        assert await session.get(MediaBlob, uploaded_id) is not None
        assert await session.get(Session, live_sess_id) is not None
