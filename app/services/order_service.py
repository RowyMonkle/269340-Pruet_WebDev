import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional
from bson import ObjectId
from bson.errors import InvalidId
from fastapi import HTTPException, status
from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload, load_only
from sqlalchemy.orm.attributes import set_committed_value #mark a relationship as already load (no select)
from pymongo.database import Database

from app.core.config import settings
from app.core.db_errors import classify_db_error, pgcode
from app.models.sql_models import Order, Ticket, OutboxEvent
from app.schemas.order import OrderCreate

logger = logging.getLogger("order_service")
#partial unique index that blocks double-booking (see sql_models.Ticket)
SEAT_UNIQUE_CONSTRAINT = "uq_tickets_event_zone_seat"  
FOREIGN_KEY_VIOLATION = "23503" # SQLSTATE when orders.user_id points at a user that does not exist

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

def _violated_constraint(exc: IntegrityError) -> str:  # which constraint an IntegrityError broke
    """Name of the violated constraint (psycopg2 diag), falling back to the error text."""
    diag = getattr(getattr(exc, "orig", None), "diag", None) 
    name = getattr(diag, "constraint_name", None)
    return (name or str(exc)).lower()  # fallback keeps working if diag is missing

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
    2. Verify events and zones in MongoDB (one read for all events), extract authoritative prices.
    3. Atomically reserve seat capacity in MongoDB: ONE conditional update per (event, zone) for the whole
       quantity (booked_count <= capacity - quantity), all-or-nothing.
    4. PostgreSQL transaction in 3 round trips: INSERT order, INSERT all tickets (one multi-row statement,
       in one global seat order so concurrent orders cannot deadlock), INSERT the SEAT_HELD outbox event.
       The user is verified by the orders.user_id FOREIGN KEY (unknown user -> 404), not by a separate SELECT,
       and nothing is read back after COMMIT: RETURNING already gave us the ids and server defaults.
    5. Catch concurrent idempotency races, unknown users, double-booking violations and database errors
       (deadlock / serialization failure -> 409, pool / statement timeout -> 503); give the MongoDB seats
       back on any failure.
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

    

     # 2. Pre-validate Event & Zone in MongoDB and determine Authoritative Pricing
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

    # 3. Atomic MongoDB Capacity Reservation (prevents overselling)
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
    try:
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
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Zone '{zone_name}' for event '{request['title']}' is sold out",
                )
            mongo_reserved.append((event_oid, zone_name, request["quantity"]))
    except Exception:
        # sold out in a later zone, or MongoDB became unreachable half way: give back the zones already taken
        # (the error then propagates: 409 for sold out, 503 for an unreachable MongoDB)
        _release_reservations(events_collection, mongo_reserved)
        raise

    # 4. PostgreSQL Transaction: Order & Ticket Creation with 10-Minute Hold Window
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

    # tickets attached through the relationship, so SQLAlchemy fills in order_id after the order INSERT
    # and knows the collection is already loaded (no SELECT tickets when the response is built).
    # validated_items is sorted, so the multi-row INSERT still takes the seats in the one global order.
    new_order.tickets = [
        Ticket(
            event_id=v_item["event_id"],
            ticket_code=f"TKT-{uuid.uuid4().hex[:12].upper()}",
            seat_zone=v_item["seat_zone"],
            seat_number=v_item["seat_number"],
            price=v_item["price"],
            status="held",  # Held during the 10-minute hold window
        )
        for v_item in validated_items
    ]

     #keep what INSERT ... RETURNING gave us (ids, created_at) valid after COMMIT.
    db.expire_on_commit = False
    
    try:
        db.add(new_order)
        db.flush()  # Generates new_order.id
        set_committed_value(new_order, "payments", [])

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
                    for t in new_order.tickets
                ],
            }),
            status="pending",
        )
        db.add(outbox)

        db.commit() # INSERT outbox + COMMIT
        

    except IntegrityError as iexc:
        db.rollback()
        # Compensate MongoDB reserved seats
        _release_reservations(events_collection, mongo_reserved)

        err_str = str(iexc).lower()
        constraint = _violated_constraint(iexc)

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

        # the foreign key replaced the SELECT user, so an unknown user is reported here
        if pgcode(iexc) == FOREIGN_KEY_VIOLATION and "user_id" in constraint:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User with ID {order_in.user_id} does not exist",
            )

        if SEAT_UNIQUE_CONSTRAINT in constraint:  # only the seat index means double-booking (was: every IntegrityError)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Double-booking prevented: One or more selected seats have already been reserved for this event.",
            )

        # any other constraint (FK, check, duplicate ticket code...) is a real bug, not a seat race
        logger.exception("Unexpected integrity error while creating order (constraint: %s)", constraint)

        # Otherwise it was a double-booking seat violation
        raise HTTPException( 
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Order could not be created because of a data integrity error. Nothing was saved.",
        )

    except Exception as exc:
        db.rollback()
        _release_reservations(events_collection, mongo_reserved)

        if isinstance(exc, HTTPException):
            raise exc
        http_exc = classify_db_error(exc)  # 409 for collisions AND 503 for pool / statement timeout
        if http_exc is not None:
            raise http_exc
        logger.exception("Dual-database transaction failed for user %s", order_in.user_id)  # FIX: moved out of the if and above the raise (was unreachable after `raise http_exc`)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Dual-database transaction failed. Nothing was saved and the reserved seats were released.",  # no str(exc), internal SQL must not leak
        )

    # 5. Telemetry logging to MongoDB 'activity_logs'
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


