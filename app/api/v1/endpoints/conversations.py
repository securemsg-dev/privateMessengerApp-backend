from __future__ import annotations
"""
app/api/v1/endpoints/conversations.py
──────────────────────────────────────
Conversation endpoints (authenticated):

  POST /conversations
      Create-or-get a 1-to-1 conversation by the other user's private_number.
      Idempotent: returns the existing conversation if both users are already
      paired up, otherwise creates a new one and adds both as participants.

  GET  /conversations
      List the current user's conversations (sorted by latest activity desc),
      including the other participant's public profile and a last-message
      preview blob (still encrypted to the server).

  GET  /conversations/{id}/messages
      Paginated message history for a conversation the user is part of.
      Cursor pagination via ?before=<ISO8601>; default limit 50, max 200.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.exc import IntegrityError

from app.core.dependencies import CurrentUser, DBSession
from app.core.limiter import limiter
from app.db.models.blocked_user import BlockedUser
from app.db.models.conversation import Conversation, conversation_participants
from app.db.models.conversation_pref import ConversationPref
from app.db.models.deleted_message import DeletedMessage
from app.db.models.message import MessageMetadata, MessageType
from app.db.models.message_reaction import MessageReaction
from app.db.models.starred_message import StarredMessage
from app.db.models.user import User
from app.schemas.messaging import (
    ConversationCreateRequest,
    ConversationPrefsUpdate,
    ConversationResponse,
    GroupCreateRequest,
    GroupMember,
    GroupMembersAddRequest,
    GroupUpdateRequest,
    LastMessagePreview,
    MessagePage,
    MessageReplyPreview,
    MessageResponse,
    ReactionAggregate,
    UserPublic,
)
from app.services import group_service
from app.services.group_service import GroupError

# Per-user pin limit for the chat list. Mirrors the frontend's PIN_LIMIT.
PIN_LIMIT = 5

router = APIRouter(prefix="/conversations", tags=["Conversations"])

# Cap message page size to protect the server. Frontend default is 50.
MAX_MESSAGE_PAGE = 200
DEFAULT_MESSAGE_PAGE = 50


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _ensure_participant(
    db,
    conversation_id: UUID,
    user_id: UUID,
):
    """Return the caller's participant row; 403 if they aren't a member."""
    row = (await db.execute(
        select(conversation_participants).where(
            conversation_participants.c.conversation_id == conversation_id,
            conversation_participants.c.user_id == user_id,
        )
    )).first()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not a participant of this conversation",
        )
    return row


def _blocked_by(user_id: UUID):
    """Subquery: everyone `user_id` has blocked (hidden from them in groups)."""
    return select(BlockedUser.blocked_id).where(BlockedUser.blocker_id == user_id)


def _group_error(err: GroupError) -> HTTPException:
    return HTTPException(status_code=err.status_code, detail=err.detail)


async def _users_by_id(db, user_ids) -> dict[UUID, User]:
    ids = [u for u in set(user_ids) if u is not None]
    if not ids:
        return {}
    rows = (await db.execute(select(User).where(User.id.in_(ids)))).scalars().all()
    return {u.id: u for u in rows}


async def _load_pref(
    db,
    conversation_id: UUID,
    user_id: UUID,
) -> Optional[ConversationPref]:
    """Load the caller's prefs for this conversation, or None if never set."""
    result = await db.execute(
        select(ConversationPref).where(
            ConversationPref.conversation_id == conversation_id,
            ConversationPref.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


async def _unread_counts_for(
    db,
    conversation_ids: list[UUID],
    current_user_id: UUID,
) -> dict[UUID, int]:
    """
    One grouped query for unread counts across a batch of conversations.
    Saves us from N+1 when hydrating the full chat list.
    """
    if not conversation_ids:
        return {}
    rows = (await db.execute(
        select(
            MessageMetadata.conversation_id,
            func.count(MessageMetadata.id),
        )
        .where(
            MessageMetadata.conversation_id.in_(conversation_ids),
            MessageMetadata.sender_id != current_user_id,
            MessageMetadata.read_at.is_(None),
        )
        .group_by(MessageMetadata.conversation_id)
    )).all()
    return {conv_id: int(count) for conv_id, count in rows}


async def _group_unread_counts_for(
    db,
    conversation_ids: list[UUID],
    current_user_id: UUID,
) -> dict[UUID, int]:
    """
    Groups use a per-member read watermark (participants.last_read_at), not
    the message-level read_at — one member reading must not clear the badge
    for everyone. Unread = other people's messages after my watermark (or my
    join time), excluding system lines and people I've blocked.
    """
    if not conversation_ids:
        return {}
    me = conversation_participants
    rows = (await db.execute(
        select(MessageMetadata.conversation_id, func.count(MessageMetadata.id))
        .join(me, and_(
            me.c.conversation_id == MessageMetadata.conversation_id,
            me.c.user_id == current_user_id,
        ))
        .where(
            MessageMetadata.conversation_id.in_(conversation_ids),
            MessageMetadata.sender_id != current_user_id,
            MessageMetadata.message_type != MessageType.system,
            MessageMetadata.deleted_at.is_(None),
            MessageMetadata.created_at > func.coalesce(me.c.last_read_at, me.c.joined_at),
            MessageMetadata.sender_id.not_in(_blocked_by(current_user_id)),
        )
        .group_by(MessageMetadata.conversation_id)
    )).all()
    return {conv_id: int(count) for conv_id, count in rows}


async def _last_messages_for(
    db,
    conversation_ids: list[UUID],
    current_user_id: UUID,
) -> dict[UUID, MessageMetadata]:
    """
    One query for the newest message of EVERY conversation in the batch.
    Uses a row_number() window (portable across Postgres and the SQLite test
    DB) instead of one ORDER BY … LIMIT 1 query per conversation.
    """
    if not conversation_ids:
        return {}
    rn = (
        func.row_number()
        .over(
            partition_by=MessageMetadata.conversation_id,
            order_by=MessageMetadata.created_at.desc(),
        )
        .label("rn")
    )
    me = conversation_participants
    sub = (
        select(MessageMetadata.id.label("mid"), rn)
        .join(Conversation, Conversation.id == MessageMetadata.conversation_id)
        .join(me, and_(
            me.c.conversation_id == MessageMetadata.conversation_id,
            me.c.user_id == current_user_id,
        ))
        .where(
            MessageMetadata.conversation_id.in_(conversation_ids),
            # Groups: only what this member may see — nothing from before
            # they joined (not encrypted for them) or from people they block.
            or_(
                Conversation.is_group.is_(False),
                and_(
                    MessageMetadata.created_at >= me.c.joined_at,
                    MessageMetadata.sender_id.not_in(_blocked_by(current_user_id)),
                ),
            ),
        )
        .subquery()
    )
    rows = (await db.execute(
        select(MessageMetadata)
        .join(sub, MessageMetadata.id == sub.c.mid)
        .where(sub.c.rn == 1)
    )).scalars().all()
    return {m.conversation_id: m for m in rows}


async def _other_participants_for(
    db,
    conversation_ids: list[UUID],
    current_user_id: UUID,
) -> dict[UUID, User]:
    """The other participant of each 1:1 conversation (pass 1:1 ids only)."""
    if not conversation_ids:
        return {}
    rows = (await db.execute(
        select(conversation_participants.c.conversation_id, User)
        .join(User, conversation_participants.c.user_id == User.id)
        .where(
            conversation_participants.c.conversation_id.in_(conversation_ids),
            User.id != current_user_id,
        )
    )).all()
    out: dict[UUID, User] = {}
    for conv_id, user in rows:
        out.setdefault(conv_id, user)  # 1:1 conversations have exactly one other
    return out


async def _prefs_for(
    db,
    conversation_ids: list[UUID],
    user_id: UUID,
) -> dict[UUID, ConversationPref]:
    """Batch-load the caller's prefs for every conversation in the list."""
    if not conversation_ids:
        return {}
    rows = (await db.execute(
        select(ConversationPref).where(
            ConversationPref.user_id == user_id,
            ConversationPref.conversation_id.in_(conversation_ids),
        )
    )).scalars().all()
    return {p.conversation_id: p for p in rows}


async def _group_members_for(
    db,
    conversation_ids: list[UUID],
) -> dict[UUID, list[tuple[str, User]]]:
    """(role, user) for every member of each group, oldest member first."""
    if not conversation_ids:
        return {}
    cp = conversation_participants
    rows = (await db.execute(
        select(cp.c.conversation_id, cp.c.role, User)
        .join(User, User.id == cp.c.user_id)
        .where(cp.c.conversation_id.in_(conversation_ids))
        .order_by(cp.c.joined_at, cp.c.user_id)
    )).all()
    out: dict[UUID, list[tuple[str, User]]] = {}
    for conv_id, role, user in rows:
        out.setdefault(conv_id, []).append((role, user))
    return out


def _preview(msg: Optional[MessageMetadata], sender: Optional[User], is_group: bool):
    if msg is None:
        return None
    p = LastMessagePreview.model_validate(msg)
    if is_group and sender is not None:
        p.sender_display_name = sender.display_name
        p.sender_public_key = sender.public_key
    return p


async def _build_responses(
    db,
    convs: list[Conversation],
    current_user_id: UUID,
) -> list[ConversationResponse]:
    """
    Hydrate conversations for the chat list (or a single one) in a fixed
    number of batched queries, whatever the list length — each round trip
    costs WAN DB latency.
    """
    if not convs:
        return []
    ids = [c.id for c in convs]
    group_ids = [c.id for c in convs if c.is_group]
    direct_ids = [c.id for c in convs if not c.is_group]

    unread_map = await _unread_counts_for(db, direct_ids, current_user_id)
    unread_map.update(await _group_unread_counts_for(db, group_ids, current_user_id))
    last_map = await _last_messages_for(db, ids, current_user_id)
    other_map = await _other_participants_for(db, direct_ids, current_user_id)
    members_map = await _group_members_for(db, group_ids)
    pref_map = await _prefs_for(db, ids, current_user_id)
    group_set = set(group_ids)
    sender_map = await _users_by_id(
        db, [m.sender_id for cid, m in last_map.items() if cid in group_set],
    )

    out: list[ConversationResponse] = []
    for c in convs:
        last_msg = last_map.get(c.id)
        pref = pref_map.get(c.id)
        participants = None
        my_role = None
        other = None
        if c.is_group:
            members = members_map.get(c.id, [])
            participants = [
                GroupMember(
                    id=u.id,
                    display_name=u.display_name,
                    profile_picture_key=u.profile_picture_key,
                    public_key=u.public_key,
                    role=role,
                )
                for role, u in members
            ]
            my_role = next((role for role, u in members if u.id == current_user_id), None)
        else:
            other = other_map.get(c.id)
        out.append(ConversationResponse(
            id=c.id,
            is_group=c.is_group,
            name=c.name,
            created_at=c.created_at,
            other_participant=UserPublic.model_validate(other) if other else None,
            last_message=_preview(
                last_msg, sender_map.get(last_msg.sender_id) if last_msg else None, c.is_group,
            ),
            unread_count=unread_map.get(c.id, 0),
            participants=participants,
            my_role=my_role,
            is_pinned=bool(pref and pref.is_pinned),
            mute_until=pref.mute_until if pref else None,
            manual_unread=bool(pref and pref.manual_unread),
        ))
    return out


async def _hydrate_conversation(
    db,
    conv: Conversation,
    current_user_id: UUID,
) -> ConversationResponse:
    """Single-conversation version of _build_responses."""
    return (await _build_responses(db, [conv], current_user_id))[0]


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post(
    "",
    response_model=ConversationResponse,
    status_code=status.HTTP_200_OK,
    summary="Create or fetch a 1:1 conversation by other user's private number",
)
@limiter.limit("30/minute")
async def create_or_get_conversation(
    request: Request,
    body: ConversationCreateRequest,
    current_user: CurrentUser,
    db: DBSession,
) -> ConversationResponse:
    if body.other_private_number == current_user.private_number:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot start a conversation with yourself",
        )

    # 1. Resolve the other user
    other_q = await db.execute(
        select(User).where(
            User.private_number == body.other_private_number,
            User.is_active == True,  # noqa: E712
        )
    )
    other = other_q.scalar_one_or_none()
    if not other:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active user with that private number",
        )

    # 2. Look for an existing 1:1 conversation between these two users.
    # `is_group=False` semantically means exactly two participants, so we just
    # check the other user is a participant and the current user is too.
    existing_q = await db.execute(
        select(Conversation)
        .join(
            conversation_participants,
            conversation_participants.c.conversation_id == Conversation.id,
        )
        .where(
            Conversation.is_group == False,  # noqa: E712
            conversation_participants.c.user_id == other.id,
            Conversation.id.in_(
                select(conversation_participants.c.conversation_id).where(
                    conversation_participants.c.user_id == current_user.id,
                )
            ),
        )
        .limit(1)
    )
    existing = existing_q.scalar_one_or_none()
    if existing:
        return await _hydrate_conversation(db, existing, current_user.id)

    # 3. Create a new 1:1 conversation + add both participants. The unique
    # direct_key closes the race where both users create "the conversation
    # with each other" simultaneously and each passes the check above — the
    # loser's INSERT fails and we return the winner's row instead.
    # Captured before the flush: a rollback expires ORM objects, and touching
    # current_user afterwards would lazy-load outside the async greenlet.
    caller_id = current_user.id
    direct_key = ":".join(sorted((str(caller_id), str(other.id))))
    conv = Conversation(is_group=False, name=None, direct_key=direct_key)
    db.add(conv)
    try:
        await db.flush()  # assign conv.id; unique violation surfaces here
    except IntegrityError:
        await db.rollback()
        winner = (await db.execute(
            select(Conversation).where(Conversation.direct_key == direct_key)
        )).scalar_one()
        return await _hydrate_conversation(db, winner, caller_id)

    await db.execute(
        conversation_participants.insert().values(
            [
                {"conversation_id": conv.id, "user_id": current_user.id},
                {"conversation_id": conv.id, "user_id": other.id},
            ]
        )
    )
    await db.flush()
    return await _hydrate_conversation(db, conv, current_user.id)


