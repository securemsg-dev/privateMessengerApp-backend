from __future__ import annotations
"""
app/db/models/call.py
──────────────────────
Call history row (Phase E). One row per call attempt — independent of
WebRTC media flow. The `end_reason` column drives Calls-tab rendering:

  completed  — connected and one side hung up
  declined   — callee declined while ringing
  missed     — callee never responded; auto-failed on timeout
  cancelled  — caller hung up before callee answered
  failed     — peer connection failed (no audio path / network)

Foreign keys are SET NULL on user/conversation delete so call history
remains visible to the surviving side after the other party leaves.
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Call(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "calls"

    conversation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    caller_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    callee_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    accepted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    ended_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    end_reason: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True,
    )

    def __repr__(self) -> str:
        return f"<Call id={self.id} caller={self.caller_id} callee={self.callee_id} reason={self.end_reason}>"
