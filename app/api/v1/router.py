from __future__ import annotations
from typing import Optional, Union, Any
"""
app/api/v1/router.py
─────────────────────
Aggregates all v1 API routes into a single router.
This router is included in main.py under /api/v1.
"""

from fastapi import APIRouter

from app.api.v1.endpoints import (
    auth,
    calls,
    contacts,
    conversations,
    devices,
    media,
    messages,
    users,
    webrtc,
)

api_router = APIRouter()

# Health check is now included directly in main.py at root

# Auth endpoints — /api/v1/auth/...
api_router.include_router(auth.router)

# Device endpoints — /api/v1/devices/...
api_router.include_router(devices.router)

# Phase A — messaging spine
api_router.include_router(users.router)         # /api/v1/users/...
api_router.include_router(contacts.router)      # /api/v1/contacts/...
api_router.include_router(conversations.router) # /api/v1/conversations/...

# Phase C.2 — per-message endpoints (star, future delete/forward)
api_router.include_router(messages.router)      # /api/v1/messages/...

# Phase D — encrypted media blob storage
api_router.include_router(media.router)         # /api/v1/media/...

# Phase E — WebRTC runtime config + call history
api_router.include_router(webrtc.router)        # /api/v1/webrtc/...
api_router.include_router(calls.router)         # /api/v1/calls/...