@router.get(
    "",
    response_model=list[ConversationResponse],
    summary="List the current user's conversations",
)
@limiter.limit("60/minute")
async def list_conversations(
    request: Request,
    current_user: CurrentUser,
    db: DBSession,
    include_groups: bool = Query(
        False,
        description="Include group chats. Sent by app builds that support "
                    "groups; older builds never see them.",
    ),
) -> list[ConversationResponse]:
    if include_groups and not current_user.supports_groups:
        # This user now runs a groups-capable build, so they may be added.
        current_user.supports_groups = True
        await db.flush()

    q = (
        select(Conversation)
        .join(
            conversation_participants,
            conversation_participants.c.conversation_id == Conversation.id,
        )
        .where(conversation_participants.c.user_id == current_user.id)
        .order_by(desc(Conversation.created_at))
    )
    if not include_groups:
        q = q.where(Conversation.is_group.is_(False))
    convs = (await db.execute(q)).scalars().all()

    out = await _build_responses(db, list(convs), current_user.id)
    # Sort by last-message recency (falls back to conv.created_at when empty)
    out.sort(
        key=lambda c: c.last_message.created_at if c.last_message else c.created_at,
        reverse=True,
    )
    return out


async def _load_reactions_for(
    db,
    message_ids: list[UUID],
    current_user_id: UUID,
) -> dict[UUID, list[ReactionAggregate]]:
    """
    One-shot fetch of reactions for a batch of messages, aggregated by emoji.
    Returns `{message_id: [{emoji, count, by_me}, ...]}`. Saves us from N+1
    when hydrating a page of messages.
    """
    if not message_ids:
        return {}
    rows = (await db.execute(
        select(
            MessageReaction.message_id,
            MessageReaction.emoji,
            MessageReaction.user_id,
        ).where(MessageReaction.message_id.in_(message_ids))
    )).all()

    # Group: { msg_id: { emoji: {"count": N, "by_me": bool} } }
    grouped: dict[UUID, dict[str, dict]] = {}
    for msg_id, emoji, user_id in rows:
        per_emoji = grouped.setdefault(msg_id, {}).setdefault(
            emoji, {"count": 0, "by_me": False},
        )
        per_emoji["count"] += 1
        if user_id == current_user_id:
            per_emoji["by_me"] = True

    return {
        msg_id: [
            ReactionAggregate(emoji=e, count=info["count"], by_me=info["by_me"])
            for e, info in by_emoji.items()
        ]
        for msg_id, by_emoji in grouped.items()
    }


