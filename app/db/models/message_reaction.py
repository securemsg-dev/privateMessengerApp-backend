from __future__ import annotations
"""
app/db/models/message_reaction.py
──────────────────────────────────
A single emoji reaction by one user on one message (Phase C.2).

Composite primary key on (user_id, message_id, emoji) means a user can have
multiple reactions on the same message (e.g. 👍 + ❤️) but only one of any
given emoji. Tapping the same emoji twice removes it (toggle behaviour
enforced at the API layer).
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, PrimaryKeyConstraint, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MessageReaction(Base):
    __tablename__ = "message_reactions"
    __table_args__ = (
        PrimaryKeyConstraint(
            "user_id", "message_id", "emoji", name="pk_message_reactions",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("messages_metadata.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    emoji: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<MessageReaction user={self.user_id} msg={self.message_id} emoji={self.emoji!r}>"
