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

# Key function: rate-limit by client IP address by default.
# Individual endpoints can override with a custom key (e.g. phone number).
limiter = Limiter(key_func=get_remote_address, default_limits=["200/minute"])
