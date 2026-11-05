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

import asyncio
import json
import logging
from collections import defaultdict
from typing import Awaitable, Callable
from uuid import UUID  # noqa: F401  (kept for callers; UUIDs stringified at boundary)

import redis.asyncio as aioredis
from fastapi import WebSocket

logger = logging.getLogger(__name__)

CONV_CHANNEL_PREFIX = "conversation"
USER_CHANNEL_PREFIX = "user"


def _is_presence_event(data: str) -> bool:
    try:
        return json.loads(data).get("type") == "presence"
    except (ValueError, AttributeError):
        return False


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
        # Presence: per user-socket "app in foreground" flag. A fresh socket
        # counts as active — old app builds never report state, and an open
        # user socket is exactly what "online" meant before.
        self._user_socket_active: dict[WebSocket, bool] = {}
        # Conversation sockets that asked for presence events (?presence=1).
        # Old builds never ask, so they never see the new frames.
        self._presence_sockets: set[WebSocket] = set()
        # Called (in a background task) whenever a user's aggregate active
        # state flips. Registered by the presence service at startup.
        self._presence_hook: Optional[Callable[[str, bool], Awaitable[None]]] = None
        self._presence_tasks: set[asyncio.Task] = set()

    # ── Per-conversation lifecycle ────────────────────────────────────────────

    async def connect(
        self,
        websocket: WebSocket,
        conversation_id: str,
        user_id: str,
        *,
        wants_presence: bool = False,
    ) -> None:
        await websocket.accept()
        self._conv_connections[conversation_id][user_id].append(websocket)
        if wants_presence:
            self._presence_sockets.add(websocket)
        logger.info("WS connected: user=%s conv=%s", user_id, conversation_id)

    async def disconnect(
        self,
        websocket: WebSocket,
        conversation_id: str,
        user_id: str,
    ) -> None:
        self._presence_sockets.discard(websocket)
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
        was_active = self._has_active_user_socket(user_id)
        await websocket.accept()
        self._user_connections[user_id].append(websocket)
        self._user_socket_active[websocket] = True
        logger.info("User WS connected: user=%s", user_id)
        if not was_active:
            self._emit_presence(user_id, True)

    async def disconnect_user(self, websocket: WebSocket, user_id: str) -> None:
        was_active = self._has_active_user_socket(user_id)
        sockets = self._user_connections.get(user_id, [])
        if websocket in sockets:
            sockets.remove(websocket)
        self._user_socket_active.pop(websocket, None)
        if not sockets:
            self._user_connections.pop(user_id, None)
        logger.info("User WS disconnected: user=%s", user_id)
        if was_active and not self._has_active_user_socket(user_id):
            self._emit_presence(user_id, False)

    # ── Presence ──────────────────────────────────────────────────────────────

    def set_presence_hook(
        self, hook: Optional[Callable[[str, bool], Awaitable[None]]],
    ) -> None:
        self._presence_hook = hook

    def _has_active_user_socket(self, user_id: str) -> bool:
        return any(
            self._user_socket_active.get(ws, False)
            for ws in self._user_connections.get(user_id, [])
        )

    def set_user_socket_active(self, websocket: WebSocket, user_id: str, active: bool) -> None:
        """Client reported foreground/background on its user socket."""
        if websocket not in self._user_socket_active:
            return
        was_active = self._has_active_user_socket(user_id)
        self._user_socket_active[websocket] = active
        now_active = self._has_active_user_socket(user_id)
        if now_active != was_active:
            self._emit_presence(user_id, now_active)

    def active_user_ids(self) -> list[str]:
        """Users with at least one foreground user socket on this instance."""
        return [uid for uid in list(self._user_connections) if self._has_active_user_socket(uid)]

    def is_user_active(self, user_id: str) -> bool:
        """
        True if the user is using the app right now (on this instance). Used to
        decide whether a new message also needs a push: a backgrounded app may
        still hold an open socket for a while, but it shows no banner.

        Users with a user socket are judged by its reported state. Without
        one (e.g. an old build whose user socket dropped), an open
        conversation socket still counts — the pre-presence behaviour.
        """
        if self._user_connections.get(user_id):
            return self._has_active_user_socket(user_id)
        return any(p.get(user_id) for p in self._conv_connections.values())

    async def drain_presence_tasks(self, timeout: float = 5.0) -> None:
        """Let in-flight presence updates finish (shutdown) instead of
        cancelling them halfway through a DB write."""
        tasks = list(self._presence_tasks)
        if tasks:
            await asyncio.wait(tasks, timeout=timeout)

    def _emit_presence(self, user_id: str, online: bool) -> None:
        hook = self._presence_hook
        if hook is None:
            return
        task = asyncio.create_task(hook(user_id, online))
        self._presence_tasks.add(task)

        def _done(t: asyncio.Task) -> None:
            self._presence_tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                logger.error("presence hook failed for user=%s", user_id, exc_info=t.exception())

        task.add_done_callback(_done)

    def is_user_connected(self, user_id: str) -> bool:
        """
        True if the user has ANY live socket on this instance — either their
        always-on per-user channel OR a conversation socket. Used to decide
        whether a new message needs an Expo push (only when fully offline) or
        just an in-app live update over the user channel.

        Note: single-instance presence. With horizontal scaling this would
        need a shared (Redis) presence set; documented as a known limitation.
        """
        if self._user_connections.get(user_id):
            return True
        for participants in self._conv_connections.values():
            if participants.get(user_id):
                return True
        return False

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
        if n == 0:
            # No subscriber received it — the per-user signaling fan-out is
            # down. Worth a warning since it silently breaks calls.
            logger.warning("publish_to_user channel=%s reached 0 subscribers", channel)

    # ── Local broadcast (called by subscriber) ────────────────────────────────

    async def broadcast_to_conversation(
        self,
        conversation_id: str,
        data: str,
    ) -> None:
        """
        Send `data` to all WebSockets in this conversation on THIS instance.

        Honours the server-internal `_skip_user_ids` routing hint: members who
        have blocked the sender are skipped, and the hint is stripped so it
        never reaches a client. Used for group chats where only *some* members
        blocked the sender — a fully-blocked message is dropped upstream and
        never published at all (see websocket/router.py::_handle_message).
        """
        skip: set[str] = set()
        if "_skip_user_ids" in data:
            try:
                payload = json.loads(data)
                skip = {str(u) for u in payload.pop("_skip_user_ids", [])}
                data = json.dumps(payload)
            except (ValueError, TypeError, AttributeError):
                logger.warning("Could not parse _skip_user_ids; delivering to all")
                skip = set()

        # Presence frames only go to sockets that opted in (?presence=1).
        presence_only = '"presence"' in data and _is_presence_event(data)

        user_sockets = self._conv_connections.get(conversation_id, {})
        disconnected: list[tuple[str, WebSocket]] = []

        for user_id, sockets in user_sockets.items():
            if user_id in skip:
                continue
            for ws in sockets:
                if presence_only and ws not in self._presence_sockets:
                    continue
                try:
                    await ws.send_text(data)
                except Exception:
                    disconnected.append((user_id, ws))

        for user_id, ws in disconnected:
            await self.disconnect(ws, conversation_id, user_id)

    async def broadcast_to_user(self, user_id: str, data: str) -> None:
        """Send `data` to every WebSocket the target user has open on THIS instance."""
        sockets = self._user_connections.get(user_id, [])
        logger.debug(
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
            logger.debug("pubsub pmessage channel=%s", channel)
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
