"""CP3: composite index for the orders list + pg_stat_statements

Revision ID: 0006_orders_list_index
Revises: 0004_expand_user_names
Create Date: 2026-10-13 10:00:00.000000

Why this revision hangs off 0004 and not off 0005
-------------------------------------------------
0005 is the CP2 contract (it drops users.full_name) and is applied by hand, never by
`docker compose up`. A revision chained after 0005 could not be applied without the
contract. So the history has two heads on purpose:

    0004 --+-- 0005_contract_user_full_name   (manual, destructive)
           +-- 0006_orders_list_index         (safe, applied by scripts/migrate.py)

scripts/migrate.py upgrades to 0006. `alembic upgrade head` refuses to run ("multiple
heads"), which is a feature: nobody can drop full_name by accident. Name the target:
    alembic upgrade 0006_orders_list_index
    alembic upgrade 0005_contract_user_full_name

What it does
------------
1. idx_orders_user_created (user_id, created_at DESC, id DESC) serves the access path of the
   orders list: "this user's newest orders, one page at a time" with no sort step, and a stable
   keyset/offset order (created_at, id). Built CONCURRENTLY, so writes are not blocked.
2. CREATE EXTENSION pg_stat_statements: DB-side proof of how many SQL statements each endpoint
   runs and which query is slowest (docker-compose.yml preloads the library; read it with
   scripts/pg_stats.py). Skipped with a notice if the server cannot create it.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0006_orders_list_index'
down_revision: Union[str, None] = '0004_expand_user_names'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX = 'idx_orders_user_created'


def upgrade() -> None:
    op.execute(
        "DO $$ BEGIN CREATE EXTENSION IF NOT EXISTS pg_stat_statements; "
        "EXCEPTION WHEN OTHERS THEN RAISE NOTICE 'pg_stat_statements was not created'; END $$"
    )

    # CONCURRENTLY cannot run inside a transaction block
    with op.get_context().autocommit_block():
        op.create_index(
            INDEX,
            'orders',
            ['user_id', sa.text('created_at DESC'), sa.text('id DESC')],
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(INDEX, table_name='orders', postgresql_concurrently=True, if_exists=True)

    op.execute(
        "DO $$ BEGIN DROP EXTENSION IF EXISTS pg_stat_statements; "
        "EXCEPTION WHEN OTHERS THEN RAISE NOTICE 'pg_stat_statements was not dropped'; END $$"
    )
