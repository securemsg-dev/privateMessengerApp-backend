from __future__ import annotations
"""
app/websocket/router.py
────────────────────────
WebSocket endpoint for real-time messaging.

URL: /ws/{conversation_id}?token=<JWT_ACCESS_TOKEN>

Authentication:
  JWT is passed as a query parameter (browsers cannot set Authorization headers
  in native WebSocket handshakes). Token is validated before upgrading.

Authorization:
  After authentication, the server verifies the user is a participant of the
  requested conversation before accepting the connection.

Inbound message types (top-level "type" discriminator):

  • "message"  — send a new chat message (Phase A; reply_to_id added in C.2)
        { "type": "message",
          "encrypted_payload": "<ciphertext>",
          "message_type": "text" | "voice" | "image",
          "self_destruct": false,
          "client_temp_id": "<optional dedupe id>",
          "reply_to_id":   "<optional uuid of message being replied to>" }

  • "delivery" — confirm receipt of a message I received (recipient only)
        { "type": "delivery", "message_id": "<uuid>" }

  • "read"     — confirm I read a message I received (recipient only)
        { "type": "read",     "message_id": "<uuid>" }

  • "reaction" — toggle one emoji on a message (Phase C.2)
        { "type": "reaction", "message_id": "<uuid>", "emoji": "👍" }

Outbound (server → client) events follow the same shape but always include
`conversation_id` so clients can multiplex if they ever subscribe to multiple.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status
from jose import JWTError
from sqlalchemy import select

from app.core.security import verify_access_token
from app.db.models.conversation import conversation_participants
from app.db.models.message import MessageMetadata, MessageType
from app.db.models.message_reaction import MessageReaction
from app.db.session import AsyncSessionLocal
from app.websocket.manager import manager

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

router = APIRouter()


async def _authenticate_websocket(token: str) -> UUID:
    """Validate the JWT passed as a query param. Raises WebSocketDisconnect on failure."""
    try:
        payload = verify_access_token(token)
        return UUID(payload["sub"])
    except (JWTError, KeyError, ValueError):
        raise WebSocketDisconnect(code=status.WS_1008_POLICY_VIOLATION)


async def _authorize_conversation(user_id: UUID, conversation_id: UUID) -> None:
    """Verify the user is a participant of the conversation."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(conversation_participants).where(
                conversation_participants.c.conversation_id == conversation_id,
                conversation_participants.c.user_id == user_id,
            )
        )
        if not result.first():
            raise WebSocketDisconnect(code=status.WS_1008_POLICY_VIOLATION)


# ── Event handlers ────────────────────────────────────────────────────────────

async def _handle_message(
    websocket: WebSocket,
    redis: aioredis.Redis,
    conversation_id: UUID,
    conv_id_str: str,
    user_id: UUID,
    user_id_str: str,
    data: dict,
) -> None:
    """Persist + publish a new chat message."""
    encrypted_payload = data.get("encrypted_payload", "")
    message_type_str = data.get("message_type", "text")
    self_destruct: bool = bool(data.get("self_destruct", False))
    # Optional client-supplied dedupe id; echoed in the published event so
    # the sender's optimistic UI can match it to the persisted message.
    client_temp_id: Optional[str] = data.get("client_temp_id")
    # Phase C.2 — inline reply target. Null/missing = standalone message.
    raw_reply_to = data.get("reply_to_id")
    reply_to_id: Optional[UUID] = None
    if raw_reply_to:
        try:
            reply_to_id = UUID(raw_reply_to)
        except (ValueError, TypeError):
            await websocket.send_text(
                json.dumps({"type": "error", "detail": "Invalid reply_to_id"})
            )
            return

    try:
        msg_type = MessageType(message_type_str)
    except ValueError:
        await websocket.send_text(
            json.dumps({"type": "error", "detail": f"Unknown message_type: {message_type_str!r}"})
        )
        return

    async with AsyncSessionLocal() as db:
        # If replying, verify the target lives in THIS conversation — stops
        # clients from cross-stitching threads they shouldn't have access to.
        if reply_to_id is not None:
            target = (await db.execute(
                select(MessageMetadata).where(
                    MessageMetadata.id == reply_to_id,
                    MessageMetadata.conversation_id == conversation_id,
                )
            )).scalar_one_or_none()
            if target is None:
                await websocket.send_text(
                    json.dumps({"type": "error", "detail": "reply_to_id not in this conversation"})
                )
                return

        msg = MessageMetadata(
            conversation_id=conversation_id,
            sender_id=user_id,
            message_type=msg_type,
            encrypted_payload=encrypted_payload,
            self_destruct=self_destruct,
            reply_to_id=reply_to_id,
        )
        db.add(msg)
        await db.commit()
        await db.refresh(msg)

    event = {
        "type": "message",
        "conversation_id": conv_id_str,
        "sender_id": user_id_str,
        "message_id": str(msg.id),
        "client_temp_id": client_temp_id,
        "encrypted_payload": encrypted_payload,
        "message_type": message_type_str,
        "self_destruct": self_destruct,
        "reply_to_id": str(reply_to_id) if reply_to_id else None,
        "timestamp": msg.created_at.isoformat(),
    }
    await manager.publish(redis, conv_id_str, event)


