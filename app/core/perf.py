from __future__ import annotations
"""
app/core/perf.py
─────────────────
Per-request performance logging (opt-in via PERF_LOGGING_ENABLED).

One log line per HTTP request:

    [perf] GET /api/v1/conversations 200 153.2ms db=11 (stmts=9 tx=2) cache=0/0

  • db     — database round trips the request made: SQL statements plus
             transaction BEGIN / COMMIT / ROLLBACK (asyncpg sends each as its
             own network round trip). Pool pre-ping round trips are NOT
             visible here (SQLAlchemy has no event for them).
  • cache  — Redis cache hits/misses recorded by app.core.cache.

The path is the route template (/conversations/{conversation_id}/messages),
never the raw URL, so logs group by endpoint and carry no identifiers.

WebSocket traffic is not covered — only HTTP requests.
"""

import logging
import time
from contextvars import ContextVar
from typing import Optional

from sqlalchemy import event
from sqlalchemy.engine import Engine

from app.core.config import settings

logger = logging.getLogger("app.perf")

# Not worth a log line each: Railway polls /health constantly.
_SKIP_PATHS = frozenset({"/health"})


class RequestStats:
    __slots__ = ("statements", "transactions", "cache_hits", "cache_misses")

    def __init__(self) -> None:
        self.statements = 0
        self.transactions = 0
        self.cache_hits = 0
        self.cache_misses = 0


# Holds a mutable object, so increments made deeper in the call stack (or in
# SQLAlchemy's greenlet, which shares this context) land on the same counters.
_stats: ContextVar[Optional[RequestStats]] = ContextVar("perf_request_stats", default=None)


def current_stats() -> Optional[RequestStats]:
    """Counters for the request being served, or None outside a measured request."""
    return _stats.get()


# ── Database round-trip counting ──────────────────────────────────────────────
# Registered on the Engine class so every engine is covered (including the
# test engine). Cost when no request is being measured: one ContextVar read.

def _on_statement(conn, cursor, statement, parameters, context, executemany) -> None:
    stats = _stats.get()
    if stats is not None:
        stats.statements += 1


def _on_transaction(conn) -> None:
    stats = _stats.get()
    if stats is not None:
        stats.transactions += 1


event.listen(Engine, "before_cursor_execute", _on_statement)
for _name in ("begin", "commit", "rollback"):
    event.listen(Engine, _name, _on_transaction)


# ── Middleware ────────────────────────────────────────────────────────────────

class PerfLogMiddleware:
    """Pure ASGI middleware (no BaseHTTPMiddleware: that runs the app in a
    separate task, which would hide the counters from this one)."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if (
            scope["type"] != "http"
            or not settings.PERF_LOGGING_ENABLED
            or scope.get("path") in _SKIP_PATHS
        ):
            await self.app(scope, receive, send)
            return

        stats = RequestStats()
        token = _stats.set(stats)
        status_code = 500  # what the client sees if the app raises
        started = time.perf_counter()

        async def send_wrapper(message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            _stats.reset(token)
            route = scope.get("route")
            path = getattr(route, "path", None) or "<unmatched>"
            logger.info(
                "[perf] %s %s %d %.1fms db=%d (stmts=%d tx=%d) cache=%d/%d",
                scope.get("method", "?"),
                path,
                status_code,
                elapsed_ms,
                stats.statements + stats.transactions,
                stats.statements,
                stats.transactions,
                stats.cache_hits,
                stats.cache_misses,
            )
