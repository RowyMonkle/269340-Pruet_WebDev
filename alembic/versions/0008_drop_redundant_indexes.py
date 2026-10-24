"""CP3: drop nine indexes that slow every write and answer no query.

Revision ID: 0008_drop_redundant_indexes
Revises: 0007_db_guards
Create Date: 2026-10-24 14:00:00.000000

Why
---
An index is not free: every INSERT/UPDATE/DELETE of its table also writes to it (more WAL, more disk, more
cache). The schema had grown 33 indexes. scripts/index_report.py plus a 120 s load test on 130,000 orders
(docs/perf/index_review.md) sorted them into two groups that can go.

A. REDUNDANT, structural (true whatever the workload). The columns are the leading columns of another index,
   so that index answers the same lookups:
     idx_orders_user_id         (user_id)      covered by idx_orders_user_created  (user_id, created_at DESC, id DESC)
     idx_orders_status          (status)       covered by idx_orders_status_expires (status, expires_at)
     idx_payments_order_id      (order_id)     covered by idx_payments_order_status (order_id, status)
     idx_tickets_event_id       (event_id)     covered by idx_tickets_event_zone    (event_id, seat_zone)

B. NO READER (evidence): 0 scans in the load test, no code path filters or sorts by the column, and with the
   index hypothetically dropped no query the application runs changes its plan or gets slower:
     idx_orders_created_at      newest orders across ALL users: no endpoint does that
     idx_orders_expires_at      the sweeper needs (status, expires_at); alone it was only its slower fallback plan
     idx_payments_status        4 distinct values, never selective on its own
     idx_outbox_event_type      outbox is write-hot and only read by status + created_at
     idx_outbox_aggregate_id    same

What is deliberately kept (and why) is listed in docs/perf/index_review.md: foreign-key indexes, every
unique/partial index, idx_orders_user_status, idx_tickets_event_zone, idx_users_last_name (CP2 verification
expects it) and idx_users_created_at.

Safety
------
* DROP INDEX CONCURRENTLY: no lock that blocks reads or writes; a lock timeout of 3 s with retries so it never
  queues behind a long transaction.
* Before dropping group A the migration checks that the covering index exists and is valid. It never drops both.
* Dropping an index loses no data. The downgrade rebuilds all nine with CREATE INDEX CONCURRENTLY.
* The sweeper query was changed in the same release (ORDER BY expires_at, id) so it really uses
  idx_orders_status_expires; before, it walked the whole primary key (23 ms at 130k orders, 0.13 ms after).
"""
import time
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.exc import OperationalError

revision: str = '0008_drop_redundant_indexes'
down_revision: Union[str, None] = '0007_db_guards'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

LOCK_NOT_AVAILABLE = '55P03'
ATTEMPTS = 5
RETRY_PAUSE_SECONDS = 2

# (index, table, column list for the downgrade, covering index that must exist before it is dropped)
REDUNDANT = [
    ('idx_orders_user_id', 'orders', '(user_id)', 'idx_orders_user_created'),
    ('idx_orders_status', 'orders', '(status)', 'idx_orders_status_expires'),
    ('idx_payments_order_id', 'payments', '(order_id)', 'idx_payments_order_status'),
    ('idx_tickets_event_id', 'tickets', '(event_id)', 'idx_tickets_event_zone'),
]
NO_READER = [
    ('idx_orders_created_at', 'orders', '(created_at DESC)', None),
    ('idx_orders_expires_at', 'orders', '(expires_at)', None),
    ('idx_payments_status', 'payments', '(status)', None),
    ('idx_outbox_event_type', 'outbox_events', '(event_type)', None),
    ('idx_outbox_aggregate_id', 'outbox_events', '(aggregate_id)', None),
]


def _valid_index_exists(bind, name: str) -> bool:
    return bind.execute(sa.text(
        "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = :n AND i.indisvalid"),
        {"n": name}).first() is not None


def _with_lock_timeout(statement: str) -> None:
    """Run one statement with a short lock timeout, retrying while the table is busy (autocommit: no savepoint needed)."""
    for attempt in range(1, ATTEMPTS + 1):
        try:
            op.execute("SET lock_timeout = '3s'")
            op.execute(statement)
            return
        except OperationalError as exc:
            if getattr(exc.orig, "pgcode", None) != LOCK_NOT_AVAILABLE or attempt == ATTEMPTS:
                raise
            print(f"[0008] busy (attempt {attempt}/{ATTEMPTS}), retrying: {statement}")
            time.sleep(RETRY_PAUSE_SECONDS)
        finally:
            op.execute("RESET lock_timeout")


def upgrade() -> None:
    bind = op.get_bind()
    missing = [cover for _, _, _, cover in REDUNDANT
               if not _valid_index_exists(bind, cover)]
    if missing:
        raise RuntimeError(
            "Refusing to drop the redundant indexes: the covering index is missing or invalid: "
            + ", ".join(sorted(set(missing))) + ". Nothing was changed.")

    with op.get_context().autocommit_block():  # DROP INDEX CONCURRENTLY cannot run inside a transaction block
        for name, _table, _columns, _cover in REDUNDANT + NO_READER:
            _with_lock_timeout(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")


def downgrade() -> None:
    bind = op.get_bind()
    with op.get_context().autocommit_block():
        for name, table, columns, _cover in REDUNDANT + NO_READER:
            invalid = bind.execute(sa.text(
                "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = :n AND NOT i.indisvalid"),
                {"n": name}).first()
            if invalid:  # a failed CREATE INDEX CONCURRENTLY leaves an INVALID shell behind
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
            _with_lock_timeout(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {table} {columns}")
