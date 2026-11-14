from __future__ import annotations
"""
app/core/cache.py
──────────────────
Small Redis cache-aside helper for hot, rarely-changing lookups.

Rules this module enforces so callers don't have to:

  • FAIL OPEN. Redis being slow, down or returning garbage is a cache miss,
    never an error — the caller falls through to Postgres as before.
  • Bounded wait. Every Redis call is capped at CACHE_OP_TIMEOUT_SECONDS so a
    stalled Redis can't hold a request hostage.
  • One switch. CACHE_ENABLED=false turns every read into a miss and every
    write into a no-op.
  • Always a TTL. Nothing is cached forever: explicit invalidation is the
    primary freshness mechanism, the TTL is the safety net for a missed one.

Usage:

    members = await cached(
        redis, f"conv:{conv_id}:members", ttl=300,
        loader=lambda: load_member_ids(db, conv_id),
    )
    ...
    await cache_delete(redis, f"conv:{conv_id}:members")   # on every write

Values must be JSON-serialisable (convert UUIDs/datetimes to str first).
`None` is never stored — a loader returning None is simply not cached.

Never put secrets, message payloads or anything not already readable by the
server in here; it is a copy of Postgres rows, nothing more.
"""

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Optional

from app.core.config import settings
from app.core.perf import current_stats

logger = logging.getLogger(__name__)

# Bump the version to orphan every existing entry after a shape change
# (old keys then simply expire).
KEY_PREFIX = "cache:v1:"


def _full_key(key: str) -> str:
    return KEY_PREFIX + key


async def _guarded(op: Awaitable[Any], what: str) -> Any:
    """Run one Redis call with a deadline; swallow and log any failure."""
    try:
        return await asyncio.wait_for(op, timeout=settings.CACHE_OP_TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — fail open on anything
        logger.warning("cache %s failed (%s); using database", what, type(exc).__name__)
        return None


def _record(hit: bool) -> None:
    stats = current_stats()
    if stats is not None:
        if hit:
            stats.cache_hits += 1
        else:
            stats.cache_misses += 1


async def cache_get(redis, key: str) -> Optional[Any]:
    """Return the cached value, or None on a miss / disabled cache / any error."""
    if not settings.CACHE_ENABLED or redis is None:
        return None
    raw = await _guarded(redis.get(_full_key(key)), "get")
    if raw is None:
        _record(hit=False)
        return None
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        _record(hit=False)
        return None
    _record(hit=True)
    return value


async def cache_set(redis, key: str, value: Any, ttl: int) -> None:
    """Store a JSON-serialisable value for `ttl` seconds. Best-effort."""
    if not settings.CACHE_ENABLED or redis is None or value is None:
        return
    try:
        raw = json.dumps(value, separators=(",", ":"))
    except (TypeError, ValueError):
        logger.warning("cache set skipped: value for %r is not JSON-serialisable", key)
        return
    await _guarded(redis.set(_full_key(key), raw, ex=max(1, int(ttl))), "set")


async def cache_delete(redis, *keys: str) -> None:
    """
    Invalidate keys. Runs even when CACHE_ENABLED is false, so entries written
    before the switch was flipped can't come back stale when it is flipped on.
    """
    if redis is None or not keys:
        return
    await _guarded(redis.delete(*[_full_key(k) for k in keys]), "delete")


async def cached(
    redis,
    key: str,
    ttl: int,
    loader: Callable[[], Awaitable[Any]],
) -> Any:
    """Cache-aside: return the cached value, else run `loader` and store it."""
    value = await cache_get(redis, key)
    if value is not None:
        return value
    value = await loader()
    await cache_set(redis, key, value, ttl)
    return value
