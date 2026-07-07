"""Performance indexes: participants by user, unread partial, session expiry

Revision ID: c9d0e1f2a3b4
Revises: a8b9c0d1e2f3
Create Date: 2026-07-07

Three targeted indexes found during the pre-launch review:

1. ix_conversation_participants_user_id — the table's PK is
   (conversation_id, user_id), which cannot serve "WHERE user_id = ?".
   That is exactly how the chat list, call-signaling authorization and
   new-message fan-out query it, so each of those was a sequential scan.

2. ix_messages_unread_by_conv — partial index backing the unread-count
   query (conversation_id IN (…) AND read_at IS NULL). Stays tiny because
   read messages drop out of the index.

3. ix_sessions_expires_at — the hourly maintenance sweeper deletes
   sessions by expiry; without this it scans the whole table.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c9d0e1f2a3b4'
down_revision: Union[str, None] = 'a8b9c0d1e2f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        'ix_conversation_participants_user_id',
        'conversation_participants',
        ['user_id'],
    )
    op.create_index(
        'ix_messages_unread_by_conv',
        'messages_metadata',
        ['conversation_id'],
        postgresql_where=sa.text('read_at IS NULL'),
    )
    op.create_index(
        'ix_sessions_expires_at',
        'sessions',
        ['expires_at'],
    )


def downgrade() -> None:
    op.drop_index('ix_sessions_expires_at', table_name='sessions')
    op.drop_index('ix_messages_unread_by_conv', table_name='messages_metadata')
    op.drop_index(
        'ix_conversation_participants_user_id',
        table_name='conversation_participants',
    )
