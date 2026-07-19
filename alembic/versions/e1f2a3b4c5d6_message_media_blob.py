"""messages.media_blob_id — plaintext blob reference for lifecycle cleanup

The blob id inside the E2EE payload is unreadable by the server, so media
files could never be deleted when their message (or owner account) was
deleted. Clients now send the blob id alongside the ciphertext; the server
uses it ONLY for storage lifecycle (delete-for-everyone, account deletion,
orphan sweep) — the content stays E2EE.

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-07-19 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision: str = 'e1f2a3b4c5d6'
down_revision: Union[str, None] = 'd0e1f2a3b4c5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'messages_metadata',
        sa.Column(
            'media_blob_id', UUID(as_uuid=True),
            sa.ForeignKey('media_blobs.id', ondelete='SET NULL'),
            nullable=True,
            comment='Blob carried by this media message. Lifecycle only — content is E2EE.',
        ),
    )
    op.create_index(
        'ix_messages_metadata_media_blob_id', 'messages_metadata', ['media_blob_id'],
    )


def downgrade() -> None:
    op.drop_index('ix_messages_metadata_media_blob_id', table_name='messages_metadata')
    op.drop_column('messages_metadata', 'media_blob_id')
