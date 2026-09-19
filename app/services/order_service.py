import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Optional
from bson import ObjectId
from bson.errors import InvalidId
from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload, load_only
from pymongo.database import Database

from app.models.sql_models import User, Order, Ticket, OutboxEvent
from app.schemas.order import OrderCreate


def get_order_by_id(db: Session, order_id: int) -> Order:
    """Fetch order details with associated tickets and payments using joinedload to eliminate N+1 queries."""
    order = (
        db.query(Order)
        .options(joinedload(Order.tickets), joinedload(Order.payments))
        .filter(Order.id == order_id)
        .first()
    )
    if not order:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Order with ID {order_id} not found",
        )
    return order


def create_order_dual_db(
    db: Session,
    mongo_db: Database,
    order_in: OrderCreate,
    idempotency_key: Optional[str] = None,
) -> Order:
    """Execute Dual-Database Transaction for ticket seat-holding and checkout.

    Orchestration:
    1. Check Idempotency-Key: if key already exists, return previous order without re-charging/duplicating.
    2. Verify customer account in PostgreSQL.
    3. Verify event and zone in MongoDB, extract authoritative price.
    4. Atomically reserve seat capacity in MongoDB using $elemMatch and $lt capacity condition (prevents overselling).
    5. Start PostgreSQL transaction: persist Order with 10-minute hold expiry and Tickets with 'held' status.
    6. Catch double-booking violations via partial unique index on (event_id, seat_zone, seat_number).
    7. Record SEAT_HELD in OutboxEvent table for cross-DB consistency.
    8. Roll back and execute compensating transactions if any step fails.
    """
    # 1. Idempotency Check
    if idempotency_key:
        existing_order = (
            db.query(Order)
            .options(joinedload(Order.tickets), joinedload(Order.payments))
            .filter(Order.idempotency_key == idempotency_key)
            .first()
        )
        if existing_order:
            return existing_order

    # 2. Verify User exists in PostgreSQL (load only id for performance)
    user = (
        db.query(User)
        .options(load_only(User.id))
        .filter(User.id == order_in.user_id)
        .first()
    )
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User with ID {order_in.user_id} does not exist",
        )

    # 3. Pre-validate Event & Zone in MongoDB and determine Authoritative Pricing
    events_collection = mongo_db["events"]
    validated_items = []
    total_amount = Decimal("0.00")

    for item in order_in.items:
        try:
            event_oid = ObjectId(item.event_id)
        except InvalidId:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid event ID: {item.event_id}",
            )

        event_doc = events_collection.find_one(
            {"_id": event_oid},
            projection={"_id": 1, "title": 1, "zones": 1, "status": 1},
        )
        if not event_doc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Event with ID '{item.event_id}' not found",
            )

        # Match requested zone
        matched_zone = None
        for z in event_doc.get("zones", []):
            if z.get("name").strip().lower() == item.seat_zone.strip().lower():
                matched_zone = z
                break

        if not matched_zone:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Zone '{item.seat_zone}' does not exist in event '{event_doc.get('title')}'",
            )

        zone_capacity = int(matched_zone.get("capacity", 0))
        # Authoritative price stored in MongoDB catalog
        authoritative_price = Decimal(str(matched_zone.get("price", 0.0)))
        total_amount += authoritative_price

        validated_items.append(
            {
                "event_id": item.event_id,
                "event_oid": event_oid,
                "event_title": event_doc.get("title", ""),
                "seat_zone": matched_zone.get("name"),
                "seat_number": item.seat_number,
                "price": authoritative_price,
                "capacity": zone_capacity,
            }
        )

    # 4. Atomic MongoDB Capacity Reservation (prevents overselling)
    mongo_reserved_zones = []
    for v_item in validated_items:
        update_res = events_collection.update_one(
            {
                "_id": v_item["event_oid"],
                "zones": {
                    "$elemMatch": {
                        "name": v_item["seat_zone"],
                        "booked_count": {"$lt": v_item["capacity"]},
                    }
                },
            },
            {"$inc": {"zones.$.booked_count": 1}},
        )
        if update_res.modified_count == 0:
            # Compensate previously reserved zones in this order
            for ev_oid, z_name in mongo_reserved_zones:
                try:
                    events_collection.update_one(
                        {"_id": ev_oid, "zones.name": z_name},
                        {"$inc": {"zones.$.booked_count": -1}},
                    )
                except Exception:
                    pass
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Zone '{v_item['seat_zone']}' for event '{v_item['event_title']}' is sold out",
            )
        mongo_reserved_zones.append((v_item["event_oid"], v_item["seat_zone"]))

    # 5. PostgreSQL Transaction: Order & Ticket Creation with 10-Minute Hold Window
    now = datetime.now(timezone.utc)
    hold_expires_at = now + timedelta(minutes=10)
    order_number = f"ORD-{now.strftime('%Y%m%d')}-{uuid.uuid4().hex[:8].upper()}"

    new_order = Order(
        order_number=order_number,
        user_id=order_in.user_id,
        total_amount=total_amount,
        status="pending",  # Pending state holds seats until paid
        payment_method=order_in.payment_method,
        expires_at=hold_expires_at,
        idempotency_key=idempotency_key,
    )

    try:
        db.add(new_order)
        db.flush()  # Generates new_order.id

        created_tickets = []
        for v_item in validated_items:
            ticket_code = f"TKT-{uuid.uuid4().hex[:12].upper()}"
            ticket = Ticket(
                order_id=new_order.id,
                event_id=v_item["event_id"],
                ticket_code=ticket_code,
                seat_zone=v_item["seat_zone"],
                seat_number=v_item["seat_number"],
                price=v_item["price"],
                status="held",  # Held during the 10-minute hold window
            )
            created_tickets.append(ticket)
            db.add(ticket)

        db.flush()

        # Transactional Outbox Pattern: Record SEAT_HELD event in PostgreSQL transaction
        outbox = OutboxEvent(
            event_type="SEAT_HELD",
            aggregate_id=str(new_order.id),
            payload=json.dumps({
                "order_id": new_order.id,
                "order_number": order_number,
                "expires_at": hold_expires_at.isoformat(),
                "tickets": [
                    {"event_id": t.event_id, "seat_zone": t.seat_zone, "seat_number": t.seat_number}
                    for t in created_tickets
                ],
            }),
            status="pending",
        )
        db.add(outbox)

        db.commit()
        db.refresh(new_order)

    except IntegrityError:
        # Enforces double-booking prevention: duplicate (event_id, seat_zone, seat_number)
        db.rollback()
        for ev_oid, z_name in mongo_reserved_zones:
            try:
                events_collection.update_one(
                    {"_id": ev_oid, "zones.name": z_name},
                    {"$inc": {"zones.$.booked_count": -1}},
                )
            except Exception:
                pass
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Double-booking prevented: One or more selected seats have already been reserved for this event.",
        )

    except Exception as exc:
        db.rollback()
        for ev_oid, z_name in mongo_reserved_zones:
            try:
                events_collection.update_one(
                    {"_id": ev_oid, "zones.name": z_name},
                    {"$inc": {"zones.$.booked_count": -1}},
                )
            except Exception:
                pass

        if isinstance(exc, HTTPException):
            raise exc
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Dual-database transaction failed: {str(exc)}",
        )

    # 6. Telemetry logging to MongoDB 'activity_logs'
    try:
        mongo_db["activity_logs"].insert_one(
            {
                "user_id": order_in.user_id,
                "action": "hold_seats",
                "resource_type": "order",
                "resource_id": order_number,
                "metadata": {
                    "order_id": new_order.id,
                    "tickets_count": len(order_in.items),
                    "total_amount": float(total_amount),
                    "expires_at": hold_expires_at.isoformat(),
                },
                "timestamp": now,
            }
        )
    except Exception:
        pass

    return new_order


def cleanup_expired_seat_holds(db: Session, mongo_db: Database) -> int:
    """Find all expired pending orders and release their held seats in MongoDB.

    Cross-DB reconciliation job ensuring held seats never stay locked forever.
    """
    now = datetime.now(timezone.utc)
    expired_orders = (
        db.query(Order)
        .options(joinedload(Order.tickets))
        .filter(Order.status == "pending", Order.expires_at < now)
        .all()
    )

    cleaned_count = 0
    for order in expired_orders:
        order.status = "expired"
        for ticket in order.tickets:
            ticket.status = "cancelled"
            try:
                mongo_db["events"].update_one(
                    {"_id": ObjectId(ticket.event_id), "zones.name": ticket.seat_zone},
                    {"$inc": {"zones.$.booked_count": -1}},
                )
            except Exception:
                pass

        outbox = OutboxEvent(
            event_type="SEAT_HOLD_EXPIRED",
            aggregate_id=str(order.id),
            payload=json.dumps({"order_id": order.id, "order_number": order.order_number, "reason": "cron_cleanup"}),
            status="pending",
        )
        db.add(outbox)
        cleaned_count += 1

    if cleaned_count > 0:
        db.commit()

    return cleaned_count
