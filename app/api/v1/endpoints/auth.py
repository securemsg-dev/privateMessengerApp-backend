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
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.models.user import User

from app.core.config import settings
from app.core.dependencies import DBSession
from app.core.limiter import limiter
from app.core.private_number import generate_private_number
from app.core.security import (
    create_delete_intent_token,
    create_registration_token,
    verify_delete_intent_token,
    verify_registration_token,
)
from app.schemas.auth import (
    ConfirmDeleteRequest,
    DeleteIntentResponse,
    LoginRequest,
    LoginResponse,
    MessageResponse,
    RefreshRequest,
    RegisterBeginResponse,
    RegisterRequest,
    RegisterResponse,
    TokenPair,
    UserResponse,
)
from app.services import auth_service
from app.services.auth_service import AuthOutcome

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["Authentication"])

# ── Per-number brute-force lockout ───────────────────────────────────────────
# The per-IP slowapi limit is spoofable behind a proxy (X-Forwarded-For), so
# failed logins are ALSO counted per private_number in Redis. Successful
# logins are never throttled by this; only failures count.
LOGIN_FAILURES_PER_NUMBER = 10
LOGIN_FAILURE_WINDOW_SECONDS = 300


def _mask(private_number: str) -> str:
    """Log-safe form of a private number: last 4 digits only."""
    return f"******{private_number[-4:]}" if len(private_number) >= 4 else "******"


async def _check_number_lockout(redis, private_number: str) -> None:
    """Raise 429 when this number has too many recent failed attempts."""
    try:
        count = await redis.get(f"login_fail:{private_number}")
    except Exception:
        logger.warning("login lockout check failed (Redis unavailable) — failing open")
        return
    if count is None:
        return
    n = int(count.decode() if isinstance(count, (bytes, bytearray)) else count)
    if n >= LOGIN_FAILURES_PER_NUMBER:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed attempts. Try again in a few minutes.",
        )


async def _record_login_failure(redis, private_number: str) -> None:
    try:
        key = f"login_fail:{private_number}"
        n = await redis.incr(key)
        if n == 1:
            await redis.expire(key, LOGIN_FAILURE_WINDOW_SECONDS)
    except Exception:
        logger.warning("login failure counter unavailable (Redis down?)")


# ── Registration ──────────────────────────────────────────────────────────────

@router.post(
    "/register/begin",
    response_model=RegisterBeginResponse,
    status_code=status.HTTP_200_OK,
    summary="Allocate a candidate private_number for registration (step 1 of 2)",
)
@limiter.limit("10/minute")
async def register_begin(
    request: Request,
    db: DBSession,
) -> RegisterBeginResponse:
    """
    Return a candidate 10-digit private_number the client uses as the KDF salt
    to derive its auth verifiers and key-backup wrap key before completing
    registration. The number is NOT persisted here — /register enforces
    uniqueness via the DB constraint, so a candidate taken in the meantime
    just yields a 409 and the client retries begin. We still probe the table
    to hand back a currently-free candidate and keep retries rare.
    """
    for _ in range(10):
        candidate = generate_private_number()
        exists = await db.execute(
            select(User.id).where(User.private_number == candidate)
        )
        if exists.scalar_one_or_none() is None:
            return RegisterBeginResponse(
                private_number=candidate,
                registration_token=create_registration_token(candidate),
            )
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Could not allocate a private number, please retry",
    )


@router.post(
    "/register",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Complete registration with a client-allocated private_number (step 2 of 2)",
)
@limiter.limit("5/minute")
async def register(
    request: Request,
    body: RegisterRequest,
    db: DBSession,
) -> RegisterResponse:
    """
    Create the account using the `private_number` from /register/begin. The
    server stores bcrypt hashes of the client-derived verifiers (in
    login_password / delete_password) plus the E2EE public key and encrypted
    key backup. Auto-logs-in (tokens returned inline). If the candidate number
    was taken between begin and complete, returns 409 so the client retries.
    """
    client_ip = request.client.host if request.client else "unknown"
    logger.info("[REGISTER] Request from %s | display_name=%r", client_ip, body.display_name)

    # The number must be one /register/begin actually issued: verify the signed
    # token and that it binds this exact number. Blocks client-chosen numbers
    # (squatting) and unauthenticated existence probing via the 409 path.
    try:
        bound_number = verify_registration_token(body.registration_token)
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired registration token — call /register/begin first",
        ) from exc
    if bound_number != body.private_number:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="private_number does not match the registration token",
        )

    try:
        user = await auth_service.register_user(
            private_number=body.private_number,
            login_password=body.login_password,
            delete_password=body.delete_password,
            display_name=body.display_name,
            public_key=body.public_key,
            encrypted_key_backup=body.encrypted_key_backup,
            db=db,
        )
    except IntegrityError:
        await db.rollback()
        logger.info("[REGISTER] private_number collision | %s", _mask(body.private_number))
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="private_number just taken, request a new one",
        )
    except Exception as exc:
        logger.exception("[REGISTER] Failed for display_name=%r | error=%s", body.display_name, exc)
        raise

    logger.info("[REGISTER] User created | id=%s private_number=%s", user.id, _mask(user.private_number))
    tokens = await auth_service.create_session(user, db)
    logger.info("[REGISTER] Session created | user_id=%s", user.id)
    return RegisterResponse(
        user=UserResponse.model_validate(user),
        tokens=tokens,
        private_number=user.private_number,
    )


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
    masked = _mask(body.private_number)
    logger.info("[LOGIN] Request from %s | private_number=%s", client_ip, masked)

    redis = request.app.state.redis
    await _check_number_lockout(redis, body.private_number)

    try:
        outcome, user = await auth_service.authenticate_or_delete_intent(
            private_number=body.private_number,
            password=body.login_password,
            db=db,
        )
    except ValueError as exc:
        await _record_login_failure(redis, body.private_number)
        logger.warning("[LOGIN] Auth failed | private_number=%s | reason=%s", masked, exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.exception("[LOGIN] Unexpected error | private_number=%s | error=%s", masked, exc)
        raise

    if outcome is AuthOutcome.AUTHENTICATED:
        tokens = await auth_service.create_session(user, db)
        logger.info("[LOGIN] authenticated | user_id=%s", user.id)
        return LoginResponse(
            user=UserResponse.model_validate(user),
            tokens=tokens,
            encrypted_key_backup=user.encrypted_key_backup,
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
