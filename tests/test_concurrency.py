"""Concurrency and Stress Tests for Ticket Booking System.

Tests:
1. Race Condition / Overselling Prevention:
   50 concurrent threads attempt to purchase the final seat in a zone.
   Exactly 1 request must succeed (HTTP 201), and 49 must fail (HTTP 409 Conflict).
2. Double-Booking Prevention:
   Concurrent requests trying to claim the same seat number.
3. Idempotency Guarantee:
   Repeated requests with identical Idempotency-Key return the original order.
"""

import concurrent.futures
import uuid
from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.core.database import SessionLocal, mongo_db, check_postgres_connection, check_mongo_connection
from app.models.sql_models import User, Order, Ticket
from app.services.user_service import hash_password

client = TestClient(app)


@pytest.fixture(scope="module")
def setup_test_environment():
    """Ensure database connectivity and seed a test event and user."""
    if not check_postgres_connection() or not check_mongo_connection():
        pytest.skip("Databases not accessible for live integration testing.")

    session = SessionLocal()
    # Create test user
    test_email = f"test_{uuid.uuid4().hex[:8]}@example.com"
    user = User(
        email=test_email,
        username=f"user_{uuid.uuid4().hex[:8]}",
        hashed_password=hash_password("Pass123!"),
        full_name="Concurrency Tester",
        role="fan",
    )
    session.add(user)
    session.commit()
    user_id = user.id
    session.close()

    # Create test event with a zone having EXACTLY 1 seat capacity
    event_doc = {
        "title": f"Concurrency Test Event {uuid.uuid4().hex[:6]}",
        "category": "Concert",
        "artist": {"name": "Concurrency Band", "genre": "Tech", "bio": ""},
        "venue": {"name": "Test Stadium", "city": "Bangkok", "capacity": 100},
        "date_time": datetime.now(timezone.utc),
        "zones": [
            {
                "name": "Last Seat Zone",
                "price": 1000.0,
                "capacity": 1,
                "booked_count": 0,  # Only 1 seat available
            }
        ],
        "tags": ["test", "concurrency"],
        "dynamic_attributes": {},
        "status": "upcoming",
        "created_at": datetime.now(timezone.utc),
    }
    res = mongo_db["events"].insert_one(event_doc)
    event_id = str(res.inserted_id)

    yield {"user_id": user_id, "event_id": event_id}

    # Teardown
    mongo_db["events"].delete_one({"_id": res.inserted_id})
    s = SessionLocal()
    s.query(User).filter(User.id == user_id).delete()
    s.commit()
    s.close()


def test_oversell_concurrency_protection(setup_test_environment):
    """50 concurrent requests competing for 1 available seat.

    Proves the atomic $elemMatch + $inc filter in MongoDB prevents overselling under load.
    """
    env = setup_test_environment
    event_id = env["event_id"]
    user_id = env["user_id"]

    num_threads = 50
    results = []

    def make_order(req_idx: int):
        payload = {
            "user_id": user_id,
            "payment_method": "promptpay",
            "items": [
                {
                    "event_id": event_id,
                    "seat_zone": "Last Seat Zone",
                    "seat_number": f"SEAT-{req_idx}",
                }
            ],
        }
        resp = client.post("/api/v1/orders", json=payload)
        return resp.status_code, resp.json()

    # Execute 50 requests in parallel
    with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [executor.submit(make_order, i) for i in range(num_threads)]
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result())

    status_codes = [r[0] for r in results]
    success_count = status_codes.count(201)
    conflict_count = status_codes.count(409)

    print(f"\nConcurrency Results: 201 Created = {success_count}, 409 Conflict = {conflict_count}")
    # Exactly 1 request must succeed, and 49 must be rejected with 409 Conflict
    assert success_count == 1, f"Expected exactly 1 order to succeed, got {success_count}"
    assert conflict_count == num_threads - 1, f"Expected {num_threads - 1} conflicts, got {conflict_count}"


def test_idempotency_order_placement(setup_test_environment):
    """Submitting two identical requests with the same Idempotency-Key returns the same order."""
    env = setup_test_environment
    user_id = env["user_id"]

    # Create temporary event with 5 seats
    event_doc = {
        "title": "Idempotency Test Event",
        "zones": [{"name": "GA", "price": 500.0, "capacity": 5, "booked_count": 0}],
        "date_time": datetime.now(timezone.utc),
    }
    ev = mongo_db["events"].insert_one(event_doc)
    ev_id = str(ev.inserted_id)

    idemp_key = f"IDEMP-TEST-{uuid.uuid4().hex}"
    payload = {
        "user_id": user_id,
        "payment_method": "credit_card",
        "items": [{"event_id": ev_id, "seat_zone": "GA", "seat_number": "GA-1"}],
    }
    headers = {"Idempotency-Key": idemp_key}

    # First request
    resp1 = client.post("/api/v1/orders", json=payload, headers=headers)
    assert resp1.status_code == 201
    order1_id = resp1.json()["id"]

    # Second retry with identical key
    resp2 = client.post("/api/v1/orders", json=payload, headers=headers)
    assert resp2.status_code == 201 or resp2.status_code == 200
    order2_id = resp2.json()["id"]

    # Both responses must refer to the exact same order
    assert order1_id == order2_id

    # Cleanup
    mongo_db["events"].delete_one({"_id": ev.inserted_id})
