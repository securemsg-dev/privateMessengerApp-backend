"""private_number auth

Replace phone_number on users with system-generated private_number (10 digits)
and add dual password hashes (login + delete).

Revision ID: a1b2c3d4e5f6
Revises: cb4c68125d29
Create Date: 2026-04-05 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = 'cb4c68125d29'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Dev data is disposable (scaffold not yet deployed). Clear users so the
    # new NOT NULL columns can be added without backfill.
    op.execute("TRUNCATE TABLE users CASCADE")

    op.drop_index(op.f('ix_users_phone_number'), table_name='users')
    op.drop_column('users', 'phone_number')

    op.add_column(
        'users',
        sa.Column(
            'private_number', sa.String(length=10), nullable=False,
            comment='System-generated 10-digit identifier, e.g. 6616970053',
        ),
    )
    op.create_index(
        op.f('ix_users_private_number'), 'users', ['private_number'], unique=True
    )
    op.add_column(
        'users',
        sa.Column(
            'login_password_hash', sa.String(length=255), nullable=False,
            comment='bcrypt hash of the login password',
        ),
    )
    op.add_column(
        'users',
        sa.Column(
            'delete_password_hash', sa.String(length=255), nullable=False,
            comment='bcrypt hash of the delete-account password',
        ),
    )


def downgrade() -> None:
    op.execute("TRUNCATE TABLE users CASCADE")

    op.drop_column('users', 'delete_password_hash')
    op.drop_column('users', 'login_password_hash')
    op.drop_index(op.f('ix_users_private_number'), table_name='users')
    op.drop_column('users', 'private_number')

    op.add_column(
        'users',
        sa.Column(
            'phone_number', sa.String(length=20), nullable=False,
            comment='E.164 format, e.g. +601XXXXXXXX',
        ),
    )
    op.create_index(
        op.f('ix_users_phone_number'), 'users', ['phone_number'], unique=True
    )
