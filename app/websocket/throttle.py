from __future__ import annotations
"""
app/websocket/throttle.py
──────────────────────────
Per-connection inbound throttle for WebSocket endpoints.

slowapi only covers HTTP, so without this a single socket can insert
thousands of message rows per second and storm the Redis fan-out. One
instance of this class lives per accepted connection — no shared state,
no cleanup needed.
"""

import time


class ConnectionThrottle:
    """Fixed-window event counter for one WebSocket connection."""

    def __init__(self, max_events: int, window_seconds: float) -> None:
        self.max_events = max_events
        self.window_seconds = window_seconds
        self._window_start = 0.0
        self._count = 0

    def allow(self) -> bool:
        """Record one event; False when the connection exceeded its window budget."""
        now = time.monotonic()
        if now - self._window_start >= self.window_seconds:
            self._window_start = now
            self._count = 0
        self._count += 1
        return self._count <= self.max_events
