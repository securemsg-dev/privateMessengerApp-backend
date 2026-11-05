from __future__ import annotations
"""
app/api/v1/endpoints/media.py
──────────────────────────────
Encrypted media blob storage (Phase D).

  POST /media/upload-url        reserve a blob and get upload coordinates
                                (presigned direct-to-bucket for new clients
                                when the bucket backend is active)
  POST /media/{id}/complete     confirm a direct upload (size-checked)
  POST /media/download-urls     batch: where to fetch each blob from
  PUT  /media/{id}              upload through the app (old builds, avatars)
  GET  /media/{id}              download through the app (old builds,
                                avatars; streams S3 blobs as a proxy)

The bytes flowing through these endpoints are CIPHERTEXT — clients encrypt
with a fresh symmetric key per blob and pass the key over the existing E2EE
message channel. The server has zero knowledge of plaintext content.
"""

from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import select

from app.core.config import settings
from app.core.dependencies import CurrentUser, DBSession
from app.core.limiter import limiter
from app.db.models.media_blob import MediaBlob
from app.schemas.messaging import (
    MediaDownloadUrl,
    MediaDownloadUrlsRequest,
    MediaDownloadUrlsResponse,
    MediaUploadRequest,
    MediaUploadResponse,
)
from app.services.media_storage import (
    OCTET_STREAM,
    api_media_path,
    default_storage,
    storage_for,
)

router = APIRouter(prefix="/media", tags=["Media"])


def _absolute_url(request: Request, path: str) -> str:
    """Turn `/api/v1/media/<id>` into a fully-qualified URL the client can hit."""
    if path.startswith("http://") or path.startswith("https://"):
        return path
    base = str(request.base_url).rstrip("/")
    return f"{base}{path}"


async def _owned_blob(db, blob_id: UUID, user_id: UUID) -> MediaBlob:
    blob = (await db.execute(
        select(MediaBlob).where(MediaBlob.id == blob_id)
    )).scalar_one_or_none()
    if blob is None:
        raise HTTPException(status_code=404, detail="Blob not reserved")
    if blob.owner_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't own this upload reservation",
        )
    return blob


@router.post(
    "/upload-url",
    response_model=MediaUploadResponse,
    status_code=status.HTTP_200_OK,
    summary="Reserve an encrypted media blob and get an upload URL",
)
@limiter.limit("60/minute")
async def request_upload_url(
    request: Request,
    body: MediaUploadRequest,
    current_user: CurrentUser,
    db: DBSession,
) -> MediaUploadResponse:
    if body.size_bytes > settings.MEDIA_MAX_BLOB_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"Blob exceeds {settings.MEDIA_MAX_BLOB_BYTES} byte limit "
                f"(requested {body.size_bytes})"
            ),
        )

    storage = default_storage()
    blob = MediaBlob(
        owner_id=current_user.id,
        size_bytes=body.size_bytes,
        mime=body.mime,
        uploaded_at=None,
        storage=storage.name,
    )
    db.add(blob)
    await db.flush()  # assign blob.id

    api_url = _absolute_url(request, api_media_path(blob.id))
    if body.direct and storage.supports_direct:
        upload_url, headers, expires_at = await storage.presign_upload(
            blob.id, body.size_bytes,
        )
        method = "direct"
    else:
        upload_url, headers, method = api_url, {}, "api"
        expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=settings.MEDIA_UPLOAD_URL_TTL_SECONDS,
        )

    return MediaUploadResponse(
        blob_id=blob.id,
        upload_url=upload_url,
        download_url=api_url,
        expires_at=expires_at,
        max_bytes=settings.MEDIA_MAX_BLOB_BYTES,
        upload_method=method,
        upload_headers=headers,
    )


