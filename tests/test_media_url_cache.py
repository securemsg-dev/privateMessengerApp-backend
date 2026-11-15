from __future__ import annotations
"""
tests/test_media_url_cache.py
──────────────────────────────
Presigned download URLs are served from Redis while fresh, and dropped the
moment the blob is deleted.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import UUID

import pytest

from app.db.models.media_blob import MediaBlob
from app.db.models.message import MessageMetadata
from tests.conftest import (
    auth_header,
    create_conversation_with_participants,
    register_and_login,
)

pytestmark = pytest.mark.asyncio


class FakeBucket:
    """Stands in for S3Storage: counts how often a URL is signed."""

    supports_direct = True

    def __init__(self, ttl_seconds: int = 3600):
        self.ttl_seconds = ttl_seconds
        self.signed = 0
        self.deleted: list[UUID] = []

    async def presign_download(self, blob_id: UUID):
        self.signed += 1
        expires = datetime.now(timezone.utc) + timedelta(seconds=self.ttl_seconds)
        return f"https://bucket.example/{blob_id}?sig={self.signed}", expires

    async def delete_bytes(self, blob_id: UUID) -> None:
        self.deleted.append(blob_id)


async def _blob(db, owner_id: UUID) -> MediaBlob:
    blob = MediaBlob(
        owner_id=owner_id, size_bytes=3, mime="image/jpeg",
        uploaded_at=datetime.now(timezone.utc), storage="s3",
    )
    db.add(blob)
    await db.flush()
    return blob


async def _urls(client, token: str, blob_id: UUID) -> dict:
    resp = await client.post(
        "/api/v1/media/download-urls",
        json={"blob_ids": [str(blob_id)]},
        headers=auth_header(token),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["urls"]


async def test_download_url_is_signed_once_and_reused(client, db_session):
    alice = await register_and_login(client)
    bob = await register_and_login(client)
    blob = await _blob(db_session, UUID(alice["user"]["id"]))
    bucket = FakeBucket()

    with patch("app.api.v1.endpoints.media.storage_for", return_value=bucket):
        first = await _urls(client, alice["tokens"]["access_token"], blob.id)
        # A different user asking for the same blob gets the same URL.
        second = await _urls(client, bob["tokens"]["access_token"], blob.id)

    assert bucket.signed == 1
    assert first[str(blob.id)]["url"] == second[str(blob.id)]["url"]
    assert second[str(blob.id)]["auth"] is False


async def test_nearly_expired_urls_are_not_cached(client, db_session):
    alice = await register_and_login(client)
    blob = await _blob(db_session, UUID(alice["user"]["id"]))
    bucket = FakeBucket(ttl_seconds=120)  # shorter than the safety margin

    with patch("app.api.v1.endpoints.media.storage_for", return_value=bucket):
        await _urls(client, alice["tokens"]["access_token"], blob.id)
        await _urls(client, alice["tokens"]["access_token"], blob.id)

    assert bucket.signed == 2


async def test_deleting_the_message_drops_the_cached_url(client, db_session):
    alice = await register_and_login(client)
    bob = await register_and_login(client)
    alice_id = UUID(alice["user"]["id"])
    conv = await create_conversation_with_participants(
        db_session, [alice_id, UUID(bob["user"]["id"])],
    )
    blob = await _blob(db_session, alice_id)
    blob_id = blob.id
    msg = MessageMetadata(
        conversation_id=conv.id, sender_id=alice_id,
        encrypted_payload="cipher", media_blob_id=blob_id,
    )
    db_session.add(msg)
    await db_session.flush()
    bucket = FakeBucket()

    with patch("app.api.v1.endpoints.media.storage_for", return_value=bucket), \
         patch("app.api.v1.endpoints.messages.storage_for", return_value=bucket):
        assert str(blob_id) in await _urls(client, alice["tokens"]["access_token"], blob_id)

        resp = await client.post(
            f"/api/v1/messages/{msg.id}/delete",
            json={"scope": "everyone"},
            headers=auth_header(alice["tokens"]["access_token"]),
        )
        assert resp.status_code == 204, resp.text
        assert bucket.deleted == [blob_id]

        # Blob row is gone and the cache entry with it → nothing to hand out.
        assert await _urls(client, bob["tokens"]["access_token"], blob_id) == {}

    assert bucket.signed == 1