async def _load_starred_for(
    db,
    message_ids: list[UUID],
    current_user_id: UUID,
) -> set[UUID]:
    """Returns the subset of `message_ids` that the caller has starred."""
    if not message_ids:
        return set()
    rows = (await db.execute(
        select(StarredMessage.message_id).where(
            StarredMessage.user_id == current_user_id,
            StarredMessage.message_id.in_(message_ids),
        )
    )).scalars().all()
    return set(rows)


async def _load_reply_previews(
    db,
    reply_ids: list[UUID],
) -> dict[UUID, MessageReplyPreview]:
    """Batch-load the original messages referenced by `reply_to_id`."""
    if not reply_ids:
        return {}
    rows = (await db.execute(
        select(MessageMetadata).where(MessageMetadata.id.in_(reply_ids))
    )).scalars().all()
    return {m.id: MessageReplyPreview.model_validate(m) for m in rows}


@router.get(
    "/{conversation_id}/messages",
    response_model=MessagePage,
    summary="Paginated message history (newest first)",
)
@limiter.limit("120/minute")
async def list_messages(
    request: Request,
    conversation_id: UUID,
    current_user: CurrentUser,
    db: DBSession,
    before: Optional[datetime] = Query(
        None,
        description="ISO8601 cursor; returns messages strictly older than this timestamp",
    ),
    limit: int = Query(DEFAULT_MESSAGE_PAGE, ge=1, le=MAX_MESSAGE_PAGE),
) -> MessagePage:
    me = await _ensure_participant(db, conversation_id, current_user.id)
    conv = await db.get(Conversation, conversation_id)
    is_group = bool(conv and conv.is_group)

    q = select(MessageMetadata).where(
        MessageMetadata.conversation_id == conversation_id,
    )
    if is_group:
        # Nothing from before this member joined (it was never encrypted for
        # them) and nothing from people they've blocked.
        q = q.where(
            MessageMetadata.created_at >= me.joined_at,
            MessageMetadata.sender_id.not_in(_blocked_by(current_user.id)),
        )
    if before is not None:
        q = q.where(MessageMetadata.created_at < before)
    q = q.order_by(MessageMetadata.created_at.desc()).limit(limit)

    raw_rows = (await db.execute(q)).scalars().all()

    # Filter out messages this user has hidden via "delete for me". Done
    # in-memory because the user-scoped hide-set is small relative to a
    # single page of messages.
    hidden_ids = (await db.execute(
        select(DeletedMessage.message_id).where(
            DeletedMessage.user_id == current_user.id,
            DeletedMessage.message_id.in_([m.id for m in raw_rows]),
        )
    )).scalars().all() if raw_rows else []
    hidden_set = set(hidden_ids)
    rows = [m for m in raw_rows if m.id not in hidden_set]

    # Batch-hydrate reactions, star flag, and reply previews so we don't N+1
    msg_ids = [m.id for m in rows]
    reply_ids = [m.reply_to_id for m in rows if m.reply_to_id is not None]
    reactions_map = await _load_reactions_for(db, msg_ids, current_user.id)
    starred_set = await _load_starred_for(db, msg_ids, current_user.id)
    reply_previews = await _load_reply_previews(db, reply_ids)

    # Groups: each message is decrypted with its SENDER's key (and labelled
    # with their name) — senders may have left, so resolve them directly.
    senders: dict[UUID, User] = {}
    if is_group:
        senders = await _users_by_id(
            db,
            [m.sender_id for m in rows] + [p.sender_id for p in reply_previews.values()],
        )
        for p in reply_previews.values():
            u = senders.get(p.sender_id)
            if u is not None:
                p.sender_display_name = u.display_name
                p.sender_public_key = u.public_key

    messages = [
        MessageResponse(
            id=m.id,
            conversation_id=m.conversation_id,
            sender_id=m.sender_id,
            message_type=m.message_type.value if hasattr(m.message_type, "value") else m.message_type,
            encrypted_payload=m.encrypted_payload,
            created_at=m.created_at,
            delivered_at=m.delivered_at,
            read_at=m.read_at,
            self_destruct=m.self_destruct,
            reactions=reactions_map.get(m.id, []),
            is_starred=m.id in starred_set,
            reply_to_id=m.reply_to_id,
            reply_preview=reply_previews.get(m.reply_to_id) if m.reply_to_id else None,
            deleted_at=m.deleted_at,
            deleted_by=m.deleted_by,
            sender_display_name=senders[m.sender_id].display_name if m.sender_id in senders else None,
            sender_public_key=senders[m.sender_id].public_key if m.sender_id in senders else None,
        )
        for m in rows
    ]

    # Use the unfiltered tail for pagination so hidden messages don't
    # collapse the page below the user's requested limit.
    next_cursor = raw_rows[-1].created_at if len(raw_rows) == limit else None
    return MessagePage(messages=messages, next_cursor=next_cursor)