async def _handle_reaction(
    websocket: WebSocket,
    redis: aioredis.Redis,
    conversation_id: UUID,
    conv_id_str: str,
    user_id: UUID,
    user_id_str: str,
    data: dict,
) -> None:
    """
    Toggle (add/remove) a single emoji reaction by the calling user on a
    message in this conversation, then broadcast the result so all peers
    update in real time.

    Wire format:
        client → server: { "type": "reaction", "message_id": <uuid>, "emoji": "👍" }
        server → all:    { "type": "reaction", "message_id", "emoji",
                            "by_user_id", "action": "added" | "removed", ... }
    """
    raw_id = data.get("message_id", "")
    emoji = data.get("emoji", "")
    if not isinstance(emoji, str) or not emoji or len(emoji) > 16:
        await websocket.send_text(
            json.dumps({"type": "error", "detail": "Invalid emoji"})
        )
        return
    try:
        message_id = UUID(raw_id)
    except (ValueError, TypeError):
        await websocket.send_text(
            json.dumps({"type": "error", "detail": "Invalid message_id"})
        )
        return

    async with AsyncSessionLocal() as db:
        # Verify the message exists in THIS conversation (auth scope already
        # checked at connect time, so participant status is implicit).
        target = (await db.execute(
            select(MessageMetadata).where(
                MessageMetadata.id == message_id,
                MessageMetadata.conversation_id == conversation_id,
            )
        )).scalar_one_or_none()
        if target is None:
            await websocket.send_text(
                json.dumps({"type": "error", "detail": "Message not in this conversation"})
            )
            return

        existing = (await db.execute(
            select(MessageReaction).where(
                MessageReaction.user_id == user_id,
                MessageReaction.message_id == message_id,
                MessageReaction.emoji == emoji,
            )
        )).scalar_one_or_none()

        if existing:
            await db.delete(existing)
            action = "removed"
        else:
            db.add(MessageReaction(
                user_id=user_id, message_id=message_id, emoji=emoji,
            ))
            action = "added"
        await db.commit()

    event = {
        "type": "reaction",
        "conversation_id": conv_id_str,
        "message_id": str(message_id),
        "emoji": emoji,
        "by_user_id": user_id_str,
        "action": action,
    }
    await manager.publish(redis, conv_id_str, event)


async def _handle_receipt(
    websocket: WebSocket,
    redis: aioredis.Redis,
    conversation_id: UUID,
    conv_id_str: str,
    user_id: UUID,
    user_id_str: str,
    kind: str,  # "delivery" | "read"
    data: dict,
) -> None:
    """Mark a message delivered/read and notify peers via Redis."""
    raw_id = data.get("message_id", "")
    try:
        message_id = UUID(raw_id)
    except (ValueError, TypeError):
        await websocket.send_text(
            json.dumps({"type": "error", "detail": "Invalid message_id"})
        )
        return

    async with AsyncSessionLocal() as db:
        # Receipts only apply to messages from the OTHER side. Acking your own
        # messages is silently ignored (no DB write, no publish).
        result = await db.execute(
            select(MessageMetadata).where(
                MessageMetadata.id == message_id,
                MessageMetadata.conversation_id == conversation_id,
                MessageMetadata.sender_id != user_id,
            )
        )
        msg = result.scalar_one_or_none()
        if not msg:
            return

        now = datetime.now(timezone.utc)
        changed = False

        if kind == "delivery" and msg.delivered_at is None:
            msg.delivered_at = now
            changed = True
        elif kind == "read":
            # `read` implies `delivered` — set both atomically
            if msg.delivered_at is None:
                msg.delivered_at = now
                changed = True
            if msg.read_at is None:
                msg.read_at = now
                changed = True

        if not changed:
            return  # already in this state — don't re-broadcast
        await db.commit()

    event = {
        "type": kind,
        "conversation_id": conv_id_str,
        "message_id": str(message_id),
        "by_user_id": user_id_str,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    await manager.publish(redis, conv_id_str, event)


# ── Endpoint ──────────────────────────────────────────────────────────────────

@router.websocket("/ws/{conversation_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    conversation_id: UUID,
    token: str = Query(..., description="JWT access token"),
) -> None:
    """
    Per-conversation WebSocket. Inbound frames are dispatched on the top-level
    `type` field — see module docstring for the wire format.
    """
    user_id = await _authenticate_websocket(token)
    await _authorize_conversation(user_id, conversation_id)

    conv_id_str = str(conversation_id)
    user_id_str = str(user_id)

    redis: aioredis.Redis = websocket.app.state.redis
    await manager.connect(websocket, conv_id_str, user_id_str)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_text(json.dumps({"type": "error", "detail": "Invalid JSON"}))
                continue

            event_type = data.get("type", "message")

            if event_type == "message":
                await _handle_message(
                    websocket, redis, conversation_id, conv_id_str,
                    user_id, user_id_str, data,
                )
            elif event_type in ("delivery", "read"):
                await _handle_receipt(
                    websocket, redis, conversation_id, conv_id_str,
                    user_id, user_id_str, event_type, data,
                )
            elif event_type == "reaction":
                await _handle_reaction(
                    websocket, redis, conversation_id, conv_id_str,
                    user_id, user_id_str, data,
                )
            else:
                await websocket.send_text(
                    json.dumps({"type": "error", "detail": f"Unknown type: {event_type!r}"})
                )

    except WebSocketDisconnect:
        await manager.disconnect(websocket, conv_id_str, user_id_str)
        logger.info("WebSocket disconnected: user=%s conv=%s", user_id_str, conv_id_str)
