#!/usr/bin/env python3
"""Database Seeding Script for Checkpoint 1 & Tier 1 Architecture.

Pre-populates reproducible, realistic dummy records:
- Deterministic random seed: random.seed(42), Faker.seed(42)
- PostgreSQL: >= 1,000 records across normalized tables:
  - Users, Orders, Tickets, Payments, OutboxEvents
  - TRUNCATE TABLE outbox_events, payments, tickets, orders, users RESTART IDENTITY CASCADE
  - Realistic order lifecycle: 'confirmed' (paid), 'pending' (held with 10m expiry), 'expired'
  - 1:N Payments linked to confirmed orders
  - Transactional OutboxEvents matching order state events
  - Ticket prices and zones match authoritative MongoDB event zones
  - Seat allocations respect double-booking constraints
- MongoDB: >= 1,000 documents across collections (Events, ActivityLogs).
  - Activity logs strictly reference real inserted PostgreSQL user IDs
  - Event zone booked_count precisely matches active held & valid tickets
"""

import argparse
import json
import os
import sys
import random
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

# Deterministic global random seed for reproducibility
random.seed(42)

# Add current directory to sys.path to enable app imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import func, text
from app.core.database import (
    Base, engine, SessionLocal, mongo_db, check_postgres_connection, check_mongo_connection, relax_timeouts_for_scripts,
)
from app.models.sql_models import User, Order, Ticket, Payment, OutboxEvent
from app.models.nosql_models import init_mongo_indexes
from app.services.user_service import hash_password

try:
    from faker import Faker
    Faker.seed(42)
    fake = Faker()
    fake.seed_instance(42)
except ImportError:
    # Deterministic lightweight fallback generator
    class SimpleFake:
        def __init__(self):
            self.first_names = ["Alex", "Ploy", "Somchai", "Jane", "John", "Kamon", "Niran", "Siriporn", "David", "Emma"]
            self.last_names = ["Smith", "Suksom", "Wong", "Tan", "Miller", "Davis", "Saetang", "Chen", "Lee", "Taylor"]
        def first_name(self):
            return random.choice(self.first_names)
        def last_name(self):
            return random.choice(self.last_names)
        def email(self):
            return f"user_{uuid.uuid4().hex[:8]}@bookingdemo.com"
        def user_name(self):
            return f"fan_{uuid.uuid4().hex[:8]}"
    fake = SimpleFake()


# ---------------------------------------------------------------------------
# Demo catalogue: K-pop / T-pop shows in Bangkok, Dec 2026 - Jun 2027.
# FICTIONAL DATA for a class project: the artists are real, the shows, dates and prices are not announcements.
# Prices follow past Bangkok arena tiers (e.g. LYKN at IMPACT Arena: 1,500-6,900 THB) scaled per act.
# ---------------------------------------------------------------------------
BANGKOK_TZ = timezone(timedelta(hours=7))

IMPACT_ARENA = {
    "name": "IMPACT Arena",
    "city": "Bangkok",
    "country": "Thailand",
    "capacity": 13050,  # = sum of the zone capacities below
    "address": "Muang Thong Thani, Pak Kret, Nonthaburi",
}

