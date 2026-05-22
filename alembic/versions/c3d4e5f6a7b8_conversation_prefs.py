"""conversation_prefs table

Phase C.1 — per-user, per-conversation preferences (pin, mute, mark unread).

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-05-06 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'conversation_prefs',
        sa.Column(
            'user_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False,
        ),
        sa.Column(
            'conversation_id', sa.dialects.postgresql.UUID(as_uuid=True), nullable=False,
        ),
        sa.Column(
            'is_pinned', sa.Boolean(), nullable=False, server_default=sa.false(),
        ),
        sa.Column(
            'mute_until', sa.DateTime(timezone=True), nullable=True,
            comment="If set and in the future, the chat is muted. Use a far-future "
                    "timestamp (e.g. 9999-12-31) for 'mute always'.",
        ),
        sa.Column(
            'manual_unread', sa.Boolean(), nullable=False, server_default=sa.false(),
        ),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), nullable=False,
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True), nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ['user_id'], ['users.id'], ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['conversation_id'], ['conversations.id'], ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint(
            'user_id', 'conversation_id', name='pk_conversation_prefs',
        ),
    )
    op.create_index(
        'ix_conversation_prefs_user_id', 'conversation_prefs', ['user_id'],
    )


def downgrade() -> None:
    op.drop_index('ix_conversation_prefs_user_id', table_name='conversation_prefs')
    op.drop_table('conversation_prefs')
