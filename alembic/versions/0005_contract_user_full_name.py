"""CP2 Phase 5 (Contract): drop the legacy users.full_name column

Revision ID: 0005_contract_user_full_name
Revises: 0004_expand_user_names
Create Date: 2026-10-11 12:00:00.000000

NOT applied by `docker compose up`: scripts/migrate.py stops at 0004 unless
MIGRATE_TARGET says otherwise. Run it on purpose, as the last demo step:

    alembic upgrade 0005_contract_user_full_name

Deploy order matters. Before this runs, every API replica must already be on a
build that no longer reads or writes users.full_name, with both flags at "new":
    user_name_write_mode = new
    user_name_read_source = new
Dropping the column under a replica that still selects it turns every user
request into a 500.

Safety:
- upgrade() aborts, and drops nothing, if a flag is not "new" or if any user
  still lacks first_name / last_name (backfill incomplete).
- DROP COLUMN only changes the catalog (no table rewrite) but needs a brief
  ACCESS EXCLUSIVE lock. lock_timeout makes it give up instead of queueing
  behind a long transaction and blocking every request behind it, and the
  attempt is retried a few times.
- downgrade() re-adds full_name (nullable) and refills it from first/last in
  small committed id ranges, so a rollback does not hold one long lock.
"""
import time
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.exc import OperationalError

revision: str = '0005_contract_user_full_name'
down_revision: Union[str, None] = '0004_expand_user_names'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

REQUIRED_FLAGS = {
    "user_name_write_mode": "new",
    "user_name_read_source": "new",
}
LOCK_NOT_AVAILABLE = "55P03"
DROP_ATTEMPTS = 5
RETRY_PAUSE_SECONDS = 2
REFILL_BATCH = 1000


def _check_ready_to_contract(bind) -> None:
    rows = bind.execute(
        sa.text("SELECT key, value FROM feature_flags WHERE key = ANY(:keys)"),
        {"keys": list(REQUIRED_FLAGS)},
    ).all()
    current = {key: value for key, value in rows}
    wrong = [
        f"{key}={current.get(key, '<unset>')} (need {need})"
        for key, need in REQUIRED_FLAGS.items()
        if current.get(key) != need
    ]
    if wrong:
        raise RuntimeError(
            "Refusing to drop users.full_name, flags are not at 'new': " + ", ".join(wrong)
        )

    missing = bind.execute(
        sa.text("SELECT count(*) FROM users WHERE first_name IS NULL OR last_name IS NULL")
    ).scalar_one()
    if missing:
        raise RuntimeError(
            f"Refusing to drop users.full_name, {missing} user(s) still lack first_name/last_name. "
            "Finish the backfill and run scripts/verify_migration.py --stage after-backfill."
        )


def upgrade() -> None:
    bind = op.get_bind()
    _check_ready_to_contract(bind)

    for attempt in range(1, DROP_ATTEMPTS + 1):
        try:
            # SAVEPOINT: a lock timeout must not abort the whole migration transaction
            with bind.begin_nested():
                bind.execute(sa.text("SET LOCAL lock_timeout = '3s'"))
                bind.execute(sa.text("ALTER TABLE users DROP COLUMN full_name"))
            return
        except OperationalError as exc:
            if getattr(exc.orig, "pgcode", None) != LOCK_NOT_AVAILABLE or attempt == DROP_ATTEMPTS:
                raise
            print(f"[0005] users is busy (attempt {attempt}/{DROP_ATTEMPTS}), retrying")
            time.sleep(RETRY_PAUSE_SECONDS)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.add_column('users', sa.Column('full_name', sa.String(length=255), nullable=True))

    # Refill outside the DDL transaction, one short committed batch at a time
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        low, high = bind.execute(sa.text("SELECT min(id), max(id) FROM users")).one()
        if low is None:
            return
        start = low
        while start <= high:
            bind.execute(
                sa.text(
                    "UPDATE users "
                    "SET full_name = btrim(concat_ws(' ', first_name, last_name)) "
                    "WHERE id >= :lo AND id < :hi AND full_name IS NULL"
                ),
                {"lo": start, "hi": start + REFILL_BATCH},
            )
            start += REFILL_BATCH