def _release_counted_seats(mongo_db: Database, groups: dict) -> None:
    """Give seats back in MongoDB: one update per (event, zone) with the number of seats, not one per ticket.

    The guard `booked_count >= n` keeps a counter from going negative. If the counter is already lower
    than n (drift), fall back to releasing one seat at a time while the counter stays above zero.
    """
    for (event_id, zone_name), quantity in groups.items():
        try:
            oid = ObjectId(event_id)
            result = mongo_db["events"].update_one(
                {"_id": oid, "zones": {"$elemMatch": {"name": zone_name, "booked_count": {"$gte": quantity}}}},
                {"$inc": {"zones.$.booked_count": -quantity}},
            )
            if result.modified_count:
                continue
            for _ in range(quantity):
                mongo_db["events"].update_one(
                    {"_id": oid, "zones": {"$elemMatch": {"name": zone_name, "booked_count": {"$gt": 0}}}},
                    {"$inc": {"zones.$.booked_count": -1}},
                )
        except Exception as exc:  # noqa: BLE001 - logged; scripts/verify_consistency.py --repair finds any seat left over
            logger.error(f"Failed to release {quantity} seat(s) in MongoDB for event {event_id} zone {zone_name}: {exc}")


def _sweep_one_batch(db: Session, mongo_db: Database, batch_size: int) -> int:
    """Expire up to batch_size overdue holds in ONE short PostgreSQL transaction, then release their seats."""
    now = datetime.now(timezone.utc)
    # Claim rows nobody else holds (SKIP LOCKED: never wait for an order that is being paid right now).
    # ORDER BY expires_at, id (oldest overdue first) lets idx_orders_status_expires answer the whole query. With
    # ORDER BY id the planner walked the primary key and filtered every order in the table (23 ms at 130k orders,
    # growing with the table, on every tick of every replica): measured in docs/perf/index_review.md.
    claimed = db.execute(
        select(Order.id, Order.order_number)
        .where(Order.status == "pending", Order.expires_at < now)
        .order_by(Order.expires_at, Order.id)
        .limit(batch_size)
        .with_for_update(of=Order, skip_locked=True)
    ).all()
    if not claimed:
        db.rollback()
        return 0
    order_ids = [row.id for row in claimed]

    # How many seats each (event, zone) gets back: counted in SQL, not by looping over tickets in Python
    groups = {
        (row.event_id, row.seat_zone): row.n
        for row in db.execute(
            select(Ticket.event_id, Ticket.seat_zone, func.count().label("n"))
            .where(Ticket.order_id.in_(order_ids), Ticket.status == "held")
            .group_by(Ticket.event_id, Ticket.seat_zone)
        )
    }

    db.execute(update(Order).where(Order.id.in_(order_ids)).values(status="expired").execution_options(synchronize_session=False))
    db.execute(
        update(Ticket)
        .where(Ticket.order_id.in_(order_ids), Ticket.status == "held")
        .values(status="cancelled")
        .execution_options(synchronize_session=False)
    )
    db.execute(
        insert(OutboxEvent),
        [
            {
                "event_type": "SEAT_HOLD_EXPIRED",
                "aggregate_id": str(row.id),
                "payload": json.dumps({"order_id": row.id, "order_number": row.order_number, "reason": "cron_cleanup"}),
                "status": "pending",
            }
            for row in claimed
        ],
    )

    # 1. Commit PostgreSQL FIRST so the database of record is right even if MongoDB fails below
    db.commit()
    # 2. Then release the seats in MongoDB
    _release_counted_seats(mongo_db, groups)
    return len(order_ids)


def cleanup_expired_seat_holds(
    db: Session,
    mongo_db: Database,
    batch_size: Optional[int] = None,
    max_batches: Optional[int] = None,
) -> int:
    """Find overdue pending orders and release their held seats in MongoDB. Returns how many orders were expired.

    Cross-DB Reconciliation with Drift Protection:
    - Works in batches (SWEEP_BATCH_SIZE orders per transaction, at most SWEEP_MAX_BATCHES per call), so a flash
      sale that leaves thousands of holds behind never means one transaction holding thousands of row locks
      or one call that runs for minutes; the next call continues.
    - SKIP LOCKED avoids conflicting with orders actively being paid.
    - Commits PostgreSQL status updates FIRST.
    - Releases MongoDB capacity with one grouped update per (event, zone) and a booked_count guard, logging failures.
    """
    batch_size = batch_size or settings.SWEEP_BATCH_SIZE
    max_batches = max_batches or settings.SWEEP_MAX_BATCHES
    total = 0
    for _ in range(max_batches):
        done = _sweep_one_batch(db, mongo_db, batch_size)
        total += done
        if done < batch_size:
            break
    return total
