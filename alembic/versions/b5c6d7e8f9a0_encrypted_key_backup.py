"""Encrypted key backup — split-derivation new-device recovery

Adds `users.encrypted_key_backup`: the user's E2EE private key encrypted
client-side with a wrap key derived from their password on a path the server
never receives. Opaque to the server; lets a new device restore the key (and
thus decrypt history) after the user signs in.

Revision ID: b5c6d7e8f9a0
Revises: a3b4c5d6e7f8
Create Date: 2026-08-22 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b5c6d7e8f9a0'
down_revision: Union[str, None] = 'a3b4c5d6e7f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column('encrypted_key_backup', sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('users', 'encrypted_key_backup')
