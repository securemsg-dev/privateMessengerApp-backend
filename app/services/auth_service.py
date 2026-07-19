from __future__ import annotations
from enum import Enum
from typing import Optional
"""
app/services/auth_service.py
─────────────────────────────
Business logic for registration, login, account deletion, and token management.
Keeps endpoint handlers thin — all DB and domain logic lives here.
"""

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.private_number import generate_unique_private_number
from app.core.security import (
    create_access_token,
    create_refresh_token,
    hash_password,
    hash_password_async,
    verify_password_async,
)
from app.db.models.media_blob import MediaBlob
from app.db.models.session import Session as UserSession
from app.db.models.user import User
from app.schemas.auth import TokenPair
from app.services.media_storage import get_storage

logger = logging.getLogger(__name__)


class AuthOutcome(Enum):
    """Result of POST /login password check."""
    AUTHENTICATED = "authenticated"   # login_password matched → issue session
    DELETE_INTENT = "delete_intent"   # delete_password matched → issue delete-intent token


# Pre-computed at import time so that the "unknown private_number" branch
# pays the same bcrypt cost as the "known user, wrong password" branch. This
# prevents an attacker from distinguishing the two via response latency.
_DUMMY_BCRYPT_HASH = hash_password("__dummy_never_matches__")


def _hash_refresh_token(token: str) -> str:
    """SHA-256 hash of the raw refresh token for storage."""
    return hashlib.sha256(token.encode()).hexdigest()


async def register_user(
    login_password: str,
    delete_password: str,
    display_name: Optional[str],
    db: AsyncSession,
) -> User:
    """
    Create a new user with a freshly generated private_number and two bcrypt
    password hashes. Returns the persisted User (flushed, not committed).
    """
    private_number = await generate_unique_private_number(db)
    user = User(
        private_number=private_number,
        login_password_hash=await hash_password_async(login_password),
        delete_password_hash=await hash_password_async(delete_password),
        display_name=display_name,
    )
    db.add(user)
    await db.flush()
    # Only the last 4 digits go to logs — the full number is the user's
    # identity and log lines outlive the request (Railway retention).
    logger.info("New user registered: private_number=******%s", private_number[-4:])
    return user


async def authenticate_or_delete_intent(
    private_number: str,
    password: str,
    db: AsyncSession,
) -> tuple[AuthOutcome, User]:
    """
    Check the supplied password against BOTH the user's login_password_hash
    and delete_password_hash and return which matched.

    Returns:
        (AuthOutcome.AUTHENTICATED, user) — login_password matched; caller should
        issue a normal session.
        (AuthOutcome.DELETE_INTENT, user) — delete_password matched; caller should
        issue a short-lived delete-intent token (no session) and let the user
        confirm deletion on a separate endpoint.

    Raises:
        ValueError — no matching user, wrong password, or deactivated account.
        Endpoint should translate to HTTP 401 with a generic error message.

    Timing-attack notes:
        bcrypt verify is run against BOTH hashes on every real-user call (and
        against a module-level dummy hash when the private_number is unknown)
        so an attacker cannot distinguish login/delete/unknown branches by
        response latency. Do NOT short-circuit or early-return.
    """
    result = await db.execute(
        select(User).where(User.private_number == private_number)
    )
    user = result.scalar_one_or_none()

    if user is None:
        # Timing equalizer — the known-user path below pays TWO bcrypt
        # verifies (login + delete hash), so the unknown-number branch must
        # pay two as well or response latency leaks which private numbers
        # exist (~150ms difference is trivially measurable).
        await verify_password_async(password, _DUMMY_BCRYPT_HASH)
        await verify_password_async(password, _DUMMY_BCRYPT_HASH)
        raise ValueError("Invalid private number or password")

    if not user.is_active:
        # Still run both verifies for timing parity, then reject.
        await verify_password_async(password, user.login_password_hash)
        await verify_password_async(password, user.delete_password_hash)
        raise ValueError("Account is deactivated")

    # Run BOTH verifies unconditionally. Do not combine into `or`.
    login_ok = await verify_password_async(password, user.login_password_hash)
    delete_ok = await verify_password_async(password, user.delete_password_hash)

    if login_ok:
        return AuthOutcome.AUTHENTICATED, user
    if delete_ok:
        return AuthOutcome.DELETE_INTENT, user
    raise ValueError("Invalid private number or password")


