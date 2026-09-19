import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import List
from bson import ObjectId
from bson.errors import InvalidId
from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload, load_only
from pymongo.database import Database

from app.models.sql_models import User, Order, Ticket
from app.schemas.order import OrderCreate


def get_order_by_id(db: Session, order_id: int) -> Order:
    """Fetch order details with associated tickets using joinedload to eliminate N+1 queries."""
    order = (
        db.query(Order)
        .options(joinedload(Order.tickets))
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
) -> Order:
    """Execute Dual-Database Transaction for ticket purchasing.

    Orchestration:
    1. Verify customer account in PostgreSQL (relational boundary).
    2. Verify event and zone in MongoDB, extract authoritative price from MongoDB catalog (ignores client tampering).
    3. Atomically reserve seat capacity in MongoDB using $elemMatch and $lt capacity condition (prevents overselling).
    4. Start PostgreSQL transaction and persist Order + Ticket entities.
    5. Catch double-booking violations via partial unique index on (event_id, seat_zone, seat_number).
    6. Roll back and execute compensating transactions if any step fails.
    7. Record telemetry to MongoDB 'activity_logs'.
    """
    # 1. Verify User exists in PostgreSQL (load only id for performance)
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

    # 2. Pre-validate Event & Zone in MongoDB and determine Authoritative Pricing
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
        # Strictly use authoritative price stored in MongoDB catalog (prevents client price manipulation)
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

    # 3. Atomic MongoDB Capacity Reservation (prevents race conditions & overselling)
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

    # 4. PostgreSQL Transaction: Order & Ticket Creation
    order_number = f"ORD-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:8].upper()}"
    new_order = Order(
        order_number=order_number,
        user_id=order_in.user_id,
        total_amount=total_amount,
        status="confirmed",
        payment_method=order_in.payment_method,
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
                status="valid",
            )
            created_tickets.append(ticket)
            db.add(ticket)

        db.flush()
        db.commit()
        db.refresh(new_order)

    except IntegrityError as iexc:
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

    # 5. Asynchronous / Telemetry logging to MongoDB 'activity_logs'
    try:
        mongo_db["activity_logs"].insert_one(
            {
                "user_id": order_in.user_id,
                "action": "create_order",
                "resource_type": "order",
                "resource_id": order_number,
                "metadata": {
                    "order_id": new_order.id,
                    "tickets_count": len(order_in.items),
                    "total_amount": float(total_amount),
                    "payment_method": order_in.payment_method,
                },
                "timestamp": datetime.now(timezone.utc),
            }
        )
    except Exception:
        pass  # Non-blocking telemetry

    return new_order
