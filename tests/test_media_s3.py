"""
S3 media backend + direct (presigned) upload flow.

moto provides an in-process S3; the app's storage factory is patched so the
media endpoints see an S3 default backend plus a local backend for legacy
blobs — the production layout after switching MEDIA_STORAGE_BACKEND=s3.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
from uuid import UUID

import boto3
import pytest
from moto import mock_aws

from app.db.models.media_blob import MediaBlob
from app.services.media_storage import LocalFileStorage, S3Storage
from tests.conftest import auth_header, register_and_login

BUCKET = "cricchat-test-media"
MEDIA = "app.api.v1.endpoints.media"


@pytest.fixture
def storages(tmp_path):
    """(s3_backend, local_backend, raw boto3 client) inside a moto sandbox."""
    with mock_aws():
        raw = boto3.client("s3", region_name="us-east-1")
        raw.create_bucket(Bucket=BUCKET)
        s3 = S3Storage(
            bucket=BUCKET,
            region="us-east-1",
            access_key="test",
            secret_key="test",
        )
        local = LocalFileStorage(base_path=str(tmp_path))
        yield s3, local, raw


@contextmanager
def media_backends(s3: S3Storage, local: LocalFileStorage, default: str = "s3"):
    by_name = {"s3": s3, "local": local}
    with patch(f"{MEDIA}.default_storage", return_value=by_name[default]), \
         patch(f"{MEDIA}.storage_for", side_effect=lambda blob: by_name[blob.storage]):
        yield


async def _reserve(client, token: str, size: int, direct: bool | None):
    body: dict = {"size_bytes": size, "mime": "image/jpeg"}
    if direct is not None:
        body["direct"] = direct
    resp = await client.post("/api/v1/media/upload-url", json=body, headers=auth_header(token))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_direct_upload_url_is_presigned_and_size_locked(client, db_session, storages):
    s3, local, _ = storages
    alice = await register_and_login(client)
    with media_backends(s3, local):
        data = await _reserve(client, alice["tokens"]["access_token"], 1234, direct=True)

    assert data["upload_method"] == "direct"
    assert data["upload_headers"] == {"Content-Type": "application/octet-stream"}
    # The bucket URL is presigned with the exact length + type signed in, so
    # the client can't PUT a bigger object than it reserved.
    qs = parse_qs(urlparse(data["upload_url"]).query)
    signed = qs["X-Amz-SignedHeaders"][0].split(";")
    assert "content-length" in signed
    assert "content-type" in signed
    assert BUCKET in data["upload_url"]
    # download_url stays the app path — it is embedded in E2EE envelopes that
    # old builds fetch with a Bearer token.
    assert data["download_url"].endswith(f"/api/v1/media/{data['blob_id']}")

    blob = await db_session.get(MediaBlob, UUID(data["blob_id"]))
    assert blob.storage == "s3"
    assert blob.uploaded_at is None


async def test_old_client_upload_is_proxied_into_bucket(client, db_session, storages):
    """A build that never sends `direct` keeps PUTting through the app."""
    s3, local, raw = storages
    alice = await register_and_login(client)
    token = alice["tokens"]["access_token"]
    with media_backends(s3, local):
        data = await _reserve(client, token, 5, direct=None)
        assert data["upload_method"] == "api"
        assert data["upload_url"].endswith(f"/api/v1/media/{data['blob_id']}")

        put = await client.put(
            f"/api/v1/media/{data['blob_id']}",
            content=b"hello",
            headers={**auth_header(token), "Content-Type": "application/octet-stream"},
        )
        assert put.status_code == 204, put.text

    obj = raw.get_object(Bucket=BUCKET, Key=f"media/{data['blob_id']}.bin")
    assert obj["Body"].read() == b"hello"
    blob = await db_session.get(MediaBlob, UUID(data["blob_id"]))
    assert blob.uploaded_at is not None


async def test_complete_marks_uploaded_only_when_size_matches(client, db_session, storages):
    s3, local, raw = storages
    alice = await register_and_login(client)
    token = alice["tokens"]["access_token"]
    with media_backends(s3, local):
        data = await _reserve(client, token, 4, direct=True)
        blob_id = data["blob_id"]
        key = f"media/{blob_id}.bin"

        # Nothing uploaded yet.
        resp = await client.post(f"/api/v1/media/{blob_id}/complete", headers=auth_header(token))
        assert resp.status_code == 409

        # Wrong size → rejected and the object is removed.
        raw.put_object(Bucket=BUCKET, Key=key, Body=b"too long")
        resp = await client.post(f"/api/v1/media/{blob_id}/complete", headers=auth_header(token))
        assert resp.status_code == 400
        assert raw.list_objects_v2(Bucket=BUCKET).get("KeyCount", 0) == 0

        # Exact size → accepted (and idempotent).
        raw.put_object(Bucket=BUCKET, Key=key, Body=b"abcd")
        resp = await client.post(f"/api/v1/media/{blob_id}/complete", headers=auth_header(token))
        assert resp.status_code == 204
        resp = await client.post(f"/api/v1/media/{blob_id}/complete", headers=auth_header(token))
        assert resp.status_code == 204

    blob = await db_session.get(MediaBlob, UUID(blob_id))
    assert blob.uploaded_at is not None


async def test_complete_requires_owner(client, storages):
    s3, local, raw = storages
    alice = await register_and_login(client)
    mallory = await register_and_login(client)
    with media_backends(s3, local):
        data = await _reserve(client, alice["tokens"]["access_token"], 3, direct=True)
        raw.put_object(Bucket=BUCKET, Key=f"media/{data['blob_id']}.bin", Body=b"abc")
        resp = await client.post(
            f"/api/v1/media/{data['blob_id']}/complete",
            headers=auth_header(mallory["tokens"]["access_token"]),
        )
    assert resp.status_code == 403


async def test_download_urls_route_by_blob_storage(client, db_session, storages):
    s3, local, raw = storages
    alice = await register_and_login(client)
    owner = UUID(alice["user"]["id"])
    now = datetime.now(timezone.utc)

    bucket_blob = MediaBlob(owner_id=owner, size_bytes=3, mime="image/jpeg", uploaded_at=now, storage="s3")
    volume_blob = MediaBlob(owner_id=owner, size_bytes=3, mime="image/jpeg", uploaded_at=now, storage="local")
    pending_blob = MediaBlob(owner_id=owner, size_bytes=3, mime="image/jpeg", uploaded_at=None, storage="s3")
    db_session.add_all([bucket_blob, volume_blob, pending_blob])
    await db_session.flush()

    with media_backends(s3, local):
        resp = await client.post(
            "/api/v1/media/download-urls",
            json={"blob_ids": [str(bucket_blob.id), str(volume_blob.id), str(pending_blob.id)]},
            headers=auth_header(alice["tokens"]["access_token"]),
        )
    assert resp.status_code == 200, resp.text
    urls = resp.json()["urls"]

    assert set(urls) == {str(bucket_blob.id), str(volume_blob.id)}  # pending omitted
    b = urls[str(bucket_blob.id)]
    assert b["auth"] is False and "X-Amz-Signature" in b["url"] and b["expires_at"]
    v = urls[str(volume_blob.id)]
    assert v["auth"] is True and v["url"].endswith(f"/api/v1/media/{volume_blob.id}")


async def test_download_urls_validates_batch_size(client):
    alice = await register_and_login(client)
    resp = await client.post(
        "/api/v1/media/download-urls",
        json={"blob_ids": []},
        headers=auth_header(alice["tokens"]["access_token"]),
    )
    assert resp.status_code == 422


async def test_legacy_get_streams_bucket_blob(client, db_session, storages):
    """Old builds fetch every blob via GET /media/{id} — including bucket ones."""
    s3, local, raw = storages
    alice = await register_and_login(client)
    blob = MediaBlob(
        owner_id=UUID(alice["user"]["id"]), size_bytes=6, mime="audio/aac",
        uploaded_at=datetime.now(timezone.utc), storage="s3",
    )
    db_session.add(blob)
    await db_session.flush()
    raw.put_object(Bucket=BUCKET, Key=f"media/{blob.id}.bin", Body=b"cipher")

    with media_backends(s3, local):
        resp = await client.get(
            f"/api/v1/media/{blob.id}",
            headers=auth_header(alice["tokens"]["access_token"]),
        )
    assert resp.status_code == 200
    assert resp.content == b"cipher"
    assert resp.headers["content-type"] == "application/octet-stream"


async def test_local_default_ignores_direct_request(client, db_session, storages):
    """With the volume as default backend, `direct` falls back to the app path."""
    s3, local, _ = storages
    alice = await register_and_login(client)
    with media_backends(s3, local, default="local"):
        data = await _reserve(client, alice["tokens"]["access_token"], 10, direct=True)
    assert data["upload_method"] == "api"
    blob = await db_session.get(MediaBlob, UUID(data["blob_id"]))
    assert blob.storage == "local"


async def test_s3_delete_is_idempotent(storages):
    s3, _, raw = storages
    blob_id = UUID("00000000-0000-0000-0000-0000000000aa")
    await s3.write_bytes(blob_id, b"x")
    assert await s3.object_size(blob_id) == 1
    await s3.delete_bytes(blob_id)
    await s3.delete_bytes(blob_id)  # second delete: no error
    assert await s3.object_size(blob_id) is None
    assert await s3.read_bytes(blob_id) is None
