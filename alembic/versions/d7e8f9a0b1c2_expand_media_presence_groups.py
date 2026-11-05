"""Expand schema for S3 media, presence and group chat (dormant columns)

All additive, all safe for the *previous* code to keep running against
(the pre-deploy migration runs while the old release is still serving, and
a rollback runs old code too):

  media_blobs.storage            'local' | 's3' — which backend holds the
                                 bytes. server_default stays 'local' so old
                                 code (which doesn't set it) keeps writing
                                 rows that match where it actually stores.
  users.last_seen_at             presence: when the user was last active
  users.show_online_status       presence privacy toggle (default on)
  users.supports_groups          set when an app build that understands
                                 groups lists conversations; only such
                                 users can be added to a group
  conversation_participants.role          'admin' | 'member'
  conversation_participants.joined_at     backfilled from the conversation's
                                          created_at (NOT migration time — a
                                          joined_at filter would otherwise
                                          hide all existing history)
  conversation_participants.last_read_at  per-member read watermark (groups)
  conversations.created_by

Revision ID: d7e8f9a0b1c2
Revises: c6d7e8f9a0b1
Create Date: 2026-11-05 12:05:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'd7e8f9a0b1c2'
down_revision: Union[str, None] = 'c6d7e8f9a0b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Media ────────────────────────────────────────────────────────────
    op.add_column(
        'media_blobs',
        sa.Column('storage', sa.String(length=16), nullable=False, server_default='local'),
    )

    # ── Presence ─────────────────────────────────────────────────────────
    op.add_column('users', sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        'users',
        sa.Column('show_online_status', sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        'users',
        sa.Column('supports_groups', sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    # ── Groups ───────────────────────────────────────────────────────────
    op.add_column(
        'conversations',
        sa.Column(
            'created_by',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('users.id', ondelete='SET NULL'),
            nullable=True,
        ),
    )
    op.add_column(
        'conversation_participants',
        sa.Column('role', sa.String(length=16), nullable=False, server_default='member'),
    )
    op.create_check_constraint(
        'ck_conversation_participants_role',
        'conversation_participants',
        "role IN ('admin', 'member')",
    )
    op.add_column(
        'conversation_participants',
        sa.Column('joined_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        """
        UPDATE conversation_participants cp
           SET joined_at = c.created_at
          FROM conversations c
         WHERE c.id = cp.conversation_id
        """
    )
    op.alter_column(
        'conversation_participants',
        'joined_at',
        nullable=False,
        server_default=sa.func.now(),
    )
    op.add_column(
        'conversation_participants',
        sa.Column('last_read_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('conversation_participants', 'last_read_at')
    op.drop_column('conversation_participants', 'joined_at')
    op.drop_constraint(
        'ck_conversation_participants_role', 'conversation_participants', type_='check',
    )
    op.drop_column('conversation_participants', 'role')
    op.drop_column('conversations', 'created_by')
    op.drop_column('users', 'supports_groups')
    op.drop_column('users', 'show_online_status')
    op.drop_column('users', 'last_seen_at')
    op.drop_column('media_blobs', 'storage')