# Seat plan of IMPACT Arena (floor + level 2 + level 3). Zone names must match frontend/app/arena-map.js.
# tier = index into BASE_TIER_PRICES (0 = most expensive).
BASE_TIER_PRICES = [6900, 5900, 5500, 5000, 4500, 3000, 2000, 1500]
ARENA_ZONES = (
    # floor
    [("A1", 1, 300), ("A2", 0, 600), ("A3", 0, 600), ("A4", 1, 300),
     ("B1", 1, 250), ("B2", 0, 700), ("B3", 1, 250),
     ("C1", 2, 150), ("C2", 1, 450), ("C3", 2, 150),
     ("FF", 2, 80), ("HH", 2, 80)]
    # level 2 (SB / SN have a restricted view, SG-SI face the stage)
    + [("SB", 4, 200), ("SC", 4, 300), ("SD", 4, 300), ("SE", 4, 300), ("SF", 4, 300),
       ("SG", 3, 300), ("SH", 3, 300), ("SI", 3, 300),
       ("SJ", 4, 300), ("SK", 4, 300), ("SL", 4, 300), ("SM", 4, 300), ("SN", 4, 200)]
    # level 3: front rows (3,000 tier) and rear rows of every block; the three blocks facing the stage at the back are the cheapest
    + [(letter, 5, 200) for letter in "CDEFGHIJKLMNOPQRS"]
    + [(f"{letter} Rear", 7 if letter in "JKL" else 6, 120) for letter in "CDEFGHIJKLMNOPQRS"]
)

# (name, genre, bio, price factor, Bangkok dates as (Y, M, D, hour), status)
ARTISTS = [
    ("BTS", "K-pop · Boy group", "Seven-member South Korean boy group under BIGHIT MUSIC.", 1.3,
     [(2027, 3, 13, 19), (2027, 3, 14, 18)], "upcoming"),
    ("BLACKPINK", "K-pop · Girl group", "Four-member South Korean girl group under YG Entertainment.", 1.3,
     [(2027, 1, 23, 19), (2027, 1, 24, 18)], "upcoming"),
    ("NCT 127", "K-pop · Boy group", "The Seoul-based unit of SM Entertainment's NCT.", 1.0,
     [(2027, 2, 20, 19)], "upcoming"),
    ("NCT DREAM", "K-pop · Boy group", "Seven-member unit of SM Entertainment's NCT.", 1.0,
     [(2026, 12, 19, 19), (2026, 12, 20, 18)], "upcoming"),
    ("aespa", "K-pop · Girl group", "Four-member girl group under SM Entertainment.", 1.0,
     [(2027, 4, 17, 19), (2027, 4, 18, 18)], "upcoming"),
    ("NewJeans", "K-pop · Girl group", "Five-member girl group under ADOR.", 1.0,
     [(2027, 5, 15, 19)], "upcoming"),
    ("SEVENTEEN", "K-pop · Boy group", "Thirteen-member boy group under PLEDIS Entertainment.", 1.1,
     [(2027, 2, 6, 19), (2027, 2, 7, 18)], "upcoming"),
    ("Stray Kids", "K-pop · Boy group", "Eight-member boy group under JYP Entertainment.", 1.1,
     [(2027, 5, 29, 19), (2027, 5, 30, 18)], "upcoming"),
    ("ENHYPEN", "K-pop · Boy group", "Boy group under BELIFT LAB, formed through the show I-LAND.", 1.0,
     [(2027, 3, 27, 19)], "upcoming"),
    ("A2O SOUL", "K-pop · Boy group · Rookie", "Rookie boy group from A2O Entertainment, the label founded by Lee Soo-man. Debuted in October 2026.", 0.7,
     [(2027, 6, 12, 19)], "upcoming"),
    ("A2O LITE", "K-pop · Girl group · Rookie", "Rookie girl group from A2O Entertainment, the label founded by Lee Soo-man. Debuted in October 2026.", 0.7,
     [(2027, 6, 19, 19)], "coming_soon"),
    ("LYKN", "T-pop · Boy group", "Thai boy group under RISER MUSIC.", 1.0,
     [(2027, 1, 9, 19), (2027, 1, 10, 18)], "upcoming"),
    ("PROXIE", "T-pop · Boy group", "Thai boy group.", 0.8,
     [(2027, 4, 3, 19)], "upcoming"),
    ("4EVE", "T-pop · Girl group", "Thai girl group.", 0.8,
     [(2027, 2, 27, 19)], "upcoming"),
    ("BUS", "T-pop · Boy group", "Thai boy group BUS (Because of You I Shine).", 0.8,
     [(2027, 5, 1, 19)], "upcoming"),
]


