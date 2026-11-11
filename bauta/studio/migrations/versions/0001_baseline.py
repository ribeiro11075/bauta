"""Studio's tables as 0.2.7 created them. A database from then has them with
no record of it, and is stamped at this revision rather than running it.
"""
import sqlalchemy as sa
from alembic import op

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:

    op.create_table(
        'studio_user',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('email', sa.String(320), nullable=False),
        sa.Column('name', sa.String(200), nullable=False),
        sa.Column('passwordHash', sa.String(200), nullable=False),
        sa.Column('role', sa.String(16), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('createdAt', sa.DateTime(timezone=True), nullable=False),
        sa.Column('lastLoginAt', sa.DateTime(timezone=True), nullable=True))
    op.create_index('ix_studio_user_email', 'studio_user', ['email'], unique=True)

    op.create_table(
        'studio_session',
        sa.Column('tokenHash', sa.String(64), primary_key=True),
        sa.Column('userId', sa.Integer(), sa.ForeignKey('studio_user.id', ondelete='CASCADE'), nullable=False),
        sa.Column('createdAt', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expiresAt', sa.DateTime(timezone=True), nullable=False))
    op.create_index('ix_studio_session_userId', 'studio_session', ['userId'])

    op.create_table(
        'studio_activity',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('userId', sa.Integer(), sa.ForeignKey('studio_user.id', ondelete='SET NULL'), nullable=True),
        sa.Column('email', sa.String(320), nullable=False),
        sa.Column('action', sa.String(64), nullable=False),
        sa.Column('target', sa.String(500), nullable=False),
        sa.Column('detail', sa.JSON(), nullable=False))
    op.create_index('ix_studio_activity_at', 'studio_activity', ['at'])

    op.create_table(
        'studio_launched_run',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('userId', sa.Integer(), sa.ForeignKey('studio_user.id', ondelete='SET NULL'), nullable=True),
        sa.Column('email', sa.String(320), nullable=False),
        sa.Column('startedAt', sa.DateTime(timezone=True), nullable=False),
        sa.Column('finishedAt', sa.DateTime(timezone=True), nullable=True),
        sa.Column('jobs', sa.JSON(), nullable=False),
        sa.Column('options', sa.JSON(), nullable=False),
        sa.Column('pid', sa.Integer(), nullable=True),
        sa.Column('exitCode', sa.Integer(), nullable=True),
        sa.Column('runId', sa.String(36), nullable=True),
        sa.Column('logPath', sa.Text(), nullable=False))
    op.create_index('ix_studio_launched_run_startedAt', 'studio_launched_run', ['startedAt'])
    op.create_index('ix_studio_launched_run_runId', 'studio_launched_run', ['runId'])

    op.create_table(
        'studio_check',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('kind', sa.String(32), nullable=False),
        sa.Column('at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('email', sa.String(320), nullable=False),
        sa.Column('result', sa.JSON(), nullable=False))
    op.create_index('ix_studio_check_kind', 'studio_check', ['kind'])

    op.create_table(
        'studio_setting',
        sa.Column('name', sa.String(64), primary_key=True),
        sa.Column('value', sa.JSON(), nullable=False))

    op.create_table(
        'studio_manifest',
        sa.Column('runId', sa.String(64), primary_key=True),
        sa.Column('archivedAt', sa.DateTime(timezone=True), nullable=False),
        sa.Column('manifest', sa.JSON(), nullable=False))


def downgrade() -> None:

    for table in ('studio_manifest', 'studio_setting', 'studio_check', 'studio_launched_run', 'studio_activity', 'studio_session', 'studio_user'):
        op.drop_table(table)
