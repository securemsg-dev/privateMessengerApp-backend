from __future__ import annotations
"""
app/services/group_service.py
──────────────────────────────
Group chat membership. The ONE place that creates groups and changes who is
in them — the REST endpoints, "leave" from the chat list, and account
deletion all go through here so the rules can't drift apart:

  • The creator is the first admin. When the last admin leaves, the
    longest-standing member is promoted. A group nobody is left in is deleted.
  • Only admins add / remove others or rename; anyone can leave.
  • New members must be the adder's contacts (share a 1:1 chat), have an
    E2EE public key, run an app build that understands groups, and have no
    block in either direction with the adder.
  • Every change writes a `system` message (plaintext JSON — membership is
    already visible to the server) and notifies clients:
        conversation channel → the system message + {"type": "group_update"}
        each affected user   → {"type": "conversation_update"}
    Removed members' open sockets on the group are closed (code 4403).

Messages themselves stay E2EE: clients wrap each message key for exactly
the current member set, which the WS layer enforces.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional
from uuid import UUID

import redis.asyncio as aioredis
from sqlalchemy import and_, delete, insert, or_, select, update

from app.core.config import settings
from app.db.models.blocked_user import BlockedUser
from app.db.models.conversation import Conversation, conversation_participants
from app.db.models.conversation_pref import ConversationPref
from app.db.models.message import MessageMetadata, MessageType
from app.db.models.user import User
from app.websocket.manager import manager

logger = logging.getLogger(__name__)

ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"
cp = conversation_participants


class GroupError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass
class GroupChange:
    """What happened, for publishing once the transaction has committed."""
    conversation_id: UUID
    system_message: Optional[MessageMetadata] = None
    #: Users whose chat list must refresh (added, removed, everyone on rename…).
    notify_user_ids: set[UUID] = field(default_factory=set)
    #: Users whose sockets on this group must be closed.
    removed_user_ids: set[UUID] = field(default_factory=set)
    #: True when the group no longer exists.
    deleted: bool = False


# ── Queries ───────────────────────────────────────────────────────────────────

async def load_group(db, conversation_id: UUID) -> Conversation:
    conv = await db.get(Conversation, conversation_id)
    if conv is None or not conv.is_group:
        raise GroupError(404, "Group not found")
    return conv


async def member_rows(db, conversation_id: UUID):
    """(user_id, role, joined_at) for every member, oldest first."""
    return (await db.execute(
        select(cp.c.user_id, cp.c.role, cp.c.joined_at)
        .where(cp.c.conversation_id == conversation_id)
        .order_by(cp.c.joined_at, cp.c.user_id)
    )).all()


async def role_of(db, conversation_id: UUID, user_id: UUID) -> Optional[str]:
    return (await db.execute(
        select(cp.c.role).where(cp.c.conversation_id == conversation_id, cp.c.user_id == user_id)
    )).scalar_one_or_none()


async def _contacts_among(db, user_id: UUID, candidates: set[UUID]) -> set[UUID]:
    """Candidates who share a 1:1 conversation with `user_id`."""
    if not candidates:
        return set()
    mine = cp.alias("mine")
    theirs = cp.alias("theirs")
    rows = (await db.execute(
        select(theirs.c.user_id)
        .join(mine, mine.c.conversation_id == theirs.c.conversation_id)
        .join(Conversation, Conversation.id == mine.c.conversation_id)
        .where(
            mine.c.user_id == user_id,
            theirs.c.user_id.in_(candidates),
            Conversation.is_group.is_(False),
        )
    )).scalars().all()
    return set(rows)


async def _blocked_among(db, user_id: UUID, candidates: set[UUID]) -> set[UUID]:
    """Candidates with a block in either direction with `user_id`."""
    if not candidates:
        return set()
    rows = (await db.execute(
        select(BlockedUser.blocker_id, BlockedUser.blocked_id).where(
            or_(
                and_(BlockedUser.blocker_id == user_id, BlockedUser.blocked_id.in_(candidates)),
                and_(BlockedUser.blocked_id == user_id, BlockedUser.blocker_id.in_(candidates)),
            )
        )
    )).all()
    return {b if a == user_id else a for a, b in rows}


async def _validate_new_members(db, actor: User, candidate_ids: Iterable[UUID]) -> list[User]:
    ids = {uid for uid in candidate_ids if uid != actor.id}
    if not ids:
        raise GroupError(422, "Pick at least one other person")

    users = (await db.execute(
        select(User).where(User.id.in_(ids), User.is_active.is_(True))
    )).scalars().all()
    by_id = {u.id: u for u in users}
    contacts = await _contacts_among(db, actor.id, set(by_id))
    if len(by_id) != len(ids) or contacts != set(by_id):
        # Unknown ids and non-contacts look the same: no existence oracle.
        raise GroupError(403, "You can only add people you already chat with")
    if await _blocked_among(db, actor.id, ids):
        raise GroupError(403, "Some of the selected people can't be added to a group")

    no_key = [u for u in users if not u.public_key]
    if no_key:
        raise GroupError(422, f"{_name(no_key[0])} hasn't set up encryption yet")
    outdated = [u for u in users if not u.supports_groups]
    if outdated:
        names = ", ".join(_name(u) for u in outdated[:3])
        raise GroupError(409, f"{names} needs to update Cricchat before joining groups")
    return sorted(users, key=lambda u: (_name(u).lower(), str(u.id)))


def _name(u: Optional[User]) -> str:
    if u is None:
        return "Someone"
    return u.display_name or "Someone"


# ── System messages ───────────────────────────────────────────────────────────

def _system_message(
    db,
    conversation_id: UUID,
    *,
    sender_id: UUID,
    event: str,
    actor: Optional[User],
    targets: Iterable[User] = (),
    name: Optional[str] = None,
) -> MessageMetadata:
    targets = list(targets)
    payload = {
        "event": event,
        "actor_id": str(actor.id) if actor else None,
        "actor_name": _name(actor) if actor else None,
        "target_ids": [str(t.id) for t in targets],
        "target_names": [_name(t) for t in targets],
    }
    if name is not None:
        payload["name"] = name
    msg = MessageMetadata(
        conversation_id=conversation_id,
        sender_id=sender_id,
        message_type=MessageType.system,
        encrypted_payload=json.dumps(payload, ensure_ascii=False),
        self_destruct=False,
    )
    db.add(msg)
    return msg


# ── Mutations (caller commits, then calls publish_change) ────────────────────

async def create_group(db, creator: User, name: str, member_ids: Iterable[UUID]) -> tuple[Conversation, GroupChange]:
    members = await _validate_new_members(db, creator, member_ids)
    if len(members) + 1 > settings.GROUP_MAX_MEMBERS:
        raise GroupError(422, f"Groups can have at most {settings.GROUP_MAX_MEMBERS} members")
    if not creator.public_key:
        raise GroupError(422, "Set up encryption before creating a group")

    conv = Conversation(is_group=True, name=name, created_by=creator.id)
    db.add(conv)
    await db.flush()
    now = datetime.now(timezone.utc)
    await db.execute(insert(cp), [
        {"conversation_id": conv.id, "user_id": creator.id, "role": ROLE_ADMIN, "joined_at": now},
        *[
            {"conversation_id": conv.id, "user_id": m.id, "role": ROLE_MEMBER, "joined_at": now}
            for m in members
        ],
    ])
    msg = _system_message(
        db, conv.id, sender_id=creator.id, event="group_created",
        actor=creator, targets=members, name=name,
    )
    await db.flush()
    return conv, GroupChange(
        conversation_id=conv.id,
        system_message=msg,
        notify_user_ids={m.id for m in members} | {creator.id},
    )


async def _require_admin(db, conversation_id: UUID, user_id: UUID) -> None:
    role = await role_of(db, conversation_id, user_id)
    if role is None:
        raise GroupError(403, "You are not a member of this group")
    if role != ROLE_ADMIN:
        raise GroupError(403, "Only group admins can do that")


async def add_members(db, actor: User, conversation_id: UUID, member_ids: Iterable[UUID]) -> GroupChange:
    await load_group(db, conversation_id)
    await _require_admin(db, conversation_id, actor.id)
    existing = {r.user_id for r in await member_rows(db, conversation_id)}
    new_ids = {uid for uid in member_ids if uid not in existing}
    new_members = await _validate_new_members(db, actor, new_ids)
    if len(existing) + len(new_members) > settings.GROUP_MAX_MEMBERS:
        raise GroupError(422, f"Groups can have at most {settings.GROUP_MAX_MEMBERS} members")

    now = datetime.now(timezone.utc)
    await db.execute(insert(cp), [
        {"conversation_id": conversation_id, "user_id": m.id, "role": ROLE_MEMBER, "joined_at": now}
        for m in new_members
    ])
    msg = _system_message(
        db, conversation_id, sender_id=actor.id, event="members_added",
        actor=actor, targets=new_members,
    )
    await db.flush()
    return GroupChange(
        conversation_id=conversation_id,
        system_message=msg,
        notify_user_ids={m.id for m in new_members},
    )


async def _detach(db, conversation_id: UUID, user_id: UUID) -> tuple[list, Optional[UUID], bool]:
    """
    Remove one member. Returns (remaining_rows, promoted_user_id, deleted).
    Promotes the longest-standing member when no admin is left; deletes the
    group (and its history) when nobody is left.
    """
    await db.execute(delete(cp).where(cp.c.conversation_id == conversation_id, cp.c.user_id == user_id))
    await db.execute(delete(ConversationPref).where(
        ConversationPref.conversation_id == conversation_id, ConversationPref.user_id == user_id,
    ))
    remaining = await member_rows(db, conversation_id)
    if not remaining:
        conv = await db.get(Conversation, conversation_id)
        if conv is not None:
            await db.delete(conv)
        await db.flush()
        return [], None, True

    promoted: Optional[UUID] = None
    if not any(r.role == ROLE_ADMIN for r in remaining):
        promoted = remaining[0].user_id  # oldest by joined_at
        await db.execute(
            update(cp)
            .where(cp.c.conversation_id == conversation_id, cp.c.user_id == promoted)
            .values(role=ROLE_ADMIN)
        )
    return remaining, promoted, False


async def remove_member(db, actor: User, conversation_id: UUID, target_id: UUID) -> GroupChange:
    """Admin removes someone else, or anyone removes themselves (= leave)."""
    if target_id == actor.id:
        return await leave_group(db, actor, conversation_id)
    await load_group(db, conversation_id)
    await _require_admin(db, conversation_id, actor.id)
    if await role_of(db, conversation_id, target_id) is None:
        raise GroupError(404, "That person isn't in this group")
    target = await db.get(User, target_id)

    _, _, deleted = await _detach(db, conversation_id, target_id)
    change = GroupChange(
        conversation_id=conversation_id,
        notify_user_ids={target_id},
        removed_user_ids={target_id},
        deleted=deleted,
    )
    if not deleted:
        change.system_message = _system_message(
            db, conversation_id, sender_id=actor.id, event="member_removed",
            actor=actor, targets=[target] if target else [],
        )
    await db.flush()
    return change


async def leave_group(db, user: User, conversation_id: UUID) -> GroupChange:
    await load_group(db, conversation_id)
    if await role_of(db, conversation_id, user.id) is None:
        raise GroupError(403, "You are not a member of this group")

    remaining, promoted, deleted = await _detach(db, conversation_id, user.id)
    change = GroupChange(
        conversation_id=conversation_id,
        notify_user_ids={user.id},
        removed_user_ids={user.id},
        deleted=deleted,
    )
    if not deleted:
        # The leaver can't author a row in a group they're no longer in, so
        # a remaining member "sends" it; the JSON names the real actor.
        change.system_message = _system_message(
            db, conversation_id, sender_id=promoted or remaining[0].user_id,
            event="member_left", actor=user,
        )
        if promoted:
            change.notify_user_ids.add(promoted)
    await db.flush()
    return change


async def rename_group(db, actor: User, conversation_id: UUID, name: str) -> GroupChange:
    conv = await load_group(db, conversation_id)
    await _require_admin(db, conversation_id, actor.id)
    if conv.name == name:
        return GroupChange(conversation_id=conversation_id)
    conv.name = name
    msg = _system_message(
        db, conversation_id, sender_id=actor.id, event="renamed", actor=actor, name=name,
    )
    members = await member_rows(db, conversation_id)
    await db.flush()
    return GroupChange(
        conversation_id=conversation_id,
        system_message=msg,
        notify_user_ids={r.user_id for r in members},
    )


async def leave_all_groups_for_deleted_account(db, user: User) -> None:
    """
    Account deletion: leave every group first so admin hand-over and empty-
    group cleanup still happen (a bare FK cascade would skip both). No live
    publish — remaining members pick the change up on their next refresh.
    """
    group_ids = (await db.execute(
        select(cp.c.conversation_id)
        .join(Conversation, Conversation.id == cp.c.conversation_id)
        .where(cp.c.user_id == user.id, Conversation.is_group.is_(True))
    )).scalars().all()
    for gid in group_ids:
        try:
            await leave_group(db, user, gid)
        except GroupError:
            continue


# ── Publishing (after commit) ─────────────────────────────────────────────────

def system_message_event(msg: MessageMetadata) -> dict:
    return {
        "type": "message",
        "conversation_id": str(msg.conversation_id),
        "sender_id": str(msg.sender_id),
        "message_id": str(msg.id),
        "client_temp_id": None,
        "encrypted_payload": msg.encrypted_payload,
        "message_type": MessageType.system.value,
        "self_destruct": False,
        "reply_to_id": None,
        "timestamp": msg.created_at.isoformat(),
    }


async def publish_change(redis: aioredis.Redis, change: GroupChange) -> None:
    """Fan the change out. Best-effort: the DB is already committed."""
    conv_id = str(change.conversation_id)
    try:
        if change.system_message is not None:
            await manager.publish(redis, conv_id, system_message_event(change.system_message))
        update_event: dict = {"type": "group_update", "conversation_id": conv_id}
        if change.removed_user_ids:
            # Server-internal hint: the manager closes these users' sockets
            # on this conversation (after delivering this frame) and strips
            # the key before anything reaches a client.
            update_event["_close_user_ids"] = [str(u) for u in change.removed_user_ids]
        await manager.publish(redis, conv_id, update_event)
        for uid in change.notify_user_ids:
            await manager.publish_to_user(redis, str(uid), {
                "type": "conversation_update",
                "conversation_id": conv_id,
                "removed": uid in change.removed_user_ids or change.deleted,
            })
    except Exception:
        logger.exception("group change publish failed conv=%s", conv_id)