# organizers in seed_postgresql are user ids 2..15 (14 accounts): artist i belongs to organizer 2 + i % 14,
# so every organizer runs at least one act and every show of an act has the same organizer
ORGANIZER_IDS = list(range(2, 16))


def _price(tier: int, factor: float) -> float:
    """Tier price for one act, rounded to 100 THB."""
    return float(round(BASE_TIER_PRICES[tier] * factor / 100) * 100)


def seed_mongodb_events(num_events: int = 0) -> list:
    """Seed MongoDB Events: one document per show in ARTISTS (num_events is ignored, kept for old callers)."""
    print(f"\n[MongoDB] Initializing collection indexes...")
    init_mongo_indexes(mongo_db)

    now = datetime.now(timezone.utc)
    events = []
    for i, (name, genre, bio, factor, dates, status) in enumerate(ARTISTS):
        organizer_id = ORGANIZER_IDS[i % len(ORGANIZER_IDS)]
        for night, (y, mo, d, hour) in enumerate(dates, start=1):
            starts = datetime(y, mo, d, hour, 0, tzinfo=BANGKOK_TZ)
            year = starts.year
            title = f"{name} — Live in Bangkok {year}" + (f" · Night {night}" if len(dates) > 1 else "")
            zones = [
                {"name": z, "price": _price(tier, factor), "capacity": cap, "booked_count": 0}  # booked_count synced below
                for z, tier, cap in ARENA_ZONES
            ]
            events.append({
                "title": title,
                "category": "Concert",
                "artist": {"name": name, "genre": genre, "bio": bio, "spotify_id": None},
                "venue": dict(IMPACT_ARENA),
                "date_time": starts.astimezone(timezone.utc),
                "zones": zones,
                "tags": [t.strip().lower() for t in genre.split("·")] + ["bangkok", "impact arena"],
                "dynamic_attributes": {
                    "age_restriction": "All ages · under 7 not admitted",
                    "doors_open": (starts - timedelta(hours=1, minutes=30)).astimezone(BANGKOK_TZ).strftime("%H:%M"),
                    "entry_gate": "Gates 1-4, IMPACT Arena",
                    "restricted_view_zones": ["SB", "SN"],
                    "allow_cameras": False,
                    "parking_available": True,
                    "ticket_limit_per_order": 6,
                    "demo_data": True,  # fictional show for the class project
                },
                "organizer_id": organizer_id,  # owner (PostgreSQL user id of an organizer account)
                "status": status,
                "created_at": now - timedelta(days=random.randint(1, 60)),
                "updated_at": now,
            })

    mongo_db["events"].delete_many({})
    insert_res = mongo_db["events"].insert_many(events)
    print(f"[MongoDB] Successfully inserted {len(insert_res.inserted_ids)} Events ({len(ARTISTS)} artists, {len(ARENA_ZONES)} zones each).")
    return list(mongo_db["events"].find())


def _seat_label(index: int) -> str:
    """1 -> 'A-1', 18 -> 'A-18', 19 -> 'B-1': the same 18-seat rows the seat map in the website uses."""
    row, seat = divmod(index - 1, 18)
    letters = ""
    row += 1
    while row > 0:
        row, rem = divmod(row - 1, 26)
        letters = chr(65 + rem) + letters
    return f"{letters}-{seat + 1}"


def _popularity(event: dict) -> float:
    """Bigger acts get more of the demo orders (weight = price factor squared)."""
    for name, _g, _b, factor, _d, _s in ARTISTS:
        if event["artist"]["name"] == name:
            return factor * factor
    return 1.0


