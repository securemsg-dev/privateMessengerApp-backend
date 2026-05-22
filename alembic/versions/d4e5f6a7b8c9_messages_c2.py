"""C.2 — message reactions, starred messages, reply metadata

Phase C.2 — three changes in one revision:
  1. New `message_reactions` table        (composite PK: user_id, message_id, emoji)
  2. New `starred_messages` table         (composite PK: user_id, message_id)
  3. New `messages_metadata.reply_to_id`  (nullable self-FK for inline reply)

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-05-09 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── message_reactions ─────────────────────────────────────────────────
    op.create_table(
        'message_reactions',
        sa.Column(
            'user_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False,
        ),
        sa.Column(
            'message_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False,
        ),
        sa.Column('emoji', sa.String(length=16), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(
            ['message_id'], ['messages_metadata.id'], ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint(
            'user_id', 'message_id', 'emoji', name='pk_message_reactions',
        ),
    )
    op.create_index(
        'ix_message_reactions_message_id',
        'message_reactions',
        ['message_id'],
    )

    # ── starred_messages ──────────────────────────────────────────────────
    op.create_table(
        'starred_messages',
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
            'user_id', 'message_id', name='pk_starred_messages',
        ),
    )
    op.create_index(
        'ix_starred_messages_user_id', 'starred_messages', ['user_id'],
    )

    # ── messages_metadata.reply_to_id ─────────────────────────────────────
    op.add_column(
        'messages_metadata',
        sa.Column(
            'reply_to_id',
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=True,
            comment='If set, the message this one is replying to. '
                    'Null = standalone message.',
        ),
    )
    op.create_foreign_key(
        'fk_messages_reply_to',
        source_table='messages_metadata',
        referent_table='messages_metadata',
        local_cols=['reply_to_id'],
        remote_cols=['id'],
        ondelete='SET NULL',
    )
    op.create_index(
        'ix_messages_reply_to_id', 'messages_metadata', ['reply_to_id'],
    )


def downgrade() -> None:
    op.drop_index('ix_messages_reply_to_id', table_name='messages_metadata')
    op.drop_constraint(
        'fk_messages_reply_to', 'messages_metadata', type_='foreignkey',
    )
    op.drop_column('messages_metadata', 'reply_to_id')

    op.drop_index('ix_starred_messages_user_id', table_name='starred_messages')
    op.drop_table('starred_messages')

    op.drop_index('ix_message_reactions_message_id', table_name='message_reactions')
    op.drop_table('message_reactions')
