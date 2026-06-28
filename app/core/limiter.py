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
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["200/minute"],
    enabled=settings.RATE_LIMITING_ENABLED,
)
