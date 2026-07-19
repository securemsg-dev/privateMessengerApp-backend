from __future__ import annotations
"""
app/api/v1/endpoints/messages.py
─────────────────────────────────
Per-message endpoints (Phase C.2 onwards):

  POST /messages/{id}/star    — star or unstar a message (caller-private)
  POST /messages/{id}/delete  — delete-for-me (hide) or delete-for-everyone
                                 (wipe payload + broadcast tombstone)

Reactions are intentionally NOT here — they fan out via WebSocket so peers
see them in real time (see app/websocket/router.py).
"""

from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select

from app.core.dependencies import CurrentUser, DBSession
from app.core.limiter import limiter
from app.db.models.conversation import conversation_participants
from app.db.models.deleted_message import DeletedMessage
from app.db.models.media_blob import MediaBlob
from app.db.models.message import MessageMetadata
from app.db.models.starred_message import StarredMessage
from app.schemas.messaging import DeleteMessageRequest, StarMessageRequest
from app.services.media_storage import get_storage
from app.websocket.manager import manager

# How long after sending a message you can still wipe it for everyone.
DELETE_FOR_EVERYONE_WINDOW = timedelta(hours=24)

router = APIRouter(prefix="/messages", tags=["Messages"])


async def _resolve_message_for_user(
    db,
    message_id: UUID,
    user_id: UUID,
) -> MessageMetadata:
    """
    Look up a message and verify the caller is a participant of its
    conversation. Raises 404 if the message doesn't exist and 403 if the
    caller isn't allowed to see it.
    """
    msg = (await db.execute(
        select(MessageMetadata).where(MessageMetadata.id == message_id)
    )).scalar_one_or_none()
    if not msg:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Message not found",
        )
    is_participant = (await db.execute(
        select(conversation_participants).where(
            conversation_participants.c.conversation_id == msg.conversation_id,
            conversation_participants.c.user_id == user_id,
        )
    )).first()
    if not is_participant:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can't act on this message",
        )
    return msg


@router.post(
    "/{message_id}/star",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Star or unstar a message (private to the caller)",
)
@limiter.limit("60/minute")
async def star_message(
    request: Request,
    message_id: UUID,
    body: StarMessageRequest,
    current_user: CurrentUser,
    db: DBSession,
) -> None:
    """
    Idempotent: setting `starred=true` twice is a no-op, same for `false`.
    Stars are private — no broadcast, no notification to other participants.
    """
    msg = await _resolve_message_for_user(db, message_id, current_user.id)

    existing = (await db.execute(
        select(StarredMessage).where(
            StarredMessage.user_id == current_user.id,
            StarredMessage.message_id == msg.id,
        )
    )).scalar_one_or_none()

    if body.starred and not existing:
        db.add(StarredMessage(user_id=current_user.id, message_id=msg.id))
    elif not body.starred and existing:
        await db.delete(existing)
    await db.flush()
    return None


@router.post(
    "/{message_id}/delete",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a message — for-me (hide) or for-everyone (wipe)",
)
@limiter.limit("30/minute")
async def delete_message(
    request: Request,
    message_id: UUID,
    body: DeleteMessageRequest,
    current_user: CurrentUser,
    db: DBSession,
) -> None:
    """
    `scope=me` — adds the message to the caller's hide set. Idempotent.
    `scope=everyone` — sender-only, within DELETE_FOR_EVERYONE_WINDOW (24h).
        Wipes encrypted_payload, sets deleted_at + deleted_by, and broadcasts
        a `deletion` event to every connected participant.
    """
    msg = await _resolve_message_for_user(db, message_id, current_user.id)

    if body.scope == "me":
        existing = (await db.execute(
            select(DeletedMessage).where(
                DeletedMessage.user_id == current_user.id,
                DeletedMessage.message_id == msg.id,
            )
        )).scalar_one_or_none()
        if not existing:
            db.add(DeletedMessage(
                user_id=current_user.id, message_id=msg.id,
            ))
            await db.flush()
        return None

    # scope == "everyone"
    if msg.sender_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the sender can delete a message for everyone",
        )
    if msg.deleted_at is not None:
        # Already wiped — no-op (idempotent), no need to re-broadcast
        return None
    age = datetime.now(timezone.utc) - msg.created_at
    if age > DELETE_FOR_EVERYONE_WINDOW:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This message is too old to delete for everyone "
                f"({DELETE_FOR_EVERYONE_WINDOW.total_seconds() / 3600:.0f}h limit)."
            ),
        )

    now = datetime.now(timezone.utc)
    msg.deleted_at = now
    msg.deleted_by = current_user.id
    msg.encrypted_payload = ""  # wipe so even DB dumps don't expose ciphertext

    # Media message: remove the ciphertext file + blob row too — otherwise the
    # bytes outlive the message forever on the storage volume.
    if msg.media_blob_id is not None:
        blob = (await db.execute(
            select(MediaBlob).where(MediaBlob.id == msg.media_blob_id)
        )).scalar_one_or_none()
        # Cleared explicitly (not left to FK SET NULL) so the tombstone row is
        # fully wiped on every backend, SQLite included.
        msg.media_blob_id = None
        if blob is not None:
            await get_storage().delete_bytes(blob.id)
            await db.delete(blob)
    await db.flush()

    # Broadcast tombstone to every connected client in this conversation.
    redis = request.app.state.redis
    event = {
        "type": "deletion",
        "conversation_id": str(msg.conversation_id),
        "message_id": str(msg.id),
        "by_user_id": str(current_user.id),
        "timestamp": now.isoformat(),
    }
    await manager.publish(redis, str(msg.conversation_id), event)
    return None
