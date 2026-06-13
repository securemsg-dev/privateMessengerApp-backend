from __future__ import annotations
"""
app/websocket/user_router.py
─────────────────────────────
Per-USER WebSocket endpoint (Phase E). Distinct from the per-conversation WS:
clients open one of these on login and keep it alive for the whole session,
regardless of which chat they're viewing. The server uses it to push events
that are addressed to a specific user — most importantly, WebRTC call
signaling (offer / answer / ICE / end).

URL: /ws/user?token=<JWT_ACCESS_TOKEN>

Inbound (client → server):
  • {type:"call_offer",  to_user_id, conversation_id, call_id, sdp}
  • {type:"call_answer", to_user_id, conversation_id, call_id, sdp}
  • {type:"call_ice",    to_user_id, conversation_id, call_id, candidate}
  • {type:"call_end",    to_user_id, conversation_id, call_id, reason}

Outbound (server → client) — same shapes, but `from_user_id` replaces
`to_user_id` so the receiver knows who's calling.

Server's job: validate the sender, swap to_user_id → from_user_id, publish
to the recipient's user channel via Redis. For privacy the server REFUSES
to forward signaling to a user the caller doesn't share a conversation
with (so unknown users can't ring you).
"""

import json
import logging
from uuid import UUID

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status
from jose import JWTError
from sqlalchemy import and_, exists, select

from app.core.security import verify_access_token
from app.db.models.conversation import conversation_participants
from app.db.models.user import User
from app.db.session import AsyncSessionLocal
from app.services.push_service import send_push_to_users
from app.websocket.manager import manager

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

router = APIRouter()

# Event types the server is willing to forward. Anything else is rejected.
ALLOWED_FORWARD_TYPES = {"call_offer", "call_answer", "call_ice", "call_end"}


async def _authenticate(token: str) -> UUID:
    try:
        payload = verify_access_token(token)
        return UUID(payload["sub"])
    except (JWTError, KeyError, ValueError):
        raise WebSocketDisconnect(code=status.WS_1008_POLICY_VIOLATION)


async def _share_a_conversation(user_a: UUID, user_b: UUID) -> bool:
    """True iff both users are participants in at least one common conversation."""
    if user_a == user_b:
        return False
    async with AsyncSessionLocal() as db:
        # Conversations where user_a is a participant
        a_convs = select(conversation_participants.c.conversation_id).where(
            conversation_participants.c.user_id == user_a,
        )
        # Does user_b appear in any of those?
        result = await db.execute(
            select(
                exists().where(
                    and_(
                        conversation_participants.c.user_id == user_b,
                        conversation_participants.c.conversation_id.in_(a_convs),
                    )
                )
            )
        )
        return bool(result.scalar())


@router.websocket("/ws/user")
async def user_websocket_endpoint(
    websocket: WebSocket,
    token: str = Query(..., description="JWT access token"),
) -> None:
    user_id = await _authenticate(token)
    user_id_str = str(user_id)

    redis: aioredis.Redis = websocket.app.state.redis
    await manager.connect_user(websocket, user_id_str)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_text(
                    json.dumps({"type": "error", "detail": "Invalid JSON"})
                )
                continue

            event_type = data.get("type")
            if event_type not in ALLOWED_FORWARD_TYPES:
                logger.info(
                    "Signal rejected (unknown type %r) from user=%s",
                    event_type, user_id_str,
                )
                await websocket.send_text(
                    json.dumps(
                        {"type": "error", "detail": f"Unknown type: {event_type!r}"}
                    )
                )
                continue

            raw_to = data.get("to_user_id", "")
            try:
                to_user_id = UUID(raw_to)
            except (ValueError, TypeError):
                logger.info(
                    "Signal rejected (bad to_user_id %r) type=%s from user=%s",
                    raw_to, event_type, user_id_str,
                )
                await websocket.send_text(
                    json.dumps({"type": "error", "detail": "Invalid to_user_id"})
                )
                continue

            logger.info(
                "Signal recv type=%s from=%s to=%s",
                event_type, user_id_str, to_user_id,
            )

            # Anti-spam: only forward signaling between users that share at
            # least one conversation. This means you can only call people
            # you have a chat with.
            if not await _share_a_conversation(user_id, to_user_id):
                logger.info(
                    "Signal DROPPED (no shared conversation) type=%s from=%s to=%s",
                    event_type, user_id_str, to_user_id,
                )
                await websocket.send_text(
                    json.dumps(
                        {"type": "error", "detail": "Recipient unreachable"}
                    )
                )
                continue

            # Re-shape: drop `to_user_id`, add `from_user_id` (the caller).
            forwarded = {
                **{k: v for k, v in data.items() if k != "to_user_id"},
                "from_user_id": user_id_str,
            }

            # For a fresh incoming call, enrich the offer with the caller's
            # identity (so the recipient screen shows a name, not "Unknown")
            # and fire a push notification so a backgrounded/closed recipient
            # is still rung. Best-effort: never let a DB/Expo hiccup block the
            # signaling itself.
            if event_type == "call_offer":
                try:
                    async with AsyncSessionLocal() as db:
                        caller = await db.get(User, user_id)
                        caller_name = (
                            (caller.display_name or "PrivaChat") if caller else "PrivaChat"
                        )
                        caller_number = caller.private_number if caller else ""
                        forwarded["caller_display_name"] = caller_name
                        forwarded["caller_private_number"] = caller_number
                        await send_push_to_users(
                            user_ids=[to_user_id],
                            title=caller_name,
                            body="Incoming call",
                            data={
                                "type": "incoming_call",
                                "caller_private_number": caller_number,
                            },
                            db=db,
                        )
                except Exception:
                    logger.exception("call_offer enrichment/push failed")

            await manager.publish_to_user(redis, str(to_user_id), forwarded)
            logger.info(
                "Signal forwarded type=%s from=%s to=%s",
                event_type, user_id_str, to_user_id,
            )

    except WebSocketDisconnect:
        await manager.disconnect_user(websocket, user_id_str)
        logger.info("User WS disconnected: user=%s", user_id_str)
