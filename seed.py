#!/usr/bin/env python3
"""Database Seeding Script for Checkpoint 1.

Pre-populates reproducible, realistic dummy records:
- Deterministic random seed: random.seed(42), Faker.seed(42)
- PostgreSQL: >= 1,000 records across normalized tables (Users, Orders, Tickets).
  - TRUNCATE TABLE tickets, orders, users RESTART IDENTITY CASCADE
  - Order total strictly matches sum of ticket prices
  - Ticket prices and zones match authoritative MongoDB event zones
  - Seat allocations respect double-booking constraints
- MongoDB: >= 1,000 documents across collections (Events, ActivityLogs).
  - Activity logs strictly reference real inserted PostgreSQL user IDs
"""

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
from app.core.database import Base, engine, SessionLocal, mongo_db, check_postgres_connection, check_mongo_connection
from app.models.sql_models import User, Order, Ticket
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
            return f"user_{uuid.uuid4().hex[:8]}@example.com"
        def user_name(self):
            return f"fan_{uuid.uuid4().hex[:8]}"
    fake = SimpleFake()


CONCERT_NAMES = [
    "BTS World Tour: Arirang in bangkok ",
    "NCT Dream Live in Bangkok",
    "Neon Horizon Music Festival",
    "Chiang Mai Indie Soundwave",
    "Ceasar feat. Olivia Dean Baan Rai Tour in Bangkok",
    "Olivia Dean Baan Rai Tour, ESAN samakee",
    "Siam Summer Beats 2026",
    "Bangkok EDM Odyssey",
    "Moonlight Jazz & Soul",
    "Retro Rock Revival World Tour",
    "Acoustic Haven Live Session",
    "Cyberpunk Synthwave Night",
    "Tropical Grooves Carnival",
    "Global Techno Underground",
    "Metallica: The Final Chapter Tour",
    "Lady Gaga, May 2027 Bangkok Spectacle",
    "Orchestral Cinematic Gala",
    "Velvet Underground Tribute",
    "Echoes of Eternity Live",
    "Midnight Acoustic Symphony",
    "Electric Dreamland Fest",
]

GENRES = ["K-Pop", "Neo-Soul", "Indie Rock", "EDM", "Jazz", "Synthwave", "Pop", "Classical", "Hip-Hop", "R&B", "Metal", "Folk"]

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