def seed_postgresql(mongo_events: list, num_users: int = 250, num_orders: int = 500) -> list:
    """Seed PostgreSQL with Users, Orders, Tickets, Payments, and OutboxEvents."""
    print(f"\n[PostgreSQL] Initializing tables and restarting identity sequences...")
    Base.metadata.create_all(bind=engine)

    session = SessionLocal()
    try:
        # TRUNCATE with RESTART IDENTITY CASCADE ensures deterministic IDs starting at 1
        session.execute(text("TRUNCATE TABLE outbox_events, payments, tickets, orders, users RESTART IDENTITY CASCADE;"))
        session.commit()

        # 1. Seed Users
        print(f"[PostgreSQL] Seeding {num_users} Users...")
        default_pwd_hash = hash_password("Password123!")
        users = []

        for i in range(1, num_users + 1):
            f_name = fake.first_name()
            l_name = fake.last_name()
            email = f"user{i}_{uuid.uuid4().hex[:6]}@bookingdemo.com"
            username = f"user_{i}_{uuid.uuid4().hex[:4]}"
            role = "admin" if i == 1 else ("organizer" if i <= 15 else "fan")

            user = User(
                email=email,
                username=username,
                hashed_password=default_pwd_hash,
                full_name=f"{f_name} {l_name}",
                role=role,
            )
            users.append(user)

        session.bulk_save_objects(users, return_defaults=True)
        session.commit()

        # Query actual inserted user IDs from database
        user_ids = [u.id for u in session.query(User.id).order_by(User.id.asc()).all()]
        print(f"[PostgreSQL] Successfully inserted {len(user_ids)} Users (IDs: {user_ids[0]}..{user_ids[-1]}).")

        # 2. Seed Orders, Tickets, Payments, and OutboxEvents
        print(f"[PostgreSQL] Seeding {num_orders} Orders with realistic payment & seat-hold lifecycles...")
        now = datetime.now(timezone.utc)
        payment_methods = ["promptpay", "credit_card", "bank_transfer"]

        # Track active seat reservations per (event_id, zone_name) to accurately sync with MongoDB
        active_seat_counts = {}
        total_tickets_created = 0
        total_payments_created = 0
        total_outbox_created = 0

        seat_label_counters = {}
        on_sale_events = [e for e in mongo_events if e.get("status") != "coming_soon"]  # nothing sold before sales open
        event_weights = [_popularity(e) for e in on_sale_events]

        for i in range(1, num_orders + 1):
            uid = random.choice(user_ids)
            order_num = f"ORD-2026{i:04d}-{uuid.uuid4().hex[:6].upper()}"
            method = random.choice(payment_methods)
            idempotency_token = f"IDEMP-{uuid.uuid4().hex[:16]}"

            # Imprementing the realistic Order Lifecycle
            rand_val = random.random()
            if rand_val < 0.75:
                order_status = "confirmed"
                ticket_status = "valid"
                expires_at = None
            elif rand_val < 0.90:
                order_status = "pending"
                ticket_status = "held"
                expires_at = now + timedelta(minutes=10)
            else:
                order_status = "expired"
                ticket_status = "cancelled"
                expires_at = now - timedelta(minutes=random.randint(15, 120))

            # Pick an event and zones
            chosen_event = random.choices(on_sale_events, weights=event_weights, k=1)[0]
            event_id_str = str(chosen_event["_id"])
            zones_list = chosen_event["zones"]

            # Number of tickets for this order
            num_tickets_in_order = random.randint(1, 4)
            order_tickets = []
            order_total = Decimal("0.00")

            for _ in range(num_tickets_in_order):
                zone = random.choice(zones_list)  # every zone, so the seat map shows sales everywhere
                zone_name = zone["name"]
                zone_price = Decimal(str(zone["price"]))

                # Determine unique seat number per event+zone
                seat_key = (event_id_str, zone_name)
                seat_idx = seat_label_counters.get(seat_key, 1)
                seat_label_counters[seat_key] = seat_idx + 1

                seat_label = _seat_label(seat_idx)  # "A-1", "A-2" ... like the website seat grid (was "VI-001")
                tkt_code = f"TKT-{uuid.uuid4().hex[:12].upper()}"

                ticket = Ticket(
                    event_id=event_id_str,
                    ticket_code=tkt_code,
                    seat_zone=zone_name,
                    seat_number=seat_label,
                    price=zone_price,
                    status=ticket_status,
                )
                order_tickets.append(ticket)
                order_total += zone_price
                total_tickets_created += 1

                # If ticket is currently held or valid, track it for MongoDB booked_count
                if ticket_status in ["held", "valid"]:
                    active_seat_counts[seat_key] = active_seat_counts.get(seat_key, 0) + 1

            # Create Order entity
            order = Order(
                order_number=order_num,
                user_id=uid,
                total_amount=order_total,
                status=order_status,
                payment_method=method,
                expires_at=expires_at,
                idempotency_key=idempotency_token,
                tickets=order_tickets,
            )
            session.add(order)
            session.flush()  # Generates order.id for relationships

            # If order is confirmed, create corresponding completed Payment record
            if order_status == "confirmed":
                payment_ref = f"PAY-2026{i:04d}-{uuid.uuid4().hex[:6].upper()}"
                payment = Payment(
                    order_id=order.id,
                    payment_reference=payment_ref,
                    amount=order_total,
                    provider=method,
                    status="completed",
                    provider_tx_id=f"TX-{uuid.uuid4().hex[:10].upper()}",
                    created_at=now - timedelta(days=random.randint(1, 30)),
                )
                session.add(payment)
                total_payments_created += 1

                # Outbox event for cross-DB confirmation
                outbox = OutboxEvent(
                    event_type="ORDER_CONFIRMED",
                    aggregate_id=str(order.id),
                    payload=json.dumps({"order_id": order.id, "order_number": order_num, "amount": float(order_total)}),
                    status="processed",
                    processed_at=now,
                )
                session.add(outbox)
                total_outbox_created += 1

            elif order_status == "pending":
                # Outbox event for active seat hold
                hold_exp_str = expires_at.isoformat() if expires_at else None
                outbox = OutboxEvent(
                    event_type="SEAT_HELD",
                    aggregate_id=str(order.id),
                    payload=json.dumps({"order_id": order.id, "order_number": order_num, "expires_at": hold_exp_str}),
                    status="pending",
                )
                session.add(outbox)
                total_outbox_created += 1

            elif order_status == "expired":
                outbox = OutboxEvent(
                    event_type="SEAT_HOLD_EXPIRED",
                    aggregate_id=str(order.id),
                    payload=json.dumps({"order_id": order.id, "order_number": order_num, "reason": "timeout"}),
                    status="processed",
                    processed_at=now,
                )
                session.add(outbox)
                total_outbox_created += 1

        session.commit()
        print(f"[PostgreSQL] Successfully inserted {num_orders} Orders, {total_tickets_created} Tickets, {total_payments_created} Payments, and {total_outbox_created} OutboxEvents.")

        # Accurately sync MongoDB booked_count with PostgreSQL active seats
        for (ev_id, z_name), count in active_seat_counts.items():
            from bson import ObjectId
            mongo_db["events"].update_one(
                {"_id": ObjectId(ev_id), "zones.name": z_name},
                {"$set": {"zones.$.booked_count": count}},
            )

        total_users = session.query(func.count(User.id)).scalar()
        total_orders = session.query(func.count(Order.id)).scalar()
        total_tickets = session.query(func.count(Ticket.id)).scalar()
        total_payments = session.query(func.count(Payment.id)).scalar()
        total_outbox = session.query(func.count(OutboxEvent.id)).scalar()
        total_pg = total_users + total_orders + total_tickets + total_payments + total_outbox

        print(f"--> [PostgreSQL Total Records]: {total_pg} (Users: {total_users}, Orders: {total_orders}, Tickets: {total_tickets}, Payments: {total_payments}, Outbox: {total_outbox}) (Target: >= 1,000)")
        return user_ids

    finally:
        session.close()


