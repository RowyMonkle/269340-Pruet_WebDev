"""CP2 Phase 3: backfill users.first_name / last_name from the legacy full_name.

Safe to run against a live database:
  * cursor pagination on the primary key (id > last_id ORDER BY id LIMIT n), never OFFSET
  * small batches (default 50, allowed 1-500), one short transaction per batch
  * rows are taken with FOR UPDATE SKIP LOCKED, so a request that is editing a user is
    never waited on (the row is picked up again in the next sweep)
  * the UPDATE only touches columns that are still NULL, so it can never overwrite a
    value written by dual-write or a concurrent PUT
  * a checkpoint (backfill_checkpoints) is written in the same transaction as each
    batch, so a stopped job resumes where it left off
  * a pause between batches, lock_timeout and statement_timeout keep it from stalling traffic

Name rule (same as the API and the verification queries): first_name = text before the
first whitespace, last_name = the rest, '' for a single word. Never NULL after the backfill.

Usage (inside the api container):
  python scripts/backfill.py --dry-run          # count only, change nothing
  python scripts/backfill.py                    # run / resume
  python scripts/backfill.py --restart          # ignore the checkpoint, scan from id 0
"""
import argparse
import os
import sys
import time
from typing import Optional, Tuple

from sqlalchemy import create_engine, text

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.core.config import settings  # noqa: E402
from app.services.user_service import split_full_name  # noqa: E402

JOB_NAME = "users_names"
NEEDS_BACKFILL = "(first_name IS NULL OR last_name IS NULL)"
MAX_SWEEPS = 5


def names_for(full_name: Optional[str], first: Optional[str], last: Optional[str]) -> Tuple[str, str]:
    """Target (first_name, last_name), keeping any part that is already stored."""
    head, tail = split_full_name(full_name)
    new_first = first if first is not None else head
    new_last = last if last is not None else (tail if head == new_first else "")
    return new_first, new_last


def count_remaining(conn) -> int:
    return conn.execute(text(f"SELECT count(*) FROM users WHERE {NEEDS_BACKFILL}")).scalar_one()


def load_checkpoint(conn) -> Tuple[int, int]:
    row = conn.execute(
        text("SELECT last_id, rows_done FROM backfill_checkpoints WHERE job_name = :job"), {"job": JOB_NAME}
    ).first()
    return (row.last_id, row.rows_done) if row else (0, 0)


def save_checkpoint(conn, last_id: int, rows_done: int) -> None:
    conn.execute(
        text(
            "INSERT INTO backfill_checkpoints (job_name, last_id, rows_done) VALUES (:job, :last, :done) "
            "ON CONFLICT (job_name) DO UPDATE SET last_id = :last, rows_done = :done, updated_at = now()"
        ),
        {"job": JOB_NAME, "last": last_id, "done": rows_done},
    )


def run_batch(engine, last_id: int, batch_size: int) -> Tuple[int, int, int]:
    """Migrate one batch. Returns (rows_updated, new_last_id, rows_seen)."""
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL lock_timeout = '2s'"))
        conn.execute(text("SET LOCAL statement_timeout = '10s'"))
        rows = conn.execute(
            text(
                f"SELECT id, full_name, first_name, last_name FROM users "
                f"WHERE id > :last AND {NEEDS_BACKFILL} "
                f"ORDER BY id LIMIT :n FOR UPDATE SKIP LOCKED"
            ),
            {"last": last_id, "n": batch_size},
        ).all()
        if not rows:
            return 0, last_id, 0
        updated = 0
        for row in rows:
            first, last = names_for(row.full_name, row.first_name, row.last_name)
            result = conn.execute(
                text(
                    "UPDATE users SET first_name = COALESCE(first_name, :first), "
                    "last_name = COALESCE(last_name, :last) "
                    f"WHERE id = :id AND {NEEDS_BACKFILL}"
                ),
                {"id": row.id, "first": first, "last": last},
            )
            updated += result.rowcount
        new_last = rows[-1].id
        _, done = load_checkpoint(conn)
        save_checkpoint(conn, new_last, done + updated)
        return updated, new_last, len(rows)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--batch-size", type=int, default=50, help="rows per batch (default 50)")
    parser.add_argument("--throttle", type=float, default=0.05, help="seconds to pause between batches")
    parser.add_argument("--dry-run", action="store_true", help="only count what would be migrated")
    parser.add_argument("--restart", action="store_true", help="ignore the saved checkpoint")
    args = parser.parse_args(argv)
    if not 1 <= args.batch_size <= 500:
        parser.error("--batch-size must be between 1 and 500")

    engine = create_engine(settings.sync_database_url, pool_pre_ping=True)
    with engine.connect() as conn:
        remaining = count_remaining(conn)
        saved_last, saved_done = load_checkpoint(conn)

    batches = -(-remaining // args.batch_size)
    print(f"[backfill] {remaining} user(s) need first_name/last_name, about {batches} batch(es) of {args.batch_size}")
    if args.dry_run:
        print(f"[backfill] dry run, nothing changed (checkpoint: last_id={saved_last}, rows_done={saved_done})")
        return 0

    last_id = 0 if args.restart else saved_last
    if last_id:
        print(f"[backfill] resuming after id {last_id}")
    started = time.time()
    total = 0
    batch_no = 0

    for sweep in range(1, MAX_SWEEPS + 1):
        while True:
            updated, last_id, seen = run_batch(engine, last_id, args.batch_size)
            if seen == 0:
                break
            batch_no += 1
            total += updated
            print(f"[backfill] batch {batch_no}: +{updated} rows, checkpoint last_id={last_id}, total {total}", flush=True)
            time.sleep(args.throttle)

        with engine.connect() as conn:
            remaining = count_remaining(conn)
        if remaining == 0:
            break
        # rows skipped because a request held their lock, or created below the cursor
        print(f"[backfill] sweep {sweep} done, {remaining} row(s) left, scanning again from id 0")
        last_id = 0
        time.sleep(1)

    print(f"[backfill] finished in {time.time() - started:.1f}s: {total} row(s) updated, {remaining} remaining")
    engine.dispose()
    return 0 if remaining == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
