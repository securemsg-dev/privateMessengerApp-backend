from __future__ import annotations
"""
app/db/models/media_blob.py
────────────────────────────
A single encrypted media blob (Phase D). Bytes are stored in the configured
backend (local filesystem in dev, S3 in prod) keyed by `id`. The server
holds no decryption key — clients send the per-blob symmetric key over
the existing E2EE message channel.

`uploaded_at` is null until the client successfully PUTs the bytes, so a
nightly job can sweep abandoned upload-url rows.
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class MediaBlob(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "media_blobs"

    owner_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Who created the upload-url. Null after account delete.",
    )
    size_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False,
        comment="Reported by client on upload-url request; validated on PUT.",
    )
    mime: Mapped[str] = mapped_column(
        String(128), nullable=False,
        comment="Hint only — server never inspects content.",
    )
    uploaded_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment="Set when bytes are received via PUT. Null = upload pending.",
    )

    def __repr__(self) -> str:
        return f"<MediaBlob id={self.id} size={self.size_bytes} mime={self.mime!r}>"