# ── Per-user prefs (Phase C.1) ────────────────────────────────────────────────

# A timestamp safely far in the future, used to represent "muted forever".
# Clients can detect this case by comparing mute_until > some-large-date.
_FOREVER = datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc)


@router.patch(
    "/{conversation_id}/prefs",
    response_model=ConversationResponse,
    status_code=status.HTTP_200_OK,
    summary="Update the caller's per-conversation preferences (pin / mute / mark unread)",
)
@limiter.limit("60/minute")
async def update_conversation_prefs(
    request: Request,
    conversation_id: UUID,
    body: ConversationPrefsUpdate,
    current_user: CurrentUser,
    db: DBSession,
) -> ConversationResponse:
    """
    Partial update — only fields explicitly present in the request body are
    applied. Returns the full hydrated `ConversationResponse` so the client
    can swap state in one go.
    """
    await _ensure_participant(db, conversation_id, current_user.id)
    fields = body.model_fields_set

    # Lazy-create the prefs row if this is the first time the user touches
    # this conversation's prefs.
    pref = await _load_pref(db, conversation_id, current_user.id)
    if pref is None:
        pref = ConversationPref(
            user_id=current_user.id,
            conversation_id=conversation_id,
        )
        db.add(pref)

    # Pin / unpin — enforce the per-user limit on pin
    if "is_pinned" in fields and body.is_pinned is not None:
        if body.is_pinned and not pref.is_pinned:
            current_pins = (await db.execute(
                select(func.count(ConversationPref.user_id)).where(
                    ConversationPref.user_id == current_user.id,
                    ConversationPref.is_pinned == True,  # noqa: E712
                )
            )).scalar() or 0
            if current_pins >= PIN_LIMIT:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Pin limit reached ({PIN_LIMIT}). Unpin another first.",
                )
        pref.is_pinned = body.is_pinned

    # Mute — `mute_seconds == 0` means unmute
    if "mute_seconds" in fields and body.mute_seconds is not None:
        if body.mute_seconds == 0:
            pref.mute_until = None
        else:
            now = datetime.now(timezone.utc)
            try:
                pref.mute_until = now + timedelta(seconds=body.mute_seconds)
            except OverflowError:
                # User asked for "mute forever" via huge seconds value
                pref.mute_until = _FOREVER

    # Manual unread toggle
    if "manual_unread" in fields and body.manual_unread is not None:
        pref.manual_unread = body.manual_unread

    await db.flush()
    # Reload the conversation row so the response includes the freshly
    # persisted prefs alongside the rest of the conversation snapshot.
    conv = (await db.execute(
        select(Conversation).where(Conversation.id == conversation_id)
    )).scalar_one()
    return await _hydrate_conversation(db, conv, current_user.id)


