from __future__ import annotations
from typing import Optional, Union, Any
"""
app/db/models/user.py
──────────────────────
User model — core identity table.
Identity is a system-generated 10-digit private number (no phone/SMS).
Two bcrypt password hashes: login_password (normal auth) and
delete_password (confirms account-wipe flow).
"""

import uuid

from sqlalchemy import Boolean, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"

    private_number: Mapped[str] = mapped_column(
        String(10), unique=True, nullable=False, index=True,
        comment="System-generated 10-digit identifier, e.g. 6616970053",
    )
    login_password_hash: Mapped[str] = mapped_column(
        String(255), nullable=False,
        comment="bcrypt hash of the login password",
    )
    delete_password_hash: Mapped[str] = mapped_column(
        String(255), nullable=False,
        comment="bcrypt hash of the delete-account password",
    )
    display_name: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True,
        comment="Chosen during profile setup",
    )
    bio: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True,
        comment="Short profile bio. Null when unset.",
    )
    profile_picture_key: Mapped[Optional[str]] = mapped_column(
        String(512), nullable=True,
        comment="S3 object key — never a public URL",
    )
    public_key: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        comment="Base64 Curve25519 public key for E2EE (Phase B). "
                "Single long-term key per user; private key stays on device.",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False,
        comment="Soft delete flag",
    )

    # ── Relationships ─────────────────────────────────────────────────────
    devices: Mapped[list["Device"]] = relationship(  # noqa: F821
        "Device", back_populates="user", cascade="all, delete-orphan",
    )
    sessions: Mapped[list["Session"]] = relationship(  # noqa: F821
        "Session", back_populates="user", cascade="all, delete-orphan",
    )
    owned_contacts: Mapped[list["Contact"]] = relationship(  # noqa: F821
        "Contact", foreign_keys="Contact.owner_id", back_populates="owner",
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} private_number={self.private_number}>"
