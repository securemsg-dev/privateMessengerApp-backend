from __future__ import annotations
from typing import Optional, Union, Any
"""
app/db/models/session.py
─────────────────────────
Session model — tracks active refresh tokens per device.
Refresh tokens are hashed before storage (never stored in plaintext).
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Session(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    device_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="SET NULL"),
        nullable=True,
    )
    refresh_token_hash: Mapped[str] = mapped_column(
        String(200), nullable=False, unique=True,
        comment="bcrypt hash of the refresh token — never stored plaintext",
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )

    # ── Relationships ─────────────────────────────────────────────────────
    user: Mapped["User"] = relationship("User", back_populates="sessions")  # noqa: F821
    device: Mapped["Optional[Device]"] = relationship("Device", back_populates="sessions")  # noqa: F821

    def __repr__(self) -> str:
        return f"<Session id={self.id} user={self.user_id} expires={self.expires_at}>"
