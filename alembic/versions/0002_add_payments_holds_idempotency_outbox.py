"""Add payments table, seat holds expiry, idempotency key, and transactional outbox

Revision ID: 0002_add_payments_holds_outbox
Revises: 0001_initial_baseline
Create Date: 2026-09-20 01:20:00.000000

Zero-Downtime Migration Pattern:
- Follows the Expand-and-Contract design pattern:
  1. Add nullable columns (expires_at, idempotency_key) to 'orders' without table locks.
  2. Create 'payments' table to enrich transactional billing (1:N payment records).
  3. Create 'outbox_events' table to support the Transactional Outbox Pattern for Dual-DB consistency.
  4. Update partial unique index on 'tickets' to safeguard both 'held' and 'valid' seating states.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '0002_add_payments_holds_outbox'
down_revision: Union[str, None] = '0001_initial_baseline'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Expand 'orders' table with nullable fields to prevent table locks
    op.add_column('orders', sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('orders', sa.Column('idempotency_key', sa.String(length=128), nullable=True))
    op.create_unique_constraint('uq_orders_idempotency_key', 'orders', ['idempotency_key'])
    op.create_index('idx_orders_expires_at', 'orders', ['expires_at'])
    op.create_index('idx_orders_status_expires', 'orders', ['status', 'expires_at'])

    # 2. Update tickets double-booking partial unique index to guard both 'held' and 'valid' tickets
    op.drop_index('uq_tickets_event_zone_seat', table_name='tickets')
    op.create_index(
        'uq_tickets_event_zone_seat',
        'tickets',
        ['event_id', 'seat_zone', 'seat_number'],
        unique=True,
        postgresql_where=sa.text("seat_number IS NOT NULL AND status IN ('held', 'valid')"),
    )

    # 3. Create 'payments' table (1:N with orders)
    op.create_table(
        'payments',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('order_id', sa.Integer(), nullable=False),
        sa.Column('payment_reference', sa.String(length=64), nullable=False),
        sa.Column('amount', sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column('provider', sa.String(length=50), server_default='promptpay', nullable=False),
        sa.Column('status', sa.String(length=50), server_default='pending', nullable=False),
        sa.Column('provider_tx_id', sa.String(length=128), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['order_id'], ['orders.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('payment_reference'),
        sa.CheckConstraint('amount >= 0', name='chk_payment_amount_positive'),
    )
    op.create_index('idx_payments_order_id', 'payments', ['order_id'])
    op.create_index('idx_payments_status', 'payments', ['status'])
    op.create_index('idx_payments_order_status', 'payments', ['order_id', 'status'])

    # 4. Create 'outbox_events' table (Transactional Outbox Pattern for cross-DB consistency)
    op.create_table(
        'outbox_events',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('event_type', sa.String(length=64), nullable=False),
        sa.Column('aggregate_type', sa.String(length=64), server_default='order', nullable=False),
        sa.Column('aggregate_id', sa.String(length=64), nullable=False),
        sa.Column('payload', sa.Text(), nullable=False),
        sa.Column('status', sa.String(length=50), server_default='pending', nullable=False),
        sa.Column('retry_count', sa.Integer(), server_default='0', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('idx_outbox_status_created', 'outbox_events', ['status', 'created_at'])
    op.create_index('idx_outbox_event_type', 'outbox_events', ['event_type'])
    op.create_index('idx_outbox_aggregate_id', 'outbox_events', ['aggregate_id'])


def downgrade() -> None:
    op.drop_table('outbox_events')
    op.drop_table('payments')
    op.drop_index('uq_tickets_event_zone_seat', table_name='tickets')
    op.create_index(
        'uq_tickets_event_zone_seat',
        'tickets',
        ['event_id', 'seat_zone', 'seat_number'],
        unique=True,
        postgresql_where=sa.text("seat_number IS NOT NULL AND status = 'valid'"),
    )
    op.drop_index('idx_orders_status_expires', table_name='orders')
    op.drop_index('idx_orders_expires_at', table_name='orders')
    op.drop_constraint('uq_orders_idempotency_key', 'orders', type_='unique')
    op.drop_column('orders', 'idempotency_key')
    op.drop_column('orders', 'expires_at')
