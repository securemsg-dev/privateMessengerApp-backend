from __future__ import annotations
from typing import Optional, Union, Any
"""
app/services/device_service.py
────────────────────────────────
Business logic for device registration and management.
"""

import logging
from uuid import UUID

from sqlalchemy import select
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
