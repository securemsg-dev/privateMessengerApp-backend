"""Safety — block + report (Google Play UGC policy)

Adds two tables:

  • `blocked_users` — one row per (blocker, blocked) direction. Blocking is
    one-way and never revealed to the blocked party; enforcement happens at
    send time so blocked messages are never persisted at all.

  • `user_reports`  — abuse reports. NOTE: `evidence` is the only column in
    the whole schema that can hold readable message content. It is populated
    solely when the reporter consents on an explicit dialog; everything else
    in the system stays E2EE ciphertext. Reporter/reported are SET NULL on
    account deletion, with private numbers denormalised alongside so a
    surviving report is still attributable.

Revision ID: a3b4c5d6e7f8
Revises: f2a3b4c5d6e7
Create Date: 2026-07-25 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'a3b4c5d6e7f8'
down_revision: Union[str, None] = 'f2a3b4c5d6e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# create_type=False: these types are created/dropped ONLY by the explicit
# .create()/.drop() calls below (idempotent via checkfirst). Without this,
# op.create_table would emit a second CREATE TYPE for the same enum and
# Postgres would abort with "type already exists".
report_reason = postgresql.ENUM(
    'spam', 'harassment', 'hate_speech', 'sexual_content', 'violence',
    'child_safety', 'impersonation', 'other',
    name='reportreason', create_type=False,
)
report_status = postgresql.ENUM(
    'pending', 'reviewing', 'actioned', 'dismissed',
    name='reportstatus', create_type=False,
)


def upgrade() -> None:
    # ── blocked_users ────────────────────────────────────────────────────
    op.create_table(
        'blocked_users',
        sa.Column(
            'id', sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True, nullable=False,
        ),
        sa.Column(
            'blocker_id', sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False,
            comment='The user who initiated the block',
        ),
        sa.Column(
            'blocked_id', sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False,
            comment='The user being blocked — never told about this row',
        ),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('blocker_id', 'blocked_id', name='uq_block_pair'),
    )
    op.create_index('ix_blocked_users_blocker_id', 'blocked_users', ['blocker_id'])
    op.create_index('ix_blocked_users_blocked_id', 'blocked_users', ['blocked_id'])

    # ── user_reports ─────────────────────────────────────────────────────
    report_reason.create(op.get_bind(), checkfirst=True)
    report_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        'user_reports',
        sa.Column(
            'id', sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True, nullable=False,
        ),
        sa.Column(
            'reporter_id', sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True,
            comment='Null after the reporter deletes their account',
        ),
        sa.Column(
            'reported_id', sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True,
            comment='Null after the reported account is deleted',
        ),
        sa.Column(
            'reporter_private_number', sa.String(10), nullable=True,
            comment='Denormalised so the report survives account deletion',
        ),
        sa.Column(
            'reported_private_number', sa.String(10), nullable=True,
            comment='Denormalised — lets moderators count reports per number',
        ),
        sa.Column(
            'conversation_id', sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey('conversations.id', ondelete='SET NULL'), nullable=True,
        ),
        sa.Column('reason', report_reason, nullable=False),
        sa.Column(
            'details', sa.Text, nullable=True,
            comment='Free-text context typed by the reporter',
        ),
        sa.Column(
            'evidence', sa.Text, nullable=True,
            comment='PLAINTEXT. JSON array of decrypted reported messages, '
                    'attached only with the reporter explicit consent.',
        ),
        sa.Column(
            'status', report_status, nullable=False,
            server_default='pending',
        ),
        sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'reviewer_notes', sa.Text, nullable=True,
            comment='Internal moderation notes — never returned to a client',
        ),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_user_reports_reporter_id', 'user_reports', ['reporter_id'])
    op.create_index('ix_user_reports_reported_id', 'user_reports', ['reported_id'])
    op.create_index(
        'ix_user_reports_reported_private_number',
        'user_reports', ['reported_private_number'],
    )
    op.create_index('ix_user_reports_reason', 'user_reports', ['reason'])
    # Moderation queue: "show me everything still pending, oldest first".
    op.create_index('ix_user_reports_status', 'user_reports', ['status'])


def downgrade() -> None:
    op.drop_index('ix_user_reports_status', table_name='user_reports')
    op.drop_index('ix_user_reports_reason', table_name='user_reports')
    op.drop_index(
        'ix_user_reports_reported_private_number', table_name='user_reports',
    )
    op.drop_index('ix_user_reports_reported_id', table_name='user_reports')
    op.drop_index('ix_user_reports_reporter_id', table_name='user_reports')
    op.drop_table('user_reports')
    report_status.drop(op.get_bind(), checkfirst=True)
    report_reason.drop(op.get_bind(), checkfirst=True)

    op.drop_index('ix_blocked_users_blocked_id', table_name='blocked_users')
    op.drop_index('ix_blocked_users_blocker_id', table_name='blocked_users')
    op.drop_table('blocked_users')
