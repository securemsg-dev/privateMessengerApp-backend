"""users.supports_view_once

Set once an app build that understands view-once lists its chats
(GET /conversations?view_once=true). Older builds would show a view-once
photo as an ordinary one that can be reopened forever, so the server only
accepts a view-once send when the recipient has this flag.

Adding a NOT NULL column with a constant default is a catalog-only change
on Postgres 11+ (no table rewrite).

Revision ID: c2d3e4f5a6b7
Revises: b0c1d2e3f4a5
Create Date: 2026-11-15 22:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'c2d3e4f5a6b7'
down_revision: Union[str, None] = 'b0c1d2e3f4a5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column(
            'supports_view_once', sa.Boolean(), nullable=False, server_default=sa.false(),
            comment='Set once an app build that understands view-once lists chats. '
                    'Only such users can be sent view-once media.',
        ),
    )


def downgrade() -> None:
    op.drop_column('users', 'supports_view_once')
