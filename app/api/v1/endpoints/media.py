from __future__ import annotations
"""
app/api/v1/endpoints/media.py
──────────────────────────────
Encrypted media blob storage (Phase D).

  POST /media/upload-url  — reserve a blob_id and get an upload URL
  PUT  /media/{blob_id}   — write the ciphertext bytes (LocalFileStorage only;
                             with S3Storage the client uploads directly to S3
                             via a pre-signed URL and never hits this endpoint)
  GET  /media/{blob_id}   — fetch the ciphertext (any authenticated user;
                             without the symmetric key this is unreadable)

The bytes flowing through these endpoints are CIPHERTEXT — clients encrypt
with a fresh symmetric key per blob and pass the key over the existing E2EE
message channel. The server has zero knowledge of plaintext content.
"""

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy import select

from app.core.config import settings
from app.core.dependencies import CurrentUser, DBSession
from app.core.limiter import limiter
from app.db.models.media_blob import MediaBlob
from app.schemas.messaging import MediaUploadRequest, MediaUploadResponse
from app.services.media_storage import get_storage

router = APIRouter(prefix="/media", tags=["Media"])


def _absolute_url(request: Request, path: str) -> str:
    """Turn `/api/v1/media/<id>` into a fully-qualified URL the client can hit."""
    if path.startswith("http://") or path.startswith("https://"):
        return path  # S3 backend already hands us absolute URLs
    base = str(request.base_url).rstrip("/")
    return f"{base}{path}"


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

    blob = MediaBlob(
        owner_id=current_user.id,
        size_bytes=body.size_bytes,
        mime=body.mime,
        uploaded_at=None,
    )
    db.add(blob)
    await db.flush()  # assign blob.id

    storage = get_storage()
    upload_path, expires_at = await storage.issue_upload_url(blob.id)
    download_path = await storage.issue_download_url(blob.id)

    return MediaUploadResponse(
        blob_id=blob.id,
        upload_url=_absolute_url(request, upload_path),
        download_url=_absolute_url(request, download_path),
        expires_at=expires_at,
        max_bytes=settings.MEDIA_MAX_BLOB_BYTES,
    )


@router.put(
    "/{blob_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Upload encrypted bytes for a previously-reserved blob",
)
@limiter.limit("60/minute")
async def upload_blob(
    request: Request,
    blob_id: UUID,
    current_user: CurrentUser,
    db: DBSession,
) -> Response:
    blob = (await db.execute(
        select(MediaBlob).where(MediaBlob.id == blob_id)
    )).scalar_one_or_none()

    if blob is None:
        raise HTTPException(status_code=404, detail="Blob not reserved")
    if blob.owner_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't own this upload reservation",
        )
    if blob.uploaded_at is not None:
        # Idempotent: re-uploading the same blob is a no-op
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    body = await request.body()
    if len(body) > settings.MEDIA_MAX_BLOB_BYTES:
        raise HTTPException(status_code=413, detail="Blob exceeds size limit")
    if len(body) != blob.size_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Uploaded {len(body)} bytes, but reservation declared "
                f"{blob.size_bytes}"
            ),
        )

    storage = get_storage()
    await storage.write_bytes(blob.id, body)
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
    travels separately over the E2EE message channel). We could narrow
    this to "must be in a conversation that references this blob", but
    that's an O(scan) lookup — defer until abuse signals demand it.
    """
    blob = (await db.execute(
        select(MediaBlob).where(MediaBlob.id == blob_id)
    )).scalar_one_or_none()

    if blob is None or blob.uploaded_at is None:
        raise HTTPException(status_code=404, detail="Blob not found")

    storage = get_storage()
    data = await storage.read_bytes(blob.id)
    if data is None:
        raise HTTPException(status_code=404, detail="Blob bytes missing")

    # Touch the user reference so unused 'request' isn't a hint warning;
    # in practice the @limiter.limit decorator above needs `request` too.
    _ = current_user
    return Response(content=data, media_type="application/octet-stream")
