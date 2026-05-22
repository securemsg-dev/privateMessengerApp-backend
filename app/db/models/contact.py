from __future__ import annotations
from typing import Optional, Union, Any
"""
app/db/models/contact.py
─────────────────────────
Contact model — a user's address book (other users on the platform).
Phone number matching is done via hash on the client; only matched users appear here.
"""

import uuid

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Contact(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "contacts"
    __table_args__ = (
        UniqueConstraint("owner_id", "contact_id", name="uq_contact_pair"),
    )

    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
        comment="The user who owns this contact entry",
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
        comment="The user being saved as a contact",
    )
    nickname: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True,
        comment="Optional local nickname shown instead of display_name",
    )

    # ── Relationships ─────────────────────────────────────────────────────
    owner: Mapped["User"] = relationship(  # noqa: F821
        "User", foreign_keys=[owner_id], back_populates="owned_contacts",
    )
    contact_user: Mapped["User"] = relationship(  # noqa: F821
        "User", foreign_keys=[contact_id],
    )

    def __repr__(self) -> str:
        return f"<Contact owner={self.owner_id} contact={self.contact_id}>"
