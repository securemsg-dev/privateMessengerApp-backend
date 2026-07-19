from __future__ import annotations
from typing import Optional, Union, Any
"""
app/db/models/message.py
─────────────────────────
MessageMetadata model — the server NEVER stores plaintext.
Only encrypted ciphertext is stored here; decryption happens client-side.

Self-destruct (burn-on-read) is flagged per message.
The client is responsible for deleting from local SQLite once opened.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class MessageType(str, enum.Enum):
    text = "text"
    voice = "voice"
    image = "image"
    document = "document"


class MessageMetadata(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "messages_metadata"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    sender_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    message_type: Mapped[MessageType] = mapped_column(
        Enum(MessageType), nullable=False, default=MessageType.text,
    )
    encrypted_payload: Mapped[str] = mapped_column(
        Text, nullable=False,
        comment="E2EE ciphertext — server cannot read this content",
    )
    delivered_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    read_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    self_destruct: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False,
        comment="Burn-on-read: client deletes from local DB after first open",
    )
    reply_to_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("messages_metadata.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Inline reply target (Phase C.2). Null = standalone message.",
    )
    media_blob_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("media_blobs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Blob carried by this media message. Used only for storage "
                "lifecycle (delete-for-everyone / account delete / orphan "
                "sweep) — the content itself stays E2EE.",
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Phase C.3 — when the sender wiped this message for everyone. "
                "encrypted_payload is cleared at the same time.",
    )
    deleted_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    # ── Relationships ─────────────────────────────────────────────────────
    conversation: Mapped["Conversation"] = relationship(  # noqa: F821
        "Conversation", back_populates="messages",
    )
    sender: Mapped["User"] = relationship("User", foreign_keys=[sender_id])  # noqa: F821

    def __repr__(self) -> str:
        return f"<MessageMetadata id={self.id} type={self.message_type} conv={self.conversation_id}>"
