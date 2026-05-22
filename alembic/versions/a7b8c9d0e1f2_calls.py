"""Phase E — calls table for call history

Adds the `calls` table that records every call (1:1 only for now). Each
call has a caller and callee, started_at (when the offer was issued),
accepted_at (null if never picked up), ended_at, and an end_reason that
the Calls-tab UI uses to render status (Missed / Declined / etc.).

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-05-10 11:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a7b8c9d0e1f2'
down_revision: Union[str, None] = 'f6a7b8c9d0e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'calls',
        sa.Column(
            'id', sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True, nullable=False,
        ),
        sa.Column(
            'conversation_id', sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey('conversations.id', ondelete='SET NULL'),
            nullable=True, index=True,
        ),
        sa.Column(
            'caller_id', sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey('users.id', ondelete='SET NULL'),
            nullable=True, index=True,
        ),
        sa.Column(
            'callee_id', sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey('users.id', ondelete='SET NULL'),
            nullable=True, index=True,
        ),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'end_reason', sa.String(32), nullable=True,
            comment="completed | declined | missed | cancelled | failed",
        ),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('calls')
