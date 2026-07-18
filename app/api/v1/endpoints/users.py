from __future__ import annotations
"""
app/api/v1/endpoints/users.py
──────────────────────────────
User profile endpoints.

  GET /users/me — Return the current user's public profile.
                   Useful right after token refresh, when /auth/refresh
                   does not return user data.
"""

from fastapi import APIRouter, HTTPException, Request, status

from app.core.dependencies import CurrentUser, DBSession
from app.core.limiter import limiter
from app.schemas.auth import ChangePasswordRequest, MessageResponse
from app.schemas.messaging import (
    ProfileUpdateRequest,
    PublicKeyUploadRequest,
    UserPublic,
)
from app.services import auth_service

router = APIRouter(prefix="/users", tags=["Users"])


@router.get(
    "/me",
    response_model=UserPublic,
    status_code=status.HTTP_200_OK,
    summary="Get the current user's profile",
)
@limiter.limit("60/minute")
async def get_me(
    request: Request,
    current_user: CurrentUser,
) -> UserPublic:
    return UserPublic.model_validate(current_user)


@router.patch(
    "/me",
    response_model=UserPublic,
    status_code=status.HTTP_200_OK,
    summary="Update the current user's display name or profile picture",
)
@limiter.limit("20/minute")
async def update_me(
    request: Request,
    body: ProfileUpdateRequest,
    current_user: CurrentUser,
    db: DBSession,
) -> UserPublic:
    """
    Partial update — only fields explicitly set (non-null) are applied.
    Pass `profile_picture_key` with the blob_id returned by POST /media/upload-url
    after the image bytes have been PUT successfully.
    `bio`: pass an empty string to clear it (stored as NULL).
    """
    if body.display_name is not None:
        current_user.display_name = body.display_name
    if body.bio is not None:
        current_user.bio = body.bio.strip() or None
    if body.profile_picture_key is not None:
        current_user.profile_picture_key = body.profile_picture_key
    await db.flush()
    return UserPublic.model_validate(current_user)


@router.post(
    "/me/password",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
    summary="Change the caller's login password",
)
@limiter.limit("5/minute")
async def change_password(
    request: Request,
    body: ChangePasswordRequest,
    current_user: CurrentUser,
    db: DBSession,
) -> MessageResponse:
    """
    Changes the LOGIN password only. All other sessions are invalidated;
    the session identified by `refresh_token` (the caller's own) survives.
    """
    try:
        await auth_service.change_login_password(
            user=current_user,
            current_password=body.current_password,
            new_password=body.new_password,
            keep_refresh_token=body.refresh_token,
            db=db,
        )
    except PermissionError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Current password is incorrect",
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    return MessageResponse(message="Password changed")


@router.post(
    "/me/public-key",
    response_model=UserPublic,
    status_code=status.HTTP_200_OK,
    summary="Upload or replace the caller's long-term E2EE public key",
)
@limiter.limit("10/minute")
async def upload_public_key(
    request: Request,
    body: PublicKeyUploadRequest,
    current_user: CurrentUser,
    db: DBSession,
) -> UserPublic:
    """
    Idempotent: pass the current public key on every app launch — if it's
    unchanged, nothing happens. Replacing the key invalidates all encrypted
    messages from older devices (clients are responsible for re-syncing).
    """
    if current_user.public_key != body.public_key:
        current_user.public_key = body.public_key
        await db.flush()
    return UserPublic.model_validate(current_user)
