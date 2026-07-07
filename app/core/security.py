from __future__ import annotations
"""
app/core/security.py
─────────────────────
JWT creation/verification and password hashing utilities.
"""

import asyncio
import secrets
from datetime import datetime, timedelta, timezone
from typing import Union, Optional, Any
from uuid import UUID

import bcrypt
from jose import JWTError, jwt

from app.core.config import settings


# ── Password helpers ──────────────────────────────────────────────────────────
# Uses the bcrypt library directly. passlib 1.7.4 is incompatible with bcrypt
# >= 4.x (its backend-detection probe trips the 72-byte limit at import time),
# so we bypass it. bcrypt's own API enforces the 72-byte rule on the raw bytes.

def hash_password(plain: str) -> str:
    hashed = bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt())
    return hashed.decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


# Async wrappers — bcrypt is ~100-250ms of pure CPU per call. Run it on a
# worker thread so a login/register doesn't freeze the event loop (which
# would stall every other request AND all WebSocket traffic on the process).

async def hash_password_async(plain: str) -> str:
    return await asyncio.to_thread(hash_password, plain)


async def verify_password_async(plain: str, hashed: str) -> bool:
    return await asyncio.to_thread(verify_password, plain, hashed)


# ── JWT helpers ───────────────────────────────────────────────────────────────

def _create_token(
    subject: Union[str, UUID],
    token_type: str,
    expires_delta: timedelta,
    extra_claims: Optional[dict[str, Any]] = None,
) -> str:
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(subject),
        "type": token_type,
        "iat": now,
        "exp": now + expires_delta,
        "jti": secrets.token_urlsafe(16),
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_access_token(user_id: Union[str, UUID], device_id: Optional[Union[str, UUID]] = None) -> str:
    """Short-lived access token (default 15 min)."""
    return _create_token(
        subject=user_id,
        token_type="access",
        expires_delta=timedelta(minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES),
        extra_claims={"device_id": str(device_id)} if device_id else None,
    )


def create_refresh_token(user_id: Union[str, UUID], device_id: Optional[Union[str, UUID]] = None) -> str:
    """Long-lived refresh token (default 30 days)."""
    return _create_token(
        subject=user_id,
        token_type="refresh",
        expires_delta=timedelta(days=settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS),
        extra_claims={"device_id": str(device_id)} if device_id else None,
    )


def decode_token(token: str) -> dict[str, Any]:
    """
    Decode and validate a JWT.

    Raises:
        JWTError: if the token is invalid or expired.
    """
    return jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])


def verify_access_token(token: str) -> dict[str, Any]:
    """Decode token and assert it is an access token."""
    payload = decode_token(token)
    if payload.get("type") != "access":
        raise JWTError("Token is not an access token")
    return payload


def verify_refresh_token(token: str) -> dict[str, Any]:
    """Decode token and assert it is a refresh token."""
    payload = decode_token(token)
    if payload.get("type") != "refresh":
        raise JWTError("Token is not a refresh token")
    return payload


# ── Delete-intent JWT ─────────────────────────────────────────────────────────
# Issued by POST /login when the supplied password matches delete_password.
# Accepted ONLY by POST /confirm-delete. Not a session token — no device_id,
# and rejected by verify_access_token / verify_refresh_token.

DELETE_INTENT_TOKEN_TYPE = "delete_intent"


def create_delete_intent_token(user_id: Union[str, UUID]) -> str:
    """Short-lived single-purpose token proving the caller knows the delete password."""
    return _create_token(
        subject=user_id,
        token_type=DELETE_INTENT_TOKEN_TYPE,
        expires_delta=timedelta(minutes=settings.DELETE_INTENT_TOKEN_EXPIRE_MINUTES),
    )


def verify_delete_intent_token(token: str) -> dict[str, Any]:
    """Decode token and assert it is a delete-intent token."""
    payload = decode_token(token)
    if payload.get("type") != DELETE_INTENT_TOKEN_TYPE:
        raise JWTError("Token is not a delete-intent token")
    return payload