@router.post(
    "/{blob_id}/complete",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Confirm a direct (presigned) upload finished",
)
@limiter.limit("60/minute")
async def complete_direct_upload(
    request: Request,
    blob_id: UUID,
    current_user: CurrentUser,
    db: DBSession,
) -> Response:
    """
    After PUTting straight to the bucket, the client calls this so the blob
    can be referenced by a message (the WS send path requires `uploaded_at`).
    The stored object's size must equal the reservation exactly.
    """
    blob = await _owned_blob(db, blob_id, current_user.id)
    if blob.uploaded_at is not None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    storage = storage_for(blob)
    if not storage.supports_direct:
        raise HTTPException(status_code=400, detail="Blob was not reserved for direct upload")

    size = await storage.object_size(blob.id)
    if size is None:
        raise HTTPException(status_code=409, detail="Upload not found in storage yet")
    if size != blob.size_bytes:
        # Don't keep (or let anyone reference) a mismatched object.
        await storage.delete_bytes(blob.id)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Uploaded {size} bytes, but reservation declared {blob.size_bytes}",
        )

    blob.uploaded_at = datetime.now(timezone.utc)
    await db.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/download-urls",
    response_model=MediaDownloadUrlsResponse,
    summary="Batch-resolve where to download blobs from",
)
@limiter.limit("120/minute")
async def download_urls(
    request: Request,
    body: MediaDownloadUrlsRequest,
    current_user: CurrentUser,
    db: DBSession,
) -> MediaDownloadUrlsResponse:
    """
    One round trip for a whole page of media. Bucket blobs get a presigned
    GET (fetched without auth, straight from storage); volume blobs get the
    app path (fetched with auth). Same access rule as GET /media/{id}: any
    authenticated user — the bytes are ciphertext.
    """
    _ = current_user
    ids = list(dict.fromkeys(body.blob_ids))  # dedupe, keep order
    blobs = (await db.execute(
        select(MediaBlob).where(MediaBlob.id.in_(ids), MediaBlob.uploaded_at.is_not(None))
    )).scalars().all()

    urls: dict[str, MediaDownloadUrl] = {}
    for blob in blobs:
        storage = storage_for(blob)
        if storage.supports_direct:
            url, expires_at = await storage.presign_download(blob.id)
            urls[str(blob.id)] = MediaDownloadUrl(url=url, auth=False, expires_at=expires_at)
        else:
            urls[str(blob.id)] = MediaDownloadUrl(
                url=_absolute_url(request, api_media_path(blob.id)), auth=True,
            )
    return MediaDownloadUrlsResponse(urls=urls)


@router.put(
    "/{blob_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Upload encrypted bytes through the app (legacy / avatars)",
)
@limiter.limit("60/minute")
async def upload_blob(
    request: Request,
    blob_id: UUID,
    current_user: CurrentUser,
    db: DBSession,
) -> Response:
    blob = await _owned_blob(db, blob_id, current_user.id)
    if blob.uploaded_at is not None:
        # Idempotent: re-uploading the same blob is a no-op
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # Cheap rejection BEFORE reading anything: a hostile client could
    # otherwise stream gigabytes into memory before the old post-read check
    # ever ran. Content-Length can lie, so the streamed read below still
    # enforces the cap on actual bytes received.
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > settings.MEDIA_MAX_BLOB_BYTES:
        raise HTTPException(status_code=413, detail="Blob exceeds size limit")

    received = bytearray()
    async for chunk in request.stream():
        received.extend(chunk)
        if len(received) > min(settings.MEDIA_MAX_BLOB_BYTES, blob.size_bytes):
            # Abort as soon as the body exceeds what the reservation declared —
            # don't buffer the rest.
            raise HTTPException(status_code=413, detail="Blob exceeds declared size")
    body = bytes(received)
    if len(body) != blob.size_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Uploaded {len(body)} bytes, but reservation declared "
                f"{blob.size_bytes}"
            ),
        )

    # For bucket blobs this proxies the bytes into the bucket (old builds
    # that never learned the direct path still work after the switch).
    await storage_for(blob).write_bytes(blob.id, body)
    blob.uploaded_at = datetime.now(timezone.utc)
    await db.flush()

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/{blob_id}",
    summary="Fetch encrypted bytes for a blob",
    responses={
        200: {"content": {"application/octet-stream": {}}},
        404: {"description": "Blob not found or not yet uploaded"},
    },
)
@limiter.limit("120/minute")
async def download_blob(
    request: Request,
    blob_id: UUID,
    current_user: CurrentUser,
    db: DBSession,
) -> Response:
    """
    Returns ciphertext as `application/octet-stream`. Any authenticated
    user may fetch (the bytes are useless without the symmetric key, which
    travels separately over the E2EE message channel).

    Old app builds fetch EVERY blob through here (the URL inside their
    envelopes), including blobs that live in the bucket — so bucket blobs are
    streamed through in chunks rather than loaded whole into memory.
    """
    blob = (await db.execute(
        select(MediaBlob).where(MediaBlob.id == blob_id)
    )).scalar_one_or_none()

    if blob is None or blob.uploaded_at is None:
        raise HTTPException(status_code=404, detail="Blob not found")

    _ = current_user  # auth enforced by the dependency; request feeds the limiter

    storage = storage_for(blob)
    # Volume blobs: stream from disk with Range support (voice/video seeking).
    path = storage.local_path(blob.id)
    if path is not None:
        return FileResponse(path, media_type=OCTET_STREAM)

    stream = await storage.open_stream(blob.id)
    if stream is None:
        raise HTTPException(status_code=404, detail="Blob bytes missing")
    return StreamingResponse(
        stream,
        media_type=OCTET_STREAM,
        headers={"Content-Length": str(blob.size_bytes)},
    )
