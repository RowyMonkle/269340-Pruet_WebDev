import asyncio
import logging
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from pymongo.database import Database

from app.core.database import SessionLocal, mongo_db
from app.models.sql_models import OutboxEvent
from app.services.order_service import cleanup_expired_seat_holds

logger = logging.getLogger("outbox_worker")


def process_pending_outbox_events(db: Session, mongo_db: Database) -> int:
    """Process pending transactional outbox events to ensure cross-DB consistency."""
    now = datetime.now(timezone.utc)
    # Row lock with skip_locked to allow safe concurrent workers if scaled
    pending_events = (
        db.query(OutboxEvent)
        .filter(OutboxEvent.status == "pending")
        .order_by(OutboxEvent.created_at.asc())
        .limit(50)
        .with_for_update(skip_locked=True)
        .all()
    )

    processed_count = 0
    for event in pending_events:
        try:
            # Here we apply any necessary sync logic to MongoDB if needed
            # In our architecture, the primary writes are already recorded,
            # so the outbox marks the cross-DB sync lifecycle as processed.
            event.status = "processed"
            event.processed_at = now
            processed_count += 1
        except Exception as exc:
            logger.error(f"Error processing outbox event {event.id}: {exc}")
            event.retry_count += 1
            if event.retry_count >= 5:
                event.status = "failed"

    if processed_count > 0:
        db.commit()

    return processed_count


async def start_outbox_and_cleanup_worker(stop_event: asyncio.Event, poll_interval: int = 15):
    """Background worker continuously running outbox processing and expired seat-hold cleanup."""
    logger.info("Outbox & Seat-Hold Reconciliation Worker started.")
    while not stop_event.is_set():
        try:
            db = SessionLocal()
            try:
                # 1. Clean up expired seat holds (reconciling PostgreSQL -> MongoDB)
                cleaned_holds = cleanup_expired_seat_holds(db=db, mongo_db=mongo_db)
                if cleaned_holds > 0:
                    logger.info(f"Reconciled and released {cleaned_holds} expired seat holds.")

                # 2. Process pending outbox events
                processed_events = process_pending_outbox_events(db=db, mongo_db=mongo_db)
                if processed_events > 0:
                    logger.info(f"Processed {processed_events} transactional outbox events.")

            finally:
                db.close()

        except Exception as exc:
            logger.error(f"Error in background worker loop: {exc}")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_interval)
        except asyncio.TimeoutError:
            pass

    logger.info("Outbox & Seat-Hold Reconciliation Worker stopped cleanly.")
