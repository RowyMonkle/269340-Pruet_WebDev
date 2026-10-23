import asyncio
import logging
from sqlalchemy import text
from sqlalchemy.orm import Session
from pymongo.database import Database

from app.core.config import settings
from app.core.database import SessionLocal, mongo_db
from app.services.order_service import cleanup_expired_seat_holds

logger = logging.getLogger("outbox_worker")

# Claim a batch of pending rows and mark them processed in ONE statement. SKIP LOCKED lets every replica run
# this loop at the same time without waiting for each other or handling the same row twice.
_MARK_PROCESSED_SQL = text(
    """
    UPDATE outbox_events
       SET status = 'processed', processed_at = now()
     WHERE id IN (
           SELECT id FROM outbox_events
            WHERE status = 'pending'
            ORDER BY created_at, id
            LIMIT :batch
              FOR UPDATE SKIP LOCKED)
    """
)

# Retention: processed rows are only history. Old ones are deleted in chunks (index idx_outbox_status_created).
_PURGE_SQL = text(
    """
    DELETE FROM outbox_events
     WHERE id IN (
           SELECT id FROM outbox_events
            WHERE status = 'processed'
              AND created_at < now() - make_interval(days => :days)
            ORDER BY created_at
            LIMIT :batch
              FOR UPDATE SKIP LOCKED)
    """
)


def process_pending_outbox_events(db: Session, mongo_db: Database, batch_size: int = 0, max_batches: int = 0) -> int:
    """Mark pending transactional-outbox events as processed, in batches. Returns how many were handled.

    In this architecture the primary writes to both databases are already done when the event is recorded,
    so processing only marks the cross-DB sync lifecycle as finished. It still has to keep up with the
    request rate: one statement per batch of OUTBOX_BATCH_SIZE (not one row at a time), up to
    OUTBOX_MAX_BATCHES batches per call.
    """
    batch_size = batch_size or settings.OUTBOX_BATCH_SIZE
    max_batches = max_batches or settings.OUTBOX_MAX_BATCHES
    total = 0
    for _ in range(max_batches):
        done = db.execute(_MARK_PROCESSED_SQL, {"batch": batch_size}).rowcount
        db.commit()
        total += done
        if done < batch_size:
            break
    return total


def purge_processed_outbox_events(db: Session, retention_days: int = -1, batch_size: int = 5000, max_batches: int = 10) -> int:
    """Delete processed outbox rows older than OUTBOX_RETENTION_DAYS (0 keeps everything). Returns rows deleted."""
    days = settings.OUTBOX_RETENTION_DAYS if retention_days < 0 else retention_days
    if days <= 0:
        return 0
    total = 0
    for _ in range(max_batches):
        done = db.execute(_PURGE_SQL, {"days": days, "batch": batch_size}).rowcount
        db.commit()
        total += done
        if done < batch_size:
            break
    return total


def run_worker_cycle() -> None:
    """One reconciliation + outbox pass. Blocking (sync SQLAlchemy / PyMongo), so it must never run on the event loop.

    Each step is isolated: a MongoDB outage must not stop the outbox from being drained, and the other way round.
    """
    db = SessionLocal()
    try:
        steps = (
            ("expired seat holds", lambda: cleanup_expired_seat_holds(db=db, mongo_db=mongo_db)),
            ("outbox events", lambda: process_pending_outbox_events(db=db, mongo_db=mongo_db)),
            ("old outbox rows", lambda: purge_processed_outbox_events(db=db)),
        )
        for label, step in steps:
            try:
                count = step()
                if count > 0:
                    logger.info(f"Worker: handled {count} {label}.")
            except Exception as exc:  # noqa: BLE001 - keep the loop alive; the next tick retries
                db.rollback()
                logger.error(f"Worker step '{label}' failed: {exc}")
    finally:
        db.close()


async def start_outbox_and_cleanup_worker(stop_event: asyncio.Event, poll_interval: int = 15):
    """Background worker continuously running outbox processing and expired seat-hold cleanup."""
    logger.info("Outbox & Seat-Hold Reconciliation Worker started.")
    while not stop_event.is_set():
        try:
            # The blocking DB work runs in a thread: a lock wait or a slow query here must never freeze the
            # event loop, because then every request on this replica would hang with it.
            await asyncio.to_thread(run_worker_cycle)
        except Exception as exc:  # exceptions raised in the thread are re-raised here by await
            logger.error(f"Error in background worker loop: {exc}")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_interval)
        except asyncio.TimeoutError:
            pass

    logger.info("Outbox & Seat-Hold Reconciliation Worker stopped cleanly.")
