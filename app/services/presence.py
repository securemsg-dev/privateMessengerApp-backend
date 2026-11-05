from __future__ import annotations
"""
app/services/presence.py
─────────────────────────
Online / last-seen presence.

  • "Online" = the user has the app in the foreground: an open /ws/user
    socket whose client last reported `active` (a fresh socket counts as
    active, so old builds behave as before).
  • The connection manager tracks that per socket and calls
    `on_presence_change` whenever a user's aggregate state flips.
  • Redis key presence:{uid} (TTL) is the shared "online" flag, so it
    survives multiple instances and self-heals after a crash; a loop
    refreshes it for locally active users.
  • users.last_seen_at is written when a user goes offline.

Visibility rules (applied to both snapshots and live updates):
  • show_online_status = false → always reported as offline, no last seen.
  • Blocked pairs (either direction) never see each other's presence.
  • Only 1:1 chat partners receive presence; groups show member counts.

Wire event (conversation channel, delivered only to sockets that connected
with ?presence=1):
    {"type": "presence", "user_id", "online": bool, "last_seen": iso | null}
"""

import asyncio
import logging
from datetime import datetime, timezone
from uuid import UUID

import redis.asyncio as aioredis
from sqlalchemy import or_, select

from app.db.models.blocked_user import BlockedUser
from app.db.models.conversation import Conversation, conversation_participants
from app.db.models.user import User
from app.db.session import AsyncSessionLocal
from app.services.moderation_service import blocks_exist_between
from app.websocket.manager import manager

logger = logging.getLogger(__name__)

PRESENCE_TTL_SECONDS = 90
REFRESH_INTERVAL_SECONDS = 30


def _key(user_id) -> str:
    return f"presence:{user_id}"


def _hidden_event(user_id) -> dict:
    return {"type": "presence", "user_id": str(user_id), "online": False, "last_seen": None}


async def is_online(redis: aioredis.Redis, user_id) -> bool:
    return bool(await redis.exists(_key(user_id)))


async def visible_event(redis: aioredis.Redis, user: User) -> dict:
    """The presence event as anyone allowed to see it would see it."""
    if not user.show_online_status:
        return _hidden_event(user.id)
    return {
        "type": "presence",
        "user_id": str(user.id),
        "online": await is_online(redis, user.id),
        "last_seen": user.last_seen_at.isoformat() if user.last_seen_at else None,
    }


async def snapshot_for(db, redis: aioredis.Redis, viewer_id: UUID, target_id: UUID) -> dict:
    """What `viewer_id` may see of `target_id` right now."""
    target = await db.get(User, target_id)
    if target is None or await blocks_exist_between(db, viewer_id, target_id):
        return _hidden_event(target_id)
    return await visible_event(redis, target)


async def publish_presence(db, redis: aioredis.Redis, user: User) -> None:
    """Send `user`'s current visible state to each 1:1 chat partner who may see it."""
    event = await visible_event(redis, user)

    me = conversation_participants.alias("me")
    peer = conversation_participants.alias("peer")
    rows = (await db.execute(
        select(me.c.conversation_id, peer.c.user_id)
        .join(Conversation, Conversation.id == me.c.conversation_id)
        .join(peer, (peer.c.conversation_id == me.c.conversation_id) & (peer.c.user_id != user.id))
        .where(me.c.user_id == user.id, Conversation.is_group.is_(False))
    )).all()
    if not rows:
        return

    blocked_pairs = (await db.execute(
        select(BlockedUser.blocker_id, BlockedUser.blocked_id).where(
            or_(BlockedUser.blocker_id == user.id, BlockedUser.blocked_id == user.id)
        )
    )).all()
    hidden_from = {b if a == user.id else a for a, b in blocked_pairs}

    for conv_id, peer_id in rows:
        if peer_id in hidden_from:
            continue
        await manager.publish(redis, str(conv_id), event)


async def on_presence_change(redis: aioredis.Redis, user_id: str, online: bool) -> None:
    """Manager hook: a user's aggregate active state flipped on this instance."""
    uid = UUID(user_id)
    async with AsyncSessionLocal() as db:
        user = await db.get(User, uid)
        if user is None:
            return
        if online:
            await redis.set(_key(uid), "1", ex=PRESENCE_TTL_SECONDS)
        else:
            await redis.delete(_key(uid))
            user.last_seen_at = datetime.now(timezone.utc)
            await db.commit()
        await publish_presence(db, redis, user)


async def run_presence_loop(redis: aioredis.Redis) -> None:
    """Keep presence keys alive for locally active users (TTL > interval)."""
    while True:
        await asyncio.sleep(REFRESH_INTERVAL_SECONDS)
        try:
            ids = manager.active_user_ids()
            if ids:
                pipe = redis.pipeline()
                for uid in ids:
                    pipe.set(_key(uid), "1", ex=PRESENCE_TTL_SECONDS)
                await pipe.execute()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("presence refresh failed")
