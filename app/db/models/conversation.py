from __future__ import annotations
from typing import Optional, Union, Any
"""
app/db/models/conversation.py
──────────────────────────────
Conversation model — future-proofed for both 1-to-1 and group chats.
Even though group messaging is not in Phase 1 scope, the schema supports it
to avoid painful migrations later.

Participants are tracked via ConversationParticipant (many-to-many).
"""

import uuid

from sqlalchemy import Boolean, Column, ForeignKey, Index, String, Table
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


# Association table — no extra columns needed at this stage
conversation_participants = Table(
    "conversation_participants",
    Base.metadata,
    Column(
        "conversation_id",
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "user_id",
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    # The PK (conversation_id, user_id) can't serve "WHERE user_id = ?" —
    # which is exactly how the chat list, call-signaling authorization, and
    # message fan-out all query this table.
    Index("ix_conversation_participants_user_id", "user_id"),
)


class Conversation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "conversations"

    is_group: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False,
        comment="False = 1-to-1 direct message; True = group (future scope)",
    )
    name: Mapped[Optional[str]] = mapped_column(
        String(200), nullable=True,
        comment="Group name — null for 1-to-1 conversations",
    )
    direct_key: Mapped[Optional[str]] = mapped_column(
        String(80), nullable=True, unique=True, index=True,
        comment='Sorted "uuid:uuid" of the two participants for 1:1 chats. '
                "The unique constraint closes the concurrent-create race. "
                "Null for groups (and legacy duplicate rows).",
    )

    # ── Relationships ─────────────────────────────────────────────────────
    participants: Mapped[list["User"]] = relationship(  # noqa: F821
        "User", secondary=conversation_participants,
    )
    messages: Mapped[list["MessageMetadata"]] = relationship(  # noqa: F821
        "MessageMetadata", back_populates="conversation", cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Conversation id={self.id} group={self.is_group}>"
