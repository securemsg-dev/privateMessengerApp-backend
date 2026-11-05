"""Add video + system to messagetype enum (expand step)

Part of the "expand first" rollout: the enum values ship before any code
writes them, so a later rollback to this release can still read rows of
either type.

Same pattern as b3c4d5e6f7a8: Postgres 12+ allows ADD VALUE inside a
transaction as long as nothing in that same transaction *uses* the new
value — no other revision in this run references 'video' or 'system'.

Revision ID: c6d7e8f9a0b1
Revises: b5c6d7e8f9a0
Create Date: 2026-11-05 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'c6d7e8f9a0b1'
down_revision: Union[str, None] = 'b5c6d7e8f9a0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE messagetype ADD VALUE IF NOT EXISTS 'video'")
    op.execute("ALTER TYPE messagetype ADD VALUE IF NOT EXISTS 'system'")


def downgrade() -> None:
    # PostgreSQL does not support removing enum values; downgrade is a no-op.
    pass
