from __future__ import annotations
"""
app/schemas/moderation.py
──────────────────────────
Pydantic schemas for the safety surface:
  - Blocking   (POST/DELETE/GET /blocks)
  - Reporting  (POST /reports)

Both are Google Play User Generated Content policy requirements.
"""

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.db.models.user_report import ReportReason
from app.schemas.messaging import UserPublic

# Cap on how much plaintext evidence a single report may carry. Keeps the
# one plaintext-bearing table from becoming a bulk message store.
MAX_EVIDENCE_MESSAGES = 20
MAX_EVIDENCE_CHARS = 4000


# ── Blocking ───────────────────────────────────────────────────────────────────

class BlockRequest(BaseModel):
    """Block a user by their UUID. Blocking is one-way and never revealed."""
    user_id: UUID


class BlockedUserEntry(BaseModel):
    """One row in the caller's blocked list."""
    user: UserPublic
    blocked_at: datetime

    model_config = {"from_attributes": True}


class BlockListResponse(BaseModel):
    blocked: list[BlockedUserEntry]


class BlockStatusResponse(BaseModel):
    """Whether the CALLER has blocked this user. Never exposes the reverse."""
    blocked: bool


# ── Reporting ──────────────────────────────────────────────────────────────────

class ReportedMessage(BaseModel):
    """
    One decrypted message attached as evidence.

    The client decrypts locally and sends plaintext here ONLY after the user
    accepts the "these messages will be sent to us unencrypted" confirmation.
    """
    message_id: Optional[UUID] = None
    sender_id: Optional[UUID] = None
    sent_at: Optional[datetime] = None
    content: str = Field(max_length=MAX_EVIDENCE_CHARS)


class ReportRequest(BaseModel):
    """
    Raise an abuse report against a user.

    `include_messages` mirrors the consent the user gave on the client. When
    it is False the server discards any `messages` payload outright, so a
    buggy or malicious client cannot smuggle plaintext in without consent.
    """
    user_id: UUID
    reason: ReportReason
    details: Optional[str] = Field(None, max_length=2000)
    conversation_id: Optional[UUID] = None
    include_messages: bool = False
    messages: list[ReportedMessage] = Field(default_factory=list)

    @field_validator("messages")
    @classmethod
    def _cap_messages(cls, v: list[ReportedMessage]) -> list[ReportedMessage]:
        if len(v) > MAX_EVIDENCE_MESSAGES:
            raise ValueError(
                f"At most {MAX_EVIDENCE_MESSAGES} messages may be attached"
            )
        return v


class ReportResponse(BaseModel):
    """
    Confirmation shown to the reporter. Deliberately says nothing about what
    action was or will be taken against the reported account.
    """
    report_id: UUID
    status: str = "received"
    message: str
