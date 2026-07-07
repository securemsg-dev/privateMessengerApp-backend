from __future__ import annotations
from typing import Optional, Union, Any
"""
app/services/device_service.py
────────────────────────────────
Business logic for device registration and management.
"""

import logging
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.device import Device, DevicePlatform

logger = logging.getLogger(__name__)


async def register_device(
    user_id: UUID,
    device_name: str,
    platform: DevicePlatform,
    db: AsyncSession,
    push_token: Optional[str] = None,
    public_key: Optional[str] = None,
) -> Device:
    """
    Register (or update) a device for the given user.
    If a device with the same push_token already exists for this user, update it.
    """
    existing: Optional[Device] = None

    if push_token:
        # A push token identifies a PHYSICAL device. If another account still
        # holds this token (previous user logged out / switched accounts on
        # this phone), delete those rows — otherwise the previous account's
        # notifications (sender name + private number) keep appearing on a
        # phone that now belongs to someone else.
        evicted = await db.execute(
            delete(Device).where(
                Device.push_token == push_token,
                Device.user_id != user_id,
            )
        )
        if evicted.rowcount:
            logger.info(
                "Reclaimed push token from %d device row(s) of other users",
                evicted.rowcount,
            )

        result = await db.execute(
            select(Device).where(
                Device.user_id == user_id,
                Device.push_token == push_token,
            )
        )
        existing = result.scalar_one_or_none()

    if existing:
        existing.device_name = device_name
        existing.platform = platform
        if public_key:
            existing.public_key = public_key
        await db.flush()
        logger.info("Updated device %s for user %s", existing.id, user_id)
        return existing

    device = Device(
        user_id=user_id,
        device_name=device_name,
        platform=platform,
        push_token=push_token,
        public_key=public_key,
    )
    db.add(device)
    await db.flush()
    logger.info("Registered new device %s for user %s", device.id, user_id)
    return device


async def list_user_devices(user_id: UUID, db: AsyncSession) -> list[Device]:
    result = await db.execute(
        select(Device).where(Device.user_id == user_id).order_by(Device.created_at.desc())
    )
    return list(result.scalars().all())


async def clear_push_token(user_id: UUID, push_token: str, db: AsyncSession) -> int:
    """
    Detach a push token from the caller's devices (logout flow). The device
    rows stay (they hold the E2EE public key); only the token is nulled so
    no further notifications reach a phone the user has signed out of.
    Returns the number of rows updated.
    """
    result = await db.execute(
        update(Device)
        .where(Device.user_id == user_id, Device.push_token == push_token)
        .values(push_token=None)
    )
    await db.flush()
    if result.rowcount:
        logger.info("Cleared push token on %d device(s) for user %s", result.rowcount, user_id)
    return result.rowcount or 0
