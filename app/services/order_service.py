import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Optional
from bson import ObjectId
from bson.errors import InvalidId
from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload, load_only, selectinload
from pymongo.database import Database

from app.core.db_errors import conflict_exception, is_retryable_conflict
from app.models.sql_models import User, Order, Ticket, OutboxEvent
from app.schemas.order import OrderCreate

logger = logging.getLogger("order_service")


def _canonical_event_id(raw_id: str) -> str:
    """The one spelling of an event id (ObjectId hex is case-insensitive, the unique seat index is not)."""
    try:
        return str(ObjectId(raw_id))
    except InvalidId:
        return raw_id


def _zone_key(zone_name: str) -> str:
    return zone_name.strip().lower()


def _lock_order_key(item: dict) -> tuple:
    """Sort key giving every order the same seat order: event, zone, then seat."""
    return (item["event_id"], item["seat_zone"], item["seat_number"] or "")


def _release_reservations(events_collection, reserved: list) -> None:
    """Give back seats reserved in MongoDB for an order that never reached PostgreSQL.

    `reserved` holds (event ObjectId, zone name, number of seats). Best effort: a failure is logged
    (never raised) so the original error still reaches the client; scripts/verify_consistency.py
    finds and repairs any seat this could not return.
    """
    for ev_oid, z_name, quantity in reserved:
        try:
            events_collection.update_one(
                {"_id": ev_oid, "zones.name": z_name},
                {"$inc": {"zones.$.booked_count": -quantity}},
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Could not release %s seat(s) for event %s zone %s: %s", quantity, ev_oid, z_name, exc)


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
    1. Check Idempotency-Key: if key already exists, verify payload fingerprint. If payload differs, return 409.
       If identical, return previous order without re-charging or duplicating seats.
    2. Verify customer account in PostgreSQL.
    3. Verify events and zones in MongoDB (one read for all events), extract authoritative prices.
    4. Atomically reserve seat capacity in MongoDB: ONE conditional update per (event, zone) for the whole
       quantity (booked_count <= capacity - quantity), all-or-nothing.
    5. Start PostgreSQL transaction: persist Order with 10-minute hold expiry and Tickets with 'held' status,
       inserting seats in one global order (event, zone, seat) so concurrent orders cannot deadlock.
    6. Catch concurrent idempotency races, double-booking violations and database collisions
       (deadlock / serialization failure -> 409); give the MongoDB seats back on any failure.
    7. Record SEAT_HELD in OutboxEvent table for cross-DB consistency.
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
            # Validate payload fingerprint: same user and matching ticket items
            if existing_order.user_id != order_in.user_id or len(existing_order.tickets) != len(order_in.items):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Idempotency key reused with different request payload (user or item count mismatch).",
                )
            existing_items_sig = sorted([(t.event_id, _zone_key(t.seat_zone)) for t in existing_order.tickets])
            request_items_sig = sorted(
                [(_canonical_event_id(item.event_id), _zone_key(item.seat_zone)) for item in order_in.items]
            )
            if existing_items_sig != request_items_sig:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Idempotency key reused with different ticket items or zones.",
                )
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

    # One query for every distinct event in the order (no query per ticket)
    wanted_oids = set()
    for item in order_in.items:
        try:
            wanted_oids.add(ObjectId(item.event_id))
        except InvalidId:
            pass  # reported in order below
    event_docs = {}
    if wanted_oids:
        event_docs = {
            doc["_id"]: doc
            for doc in events_collection.find(
                {"_id": {"$in": list(wanted_oids)}},
                projection={"_id": 1, "title": 1, "zones": 1, "status": 1},
            )
        }

    for item in order_in.items:
        try:
            event_oid = ObjectId(item.event_id)
        except InvalidId:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid event ID: {item.event_id}",
            )

        event_doc = event_docs.get(event_oid)
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
                # canonical spelling: "ABC..." and "abc..." are the same event and must lock the same seat
                "event_id": str(event_oid),
                "event_oid": event_oid,
                "event_title": event_doc.get("title", ""),
                "seat_zone": matched_zone.get("name"),
                "seat_number": (item.seat_number or "").strip() or None,
                "price": authoritative_price,
                "capacity": zone_capacity,
            }
        )

    # Concurrency control (pessimistic ordering): every order takes its seats in the same
    # global order. Two orders that want the same seats in opposite order would otherwise
    # each hold one unique-index entry and wait for the other (deadlock, HTTP 500).
    validated_items.sort(key=_lock_order_key)

    # 4. Atomic MongoDB Capacity Reservation (prevents overselling)
    # One conditional update per (event, zone) for ALL the tickets the order takes there: the order
    # either gets its whole quantity or nothing, so concurrent multi-ticket orders can never block
    # each other with half-reserved zones, and a 4-ticket order costs one Mongo call, not four.
    zone_requests = {}
    for v_item in validated_items:
        key = (v_item["event_oid"], v_item["seat_zone"])
        request = zone_requests.setdefault(
            key, {"quantity": 0, "capacity": v_item["capacity"], "title": v_item["event_title"]}
        )
        request["quantity"] += 1

    mongo_reserved = []  # (event ObjectId, zone name, seats) taken so far, returned if anything fails
    for (event_oid, zone_name), request in zone_requests.items():
        update_res = events_collection.update_one(
            {
                "_id": event_oid,
                "zones": {
                    "$elemMatch": {
                        "name": zone_name,
                        "booked_count": {"$lte": request["capacity"] - request["quantity"]},
                    }
                },
            },
            {"$inc": {"zones.$.booked_count": request["quantity"]}},
        )
        if update_res.modified_count == 0:
            # Compensate previously reserved zones in this order
            _release_reservations(events_collection, mongo_reserved)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Zone '{zone_name}' for event '{request['title']}' is sold out",
            )
        mongo_reserved.append((event_oid, zone_name, request["quantity"]))

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

    except IntegrityError as iexc:
        db.rollback()
        # Compensate MongoDB reserved seats
        _release_reservations(events_collection, mongo_reserved)

        err_str = str(iexc).lower()

        # If concurrent duplicate idempotency key raced, return winning order
        if idempotency_key and ("idempotency_key" in err_str or "uq_orders_idempotency_key" in err_str):
            winning_order = (
                db.query(Order)
                .options(joinedload(Order.tickets), joinedload(Order.payments))
                .filter(Order.idempotency_key == idempotency_key)
                .first()
            )
            if winning_order:
                return winning_order

        # Otherwise it was a double-booking seat violation
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Double-booking prevented: One or more selected seats have already been reserved for this event.",
        )

    except Exception as exc:
        db.rollback()
        _release_reservations(events_collection, mongo_reserved)

        if isinstance(exc, HTTPException):
            raise exc
        if is_retryable_conflict(exc):
            # deadlock / serialization failure / lock timeout: nothing was written, the client retries
            raise conflict_exception(exc)
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

    Cross-DB Reconciliation with Drift Protection:
    - Uses with_for_update(skip_locked=True) to avoid conflicting with orders actively being paid.
    - Commits PostgreSQL status updates FIRST.
    - Releases MongoDB capacity ONLY with booked_count > 0 guard, logging any failures.
    """
    now = datetime.now(timezone.utc)
    expired_orders = (
        db.query(Order)
        .options(selectinload(Order.tickets))
        .filter(Order.status == "pending", Order.expires_at < now)
        .with_for_update(of=Order, skip_locked=True)
        .all()
    )

    if not expired_orders:
        return 0

    to_release = []
    cleaned_count = 0

    for order in expired_orders:
        order.status = "expired"
        for ticket in order.tickets:
            ticket.status = "cancelled"
            to_release.append((ticket.event_id, ticket.seat_zone))

        outbox = OutboxEvent(
            event_type="SEAT_HOLD_EXPIRED",
            aggregate_id=str(order.id),
            payload=json.dumps({"order_id": order.id, "order_number": order.order_number, "reason": "cron_cleanup"}),
            status="pending",
        )
        db.add(outbox)
        cleaned_count += 1

    # 1. Commit PostgreSQL transaction FIRST to ensure consistency
    db.commit()

    # 2. Release seats in MongoDB with booked_count > 0 guard
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
            logger.error(f"Failed to release seat in MongoDB for event {ev_id} zone {z_name}: {exc}")

    return cleaned_count
