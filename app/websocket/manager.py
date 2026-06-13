from __future__ import annotations
from typing import Optional, Union, Any
"""
app/websocket/manager.py
─────────────────────────
WebSocket connection manager using Redis pub/sub for horizontal scaling.

Architecture:
  - Each EC2 instance maintains an in-memory dict of active WebSocket
    connections, partitioned by *channel kind*:

      • Per-conversation: chat messages, receipts, reactions, deletions.
      • Per-user (Phase E): targeted signaling for an individual user
        regardless of which conversation they have open. Used by the call
        signaling layer (offer / answer / ICE) and any other future
        "directly to this user" event.

  - When a message arrives via WebSocket, it is published to the matching
    Redis channel. Every instance subscribes to BOTH channel families and
    delivers to its local sockets. This lets multiple EC2 instances serve
    different users in the same conversation OR the same user across
    devices.

Channel naming:
  conversation:{conversation_id}    — chat fan-out
  user:{user_id}                     — targeted user fan-out (Phase E+)
"""

import json
import logging
from collections import defaultdict
from uuid import UUID  # noqa: F401  (kept for callers; UUIDs stringified at boundary)

import redis.asyncio as aioredis
from fastapi import WebSocket

logger = logging.getLogger(__name__)

CONV_CHANNEL_PREFIX = "conversation"
USER_CHANNEL_PREFIX = "user"


