"""CP3: database-level guards. The database itself refuses a double charge and an impossible status.

Revision ID: 0007_db_guards
Revises: 0006_orders_list_index
Create Date: 2026-10-24 10:00:00.000000

Why
---
Until now "an order is paid at most once" was protected by application code only (a row lock plus an
`if order.status == ...` check). A future endpoint, a script, a hand-written UPDATE or a bug could write
a second completed payment and nothing would stop it; verify_consistency.py would only notice afterwards.
This revision moves the rule into the database, so it holds whoever writes:

  1. uq_payments_order_completed   at most ONE completed payment per order (partial unique index)
  2. chk_*_status / chk_users_role  a status or role outside the known set is refused (CHECK constraints)

How it stays zero-downtime
--------------------------
* The unique index is built CONCURRENTLY: writes to `payments` are not blocked while it builds.
* The CHECK constraints are added NOT VALID (instant, new writes are checked at once) and VALIDATEd
  afterwards (scans the table but only takes SHARE UPDATE EXCLUSIVE: reads and writes continue).
* Every ALTER uses lock_timeout 3 s and is retried, so it never queues behind a long transaction and
  blocks everything behind it.
* Existing data is checked first. If a row would violate a rule the migration stops with the list of
  offending rows and changes nothing. It never deletes or "fixes" money data by itself.

The MongoDB half of the same idea (an event zone can never have booked_count < 0 or > capacity) is a
collection validator applied at startup by app/models/nosql_models.py.

Downgrade removes the index and the constraints.
"""
import time
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.exc import OperationalError

revision: str = '0007_db_guards'
down_revision: Union[str, None] = '0006_orders_list_index'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UNIQUE_INDEX = 'uq_payments_order_completed'
LOCK_NOT_AVAILABLE = '55P03'
ATTEMPTS = 5
RETRY_PAUSE_SECONDS = 2

# table -> (constraint name, column, allowed values). Keep in step with app/models/sql_models.py.
STATUS_CHECKS = {
    'users': ('chk_users_role', 'role', ('fan', 'organizer', 'admin')),
    'orders': ('chk_orders_status', 'status', ('pending', 'paid', 'confirmed', 'expired', 'cancelled')),
    'tickets': ('chk_tickets_status', 'status', ('held', 'valid', 'used', 'refunded', 'cancelled')),
    'payments': ('chk_payments_status', 'status', ('pending', 'completed', 'failed', 'refunded')),
    'outbox_events': ('chk_outbox_status', 'status', ('pending', 'processed', 'failed')),
}


def _quoted(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _refuse_if_data_breaks_a_rule(bind) -> None:
    """Stop before changing anything if existing rows would violate a new rule."""
    problems = []
    duplicates = bind.execute(sa.text(
        "SELECT order_id, count(*) AS n FROM payments WHERE status = 'completed' "
        "GROUP BY order_id HAVING count(*) > 1 ORDER BY order_id LIMIT 5"
    )).all()
    if duplicates:
        problems.append(
            "orders with more than one completed payment (double charge): "
            + ", ".join(f"order {row.order_id} x{row.n}" for row in duplicates)
            + ". Decide which payment is real (refund the other), then re-run. Nothing was changed."
        )
    for table, (name, column, values) in STATUS_CHECKS.items():
        bad = [row[0] for row in bind.execute(sa.text(
            f"SELECT DISTINCT {column} FROM {table} WHERE {column} NOT IN ({_quoted(values)}) LIMIT 5"))]
        if bad:
            problems.append(f"{table}.{column} has values outside {sorted(values)}: {bad}. Nothing was changed.")
    if problems:
        raise RuntimeError("Refusing to add the database guards:\n  - " + "\n  - ".join(problems))


def _ddl_with_retry(bind, statement: str) -> None:
    """Run a lock-taking ALTER; give up quickly and try again instead of queueing behind a long transaction."""
    for attempt in range(1, ATTEMPTS + 1):
        try:
            with bind.begin_nested():  # SAVEPOINT: a lock timeout must not abort the whole migration
                bind.execute(sa.text(statement))
            return
        except OperationalError as exc:
            if getattr(exc.orig, "pgcode", None) != LOCK_NOT_AVAILABLE or attempt == ATTEMPTS:
                raise
            print(f"[0007] table is busy (attempt {attempt}/{ATTEMPTS}), retrying")
            time.sleep(RETRY_PAUSE_SECONDS)


def _drop_if_invalid(index_name: str) -> None:
    """A CREATE INDEX CONCURRENTLY that failed leaves an INVALID index behind; remove it before trying again."""
    bind = op.get_bind()
    invalid = bind.execute(sa.text(
        "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "WHERE c.relname = :n AND NOT i.indisvalid"), {"n": index_name}).first()
    if invalid:
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {index_name}")


def upgrade() -> None:
    bind = op.get_bind()
    _refuse_if_data_breaks_a_rule(bind)

    # 1. at most one completed payment per order. CONCURRENTLY cannot run inside a transaction block.
    with op.get_context().autocommit_block():
        _drop_if_invalid(UNIQUE_INDEX)
        try:
            op.execute(
                f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {UNIQUE_INDEX} "
                "ON payments (order_id) WHERE status = 'completed'"
            )
        except Exception:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {UNIQUE_INDEX}")
            raise

    # 2. status / role vocabularies: NOT VALID first (instant), VALIDATE after (does not block reads or writes)
    bind = op.get_bind()
    bind.execute(sa.text("SET LOCAL lock_timeout = '3s'"))
    for table, (name, column, values) in STATUS_CHECKS.items():
        known = bind.execute(sa.text("SELECT 1 FROM pg_constraint WHERE conname = :n"), {"n": name}).first()
        if not known:
            _ddl_with_retry(bind, f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({column} IN ({_quoted(values)})) NOT VALID")
        _ddl_with_retry(bind, f"ALTER TABLE {table} VALIDATE CONSTRAINT {name}")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '3s'")
    for table, (name, _column, _values) in STATUS_CHECKS.items():
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {UNIQUE_INDEX}")
