"""C.3 — message deletion

Phase C.3:
  1. New `deleted_messages` table (per-user "delete for me")
  2. `messages_metadata.deleted_at`  — timestamp when wiped for everyone
  3. `messages_metadata.deleted_by`  — uid of the sender who triggered it

When `deleted_at` is set, the server clears `encrypted_payload` to an empty
string. Clients render a tombstone in place.

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-05-09 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e5f6a7b8c9d0'
down_revision: Union[str, None] = 'd4e5f6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── deleted_messages (delete-for-me) ──────────────────────────────────
    op.create_table(
        'deleted_messages',
        sa.Column(
            'user_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False,
        ),
        sa.Column(
            'message_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False,
        ),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(
            ['message_id'], ['messages_metadata.id'], ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint(
            'user_id', 'message_id', name='pk_deleted_messages',
        ),
    )
    op.create_index(
        'ix_deleted_messages_user_id', 'deleted_messages', ['user_id'],
    )

    # ── delete-for-everyone columns on messages_metadata ──────────────────
    op.add_column(
        'messages_metadata',
        sa.Column(
            'deleted_at', sa.DateTime(timezone=True), nullable=True,
            comment='When the sender wiped this message for everyone. '
                    'Encrypted_payload is cleared at the same time.',
        ),
    )
    op.add_column(
        'messages_metadata',
        sa.Column(
            'deleted_by',
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey('users.id', ondelete='SET NULL'),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column('messages_metadata', 'deleted_by')
    op.drop_column('messages_metadata', 'deleted_at')
    op.drop_index('ix_deleted_messages_user_id', table_name='deleted_messages')
    op.drop_table('deleted_messages')
