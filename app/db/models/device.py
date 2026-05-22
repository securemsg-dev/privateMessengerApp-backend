from __future__ import annotations
from typing import Optional, Union, Any
"""
app/db/models/device.py
────────────────────────
Device model — one user can have multiple registered devices.
Each device holds its own E2EE public key (never sent to server from client).
"""

import enum
import uuid

from sqlalchemy import Enum, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class DevicePlatform(str, enum.Enum):
    ios = "ios"
    android = "android"


class Device(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "devices"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    device_name: Mapped[str] = mapped_column(
        String(200), nullable=False,
        comment="e.g. iPhone 15 Pro, Pixel 8",
    )
    push_token: Mapped[Optional[str]] = mapped_column(
        String(512), nullable=True,
        comment="APNs token (iOS) or FCM registration token (Android)",
    )
    platform: Mapped[DevicePlatform] = mapped_column(
        Enum(DevicePlatform), nullable=False,
    )
    public_key: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        comment="E2EE X25519 public key for this device (Base64 encoded)",
    )

    # ── Relationships ─────────────────────────────────────────────────────
    user: Mapped["User"] = relationship("User", back_populates="devices")  # noqa: F821
    sessions: Mapped[list["Session"]] = relationship(  # noqa: F821
        "Session", back_populates="device", cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Device id={self.id} platform={self.platform} user={self.user_id}>"
