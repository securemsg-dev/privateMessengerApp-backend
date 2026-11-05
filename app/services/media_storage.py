from __future__ import annotations
"""
app/services/media_storage.py
──────────────────────────────
Storage backends for E2EE-encrypted media blobs (Phase D).

  • LocalFileStorage — files under MEDIA_LOCAL_PATH (a Railway Volume in
    production), served through the app's GET /media/{id}.

  • S3Storage — any S3-compatible bucket (Railway Buckets in production).
    New clients upload/download DIRECTLY with short-lived presigned URLs, so
    media bytes never pass through the app server. Old app builds still go
    through PUT/GET /media/{id}; for S3 blobs the app proxies those.

Each MediaBlob row records which backend holds its bytes (`storage` column),
fixed at creation. Callers resolve the backend per blob with
`storage_for(blob)`; new blobs go to `default_storage()`. Both backends stay
live, so switching MEDIA_STORAGE_BACKEND never strands existing media.

The blobs themselves are CIPHERTEXT — the server never holds keys.
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AsyncIterator, Optional
from uuid import UUID

from app.core.config import settings

logger = logging.getLogger(__name__)

OCTET_STREAM = "application/octet-stream"
STREAM_CHUNK_BYTES = 256 * 1024


def api_media_path(blob_id: UUID) -> str:
    """App-relative URL of a blob's PUT/GET endpoint (stable forever: it is
    embedded inside E2EE envelopes that old app builds fetch)."""
    return f"/api/v1/media/{blob_id}"


class StorageBackend(ABC):
    """Operations every backend supports."""

    #: Value stored in MediaBlob.storage.
    name: str = ""
    #: True when clients can PUT/GET the bucket directly via presigned URLs.
    supports_direct: bool = False

    @abstractmethod
    async def write_bytes(self, blob_id: UUID, data: bytes) -> None:
        """Persist ciphertext received through the app (legacy PUT path)."""

    @abstractmethod
    async def read_bytes(self, blob_id: UUID) -> Optional[bytes]:
        """Return ciphertext, or None if nothing is stored."""

    @abstractmethod
    async def delete_bytes(self, blob_id: UUID) -> None:
        """Remove stored ciphertext. No-op if nothing was written."""

    def local_path(self, blob_id: UUID) -> Optional[Path]:
        """Filesystem path for streaming with FileResponse, or None."""
        return None

    async def open_stream(self, blob_id: UUID) -> Optional[AsyncIterator[bytes]]:
        """Chunked reader for proxying a blob to a client, or None if missing."""
        data = await self.read_bytes(blob_id)
        if data is None:
            return None

        async def _one() -> AsyncIterator[bytes]:
            yield data

        return _one()

    # ── Direct (presigned) access — S3 only ──────────────────────────────

    async def presign_upload(
        self, blob_id: UUID, size_bytes: int,
    ) -> tuple[str, dict[str, str], datetime]:
        """Return (url, headers_client_must_send, expires_at)."""
        raise NotImplementedError

    async def presign_download(self, blob_id: UUID) -> tuple[str, datetime]:
        """Return (url, expires_at)."""
        raise NotImplementedError

    async def object_size(self, blob_id: UUID) -> Optional[int]:
        """Size of the stored object, or None if it doesn't exist."""
        raise NotImplementedError


# ── Local filesystem ──────────────────────────────────────────────────────────

class LocalFileStorage(StorageBackend):
    """Stores blobs under MEDIA_LOCAL_PATH/<blob_id>.bin."""

    name = "local"

    def __init__(self, base_path: str):
        self.base_path = Path(base_path).resolve()
        self.base_path.mkdir(parents=True, exist_ok=True)
        logger.info("LocalFileStorage rooted at %s", self.base_path)

    def _path(self, blob_id: UUID) -> Path:
        return self.base_path / f"{blob_id}.bin"

    def local_path(self, blob_id: UUID) -> Optional[Path]:
        path = self._path(blob_id)
        return path if path.exists() else None

    async def write_bytes(self, blob_id: UUID, data: bytes) -> None:
        path = self._path(blob_id)

        def _write() -> None:
            # Atomic write: tmp file + rename
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)

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


# ── S3-compatible bucket ──────────────────────────────────────────────────────

