from __future__ import annotations
"""
app/api/v1/endpoints/messages.py
─────────────────────────────────
Per-message endpoints (Phase C.2 onwards):

  POST /messages/{id}/star    — star or unstar a message (caller-private)
  POST /messages/{id}/delete  — delete-for-me (hide) or delete-for-everyone
                                 (wipe payload + broadcast tombstone)
  POST /messages/{id}/opened  — recipient opened a view-once photo/video:
                                 wipe payload + blob, broadcast "opened"

Reactions are intentionally NOT here — they fan out via WebSocket so peers
see them in real time (see app/websocket/router.py).
"""

import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select, update

from app.core.dependencies import CurrentUser, DBSession
from app.core.limiter import limiter
from app.db.models.conversation import conversation_participants
from app.db.models.deleted_message import DeletedMessage
from app.db.models.media_blob import MediaBlob
from app.db.models.message import MessageMetadata, MessageType
from app.db.models.starred_message import StarredMessage
from app.schemas.messaging import DeleteMessageRequest, StarMessageRequest
from app.services.media_storage import forget_download_url, storage_for
from app.websocket.manager import manager

# How long after sending a message you can still wipe it for everyone.
DELETE_FOR_EVERYONE_WINDOW = timedelta(hours=24)

logger = logging.getLogger(__name__)

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
    if msg.message_type == MessageType.system:
        # Group events ("Alice added Bob") belong to the group, not the actor.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This message can't be deleted for everyone",
        )
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
            await storage_for(blob).delete_bytes(blob.id)
            await db.delete(blob)
            # The bytes are gone — stop handing out the cached download URL.
            await forget_download_url(request.app.state.redis, blob.id)
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


@router.post(
    "/{message_id}/opened",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Mark a view-once photo/video as opened (wipes it for everyone)",
)
@limiter.limit("60/minute")
async def mark_view_once_opened(
    request: Request,
    message_id: UUID,
    current_user: CurrentUser,
    db: DBSession,
) -> None:
    """
    Called by the RECIPIENT's app when it opens a view-once message. From that
    moment the media is gone server-side: the ciphertext payload (which holds
    the media key) is cleared and the blob is deleted, so it can't be fetched
    again from any device. Both sides get a `view_once_opened` event and show
    "Opened". Idempotent — a second call is a no-op.
    """
    msg = await _resolve_message_for_user(db, message_id, current_user.id)
    if not msg.self_destruct:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Not a view-once message",
        )
    if msg.sender_id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the recipient can open a view-once message",
        )
    if msg.opened_at is not None or msg.deleted_at is not None:
        return None

    conversation_id, blob_id = msg.conversation_id, msg.media_blob_id
    redis = request.app.state.redis
    now = datetime.now(timezone.utc)

    # One conditional UPDATE decides who opens it. Concurrent opens (two
    # devices, or a retry after a timeout) race here: Postgres re-checks the
    # WHERE after the winner commits, so the loser matches 0 rows and is a
    # no-op instead of a 500.
    claimed = await db.execute(
        update(MessageMetadata)
        .where(
            MessageMetadata.id == message_id,
            MessageMetadata.opened_at.is_(None),
            MessageMetadata.deleted_at.is_(None),
        )
        .values(opened_at=now, encrypted_payload="", media_blob_id=None)  # payload held the media key
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount == 0:
        return None
    # Commit BEFORE touching storage: if deleting the bytes then fails, the
    # message is still correctly "Opened" and the unreferenced blob is
    # removed later by the maintenance orphan sweep.
    await db.commit()

    if blob_id is not None:
        # The bytes are going — stop handing out the cached download URL.
        await forget_download_url(redis, blob_id)
        blob = await db.get(MediaBlob, blob_id)
        if blob is not None:
            try:
                await storage_for(blob).delete_bytes(blob.id)
                await db.delete(blob)
                await db.commit()
            except Exception:  # noqa: BLE001
                await db.rollback()
                logger.warning("view-once blob cleanup deferred to sweep blob=%s", blob_id, exc_info=True)

    try:
        await manager.publish(redis, str(conversation_id), {
            "type": "view_once_opened",
            "conversation_id": str(conversation_id),
            "message_id": str(message_id),
            "by_user_id": str(current_user.id),
            "timestamp": now.isoformat(),
        })
    except Exception:  # noqa: BLE001 — opening already succeeded; peers see it on next history load
        logger.warning("view_once_opened publish failed msg=%s", message_id, exc_info=True)
    return None
