import json
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import List
from fastapi import HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session
from pymongo.database import Database

from app.models.sql_models import Order, Payment, OutboxEvent, Ticket
from app.schemas.payment import PaymentCreate
from app.services.order_service import _release_counted_seats

logger = logging.getLogger("payment_service")


def process_order_payment(
    db: Session,
    mongo_db: Database,
    order_id: int,
    payment_in: PaymentCreate,
) -> Payment:
    """Process payment for an existing pending order.

    Concurrency & Race Prevention:
    - Row-level lock via .with_for_update() prevents double-payments and races with cleanup.
    - If hold expired (expires_at < now):
      - Postgres commit happens FIRST (order -> expired, tickets -> cancelled).
      - Mongo seat count decremented only with {"booked_count": {"$gt": 0}} guard.
      - Returns HTTP 410 Gone.
    - If valid:
      - payment -> completed.
      - order -> confirmed.
      - tickets -> valid.
      - Transactional outbox event recorded.

    Query budget: the number of tickets never changes the number of SQL round trips. Tickets are changed
    with ONE UPDATE ... WHERE order_id = ..., never one UPDATE per ticket (tests/test_query_counts.py).
    """
    # Acquire pessimistic row-level lock on the order
    order = (
        db.query(Order)
        .filter(Order.id == order_id)
        .with_for_update(of=Order)
        .first()
    )
    if not order:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Order with ID {order_id} not found",
        )

    now = datetime.now(timezone.utc)

    # Check if order is already resolved
    if order.status in ["paid", "confirmed"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, #the request conflicts with the order's current state (e.g. double payment race)
            detail=f"Order {order.order_number} is already paid and confirmed",
        )
    if order.status == "expired":  
        raise HTTPException(
            status_code=status.HTTP_410_GONE,  
            detail=f"Order {order.order_number} has expired and its seats were released. Please place a new order.",
        )
    if order.status == "cancelled": 
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,  
            detail=f"Order {order.order_number} is cancelled and cannot receive payments",
        )

    # Check seat hold expiry (10-minute hold window)
    if order.expires_at and order.expires_at < now:
        order.status = "expired"
        # seats to give back, counted per (event, zone) in SQL; then every held ticket cancelled in one statement
        to_release = {
            (row.event_id, row.seat_zone): row.n
            for row in db.execute(
                select(Ticket.event_id, Ticket.seat_zone, func.count().label("n"))
                .where(Ticket.order_id == order.id, Ticket.status == "held")
                .group_by(Ticket.event_id, Ticket.seat_zone)
            )
        }
        db.execute(
            update(Ticket)
            .where(Ticket.order_id == order.id, Ticket.status == "held")
            .values(status="cancelled")
            .execution_options(synchronize_session=False)
        )

        # Record outbox event for seat release
        outbox = OutboxEvent(
            event_type="SEAT_HOLD_EXPIRED",
            aggregate_id=str(order.id),
            payload=json.dumps({"order_id": order.id, "order_number": order.order_number, "reason": "timeout"}),
            status="pending",
        )
        db.add(outbox)

        # Commit PostgreSQL state FIRST before altering MongoDB
        db.commit()

        # Release held seats in MongoDB: one update per (event, zone) with a >= guard (never below zero)
        _release_counted_seats(mongo_db, to_release)

        # Return HTTP 410 Gone for expired seat holds
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Seat hold has expired (10-minute limit exceeded). The seats have been released back to the event catalog.",
        )

    # Verify payment amount matches order total
    if Decimal(str(payment_in.amount)) != order.total_amount:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Payment amount ({payment_in.amount}) does not match order total ({order.total_amount})",
        )

    # Create Payment record
    payment_ref = f"PAY-{now.strftime('%Y%m%d')}-{uuid.uuid4().hex[:8].upper()}"
    provider_tx = payment_in.provider_tx_id or f"TX-{uuid.uuid4().hex[:12].upper()}"

    payment = Payment(
        order_id=order.id,
        payment_reference=payment_ref,
        amount=order.total_amount,
        provider=payment_in.provider,
        status="completed",
        provider_tx_id=provider_tx,
    )
    db.add(payment)

    # Transition order state to confirmed
    order.status = "confirmed"
    order.expires_at = None  # Seat hold converted into confirmed reservation

    # Transition all held tickets to valid: ONE statement for any number of tickets
    tickets_count = db.execute(
        update(Ticket)
        .where(Ticket.order_id == order.id, Ticket.status == "held")
        .values(status="valid")
        .execution_options(synchronize_session=False)
    ).rowcount

    # Transactional Outbox Pattern: Record ORDER_PAID event
    outbox = OutboxEvent(
        event_type="ORDER_PAID",
        aggregate_id=str(order.id),
        payload=json.dumps({
            "order_id": order.id,
            "order_number": order.order_number,
            "payment_reference": payment_ref,
            "amount": float(order.total_amount),
            "tickets_count": tickets_count,
        }),
        status="pending",
    )
    db.add(outbox)

    # keep what the telemetry needs: after commit the order is expired and reading it would cost a SELECT
    telemetry = {
        "user_id": order.user_id,
        "order_id": order.id,
        "order_number": order.order_number,
        "amount": float(order.total_amount),
    }

    db.commit()
    db.refresh(payment)

    # Telemetry logging to MongoDB
    try:
        mongo_db["activity_logs"].insert_one({
            "user_id": telemetry["user_id"],
            "action": "order_paid",
            "resource_type": "payment",
            "resource_id": payment_ref,
            "metadata": {
                "order_id": telemetry["order_id"],
                "order_number": telemetry["order_number"],
                "amount": telemetry["amount"],
                "provider": payment_in.provider,
            },
            "timestamp": now,
        })
    except Exception:
        pass

    return payment


def get_payments_by_order_id(db: Session, order_id: int) -> List[Payment]:
    """Retrieve all payment records for an order."""
    if db.query(Order.id).filter(Order.id == order_id).first() is None:  # tell "no such order" apart from "no payments yet"
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,  #previously returned 200 with [] for any unknown id
            detail=f"Order with ID {order_id} not found",
        )
    return db.query(Payment).filter(Payment.order_id == order_id).order_by(Payment.created_at.desc()).all()