def seed_mongodb_events(num_events: int = 200) -> list:
    """Seed MongoDB Events collection and return inserted event documents with zones."""
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

        # Stage zones with matching prices
        zones = []
        for zone in STAGE_ZONES:
            zones.append({
                "name": zone["name"],
                "price": zone["price"],
                "capacity": zone["capacity"],
                "booked_count": 0,  # Updated as tickets are generated
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
            "status": "upcoming",
            "created_at": now - timedelta(days=random.randint(1, 60)),
            "updated_at": now,
        }
        events.append(event_doc)

    mongo_db["events"].delete_many({})
    insert_res = mongo_db["events"].insert_many(events)
    print(f"[MongoDB] Successfully inserted {len(insert_res.inserted_ids)} Events.")

    # Return full events list with ObjectIds
    cursor = list(mongo_db["events"].find())
    return cursor


def seed_postgresql(mongo_events: list, num_users: int = 250, num_orders: int = 500) -> list:
    """Seed PostgreSQL with Users, Orders, and Tickets matching MongoDB zone pricing."""
    print(f"\n[PostgreSQL] Initializing tables and restarting identity sequences...")
    Base.metadata.create_all(bind=engine)

    session = SessionLocal()
    try:
        # TRUNCATE with RESTART IDENTITY CASCADE ensures deterministic IDs starting at 1
        session.execute(text("TRUNCATE TABLE tickets, orders, users RESTART IDENTITY CASCADE;"))
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

        # 2. Seed Orders and Tickets together with realistic pricing
        print(f"[PostgreSQL] Seeding {num_orders} Orders with accurately matched Tickets...")
        statuses = ["confirmed", "confirmed", "confirmed", "pending"]
        payment_methods = ["credit_card", "promptpay", "bank_transfer"]

        # Track seat reservations per (event_id, zone_name) to prevent double-booking
        seat_counters = {}
        total_tickets_created = 0

        for i in range(1, num_orders + 1):
            uid = random.choice(user_ids)
            order_num = f"ORD-2026{i:04d}-{uuid.uuid4().hex[:6].upper()}"
            status = random.choice(statuses)
            method = random.choice(payment_methods)

            # Pick an event from MongoDB
            chosen_event = random.choice(mongo_events)
            event_id_str = str(chosen_event["_id"])
            zones_list = chosen_event["zones"]

            # Number of tickets for this order: 1 to 4
            num_tickets_in_order = random.randint(1, 4)
            order_tickets = []
            order_total = Decimal("0.00")

            for _ in range(num_tickets_in_order):
                zone = random.choice(zones_list)
                zone_name = zone["name"]
                zone_price = Decimal(str(zone["price"]))

                # Determine seat number avoiding duplicate seats
                seat_key = (event_id_str, zone_name)
                current_seat_idx = seat_counters.get(seat_key, 1)
                seat_counters[seat_key] = current_seat_idx + 1

                seat_label = f"{zone_name[:2].strip()}-{current_seat_idx:03d}"
                tkt_code = f"TKT-{uuid.uuid4().hex[:12].upper()}"

                ticket = Ticket(
                    event_id=event_id_str,
                    ticket_code=tkt_code,
                    seat_zone=zone_name,
                    seat_number=seat_label,
                    price=zone_price,
                    status="valid",
                )
                order_tickets.append(ticket)
                order_total += zone_price
                total_tickets_created += 1

            # Create Order with total_amount strictly equal to the sum of its tickets
            order = Order(
                order_number=order_num,
                user_id=uid,
                total_amount=order_total,
                status=status,
                payment_method=method,
                tickets=order_tickets,
            )
            session.add(order)

        session.commit()
        print(f"[PostgreSQL] Successfully inserted {num_orders} Orders and {total_tickets_created} Tickets.")

        total_users = session.query(func.count(User.id)).scalar()
        total_orders = session.query(func.count(Order.id)).scalar()
        total_tickets = session.query(func.count(Ticket.id)).scalar()
        total_pg = total_users + total_orders + total_tickets
        print(f"--> [PostgreSQL Total Records]: {total_pg} (Users: {total_users}, Orders: {total_orders}, Tickets: {total_tickets}) (Target: >= 1,000)")

        # Sync MongoDB booked_counts with seeded tickets
        for (ev_id, z_name), count in seat_counters.items():
            from bson import ObjectId
            mongo_db["events"].update_one(
                {"_id": ObjectId(ev_id), "zones.name": z_name},
                {"$set": {"zones.$.booked_count": count - 1}},
            )

        return user_ids

    finally:
        session.close()


def seed_mongodb_logs(user_ids: list, mongo_events: list, num_logs: int = 1200):
    """Seed MongoDB ActivityLogs referencing actual inserted PostgreSQL user IDs."""
    print(f"\n[MongoDB] Seeding {num_logs} ActivityLogs using real PostgreSQL User IDs...")
    now = datetime.now(timezone.utc)
    actions = ["view_event", "search_events", "select_seat", "add_to_cart", "checkout_init", "login", "view_ticket"]
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

    # 1. Seed MongoDB events first so zones and prices are established
    mongo_events = seed_mongodb_events(num_events=200)

    # 2. Seed PostgreSQL users, orders, and tickets matching MongoDB zone prices
    user_ids = seed_postgresql(mongo_events=mongo_events, num_users=250, num_orders=500)

    # 3. Seed MongoDB activity logs referencing real inserted user IDs
    seed_mongodb_logs(user_ids=user_ids, mongo_events=mongo_events, num_logs=1200)

    print("\n" + "=" * 70)
    print("  Database seeding complete! Over 1,000 realistic records in both DBs.")
    print("=" * 70)


if __name__ == "__main__":
    main()