class ConnectionManager:
    """
    Manages active WebSocket connections per conversation AND per user, and
    coordinates message delivery across instances via Redis pub/sub.
    """

    def __init__(self) -> None:
        # { conversation_id: { user_id: [WebSocket] } }
        self._conv_connections: dict[str, dict[str, list[WebSocket]]] = defaultdict(
            lambda: defaultdict(list)
        )
        # { user_id: [WebSocket] } — call signaling + future user-targeted events
        self._user_connections: dict[str, list[WebSocket]] = defaultdict(list)

    # ── Per-conversation lifecycle ────────────────────────────────────────────

    async def connect(
        self,
        websocket: WebSocket,
        conversation_id: str,
        user_id: str,
    ) -> None:
        await websocket.accept()
        self._conv_connections[conversation_id][user_id].append(websocket)
        logger.info("WS connected: user=%s conv=%s", user_id, conversation_id)

    async def disconnect(
        self,
        websocket: WebSocket,
        conversation_id: str,
        user_id: str,
    ) -> None:
        sockets = self._conv_connections[conversation_id].get(user_id, [])
        if websocket in sockets:
            sockets.remove(websocket)
        if not sockets:
            self._conv_connections[conversation_id].pop(user_id, None)
        if not self._conv_connections[conversation_id]:
            self._conv_connections.pop(conversation_id, None)
        logger.info("WS disconnected: user=%s conv=%s", user_id, conversation_id)

    # ── Per-user lifecycle (Phase E) ──────────────────────────────────────────

    async def connect_user(self, websocket: WebSocket, user_id: str) -> None:
        await websocket.accept()
        self._user_connections[user_id].append(websocket)
        logger.info("User WS connected: user=%s", user_id)

    async def disconnect_user(self, websocket: WebSocket, user_id: str) -> None:
        sockets = self._user_connections.get(user_id, [])
        if websocket in sockets:
            sockets.remove(websocket)
        if not sockets:
            self._user_connections.pop(user_id, None)
        logger.info("User WS disconnected: user=%s", user_id)

    # ── Publishing ────────────────────────────────────────────────────────────

    async def publish(
        self,
        redis: aioredis.Redis,
        conversation_id: str,
        message: dict,
    ) -> None:
        """Publish to a conversation's channel — fans out to all participants."""
        channel = f"{CONV_CHANNEL_PREFIX}:{conversation_id}"
        await redis.publish(channel, json.dumps(message))

    async def publish_to_user(
        self,
        redis: aioredis.Redis,
        user_id: str,
        message: dict,
    ) -> None:
        """Publish to a single user's channel — for call signaling, etc."""
        channel = f"{USER_CHANNEL_PREFIX}:{user_id}"
        n = await redis.publish(channel, json.dumps(message))
        logger.info("publish_to_user channel=%s subscribers=%s", channel, n)

    # ── Local broadcast (called by subscriber) ────────────────────────────────

    async def broadcast_to_conversation(
        self,
        conversation_id: str,
        data: str,
    ) -> None:
        """Send `data` to all WebSockets in this conversation on THIS instance."""
        user_sockets = self._conv_connections.get(conversation_id, {})
        disconnected: list[tuple[str, WebSocket]] = []

        for user_id, sockets in user_sockets.items():
            for ws in sockets:
                try:
                    await ws.send_text(data)
                except Exception:
                    disconnected.append((user_id, ws))

        for user_id, ws in disconnected:
            await self.disconnect(ws, conversation_id, user_id)

    async def broadcast_to_user(self, user_id: str, data: str) -> None:
        """Send `data` to every WebSocket the target user has open on THIS instance."""
        sockets = self._user_connections.get(user_id, [])
        logger.info(
            "Deliver to user=%s — %d local socket(s)", user_id, len(sockets)
        )
        disconnected: list[WebSocket] = []
        for ws in sockets:
            try:
                await ws.send_text(data)
            except Exception:
                disconnected.append(ws)
        for ws in disconnected:
            await self.disconnect_user(ws, user_id)

    # ── Redis subscriber loop ─────────────────────────────────────────────────

    async def _route_pmessage(self, raw_message: dict) -> None:
        """Route one pmessage dict to the right local broadcaster."""
        if raw_message is None or raw_message.get("type") != "pmessage":
            return
        try:
            ch = raw_message["channel"]
            channel: str = ch.decode() if isinstance(ch, (bytes, bytearray)) else ch
            d = raw_message["data"]
            data: str = d.decode() if isinstance(d, (bytes, bytearray)) else d
            logger.info("pubsub pmessage channel=%s", channel)
            if channel.startswith(f"{CONV_CHANNEL_PREFIX}:"):
                conversation_id = channel.split(":", 1)[1]
                await self.broadcast_to_conversation(conversation_id, data)
            elif channel.startswith(f"{USER_CHANNEL_PREFIX}:"):
                user_id = channel.split(":", 1)[1]
                await self.broadcast_to_user(user_id, data)
        except Exception:
            logger.exception("Error processing pub/sub message")

    async def start_subscriber(self, redis: aioredis.Redis) -> None:
        """
        Long-running async task: subscribe to BOTH conversation:* and user:*
        channel families and route incoming pub/sub messages to the right
        local broadcaster. Run once at app startup as a background task.

        Uses an explicit ``get_message`` polling loop (NOT ``listen()``): the
        async-generator form of redis-py's asyncio PubSub can silently stop
        yielding pmessages against a real server even while PUBLISH reports
        subscribers>0 — which previously broke ALL real-time delivery (calls
        AND live chat). Polling actively pumps the socket and lets us recover
        the subscription if the connection drops, so the task never dies
        silently.
        """
        import asyncio

        patterns = (f"{CONV_CHANNEL_PREFIX}:*", f"{USER_CHANNEL_PREFIX}:*")

        while True:
            pubsub = redis.pubsub()
            try:
                await pubsub.psubscribe(*patterns)
                logger.info(
                    "Redis pub/sub subscriber started (conversation + user channels)"
                )
                while True:
                    message = await pubsub.get_message(
                        ignore_subscribe_messages=True, timeout=1.0
                    )
                    if message is not None:
                        await self._route_pmessage(message)
            except asyncio.CancelledError:
                # App shutdown — unwind cleanly.
                await pubsub.aclose()
                raise
            except Exception:
                logger.exception(
                    "pub/sub subscriber loop error; resubscribing in 1s"
                )
                try:
                    await pubsub.aclose()
                except Exception:
                    pass
                await asyncio.sleep(1)


# Singleton — shared across the app lifecycle
manager = ConnectionManager()