async def change_login_password(
    user: User,
    current_password: str,
    new_password: str,
    keep_refresh_token: Optional[str],
    db: AsyncSession,
) -> None:
    """
    Change the user's LOGIN password (delete password is untouched).

    Raises:
        PermissionError — current_password does not match login_password_hash.
            Endpoint should translate to HTTP 403.
        ValueError — new password collides with the delete password. Without
            this guard every future login with it would silently enter the
            account-wipe branch (authenticate_or_delete_intent checks both
            hashes). Endpoint should translate to HTTP 400 with a generic
            message that does not confirm the collision.

    Session handling: all of the user's sessions are deleted EXCEPT the one
    matching `keep_refresh_token` (the caller's own), so other devices that
    knew the old password are logged out on their next refresh. If the token
    is None or unknown, every session is deleted — the caller then re-logins.
    """
    if not await verify_password_async(current_password, user.login_password_hash):
        raise PermissionError("Current password is incorrect")

    if await verify_password_async(new_password, user.delete_password_hash):
        raise ValueError("Please choose a different password")

    user.login_password_hash = await hash_password_async(new_password)

    keep_hash = _hash_refresh_token(keep_refresh_token) if keep_refresh_token else None
    result = await db.execute(
        select(UserSession).where(UserSession.user_id == user.id)
    )
    for session in result.scalars().all():
        if keep_hash is None or session.refresh_token_hash != keep_hash:
            await db.delete(session)
    await db.flush()
    logger.info(
        "Login password changed: user_id=%s private_number=******%s",
        user.id,
        user.private_number[-4:],
    )


async def delete_user_by_id(user_id: UUID, db: AsyncSession) -> None:
    """
    Hard-delete a user row by id. Trust is established by the caller having
    already presented a valid delete-intent token; this function does not
    re-check any password.

    FK cascades wipe devices, sessions, contacts, messages, and conversation
    participations. Idempotent: if the user is already gone (e.g. concurrent
    delete from another device), logs and returns normally.
    """
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        logger.info("delete_user_by_id: user already gone | user_id=%s", user_id)
        return
    logger.info(
        "Deleting account: user_id=%s private_number=******%s",
        user.id,
        user.private_number[-4:],
    )

    # Media cleanup: media_blobs.owner_id is only SET NULL by the FK, so
    # without this the ciphertext files (avatar + every sent attachment)
    # would sit on the storage volume forever. File deletion is best-effort
    # — a storage hiccup must not block the account wipe.
    storage = get_storage()
    owned_blobs = (await db.execute(
        select(MediaBlob).where(MediaBlob.owner_id == user_id)
    )).scalars().all()
    for blob in owned_blobs:
        try:
            await storage.delete_bytes(blob.id)
        except Exception:
            logger.warning("Account delete: failed to remove blob file %s", blob.id)
        await db.delete(blob)

    await db.delete(user)
    await db.flush()


async def create_session(
    user: User,
    db: AsyncSession,
    device_id: Optional[UUID] = None,
) -> TokenPair:
    """
    Create a new session: generate JWT pair, store hashed refresh token in DB.
    Returns the raw TokenPair (access + refresh tokens).
    """
    access_token = create_access_token(user.id, device_id=device_id)
    refresh_token = create_refresh_token(user.id, device_id=device_id)

    session = UserSession(
        user_id=user.id,
        device_id=device_id,
        refresh_token_hash=_hash_refresh_token(refresh_token),
        expires_at=datetime.now(timezone.utc) + timedelta(days=settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS),
    )
    db.add(session)
    await db.flush()

    return TokenPair(access_token=access_token, refresh_token=refresh_token)


async def refresh_session(
    raw_refresh_token: str,
    db: AsyncSession,
) -> TokenPair:
    """
    Validate a refresh token, rotate it (delete old session, create new), return new token pair.

    Raises ValueError on invalid / expired token.
    """
    token_hash = _hash_refresh_token(raw_refresh_token)
    now = datetime.now(timezone.utc)

    result = await db.execute(
        select(UserSession).where(
            UserSession.refresh_token_hash == token_hash,
            UserSession.expires_at > now,
        )
    )
    session = result.scalar_one_or_none()
    if not session:
        raise ValueError("Invalid or expired refresh token")

    # Load the associated user
    user_result = await db.execute(select(User).where(User.id == session.user_id))
    user = user_result.scalar_one_or_none()
    if not user or not user.is_active:
        raise ValueError("User not found or deactivated")

    device_id = session.device_id

    # Rotate: delete old session
    await db.delete(session)
    await db.flush()

    # Issue new token pair
    return await create_session(user, db, device_id=device_id)


async def invalidate_session(raw_refresh_token: str, db: AsyncSession) -> None:
    """Delete the session matching the given refresh token (logout)."""
    token_hash = _hash_refresh_token(raw_refresh_token)
    result = await db.execute(
        select(UserSession).where(UserSession.refresh_token_hash == token_hash)
    )
    session = result.scalar_one_or_none()
    if session:
        await db.delete(session)
        await db.flush()
