"""user bio column

Adds a short free-text bio to the users table. Peer-visible via UserPublic
(contact lookup, conversation participants). 128 chars matches the client's
input counter.

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-07-18 17:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd0e1f2a3b4c5'
down_revision: Union[str, None] = 'c9d0e1f2a3b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column(
            'bio', sa.String(128), nullable=True,
            comment='Short profile bio. Null when unset.',
        ),
    )


def downgrade() -> None:
    op.drop_column('users', 'bio')
