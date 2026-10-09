"""CP2 Phase 1 (Expand): add first_name / last_name next to users.full_name

Revision ID: 0004_expand_user_names
Revises: 0003_feature_flags
Create Date: 2026-10-10 11:00:00.000000

Every statement here is safe under live write traffic:
- ADD COLUMN without a default is a catalog-only change (no table rewrite).
- DROP NOT NULL on full_name is catalog-only too. It lets the API stop
  writing the legacy column later (write mode "new") before it is dropped.
- The last_name index is built with CREATE INDEX CONCURRENTLY, which does
  not block INSERT/UPDATE while it builds.
- lock_timeout makes the ALTERs give up instead of queueing behind a long
  transaction and blocking every request behind them.

Downgrade is only safe while user_name_write_mode is "legacy" or "dual";
it refills full_name from first/last for any row that has none.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '0004_expand_user_names'
down_revision: Union[str, None] = '0003_feature_flags'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.add_column('users', sa.Column('first_name', sa.String(length=100), nullable=True))
    op.add_column('users', sa.Column('last_name', sa.String(length=100), nullable=True))
    op.alter_column('users', 'full_name', existing_type=sa.String(length=255), nullable=True)

    # CONCURRENTLY cannot run inside a transaction block
    with op.get_context().autocommit_block():
        op.create_index(
            'idx_users_last_name',
            'users',
            ['last_name'],
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            'idx_users_last_name',
            table_name='users',
            postgresql_concurrently=True,
            if_exists=True,
        )

    op.execute("SET LOCAL lock_timeout = '3s'")
    op.execute(
        "UPDATE users SET full_name = trim(concat_ws(' ', first_name, last_name)) "
        "WHERE full_name IS NULL"
    )
    op.alter_column('users', 'full_name', existing_type=sa.String(length=255), nullable=False)
    op.drop_column('users', 'last_name')
    op.drop_column('users', 'first_name')
