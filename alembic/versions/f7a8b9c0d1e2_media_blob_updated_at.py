"""media_blobs.updated_at — add the column the TimestampMixin requires

The shared TimestampMixin (app/db/base.py) declares BOTH created_at and
updated_at as NOT NULL, and MediaBlob uses it. The original media_blobs
migration (f6a7b8c9d0e1) created created_at but omitted updated_at, so every
MediaBlob INSERT failed with:

    asyncpg.exceptions.UndefinedColumnError:
    column "updated_at" of relation "media_blobs" does not exist

…which 500'd ALL media uploads (photos, voice notes, profile pictures) at the
db.flush() in endpoints/media.py before the storage backend was ever reached.

Written with ADD COLUMN IF NOT EXISTS so it is safe to run even if the column
was already added out-of-band (e.g. a manual ALTER to unblock staging).

Revision ID: f7a8b9c0d1e2
Revises: b3c4d5e6f7a8
Create Date: 2026-06-07
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'f7a8b9c0d1e2'
down_revision: Union[str, None] = 'b3c4d5e6f7a8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # NOT NULL with a temporary server_default so any pre-existing rows backfill
    # cleanly; the ORM supplies its own value on every insert, so we then drop
    # the default to match how created_at / updated_at behave on other tables.
    op.execute(
        "ALTER TABLE media_blobs "
        "ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now()"
    )
    op.execute("ALTER TABLE media_blobs ALTER COLUMN updated_at DROP DEFAULT")


def downgrade() -> None:
    op.execute("ALTER TABLE media_blobs DROP COLUMN IF EXISTS updated_at")
