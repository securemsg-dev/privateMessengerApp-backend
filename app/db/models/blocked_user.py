from __future__ import annotations
"""
app/db/models/blocked_user.py
──────────────────────────────
BlockedUser model — one row per (blocker, blocked) direction.

Blocking is deliberately ONE-WAY and private: the blocked user is never told,
and nothing in any API response reveals the block to them. Enforcement happens
at send time (see app/websocket/router.py::_handle_message and
app/api/v1/endpoints/calls.py) — a blocked sender's message is accepted and
echoed back to their own socket, but never persisted or delivered.

Required by Google Play's User Generated Content policy (in-app blocking).
"""

import uuid

from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class BlockedUser(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "blocked_users"
    __table_args__ = (
        UniqueConstraint("blocker_id", "blocked_id", name="uq_block_pair"),
    )

    blocker_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
        comment="The user who initiated the block",
    )
    blocked_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
        comment="The user being blocked — never told about this row",
    )

    # ── Relationships ─────────────────────────────────────────────────────
    blocker: Mapped["User"] = relationship(  # noqa: F821
        "User", foreign_keys=[blocker_id],
    )
    blocked: Mapped["User"] = relationship(  # noqa: F821
        "User", foreign_keys=[blocked_id],
    )

    def __repr__(self) -> str:
        return f"<BlockedUser blocker={self.blocker_id} blocked={self.blocked_id}>"
