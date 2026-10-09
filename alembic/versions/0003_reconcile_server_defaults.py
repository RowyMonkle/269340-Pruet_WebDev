"""Reconcile server defaults between Alembic and init-db/init.sql

Revision ID: 0003_reconcile_defaults
Revises: 0002_add_payments_holds_outbox
Create Date: 2026-10-09 22:00:00.000000

0001 created orders.payment_method DEFAULT 'credit_card' and
tickets.status DEFAULT 'valid', while init.sql and the ORM use
'promptpay' and 'held'. A database built by `alembic upgrade` and one
built by init.sql + `alembic stamp` therefore behaved differently.

ALTER COLUMN ... SET DEFAULT only changes catalog metadata (no table
rewrite, no row scan), so this is safe to run against live traffic.
"""
from typing import Sequence, Union
from alembic import op

revision: str = '0003_reconcile_defaults'
down_revision: Union[str, None] = '0002_add_payments_holds_outbox'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('orders', 'payment_method', server_default='promptpay')
    op.alter_column('tickets', 'status', server_default='held')


def downgrade() -> None:
    op.alter_column('tickets', 'status', server_default='valid')
    op.alter_column('orders', 'payment_method', server_default='credit_card')
