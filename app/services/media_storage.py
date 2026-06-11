from __future__ import annotations
"""
app/services/media_storage.py
──────────────────────────────
Abstract storage backend for E2EE-encrypted media blobs (Phase D).

Two concrete implementations:

  • LocalFileStorage — writes to MEDIA_LOCAL_PATH on the server's filesystem
    and serves via the FastAPI app's GET /media/{id} endpoint. Default for
    development; works with no external infra.

  • S3Storage — stub for production. Uploads to AWS S3 and issues pre-signed
    PUT/GET URLs so the bytes never flow through this app. Method bodies are
    placeholders — paste real boto3 code (or any S3-compatible SDK) later.

Pick the active backend with the MEDIA_STORAGE_BACKEND env var (`local` or
`s3`). All callers go through `get_storage()` so swapping is a one-line
config change.

The blobs themselves are CIPHERTEXT — the server never holds keys. This
class deliberately does not know or care about the contents.
"""

import asyncio
import logging
import os
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from uuid import UUID

from app.core.config import settings

logger = logging.getLogger(__name__)


class StorageBackend(ABC):
    """All storage backends implement these four operations."""

    @abstractmethod
    async def issue_upload_url(self, blob_id: UUID) -> tuple[str, datetime]:
        """
        Return `(upload_url, expires_at)`. The client PUTs raw ciphertext
        bytes to the URL with `Content-Type: application/octet-stream`.
        """

    @abstractmethod
    async def issue_download_url(self, blob_id: UUID) -> str:
        """Return a short-lived URL the client can GET to fetch ciphertext."""

    @abstractmethod
    async def write_bytes(self, blob_id: UUID, data: bytes) -> None:
        """
        Persist ciphertext for a blob. For `local`, this is the actual write;
        for `s3` this is a no-op because the client uploads directly to S3.
        """

    @abstractmethod
    async def read_bytes(self, blob_id: UUID) -> Optional[bytes]:
        """Return ciphertext, or None if the blob hasn't been uploaded yet."""

    @abstractmethod
    async def delete_bytes(self, blob_id: UUID) -> None:
        """Remove stored ciphertext for a blob. No-op if nothing was written."""


# ── Local filesystem (default — works out of the box) ────────────────────────

class LocalFileStorage(StorageBackend):
    """
    Stores blobs under MEDIA_LOCAL_PATH/<blob_id>.bin. Upload + download URLs
    point at our own FastAPI app so the client uses the same auth and base
    URL as every other request.
    """

    def __init__(self, base_path: str, ttl_seconds: int):
        self.base_path = Path(base_path).resolve()
        self.base_path.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = ttl_seconds
        logger.info("LocalFileStorage rooted at %s", self.base_path)

    def _path(self, blob_id: UUID) -> Path:
        return self.base_path / f"{blob_id}.bin"

    async def issue_upload_url(self, blob_id: UUID) -> tuple[str, datetime]:
        # The client uploads via PUT /api/v1/media/{blob_id}; the URL is
        # built by the endpoint layer from the active request, so here we
        # just return a relative path and let the caller prefix it.
        expires = datetime.now(timezone.utc) + timedelta(seconds=self.ttl_seconds)
        return (f"/api/v1/media/{blob_id}", expires)

    async def issue_download_url(self, blob_id: UUID) -> str:
        return f"/api/v1/media/{blob_id}"

    async def write_bytes(self, blob_id: UUID, data: bytes) -> None:
        path = self._path(blob_id)

        def _write() -> None:
            # Atomic write: tmp file + rename
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)

        # Keep blocking file I/O off the event loop.
        await asyncio.to_thread(_write)

    async def read_bytes(self, blob_id: UUID) -> Optional[bytes]:
        path = self._path(blob_id)

        def _read() -> Optional[bytes]:
            if not path.exists():
                return None
            return path.read_bytes()

        return await asyncio.to_thread(_read)

    async def delete_bytes(self, blob_id: UUID) -> None:
        path = self._path(blob_id)

        def _delete() -> None:
            path.unlink(missing_ok=True)
            path.with_suffix(".tmp").unlink(missing_ok=True)

        await asyncio.to_thread(_delete)


# ── S3 (stub — fill in when bucket + creds are ready) ────────────────────────

class S3Storage(StorageBackend):
    """
    Production storage backend backed by AWS S3 (or any S3-compatible API).
    Method bodies are intentionally TODO — drop in boto3 calls once the
    bucket + IAM credentials exist. The interface is what matters here:
    swapping LocalFileStorage → S3Storage requires zero changes outside
    this file.
    """

    def __init__(
        self,
        bucket: str,
        region: str,
        access_key: str,
        secret_key: str,
        presign_ttl_seconds: int,
    ):
        self.bucket = bucket
        self.region = region
        self.access_key = access_key
        self.secret_key = secret_key
        self.presign_ttl_seconds = presign_ttl_seconds
        # TODO: initialise boto3 client here
        # import boto3
        # self._s3 = boto3.client(
        #     "s3",
        #     region_name=region,
        #     aws_access_key_id=access_key,
        #     aws_secret_access_key=secret_key,
        # )
        logger.warning(
            "S3Storage initialised in stub mode — calls will raise. "
            "Swap in boto3 implementation when AWS credentials are available."
        )

    async def issue_upload_url(self, blob_id: UUID) -> tuple[str, datetime]:
        # TODO: replace with self._s3.generate_presigned_url('put_object', ...)
        # return that URL + (now + presign_ttl_seconds)
        raise NotImplementedError(
            "S3Storage.issue_upload_url — paste boto3 generate_presigned_url here"
        )

    async def issue_download_url(self, blob_id: UUID) -> str:
        # TODO: self._s3.generate_presigned_url('get_object', ...)
        raise NotImplementedError(
            "S3Storage.issue_download_url — paste boto3 generate_presigned_url here"
        )

    async def write_bytes(self, blob_id: UUID, data: bytes) -> None:
        # No-op for S3: the client PUTs directly to the pre-signed URL.
        # We don't proxy bytes through the app server.
        return None

    async def read_bytes(self, blob_id: UUID) -> Optional[bytes]:
        # TODO: self._s3.get_object(...)['Body'].read()
        raise NotImplementedError(
            "S3Storage.read_bytes — paste boto3 get_object here"
        )

    async def delete_bytes(self, blob_id: UUID) -> None:
        # TODO: self._s3.delete_object(Bucket=self.bucket, Key=...)
        raise NotImplementedError(
            "S3Storage.delete_bytes — paste boto3 delete_object here"
        )


# ── Factory ──────────────────────────────────────────────────────────────────

_storage: Optional[StorageBackend] = None


def get_storage() -> StorageBackend:
    """Lazily build the configured backend on first call; cache the instance."""
    global _storage
    if _storage is not None:
        return _storage

    if settings.MEDIA_STORAGE_BACKEND == "s3":
        # S3Storage is still a stub — fail loudly at startup rather than on
        # the first media request in production. Remove this guard once the
        # boto3 methods below are implemented.
        raise RuntimeError(
            "MEDIA_STORAGE_BACKEND=s3 is not supported yet: S3Storage is a "
            "stub (methods raise NotImplementedError). Use MEDIA_STORAGE_BACKEND=local "
            "with a persistent volume, or implement S3Storage first."
        )
    else:
        _storage = LocalFileStorage(
            base_path=settings.MEDIA_LOCAL_PATH,
            ttl_seconds=settings.MEDIA_UPLOAD_URL_TTL_SECONDS,
        )
    return _storage