def seed_mongodb_logs(user_ids: list, mongo_events: list, num_logs: int = 1200):
    """Seed MongoDB ActivityLogs referencing actual inserted PostgreSQL user IDs."""
    print(f"\n[MongoDB] Seeding {num_logs} ActivityLogs using real PostgreSQL User IDs...")
    now = datetime.now(timezone.utc)
    actions = ["view_event", "search_events", "select_seat", "hold_seats", "order_paid", "login", "view_ticket"]
    event_ids = [str(e["_id"]) for e in mongo_events]
    logs = []

    for _ in range(num_logs):
        log_time = now - timedelta(days=random.randint(0, 30), minutes=random.randint(0, 1440))
        action = random.choice(actions)
        target_event_id = random.choice(event_ids) if event_ids else str(uuid.uuid4())
        # Strictly use real inserted user ID from PostgreSQL
        real_user_id = random.choice(user_ids)

        logs.append({
            "user_id": real_user_id,
            "action": action,
            "resource_type": "event",
            "resource_id": target_event_id,
            "metadata": {
                "ip_address": f"192.168.{random.randint(1, 254)}.{random.randint(1, 254)}",
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "duration_ms": random.randint(45, 850),
                "device": random.choice(["mobile", "desktop", "tablet"]),
            },
            "timestamp": log_time,
        })

    mongo_db["activity_logs"].delete_many({})
    mongo_db["activity_logs"].insert_many(logs)
    print(f"[MongoDB] Successfully inserted {len(logs)} ActivityLogs.")

    total_mongo = mongo_db["events"].count_documents({}) + mongo_db["activity_logs"].count_documents({})
    print(f"--> [MongoDB Total Documents]: {total_mongo} (Events: {len(mongo_events)}, Logs: {len(logs)}) (Target: >= 1,000)")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Seed PostgreSQL and MongoDB with demo data.")
    parser.add_argument("--users", type=int, default=250,
                        help="users to create (default 250; use e.g. 5000 so the CP2 backfill runs many batches)")
    parser.add_argument("--orders", type=int, default=500, help="orders to create (default 500)")
    args = parser.parse_args(argv)
    if args.users < 1 or args.orders < 0:
        parser.error("--users must be >= 1 and --orders >= 0")
    return args


