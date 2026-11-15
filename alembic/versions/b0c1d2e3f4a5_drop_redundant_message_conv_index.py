"""drop redundant messages_metadata(conversation_id) index

ix_messages_metadata_conversation_id (initial schema) is fully covered by
ix_messages_metadata_conversation_created (conversation_id, created_at):
any lookup on conversation_id alone can use the composite's leading column.
Keeping both only costs an extra index write on every message INSERT and
the disk/RAM to hold a second copy of the column.

The composite is re-asserted first (IF NOT EXISTS), so this can never leave
the table without a conversation_id index.

Revision ID: b0c1d2e3f4a5
Revises: a9b0c1d2e3f4
Create Date: 2026-11-15 18:00:00.000000
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'b0c1d2e3f4a5'
down_revision: Union[str, None] = 'a9b0c1d2e3f4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_messages_metadata_conversation_created "
        "ON messages_metadata (conversation_id, created_at)"
    )
    op.execute("DROP INDEX IF EXISTS ix_messages_metadata_conversation_id")


def downgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_messages_metadata_conversation_id "
        "ON messages_metadata (conversation_id)"
    )
