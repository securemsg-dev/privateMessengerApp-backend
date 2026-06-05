from __future__ import annotations
"""
app/services/push_service.py
─────────────────────────────
Send push notifications via the Expo Push API.

Architecture:
  - No APNs/FCM credentials needed — Expo's hosted service handles routing
    to Apple and Google on behalf of your Expo project.
  - Tokens stored in `devices.push_token` are ExponentPushToken[...] strings
    obtained by the client via expo-notifications.
  - Server sends a POST to https://exp.host/--/api/v2/push/send with up to
    100 receipts per batch.
  - `DeviceNotRegistered` receipts → token deleted from DB (keeps table clean).

Privacy note:
  Notification body NEVER contains plaintext message content — the app is
  E2EE so the server has no plaintext to include. Body is always a generic
  string like "New message" so Expo's servers never see user content.
"""

import logging
from uuid import UUID

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.device import Device

logger = logging.getLogger(__name__)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
_BATCH_SIZE = 100


async def _send_expo_batch(
    notifications: list[dict],
    http: httpx.AsyncClient,
    db: AsyncSession,
    token_to_device_id: dict[str, UUID],
) -> None:
    """POST one batch to the Expo Push API and clean up stale tokens."""
    try:
        resp = await http.post(
            EXPO_PUSH_URL,
            json=notifications,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=10.0,
        )
        resp.raise_for_status()
        result = resp.json()
    except Exception as exc:
        logger.warning("[push] Expo batch failed: %s", exc)
        return

    stale_tokens: list[str] = []
    for item, notif in zip(result.get("data", []), notifications):
        if item.get("status") == "error":
            details = item.get("details", {})
            if details.get("error") == "DeviceNotRegistered":
                stale_tokens.append(notif["to"])
            else:
                logger.warning("[push] Expo error for token %s: %s", notif["to"], item)

    if stale_tokens:
        stale_ids = [token_to_device_id[t] for t in stale_tokens if t in token_to_device_id]
        if stale_ids:
            await db.execute(delete(Device).where(Device.id.in_(stale_ids)))
            await db.commit()
            logger.info("[push] Removed %d stale push tokens", len(stale_ids))


async def send_push_to_users(
    user_ids: list[UUID],
    title: str,
    body: str,
    data: dict,
    db: AsyncSession,
) -> None:
    """
    Send a push notification to all devices belonging to `user_ids`.
    Silently no-ops if no push tokens are registered.
    """
    if not user_ids:
        return

    rows = (
        await db.execute(
            select(Device.id, Device.push_token).where(
                Device.user_id.in_(user_ids),
                Device.push_token.is_not(None),
            )
        )
    ).all()

    if not rows:
        return

    token_to_device_id: dict[str, UUID] = {
        row.push_token: row.id for row in rows if row.push_token
    }
    tokens = list(token_to_device_id.keys())

    notifications = [
        {
            "to": token,
            "title": title,
            "body": body,
            "data": data,
            "sound": "default",
            "priority": "high",
        }
        for token in tokens
    ]

    async with httpx.AsyncClient() as http:
        for i in range(0, len(notifications), _BATCH_SIZE):
            await _send_expo_batch(
                notifications[i : i + _BATCH_SIZE],
                http,
                db,
                token_to_device_id,
            )