# ── C.4 — Leave / delete conversation ────────────────────────────────────────

@router.delete(
    "/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Leave a conversation (remove self from participants)",
)
@limiter.limit("30/minute")
async def leave_conversation(
    request: Request,
    conversation_id: UUID,
    current_user: CurrentUser,
    db: DBSession,
) -> None:
    """
    Remove the caller from `conversation_participants`. The conversation row
    and its messages are intentionally left on the server — encrypted blobs
    are meaningless to the server, and the other participant keeps their
    history. When both participants have left, the row is orphaned but
    harmless; it will be cleaned up by FK cascade when users are deleted.

    Caller's `ConversationPref` row is wiped alongside the participant row
    to avoid orphaned pref data accumulating.
    """
    await _ensure_participant(db, conversation_id, current_user.id)

    conv = await db.get(Conversation, conversation_id)
    if conv is not None and conv.is_group:
        # Groups: admin hand-over, system line, empty-group cleanup, socket
        # kick — all owned by the group service.
        try:
            change = await group_service.leave_group(db, current_user, conversation_id)
        except GroupError as err:
            raise _group_error(err)
        await db.commit()
        await group_service.publish_change(request.app.state.redis, change)
        return None

    await db.execute(
        conversation_participants.delete().where(
            conversation_participants.c.conversation_id == conversation_id,
            conversation_participants.c.user_id == current_user.id,
        )
    )

    pref = await _load_pref(db, conversation_id, current_user.id)
    if pref:
        await db.delete(pref)

    await db.flush()
    return None


