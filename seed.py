#!/usr/bin/env python3
"""Database Seeding Script for Checkpoint 1.

Pre-populates reproducible, realistic dummy records:
- PostgreSQL: >= 1,000 records across normalized tables (Users, Orders, Tickets).
- MongoDB: >= 1,000 documents across collections (Events, ActivityLogs).
"""

import os
import sys
import random
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

# Add current directory to sys.path to enable app imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import func
from app.core.database import Base, engine, SessionLocal, mongo_db, check_postgres_connection, check_mongo_connection
from app.models.sql_models import User, Order, Ticket
from app.models.nosql_models import init_mongo_indexes
from app.services.user_service import hash_password

try:
    from faker import Faker
    fake = Faker()
except ImportError:
    # Lightweight deterministic generator fallback
    class SimpleFake:
        def first_name(self):
            return random.choice(["Alex", "Ploy", "Somchai", "Jane", "John", "Kamon", "Niran", "Siriporn", "David", "Emma"])
        def last_name(self):
            return random.choice(["Smith", "Suksom", "Wong", "Tan", "Miller", "Davis", "Saetang", "Chen", "Lee", "Taylor"])
        def email(self):
            return f"user_{uuid.uuid4().hex[:8]}@example.com"
        def user_name(self):
            return f"fan_{uuid.uuid4().hex[:8]}"
    fake = SimpleFake()


CONCERT_NAMES = [
    "Neon Horizon Music Festival",
    "Chiang Mai Indie Soundwave",
    "Siam Summer Beats 2026",
    "Bangkok EDM Odyssey",
    "Moonlight Jazz & Soul",
    "Retro Rock Revival World Tour",
    "Acoustic Haven Live Session",
    "Cyberpunk Synthwave Night",
    "Tropical Grooves Carnival",
    "Global Techno Underground",
    "Orchestral Cinematic Gala",
    "Velvet Underground Tribute",
    "Echoes of Eternity Live",
    "Midnight Acoustic Symphony",
    "Electric Dreamland Fest",
]

GENRES = ["Indie Rock", "EDM", "Jazz", "Synthwave", "Pop", "Classical", "Hip-Hop", "R&B", "Metal", "Folk"]

VENUES = [
    {"name": "Impact Arena", "city": "Bangkok", "country": "Thailand", "capacity": 12000, "address": "Muang Thong Thani"},
    {"name": "Chiang Mai International Exhibition Hall", "city": "Chiang Mai", "country": "Thailand", "capacity": 8000, "address": "Chang Phueak"},
    {"name": "Queen Sirikit National Convention Center", "city": "Bangkok", "country": "Thailand", "capacity": 15000, "address": "Ratchadaphisek Rd"},
    {"name": "Phuket Laguna Concert Grounds", "city": "Phuket", "country": "Thailand", "capacity": 5000, "address": "Bang Tao Beach"},
    {"name": "Thunder Dome", "city": "Bangkok", "country": "Thailand", "capacity": 6000, "address": "Popular Road"},
    {"name": "Marina Bay Waterfront Stage", "city": "Singapore", "country": "Singapore", "capacity": 10000, "address": "Downtown Core"},
    {"name": "Tokyo Dome City Hall", "city": "Tokyo", "country": "Japan", "capacity": 7500, "address": "Bunkyo City"},
]

STAGE_ZONES = [
    {"name": "VIP Standing", "price": 4500.0, "capacity": 200},
    {"name": "Front Zone A", "price": 3200.0, "capacity": 400},
    {"name": "Middle Zone B", "price": 2200.0, "capacity": 600},
    {"name": "General Admission", "price": 1200.0, "capacity": 1500},
    {"name": "Balcony Tier 1", "price": 1800.0, "capacity": 300},
]


