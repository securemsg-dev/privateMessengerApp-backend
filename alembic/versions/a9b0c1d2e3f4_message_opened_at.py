"""view-once media: messages_metadata.opened_at

View-once messages reuse the existing `self_destruct` flag. `opened_at` is set
when the recipient opens one: the server then wipes the ciphertext and deletes
the media blob, and both clients render "Opened". Null for every other message.

Revision ID: a9b0c1d2e3f4
Revises: d7e8f9a0b1c2
Create Date: 2026-11-15 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a9b0c1d2e3f4'
down_revision: Union[str, None] = 'd7e8f9a0b1c2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'messages_metadata',
        sa.Column(
            'opened_at', sa.DateTime(timezone=True), nullable=True,
            comment='View-once media: when the recipient opened it. '
                    'Payload and blob are wiped at the same time.',
        ),
    )


def downgrade() -> None:
    op.drop_column('messages_metadata', 'opened_at')
