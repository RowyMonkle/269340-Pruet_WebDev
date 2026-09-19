"""Initial baseline schema (Users, Orders, Tickets)

Revision ID: 0001_initial_baseline
Revises: 
Create Date: 2026-09-15 18:00:00.000000

Zero-Downtime Migration Pattern:
- All initial tables create appropriate primary keys, foreign keys, and indexes.
- Future schema evolution follows the 'Expand and Contract' workflow:
  1. EXPAND: Add nullable columns or new collections alongside legacy data.
  2. DUAL WRITE: API writes to both legacy and new structures simultaneously.
  3. BACKFILL: Asynchronously migrate background records.
  4. SWITCH READ: Point API reads to newly populated columns.
  5. CONTRACT: Safely drop legacy columns only after old code paths are decommissioned.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '0001_initial_baseline'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Users Table
    op.create_table(
        'users',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('email', sa.String(length=255), nullable=False),
        sa.Column('username', sa.String(length=100), nullable=False),
        sa.Column('hashed_password', sa.String(length=255), nullable=False),
        sa.Column('full_name', sa.String(length=255), nullable=False),
        sa.Column('role', sa.String(length=50), server_default='fan', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('email'),
        sa.UniqueConstraint('username'),
    )
    op.create_index('idx_users_created_at', 'users', [sa.text('created_at DESC')])

    # 2. Orders Table
    op.create_table(
        'orders',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('order_number', sa.String(length=64), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('total_amount', sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column('status', sa.String(length=50), server_default='pending', nullable=False),
        sa.Column('payment_method', sa.String(length=50), server_default='credit_card', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('order_number'),
        sa.CheckConstraint('total_amount >= 0', name='chk_order_total_amount_positive'),
    )
    op.create_index('idx_orders_user_id', 'orders', ['user_id'])
    op.create_index('idx_orders_status', 'orders', ['status'])
    op.create_index('idx_orders_created_at', 'orders', [sa.text('created_at DESC')])
    op.create_index('idx_orders_user_status', 'orders', ['user_id', 'status'])

    # 3. Tickets Table
    op.create_table(
        'tickets',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('order_id', sa.Integer(), nullable=False),
        sa.Column('event_id', sa.String(length=64), nullable=False),
        sa.Column('ticket_code', sa.String(length=64), nullable=False),
        sa.Column('seat_zone', sa.String(length=50), nullable=False),
        sa.Column('seat_number', sa.String(length=50), nullable=True),
        sa.Column('price', sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column('status', sa.String(length=50), server_default='valid', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['order_id'], ['orders.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('ticket_code'),
        sa.CheckConstraint('price >= 0', name='chk_ticket_price_positive'),
    )
    op.create_index('idx_tickets_order_id', 'tickets', ['order_id'])
    op.create_index('idx_tickets_event_id', 'tickets', ['event_id'])
    op.create_index('idx_tickets_event_zone', 'tickets', ['event_id', 'seat_zone'])

    # Partial unique index: prevent double-booking for valid tickets with assigned seats
    op.create_index(
        'uq_tickets_event_zone_seat',
        'tickets',
        ['event_id', 'seat_zone', 'seat_number'],
        unique=True,
        postgresql_where=sa.text("seat_number IS NOT NULL AND status = 'valid'"),
    )


def downgrade() -> None:
    op.drop_index('uq_tickets_event_zone_seat', table_name='tickets')
    op.drop_table('tickets')
    op.drop_table('orders')
    op.drop_table('users')