# ── Group chat ────────────────────────────────────────────────────────────────

@router.post(
    "/groups",
    response_model=ConversationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a group chat with some of your contacts",
)
@limiter.limit("10/minute")
async def create_group(
    request: Request,
    body: GroupCreateRequest,
    current_user: CurrentUser,
    db: DBSession,
) -> ConversationResponse:
    caller_id = current_user.id
    try:
        conv, change = await group_service.create_group(db, current_user, body.name, body.member_ids)
    except GroupError as err:
        raise _group_error(err)
    await db.commit()
    await group_service.publish_change(request.app.state.redis, change)
    return await _hydrate_conversation(db, conv, caller_id)


@router.get(
    "/{conversation_id}",
    response_model=ConversationResponse,
    summary="Fetch one conversation (e.g. after a group_update event)",
)
@limiter.limit("120/minute")
async def get_conversation(
    request: Request,
    conversation_id: UUID,
    current_user: CurrentUser,
    db: DBSession,
) -> ConversationResponse:
    await _ensure_participant(db, conversation_id, current_user.id)
    conv = await db.get(Conversation, conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return await _hydrate_conversation(db, conv, current_user.id)


@router.patch(
    "/{conversation_id}",
    response_model=ConversationResponse,
    summary="Rename a group (admins only)",
)
@limiter.limit("30/minute")
async def update_group(
    request: Request,
    conversation_id: UUID,
    body: GroupUpdateRequest,
    current_user: CurrentUser,
    db: DBSession,
) -> ConversationResponse:
    caller_id = current_user.id
    try:
        change = await group_service.rename_group(db, current_user, conversation_id, body.name)
    except GroupError as err:
        raise _group_error(err)
    await db.commit()
    await group_service.publish_change(request.app.state.redis, change)
    conv = await db.get(Conversation, conversation_id)
    return await _hydrate_conversation(db, conv, caller_id)


@router.post(
    "/{conversation_id}/members",
    response_model=ConversationResponse,
    summary="Add contacts to a group (admins only)",
)
@limiter.limit("30/minute")
async def add_group_members(
    request: Request,
    conversation_id: UUID,
    body: GroupMembersAddRequest,
    current_user: CurrentUser,
    db: DBSession,
) -> ConversationResponse:
    caller_id = current_user.id
    try:
        change = await group_service.add_members(db, current_user, conversation_id, body.member_ids)
    except GroupError as err:
        raise _group_error(err)
    await db.commit()
    await group_service.publish_change(request.app.state.redis, change)
    conv = await db.get(Conversation, conversation_id)
    return await _hydrate_conversation(db, conv, caller_id)


@router.delete(
    "/{conversation_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a member (admins), or yourself (= leave the group)",
)
@limiter.limit("30/minute")
async def remove_group_member(
    request: Request,
    conversation_id: UUID,
    user_id: UUID,
    current_user: CurrentUser,
    db: DBSession,
) -> None:
    try:
        change = await group_service.remove_member(db, current_user, conversation_id, user_id)
    except GroupError as err:
        raise _group_error(err)
    await db.commit()
    await group_service.publish_change(request.app.state.redis, change)
    return None
