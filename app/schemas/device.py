from __future__ import annotations
from typing import Optional, Union, Any
"""
app/schemas/device.py
──────────────────────
Pydantic schemas for device registration.
"""

from uuid import UUID

from pydantic import BaseModel

from app.db.models.device import DevicePlatform


class DeviceRegisterRequest(BaseModel):
    device_name: str
    platform: DevicePlatform
    push_token: Optional[str] = None
    public_key: Optional[str] = None  # Base64-encoded X25519 public key


class DeviceResponse(BaseModel):
    id: UUID
    device_name: str
    platform: DevicePlatform
    push_token: Optional[str]
    public_key: Optional[str]

    model_config = {"from_attributes": True}
