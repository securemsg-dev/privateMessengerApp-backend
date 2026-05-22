"""Phase D — encrypted media blobs

Adds the `media_blobs` table that records every uploaded encrypted blob.
The actual bytes live in the storage backend (local filesystem in dev, S3
in prod) keyed by `id`. The server NEVER sees plaintext — clients encrypt
with a fresh symmetric key per blob and pass that key over the existing
E2EE message channel.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-05-10 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f6a7b8c9d0e1'
down_revision: Union[str, None] = 'e5f6a7b8c9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'media_blobs',
        sa.Column(
            'id', sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True, nullable=False,
        ),
        sa.Column(
            'owner_id', sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True,
        ),
        sa.Column(
            'size_bytes', sa.BigInteger, nullable=False,
            comment='Reported by the client on upload-url request, validated on PUT',
        ),
        sa.Column(
            'mime', sa.String(128), nullable=False,
            comment='Hint only — server never inspects content',
        ),
        sa.Column(
            'uploaded_at', sa.DateTime(timezone=True), nullable=True,
            comment='Set when bytes are received via PUT. Null = upload pending.',
        ),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_media_blobs_owner_id', 'media_blobs', ['owner_id'])


def downgrade() -> None:
    op.drop_index('ix_media_blobs_owner_id', table_name='media_blobs')
    op.drop_table('media_blobs')
