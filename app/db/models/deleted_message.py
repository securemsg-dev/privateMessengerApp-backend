from __future__ import annotations
"""
app/db/models/deleted_message.py
─────────────────────────────────
Per-user "delete for me" hide flag (Phase C.3).

Composite PK on (user_id, message_id). When a row exists, the message is
filtered out of `list_messages` for that user and absent from any future
WS broadcast they receive (the broadcast still happens; the client checks
its local hide-set when rendering — see frontend).

This is distinct from `messages_metadata.deleted_at` which is the
"delete for everyone" sentinel — that wipes the encrypted payload
server-side and broadcasts a tombstone to all participants.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, PrimaryKeyConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DeletedMessage(Base):
    __tablename__ = "deleted_messages"
    __table_args__ = (
        PrimaryKeyConstraint(
            "user_id", "message_id", name="pk_deleted_messages",
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
        return f"<DeletedMessage user={self.user_id} msg={self.message_id}>"
