from __future__ import annotations
from typing import Optional
"""
app/db/models/conversation_pref.py
───────────────────────────────────
Per-user, per-conversation preferences (Phase C.1):

  is_pinned       — chat sticks to the top of the user's chat list
  mute_until      — notifications silenced until this timestamp; null = unmuted
  manual_unread   — the user explicitly marked the chat as unread
                    (independent from the message-level read receipts)

Each (user_id, conversation_id) pair is unique. Rows are created lazily on
first prefs change; defaults reflect "unset" semantics.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, PrimaryKeyConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class ConversationPref(TimestampMixin, Base):
    __tablename__ = "conversation_prefs"
    __table_args__ = (
        PrimaryKeyConstraint("user_id", "conversation_id", name="pk_conversation_prefs"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )

    is_pinned: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False,
    )
    mute_until: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment="If set and in the future, the chat is muted. Use a far-future "
                "timestamp (e.g. 9999-12-31) for 'mute always'.",
    )
    manual_unread: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<ConversationPref user={self.user_id} conv={self.conversation_id} "
            f"pinned={self.is_pinned} muted={self.mute_until is not None}>"
        )
