import json
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import List
from bson import ObjectId
from fastapi import HTTPException, status
from sqlalchemy.orm import Session, joinedload
from pymongo.database import Database

from app.models.sql_models import Order, Payment, OutboxEvent
from app.schemas.payment import PaymentCreate

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
    """
    # Acquire pessimistic row-level lock on the order
    order = (
        db.query(Order)
        .options(joinedload(Order.tickets), joinedload(Order.payments))
        .filter(Order.id == order_id)
        .with_for_update()  # Crucial: prevents concurrent payments and races with cleanup
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
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Order {order.order_number} is already paid and confirmed",
        )
    if order.status in ["expired", "cancelled"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Order {order.order_number} is {order.status} and cannot receive payments",
        )

    # Check seat hold expiry (10-minute hold window)
    if order.expires_at and order.expires_at < now:
        order.status = "expired"
        to_release = []
        for ticket in order.tickets:
            ticket.status = "cancelled"
            to_release.append((ticket.event_id, ticket.seat_zone))

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

        # Release held seats in MongoDB with > 0 guard
        for ev_id, z_name in to_release:
            try:
                mongo_db["events"].update_one(
                    {
                        "_id": ObjectId(ev_id),
                        "zones": {
                            "$elemMatch": {
                                "name": z_name,
                                "booked_count": {"$gt": 0},
                            }
                        },
                    },
                    {"$inc": {"zones.$.booked_count": -1}},
                )
            except Exception as exc:
                logger.error(f"Failed to decrement MongoDB booked_count on expiry for event {ev_id} zone {z_name}: {exc}")

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

    # Transition all held tickets to valid
    for ticket in order.tickets:
        ticket.status = "valid"

    # Transactional Outbox Pattern: Record ORDER_PAID event
    outbox = OutboxEvent(
        event_type="ORDER_PAID",
        aggregate_id=str(order.id),
        payload=json.dumps({
            "order_id": order.id,
            "order_number": order.order_number,
            "payment_reference": payment_ref,
            "amount": float(order.total_amount),
            "tickets_count": len(order.tickets),
        }),
        status="pending",
    )
    db.add(outbox)

    db.commit()
    db.refresh(payment)
    db.refresh(order)

    # Telemetry logging to MongoDB
    try:
        mongo_db["activity_logs"].insert_one({
            "user_id": order.user_id,
            "action": "order_paid",
            "resource_type": "payment",
            "resource_id": payment_ref,
            "metadata": {
                "order_id": order.id,
                "order_number": order.order_number,
                "amount": float(order.total_amount),
                "provider": payment_in.provider,
            },
            "timestamp": now,
        })
    except Exception:
        pass

    return payment


def get_payments_by_order_id(db: Session, order_id: int) -> List[Payment]:
    """Retrieve all payment records for an order."""
    return db.query(Payment).filter(Payment.order_id == order_id).order_by(Payment.created_at.desc()).all()
