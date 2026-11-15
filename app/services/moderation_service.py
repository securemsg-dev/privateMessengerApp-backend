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

from app.core.cache import cache_delete, cache_get, cache_set
from app.db.models.blocked_user import BlockedUser
from app.db.models.conversation import conversation_participants

# Member lists change rarely (group add/remove/leave, 1:1 leave, account
# delete) and every one of those writes calls forget_participants() after its
# commit, so the TTL is only the safety net for a missed invalidation.
MEMBERS_CACHE_TTL_SECONDS = 300


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
    """All user ids participating in a conversation (always from Postgres)."""
    rows = (await db.execute(
        select(conversation_participants.c.user_id).where(
            conversation_participants.c.conversation_id == conversation_id,
        )
    )).scalars().all()
    return set(rows)


def _members_key(conversation_id) -> str:
    return f"conv:{conversation_id}:members"


async def cached_participant_ids(
    db: AsyncSession,
    redis,
    conversation_id: uuid.UUID,
    *,
    fresh: bool = False,
) -> set[uuid.UUID]:
    """
    participant_ids() through the Redis cache — saves a Postgres round trip on
    every message send, reaction and new-message fan-out.

    The cache can trail a membership change by a moment (a reader racing the
    write's invalidation). Callers that REJECT something because of
    membership must confirm with `fresh=True` first; that re-reads Postgres
    and overwrites the cache, so a stale entry heals on first contradiction.
    """
    key = _members_key(conversation_id)
    if not fresh:
        hit = await cache_get(redis, key)
        if isinstance(hit, list):
            try:
                return {uuid.UUID(x) for x in hit}
            except (TypeError, ValueError):
                pass  # malformed entry → reload
    members = await participant_ids(db, conversation_id)
    await cache_set(
        redis, key, sorted(str(m) for m in members), ttl=MEMBERS_CACHE_TTL_SECONDS,
    )
    return members


async def forget_participants(redis, *conversation_ids) -> None:
    """Invalidate cached member lists. Call AFTER the membership write commits."""
    await cache_delete(redis, *[_members_key(c) for c in conversation_ids])