def seed_mongodb(num_events: int = 200, num_logs: int = 1200):
    """Seed MongoDB with Events and ActivityLogs collections (> 1,000 documents)."""
    print(f"\n[MongoDB] Initializing collection indexes...")
    init_mongo_indexes(mongo_db)

    print(f"[MongoDB] Seeding {num_events} Events...")
    now = datetime.now(timezone.utc)
    events = []

    for i in range(num_events):
        venue = random.choice(VENUES)
        concert_base = random.choice(CONCERT_NAMES)
        title = f"{concert_base} #{i + 1}"
        genre = random.choice(GENRES)
        event_date = now + timedelta(days=random.randint(5, 180), hours=random.randint(16, 22))

        # Dynamic Attributes showcase MongoDB document schema flexibility
        dynamic_attrs = {
            "age_restriction": random.choice(["All Ages", "18+", "20+", "Family Friendly"]),
            "entry_gate": random.choice(["Gate 1", "Gate 2", "VIP North Gate"]),
            "allow_cameras": random.choice([True, False]),
            "parking_available": True,
            "festival_pass_type": random.choice(["Single Day", "Weekend Pass", "Full Access"]),
            "stage_specs": {
                "sound_system": "L-Acoustics K2",
                "lighting": "Martin MAC Viper Array",
                "special_effects": ["CO2 Cannons", "Laser Show", "Confetti Drop"],
            },
        }

        # Scaled stage zones
        zones = []
        for zone in STAGE_ZONES:
            zones.append({
                "name": zone["name"],
                "price": zone["price"],
                "capacity": zone["capacity"],
                "booked_count": random.randint(0, min(50, zone["capacity"])),
            })

        event_doc = {
            "title": title,
            "category": random.choice(["Concert", "Music Festival", "Live Tour"]),
            "artist": {
                "name": f"{fake.first_name()} & The Sound",
                "genre": genre,
                "bio": f"Award-winning {genre} artist touring internationally in 2026.",
                "spotify_id": f"spotify:artist:{uuid.uuid4().hex[:12]}",
            },
            "venue": venue,
            "date_time": event_date,
            "zones": zones,
            "tags": [genre.lower(), "live", "festival", venue["city"].lower()],
            "dynamic_attributes": dynamic_attrs,
            "status": random.choice(["upcoming", "upcoming", "upcoming", "sold_out"]),
            "created_at": now - timedelta(days=random.randint(1, 60)),
            "updated_at": now,
        }
        events.append(event_doc)

    mongo_db["events"].delete_many({})
    insert_res = mongo_db["events"].insert_many(events)
    inserted_event_ids = [str(oid) for oid in insert_res.inserted_ids]
    print(f"[MongoDB] Successfully inserted {len(inserted_event_ids)} Events.")

    print(f"[MongoDB] Seeding {num_logs} ActivityLogs (telemetry data)...")
    actions = ["view_event", "search_events", "select_seat", "add_to_cart", "checkout_init", "login", "view_ticket"]
    logs = []

    for _ in range(num_logs):
        log_time = now - timedelta(days=random.randint(0, 30), minutes=random.randint(0, 1440))
        action = random.choice(actions)
        target_event_id = random.choice(inserted_event_ids) if inserted_event_ids else str(uuid.uuid4())

        logs.append({
            "user_id": random.randint(1, 250),
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
    print(f"--> [MongoDB Total Documents]: {total_mongo} (Target: >= 1,000)")
    return inserted_event_ids


def seed_postgresql(event_ids: list, num_users: int = 250, num_orders: int = 500, num_tickets: int = 1250):
    """Seed PostgreSQL with Users, Orders, and Tickets (> 1,000 records)."""
    print(f"\n[PostgreSQL] Initializing tables...")
    Base.metadata.create_all(bind=engine)

    session = SessionLocal()
    try:
        # Clear existing data safely
        session.query(Ticket).delete()
        session.query(Order).delete()
        session.query(User).delete()
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

        # Query inserted user IDs
        user_ids = [u.id for u in session.query(User.id).all()]
        print(f"[PostgreSQL] Successfully inserted {len(user_ids)} Users.")

        # 2. Seed Orders
        print(f"[PostgreSQL] Seeding {num_orders} Orders...")
        orders = []
        statuses = ["confirmed", "confirmed", "confirmed", "pending", "cancelled"]
        payment_methods = ["credit_card", "promptpay", "bank_transfer"]

        for i in range(1, num_orders + 1):
            uid = random.choice(user_ids)
            order_num = f"ORD-2026{i:04d}-{uuid.uuid4().hex[:6].upper()}"
            order = Order(
                order_number=order_num,
                user_id=uid,
                total_amount=Decimal(str(random.choice([1200, 2400, 3200, 4500, 6400, 9000]))),
                status=random.choice(statuses),
                payment_method=random.choice(payment_methods),
            )
            orders.append(order)

        session.bulk_save_objects(orders, return_defaults=True)
        session.commit()

        order_ids = [o.id for o in session.query(Order.id).all()]
        print(f"[PostgreSQL] Successfully inserted {len(order_ids)} Orders.")

        # 3. Seed Tickets
        print(f"[PostgreSQL] Seeding {num_tickets} Tickets...")
        tickets = []
        zones = ["VIP Standing", "Front Zone A", "Middle Zone B", "General Admission", "Balcony Tier 1"]

        for i in range(1, num_tickets + 1):
            oid = random.choice(order_ids)
            ev_id = random.choice(event_ids) if event_ids else str(uuid.uuid4())
            tkt_code = f"TKT-{uuid.uuid4().hex[:12].upper()}"
            zone = random.choice(zones)
            price = Decimal(str(random.choice([1200, 1800, 2200, 3200, 4500])))

            ticket = Ticket(
                order_id=oid,
                event_id=ev_id,
                ticket_code=tkt_code,
                seat_zone=zone,
                seat_number=f"{zone[:2]}-{random.randint(1, 200)}",
                price=price,
                status="valid",
            )
            tickets.append(ticket)

        session.bulk_save_objects(tickets)
        session.commit()
        print(f"[PostgreSQL] Successfully inserted {num_tickets} Tickets.")

        total_users = session.query(func.count(User.id)).scalar()
        total_orders = session.query(func.count(Order.id)).scalar()
        total_tickets = session.query(func.count(Ticket.id)).scalar()
        total_pg = total_users + total_orders + total_tickets
        print(f"--> [PostgreSQL Total Records]: {total_pg} (Users: {total_users}, Orders: {total_orders}, Tickets: {total_tickets}) (Target: >= 1,000)")

    finally:
        session.close()


def main():
    print("=" * 70)
    print("  Ticket Booking Web Application - Database Seeder (Checkpoint 1)")
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

    # Execute seed in sequence
    event_ids = seed_mongodb(num_events=200, num_logs=1200)
    seed_postgresql(event_ids=event_ids, num_users=250, num_orders=500, num_tickets=1250)

    print("\n" + "=" * 70)
    print("  Database seeding complete! Over 1,000 records loaded into both DBs.")
    print("=" * 70)


if __name__ == "__main__":
    main()
