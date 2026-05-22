from __future__ import annotations
"""
app/db/models/starred_message.py
─────────────────────────────────
Per-user "saved/star" flag on a message (Phase C.2).

Composite PK on (user_id, message_id) — each user has their own private set
of starred messages. Starring is intentionally not broadcast to other
participants (it's a personal bookmark, not a social signal).
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, PrimaryKeyConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class StarredMessage(Base):
    __tablename__ = "starred_messages"
    __table_args__ = (
        PrimaryKeyConstraint(
            "user_id", "message_id", name="pk_starred_messages",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("messages_metadata.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<StarredMessage user={self.user_id} msg={self.message_id}>"
