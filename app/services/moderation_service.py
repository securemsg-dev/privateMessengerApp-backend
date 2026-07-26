from __future__ import annotations
"""
app/services/moderation_service.py
───────────────────────────────────
Shared block-checking helpers used by every delivery path (chat messages,
call setup, call signaling).

Design note — blocks are checked at SEND time, not read time. A blocked
message is never persisted, so it can never resurface later through history
pagination or an unblock. The sender still gets their event echoed back to
their own socket, so their UI shows the message as sent and they are never
told they have been blocked (telling them is a safety problem, not a feature).
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.blocked_user import BlockedUser
from app.db.models.conversation import conversation_participants


async def is_blocked(
    db: AsyncSession,
    blocker_id: uuid.UUID,
    blocked_id: uuid.UUID,
) -> bool:
    """True if `blocker_id` has blocked `blocked_id` (one direction only)."""
    row = (await db.execute(
        select(BlockedUser.id).where(
            BlockedUser.blocker_id == blocker_id,
            BlockedUser.blocked_id == blocked_id,
        )
    )).first()
    return row is not None


async def blocks_exist_between(
    db: AsyncSession,
    user_a: uuid.UUID,
    user_b: uuid.UUID,
) -> bool:
    """
    True if EITHER user has blocked the other.

    Used for calls: if I blocked you I don't want your call, and if you
    blocked me I must not be able to ring you.
    """
    row = (await db.execute(
        select(BlockedUser.id).where(
            ((BlockedUser.blocker_id == user_a) & (BlockedUser.blocked_id == user_b))
            | ((BlockedUser.blocker_id == user_b) & (BlockedUser.blocked_id == user_a))
        )
    )).first()
    return row is not None


async def recipients_blocking(
    db: AsyncSession,
    conversation_id: uuid.UUID,
    sender_id: uuid.UUID,
) -> set[uuid.UUID]:
    """
    Every participant of `conversation_id` (excluding the sender) who has
    blocked the sender.

    For a 1-to-1 chat a non-empty result means "drop this message entirely".
    For a group it identifies which members must be skipped, so one blocker
    doesn't silence the sender for the whole group.
    """
    rows = (await db.execute(
        select(BlockedUser.blocker_id)
        .join(
            conversation_participants,
            conversation_participants.c.user_id == BlockedUser.blocker_id,
        )
        .where(
            conversation_participants.c.conversation_id == conversation_id,
            BlockedUser.blocked_id == sender_id,
            BlockedUser.blocker_id != sender_id,
        )
    )).scalars().all()
    return set(rows)


async def participant_ids(
    db: AsyncSession,
    conversation_id: uuid.UUID,
) -> set[uuid.UUID]:
    """All user ids participating in a conversation."""
    rows = (await db.execute(
        select(conversation_participants.c.user_id).where(
            conversation_participants.c.conversation_id == conversation_id,
        )
    )).scalars().all()
    return set(rows)
