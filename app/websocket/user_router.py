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
import time
from uuid import UUID

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status
from jose import JWTError
from sqlalchemy import and_, exists, select

from app.core.config import settings
from app.core.security import verify_access_token
from app.db.models.conversation import conversation_participants
from app.db.models.user import User
from app.db.session import AsyncSessionLocal
from app.services.push_service import send_push_to_users
from app.websocket.manager import manager
from app.websocket.throttle import ConnectionThrottle

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

router = APIRouter()

# Event types the server is willing to forward. Anything else is rejected.
ALLOWED_FORWARD_TYPES = {"call_offer", "call_answer", "call_ice", "call_end"}

# ── Shared-conversation verdict cache ────────────────────────────────────────
# _share_a_conversation used to hit the DB once per signaling frame — and ICE
# fires dozens of candidates during call setup, so with a remote Postgres each
# candidate paid a full network round trip, serially delaying call connect.
# The membership answer barely changes mid-call, so cache it briefly.
_SHARE_CACHE_TTL_SECONDS = 30.0
_share_cache: dict[tuple[UUID, UUID], tuple[bool, float]] = {}


def _share_cache_get(user_a: UUID, user_b: UUID) -> bool | None:
    entry = _share_cache.get((user_a, user_b))
    if entry is None:
        return None
    verdict, expires = entry
    if time.monotonic() > expires:
        _share_cache.pop((user_a, user_b), None)
        return None
    return verdict


def _share_cache_put(user_a: UUID, user_b: UUID, verdict: bool) -> None:
    # Opportunistic pruning keeps the dict from growing unbounded.
    if len(_share_cache) > 4096:
        now = time.monotonic()
        for key in [k for k, (_, exp) in _share_cache.items() if exp < now]:
            _share_cache.pop(key, None)
    _share_cache[(user_a, user_b)] = (verdict, time.monotonic() + _SHARE_CACHE_TTL_SECONDS)


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
    cached = _share_cache_get(user_a, user_b)
    if cached is not None:
        return cached
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
        verdict = bool(result.scalar())
    _share_cache_put(user_a, user_b, verdict)
    return verdict


@router.websocket("/ws/user")
async def user_websocket_endpoint(
    websocket: WebSocket,
    token: str = Query(..., description="JWT access token"),
) -> None:
    try:
        user_id = await _authenticate(token)
    except WebSocketDisconnect:
        # Accept-then-close so the 1008 code actually reaches the client —
        # see the matching comment in websocket/router.py.
        await websocket.accept()
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    user_id_str = str(user_id)

    redis: aioredis.Redis = websocket.app.state.redis
    await manager.connect_user(websocket, user_id_str)
    throttle = ConnectionThrottle(settings.WS_USER_EVENTS_PER_10S, 10.0)

    try:
        while True:
            raw = await websocket.receive_text()
            if len(raw) > settings.WS_MAX_FRAME_BYTES:
                await websocket.send_text(
                    json.dumps({"type": "error", "detail": "Frame too large"})
                )
                continue
            if not throttle.allow():
                await websocket.send_text(
                    json.dumps({"type": "error", "detail": "Rate limit exceeded — slow down"})
                )
                continue
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
                            (caller.display_name or "Cricchat") if caller else "Cricchat"
                        )
                        caller_number = caller.private_number if caller else ""
                        # Identity must ride EVERY forwarded offer — a recipient
                        # whose WS reconnects mid-ring only sees a resent offer.
                        forwarded["caller_display_name"] = caller_name
                        forwarded["caller_private_number"] = caller_number

                        # …but only PUSH once per call. The caller re-sends
                        # call_offer every few seconds while ringing, so without
                        # this guard a sleeping callee gets a ~10× notification
                        # storm. A Redis SET NX with the ring-timeout TTL
                        # collapses the resends into a single notification.
                        call_id = data.get("call_id")
                        first_offer = True
                        if call_id:
                            first_offer = bool(
                                await redis.set(
                                    f"call_push:{call_id}", "1", nx=True, ex=35
                                )
                            )
                        if first_offer:
                            await send_push_to_users(
                                user_ids=[to_user_id],
                                title=caller_name,
                                body="Incoming call",
                                data={
                                    "type": "incoming_call",
                                    "caller_private_number": caller_number,
                                },
                                db=db,
                                channel_id="calls",
                            )
                except Exception:
                    logger.exception("call_offer enrichment/push failed")

            await manager.publish_to_user(redis, str(to_user_id), forwarded)
            # call_ice fires many times per call — keep it at debug. The
            # offer/answer/end events are the useful low-volume signals.
            log = logger.debug if event_type == "call_ice" else logger.info
            log(
                "Signal forwarded type=%s from=%s to=%s",
                event_type, user_id_str, to_user_id,
            )

    except WebSocketDisconnect:
        logger.info("User WS disconnected: user=%s", user_id_str)
    finally:
        # Any exit path (disconnect, handler crash, cancellation) must
        # unregister the socket or the manager holds it forever.
        await manager.disconnect_user(websocket, user_id_str)
