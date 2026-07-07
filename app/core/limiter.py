from __future__ import annotations
from typing import Optional, Union, Any
"""
app/core/limiter.py
────────────────────
Global slowapi rate limiter instance.
Import `limiter` here and attach it to the FastAPI app in main.py.
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

from app.core.config import settings

# Key function: rate-limit by client IP address by default.
# Individual endpoints can override with a custom key (e.g. phone number).
# `enabled` is config-driven so authenticated load tests can switch limiting
# off via the RATE_LIMITING_ENABLED env var (no code change). Keep it on in prod.
#
# Storage lives in Redis so limits survive deploys and are shared across
# uvicorn workers/replicas — the default in-memory storage is per-process,
# which silently multiplies every limit by the worker count. If Redis is
# briefly unreachable, limits falls back to in-memory rather than 500ing
# every request.
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["200/minute"],
    storage_uri=settings.REDIS_URL,
    in_memory_fallback_enabled=True,
    enabled=settings.RATE_LIMITING_ENABLED,
)
