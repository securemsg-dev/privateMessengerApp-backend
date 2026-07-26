from __future__ import annotations
from typing import Optional
"""
app/db/models/user_report.py
─────────────────────────────
UserReport model — an abuse report raised by one user against another.

Required by Google Play's User Generated Content policy (in-app reporting +
a moderation record showing reports are acted on).

⚠️  PLAINTEXT NOTICE
This is the ONLY table in the system that can hold readable message content.
Everything else is E2EE ciphertext the server cannot decrypt. `evidence` is
populated only when the reporter explicitly consents on a confirmation dialog
("these messages will be sent to us unencrypted") — the client decrypts the
reported messages locally and attaches them here. A report with no consent
simply has evidence = NULL and is actioned on the reason alone.

Reporter/reported are SET NULL on account deletion so the moderation record
survives a wipe, with the private numbers denormalised alongside so a
surviving report is still attributable.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ReportReason(str, enum.Enum):
    spam = "spam"
    harassment = "harassment"
    hate_speech = "hate_speech"
    sexual_content = "sexual_content"
    violence = "violence"
    child_safety = "child_safety"
    impersonation = "impersonation"
    other = "other"


class ReportStatus(str, enum.Enum):
    pending = "pending"
    reviewing = "reviewing"
    actioned = "actioned"
    dismissed = "dismissed"


class UserReport(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "user_reports"

    reporter_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True, index=True,
        comment="Who raised the report. Null after the reporter deletes their account.",
    )
    reported_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True, index=True,
        comment="Who was reported. Null after that account is deleted.",
    )
    reporter_private_number: Mapped[Optional[str]] = mapped_column(
        String(10), nullable=True,
        comment="Denormalised so the report stays attributable after account delete",
    )
    reported_private_number: Mapped[Optional[str]] = mapped_column(
        String(10), nullable=True, index=True,
        comment="Denormalised — also lets moderators count reports against a number",
    )
    conversation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
        comment="Where the reported behaviour happened, when applicable",
    )

    reason: Mapped[ReportReason] = mapped_column(
        Enum(ReportReason), nullable=False, index=True,
    )
    details: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        comment="Free-text context typed by the reporter (max 2000 chars)",
    )
    evidence: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        comment="⚠️ PLAINTEXT. JSON array of decrypted reported messages, "
                "attached only with the reporter's explicit consent. Null "
                "when they declined to share message content.",
    )

    status: Mapped[ReportStatus] = mapped_column(
        Enum(ReportStatus), nullable=False, default=ReportStatus.pending,
        index=True,
    )
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment="When a moderator closed this report — drives the published "
                "response-time commitment on /report-abuse",
    )
    reviewer_notes: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        comment="Internal moderation notes. Never returned to any client.",
    )

    # ── Relationships ─────────────────────────────────────────────────────
    reporter: Mapped[Optional["User"]] = relationship(  # noqa: F821
        "User", foreign_keys=[reporter_id],
    )
    reported: Mapped[Optional["User"]] = relationship(  # noqa: F821
        "User", foreign_keys=[reported_id],
    )

    def __repr__(self) -> str:
        return f"<UserReport id={self.id} reason={self.reason} status={self.status}>"