class S3Storage(StorageBackend):
    """
    Any S3-compatible API via boto3 (sync client; network calls run in a
    thread). Presigning is a local computation — no network round trip.
    """

    name = "s3"
    supports_direct = True

    def __init__(
        self,
        *,
        bucket: str,
        region: str,
        access_key: str,
        secret_key: str,
        endpoint_url: Optional[str] = None,
        url_style: str = "virtual",
        upload_ttl_seconds: int = 600,
        download_ttl_seconds: int = 3600,
        key_prefix: str = "media/",
    ):
        import boto3
        from botocore.config import Config

        self.bucket = bucket
        self.key_prefix = key_prefix
        self.upload_ttl_seconds = upload_ttl_seconds
        self.download_ttl_seconds = download_ttl_seconds
        self._s3 = boto3.client(
            "s3",
            region_name=region or None,
            endpoint_url=endpoint_url or None,
            aws_access_key_id=access_key or None,
            aws_secret_access_key=secret_key or None,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path" if url_style == "path" else "virtual"},
                # botocore ≥1.36 adds CRC checksums to every request by
                # default, which many S3-compatible stores reject. Only send
                # them when an operation actually requires one.
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )
        logger.info(
            "S3Storage bucket=%s endpoint=%s style=%s",
            bucket, endpoint_url or "aws", url_style,
        )

    def _key(self, blob_id: UUID) -> str:
        return f"{self.key_prefix}{blob_id}.bin"

    @staticmethod
    def _is_missing(err: Exception) -> bool:
        from botocore.exceptions import ClientError

        if not isinstance(err, ClientError):
            return False
        code = str(err.response.get("Error", {}).get("Code", ""))
        return code in {"404", "NoSuchKey", "NotFound"}

    async def presign_upload(
        self, blob_id: UUID, size_bytes: int,
    ) -> tuple[str, dict[str, str], datetime]:
        # ContentType AND ContentLength are signed into the URL, so the client
        # can only PUT exactly the reserved number of bytes (without it, a
        # presigned PUT accepts objects up to 5 GB).
        url = self._s3.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": self.bucket,
                "Key": self._key(blob_id),
                "ContentType": OCTET_STREAM,
                "ContentLength": size_bytes,
            },
            ExpiresIn=self.upload_ttl_seconds,
            HttpMethod="PUT",
        )
        expires = datetime.now(timezone.utc) + timedelta(seconds=self.upload_ttl_seconds)
        return url, {"Content-Type": OCTET_STREAM}, expires

    async def presign_download(self, blob_id: UUID) -> tuple[str, datetime]:
        url = self._s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": self._key(blob_id)},
            ExpiresIn=self.download_ttl_seconds,
            HttpMethod="GET",
        )
        expires = datetime.now(timezone.utc) + timedelta(seconds=self.download_ttl_seconds)
        return url, expires

    async def object_size(self, blob_id: UUID) -> Optional[int]:
        def _head() -> Optional[int]:
            try:
                resp = self._s3.head_object(Bucket=self.bucket, Key=self._key(blob_id))
            except Exception as err:  # noqa: BLE001 — classified below
                if self._is_missing(err):
                    return None
                raise
            return int(resp["ContentLength"])

        return await asyncio.to_thread(_head)

    async def write_bytes(self, blob_id: UUID, data: bytes) -> None:
        await asyncio.to_thread(
            self._s3.put_object,
            Bucket=self.bucket,
            Key=self._key(blob_id),
            Body=data,
            ContentType=OCTET_STREAM,
        )

    async def read_bytes(self, blob_id: UUID) -> Optional[bytes]:
        def _get() -> Optional[bytes]:
            try:
                resp = self._s3.get_object(Bucket=self.bucket, Key=self._key(blob_id))
            except Exception as err:  # noqa: BLE001
                if self._is_missing(err):
                    return None
                raise
            return resp["Body"].read()

        return await asyncio.to_thread(_get)

    async def open_stream(self, blob_id: UUID) -> Optional[AsyncIterator[bytes]]:
        from starlette.concurrency import iterate_in_threadpool

        def _get():
            try:
                return self._s3.get_object(Bucket=self.bucket, Key=self._key(blob_id))
            except Exception as err:  # noqa: BLE001
                if self._is_missing(err):
                    return None
                raise

        resp = await asyncio.to_thread(_get)
        if resp is None:
            return None
        body = resp["Body"]

        async def _chunks() -> AsyncIterator[bytes]:
            try:
                async for chunk in iterate_in_threadpool(
                    body.iter_chunks(chunk_size=STREAM_CHUNK_BYTES)
                ):
                    yield chunk
            finally:
                body.close()

        return _chunks()

    async def delete_bytes(self, blob_id: UUID) -> None:
        def _delete() -> None:
            try:
                self._s3.delete_object(Bucket=self.bucket, Key=self._key(blob_id))
            except Exception as err:  # noqa: BLE001
                if not self._is_missing(err):
                    raise

        await asyncio.to_thread(_delete)


# ── Factory ──────────────────────────────────────────────────────────────────

_backends: dict[str, StorageBackend] = {}


def s3_configured() -> bool:
    return bool(
        settings.AWS_S3_BUCKET_NAME
        and settings.AWS_ACCESS_KEY_ID
        and settings.AWS_SECRET_ACCESS_KEY
    )


def get_backend(name: str) -> StorageBackend:
    """Backend by name ('local' | 's3'); built once and cached."""
    backend = _backends.get(name)
    if backend is not None:
        return backend

    if name == "s3":
        if not s3_configured():
            raise RuntimeError(
                "S3 media storage requested but AWS_S3_BUCKET_NAME / "
                "AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY are not all set."
            )
        backend = S3Storage(
            bucket=settings.AWS_S3_BUCKET_NAME,
            region=settings.s3_region,
            access_key=settings.AWS_ACCESS_KEY_ID,
            secret_key=settings.AWS_SECRET_ACCESS_KEY,
            endpoint_url=settings.AWS_ENDPOINT_URL or None,
            url_style=settings.AWS_S3_URL_STYLE,
            upload_ttl_seconds=settings.MEDIA_UPLOAD_URL_TTL_SECONDS,
            download_ttl_seconds=settings.AWS_S3_PRESIGN_EXPIRY_SECONDS,
        )
    elif name == "local":
        backend = LocalFileStorage(base_path=settings.MEDIA_LOCAL_PATH)
    else:
        raise RuntimeError(f"Unknown media storage backend {name!r}")

    _backends[name] = backend
    return backend


def default_storage() -> StorageBackend:
    """Backend that new blobs are written to (MEDIA_STORAGE_BACKEND)."""
    return get_backend(settings.MEDIA_STORAGE_BACKEND)


def storage_for(blob) -> StorageBackend:
    """Backend holding an existing blob's bytes (its `storage` column)."""
    return get_backend(getattr(blob, "storage", None) or "local")


def reset_backends() -> None:
    """Drop cached backends (tests)."""
    _backends.clear()
