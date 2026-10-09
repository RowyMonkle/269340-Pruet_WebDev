"""Add feature_flags and backfill_checkpoints tables

Revision ID: 0003_feature_flags
Revises: 0002_add_payments_holds_outbox
Create Date: 2026-10-10 10:00:00.000000

Supports the CP2 expand/contract migration without restarting the API:
- feature_flags: runtime switches (dual write, read source) read by every
  replica through a short TTL cache, so a phase change needs no deploy.
- backfill_checkpoints: last processed id per backfill job, so a stopped
  backfill resumes where it left off instead of rescanning the table.

Both are brand-new tables, so creating them takes no lock on existing data.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '0003_feature_flags'
down_revision: Union[str, None] = '0002_add_payments_holds_outbox'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'feature_flags',
        sa.Column('key', sa.String(length=64), nullable=False),
        sa.Column('value', sa.String(length=64), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('key'),
    )
    op.bulk_insert(
        sa.table('feature_flags', sa.column('key', sa.String), sa.column('value', sa.String)),
        [
            {'key': 'user_name_write_mode', 'value': 'legacy'},
            {'key': 'user_name_read_source', 'value': 'legacy'},
        ],
    )

    op.create_table(
        'backfill_checkpoints',
        sa.Column('job_name', sa.String(length=64), nullable=False),
        sa.Column('last_id', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('rows_done', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('job_name'),
    )


def downgrade() -> None:
    op.drop_table('backfill_checkpoints')
    op.drop_table('feature_flags')
