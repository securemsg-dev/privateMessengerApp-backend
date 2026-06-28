"""messages_metadata composite index for chat-history pagination

Opening a chat runs:
    WHERE conversation_id = :id [AND created_at < :before]
    ORDER BY created_at DESC LIMIT :n

Only conversation_id was indexed, so Postgres filtered on it then sorted by
created_at. A composite (conversation_id, created_at) lets the planner walk the
index in order and stop at LIMIT — no separate sort — which keeps chat opens
fast as message volume grows. Idempotent (IF NOT EXISTS) so it's safe to re-run.

Revision ID: a8b9c0d1e2f3
Revises: f7a8b9c0d1e2
Create Date: 2026-06-28
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'a8b9c0d1e2f3'
down_revision: Union[str, None] = 'f7a8b9c0d1e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_messages_metadata_conversation_created "
        "ON messages_metadata (conversation_id, created_at)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_messages_metadata_conversation_created")