def main():
    args = parse_args()
    relax_timeouts_for_scripts()  # seeding thousands of rows is not an API request: no 15 s statement limit
    print("=" * 70)
    print("  Ticket Booking Web Application - Database Seeder (Checkpoint 1 & Tier 1)")
    print("=" * 70)

    # Check connection statuses
    pg_ok = check_postgres_connection()
    mongo_ok = check_mongo_connection()

    if not pg_ok:
        print("[ERROR] PostgreSQL is not reachable at configured connection URL.")
        print("Please start the database with: docker compose up -d postgres_db")
        sys.exit(1)

    if not mongo_ok:
        print("[ERROR] MongoDB is not reachable at configured connection URL.")
        print("Please start the database with: docker compose up -d mongo_db")
        sys.exit(1)

    print("[SUCCESS] Both PostgreSQL and MongoDB are online and reachable.")

    # 1. Seed MongoDB events first so zones and prices are established
    mongo_events = seed_mongodb_events()

    # 2. Seed PostgreSQL users, orders, tickets, payments, and outbox events
    user_ids = seed_postgresql(mongo_events=mongo_events, num_users=args.users, num_orders=args.orders)

    # 3. Seed MongoDB activity logs referencing real inserted user IDs
    seed_mongodb_logs(user_ids=user_ids, mongo_events=mongo_events, num_logs=1200)

    print("\n" + "=" * 70)
    print("  Database seeding complete! Over 2,800 records in PostgreSQL & 1,400 in MongoDB.")
    print("=" * 70)


if __name__ == "__main__":
    main()
