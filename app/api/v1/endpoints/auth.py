from __future__ import annotations
"""
app/api/v1/endpoints/auth.py
─────────────────────────────
Authentication endpoints (no phone number, no OTP):
  POST /register         — Create account, returns generated private_number + tokens
  POST /login            — Authenticate with private_number + password. The
                           password field accepts either login_password
                           (→ normal session) or delete_password (→ delete-intent
                           token, which the client uses to confirm deletion).
  POST /confirm-delete   — Consume a delete-intent token to hard-delete the account
  POST /refresh          — Refresh access token using refresh token
  POST /logout           — Invalidate current session

Rate limiting strategy:
  - Register:       5/minute per IP
  - Login:          10/minute per IP (brute-force slow-down)
  - Confirm-delete: 3/minute per IP (defense against panic-spam)
  - Refresh/logout: 30/minute
"""

import logging
from typing import Union
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from jose import JWTError

from app.core.config import settings
from app.core.dependencies import DBSession
from app.core.limiter import limiter
from app.core.security import (
    create_delete_intent_token,
    verify_delete_intent_token,
)
from app.schemas.auth import (
    ConfirmDeleteRequest,
    DeleteIntentResponse,
    LoginRequest,
    LoginResponse,
    MessageResponse,
    RefreshRequest,
    RegisterRequest,
    RegisterResponse,
    TokenPair,
    UserResponse,
)
from app.services import auth_service
from app.services.auth_service import AuthOutcome

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["Authentication"])


# ── Registration ──────────────────────────────────────────────────────────────

@router.post(
    "/register",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new account (generates private_number)",
)
@limiter.limit("5/minute")
async def register(
    request: Request,
    body: RegisterRequest,
    db: DBSession,
) -> RegisterResponse:
    """
    Create a new account. The server generates a unique 10-digit private_number
    and stores bcrypt hashes of both passwords. The user is auto-logged-in
    (tokens returned inline).
    """
    client_ip = request.client.host if request.client else "unknown"
    logger.info("[REGISTER] Request from %s | display_name=%r", client_ip, body.display_name)
    try:
        user = await auth_service.register_user(
            login_password=body.login_password,
            delete_password=body.delete_password,
            display_name=body.display_name,
            db=db,
        )
        logger.info("[REGISTER] User created | id=%s private_number=%s", user.id, user.private_number)
        tokens = await auth_service.create_session(user, db)
        logger.info("[REGISTER] Session created | user_id=%s", user.id)
        return RegisterResponse(
            user=UserResponse.model_validate(user),
            tokens=tokens,
            private_number=user.private_number,
        )
    except Exception as exc:
        logger.exception("[REGISTER] Failed for display_name=%r | error=%s", body.display_name, exc)
        raise


# ── Login ─────────────────────────────────────────────────────────────────────

@router.post(
    "/login",
    response_model=Union[LoginResponse, DeleteIntentResponse],
    summary="Log in with private_number + password (login OR delete-intent)",
)
@limiter.limit("10/minute")
async def login(
    request: Request,
    body: LoginRequest,
    db: DBSession,
) -> Union[LoginResponse, DeleteIntentResponse]:
    """
    Authenticate the user and return one of two outcomes:

    1. The password matched the user's `login_password` → issue a normal
       access + refresh token pair and return ``LoginResponse``.
    2. The password matched the user's `delete_password` → issue a short-lived
       delete-intent token (no session is created) and return
       ``DeleteIntentResponse``. The client should show a confirmation dialog
       and, on confirm, call POST /confirm-delete with the token.

    Any other case returns HTTP 401 with a generic error message.
    """
    client_ip = request.client.host if request.client else "unknown"
    logger.info("[LOGIN] Request from %s | private_number=%s", client_ip, body.private_number)
    try:
        outcome, user = await auth_service.authenticate_or_delete_intent(
            private_number=body.private_number,
            password=body.login_password,
            db=db,
        )
    except ValueError as exc:
        logger.warning("[LOGIN] Auth failed | private_number=%s | reason=%s", body.private_number, exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.exception("[LOGIN] Unexpected error | private_number=%s | error=%s", body.private_number, exc)
        raise

    if outcome is AuthOutcome.AUTHENTICATED:
        tokens = await auth_service.create_session(user, db)
        logger.info("[LOGIN] authenticated | user_id=%s", user.id)
        return LoginResponse(
            user=UserResponse.model_validate(user),
            tokens=tokens,
        )

    # AuthOutcome.DELETE_INTENT — do NOT create a session, do NOT return user/tokens.
    delete_token = create_delete_intent_token(user.id)
    logger.info("[LOGIN] delete-intent issued | user_id=%s", user.id)
    return DeleteIntentResponse(
        delete_token=delete_token,
        expires_in=settings.DELETE_INTENT_TOKEN_EXPIRE_MINUTES * 60,
    )


# ── Confirm delete (from login screen) ───────────────────────────────────────

@router.post(
    "/confirm-delete",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
    summary="Confirm account deletion using a delete-intent token",
)
@limiter.limit("3/minute")
async def confirm_delete(
    request: Request,
    body: ConfirmDeleteRequest,
    db: DBSession,
) -> MessageResponse:
    """
    Consume a delete-intent token (issued by POST /login when the user
    supplied their delete_password) and hard-delete the associated account.
    FK cascades wipe devices, sessions, contacts, messages, and conversation
    participations.
    """
    try:
        payload = verify_delete_intent_token(body.delete_token)
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired delete token",
        ) from exc

    try:
        user_id = UUID(payload["sub"])
    except (KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired delete token",
        ) from exc

    await auth_service.delete_user_by_id(user_id, db)
    logger.info("[CONFIRM_DELETE] user_id=%s", user_id)
    return MessageResponse(message="Account deleted.")


# ── Token Refresh ─────────────────────────────────────────────────────────────

@router.post(
    "/refresh",
    response_model=TokenPair,
    summary="Refresh access token",
)
@limiter.limit("30/minute")
async def refresh(
    request: Request,
    body: RefreshRequest,
    db: DBSession,
) -> dict:
    """
    Exchange a valid refresh token for a new JWT access + refresh token pair.
    The old refresh token is invalidated (rotation).
    """
    try:
        tokens = await auth_service.refresh_session(body.refresh_token, db)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        ) from exc
    return tokens.model_dump()


# ── Logout ────────────────────────────────────────────────────────────────────

@router.post(
    "/logout",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
    summary="Logout (invalidate session)",
)
@limiter.limit("30/minute")
async def logout(
    request: Request,
    body: RefreshRequest,
    db: DBSession,
) -> MessageResponse:
    """
    Invalidate the session associated with the provided refresh token.
    """
    await auth_service.invalidate_session(body.refresh_token, db)
    return MessageResponse(message="Successfully logged out.")
