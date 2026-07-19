"""conversations.direct_key — unique key closing the duplicate-1:1 race

Two clients creating "the conversation with each other" at the same moment
could both pass the existence check and insert two rows. A sorted
"uuidA:uuidB" key with a unique index makes the second insert fail so the
endpoint can return the winner instead.

Backfill: existing two-participant 1:1 conversations get their key; when the
same pair already has duplicates, only the OLDEST row is keyed (the rest stay
null and remain accessible — they just aren't the canonical row the create
endpoint converges on).

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-07-19 13:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'f2a3b4c5d6e7'
down_revision: Union[str, None] = 'e1f2a3b4c5d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'conversations',
        sa.Column(
            'direct_key', sa.String(80), nullable=True,
            comment='Sorted "uuid:uuid" of the two participants for 1:1 chats.',
        ),
    )

    # Backfill 1:1 conversations that have exactly two participants.
    op.execute(sa.text("""
        UPDATE conversations c
        SET direct_key = sub.key
        FROM (
            SELECT conversation_id,
                   string_agg(user_id::text, ':' ORDER BY user_id::text) AS key,
                   COUNT(*) AS n
            FROM conversation_participants
            GROUP BY conversation_id
        ) sub
        WHERE c.id = sub.conversation_id
          AND c.is_group = false
          AND sub.n = 2
    """))

    # De-duplicate: keep the key only on the oldest row per pair.
    op.execute(sa.text("""
        UPDATE conversations
        SET direct_key = NULL
        WHERE id IN (
            SELECT id FROM (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY direct_key ORDER BY created_at, id
                       ) AS rn
                FROM conversations
                WHERE direct_key IS NOT NULL
            ) ranked
            WHERE ranked.rn > 1
        )
    """))

    op.create_index(
        'ix_conversations_direct_key', 'conversations', ['direct_key'], unique=True,
    )


def downgrade() -> None:
    op.drop_index('ix_conversations_direct_key', table_name='conversations')
    op.drop_column('conversations', 'direct_key')
