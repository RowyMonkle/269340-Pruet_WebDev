"""Concurrency and Integration Tests for Ticket Booking System.

Test Coverage:
1. test_oversell_concurrency_protection:
   50 concurrent threads race for the final available seat in a zone.
   Exactly 1 request succeeds (HTTP 201), and 49 are rejected (HTTP 409 Conflict).
2. test_same_seat_concurrency_protection:
   2 concurrent threads attempt to claim the exact same seat number.
   Exactly 1 succeeds, and the other receives HTTP 409 (Double-booking prevented).
3. test_idempotency_order_placement:
   Repeated requests with identical Idempotency-Key return the original order.
4. test_idempotency_payload_mismatch:
   Replaying an existing Idempotency-Key with a different payload returns HTTP 409.
5. test_paying_twice_fails_with_400:
   First payment confirms order. Second payment attempt returns HTTP 400 Bad Request.
6. test_expiry_then_pay_fails_with_410:
   Paying for an order whose seat hold has expired triggers automatic release and HTTP 410 Gone.
"""

import concurrent.futures
import uuid
from datetime import datetime, timedelta, timezone
import pytest
from bson import ObjectId
from fastapi.testclient import TestClient

from app.main import app
from app.core.database import SessionLocal, mongo_db, check_postgres_connection, check_mongo_connection
from app.models.sql_models import User, Order, Ticket, Payment, OutboxEvent
from app.services.user_service import hash_password

client = TestClient(app)


@pytest.fixture(scope="module")
def setup_test_environment():
    """Ensure database connectivity, create test user, and clean up safely on teardown."""
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

    created_event_ids = []

    def make_event(zones: list) -> str:
        doc = {
            "title": f"Test Event {uuid.uuid4().hex[:6]}",
            "category": "Concert",
            "artist": {"name": "Test Artist", "genre": "Indie", "bio": ""},
            "venue": {"name": "Test Stadium", "city": "Bangkok", "capacity": 1000},
            "date_time": datetime.now(timezone.utc) + timedelta(days=7),
            "zones": zones,
            "tags": ["test"],
            "dynamic_attributes": {},
            "status": "upcoming",
            "created_at": datetime.now(timezone.utc),
        }
        res = mongo_db["events"].insert_one(doc)
        ev_id = str(res.inserted_id)
        created_event_ids.append(res.inserted_id)
        return ev_id

    yield {"user_id": user_id, "make_event": make_event}

    # Safe Teardown: Delete payments, tickets, outbox events, and orders before deleting user (avoids RESTRICT FK error)
    s = SessionLocal()
    try:
        order_ids = [o.id for o in s.query(Order.id).filter(Order.user_id == user_id).all()]
        if order_ids:
            s.query(Payment).filter(Payment.order_id.in_(order_ids)).delete(synchronize_session=False)
            s.query(Ticket).filter(Ticket.order_id.in_(order_ids)).delete(synchronize_session=False)
            for oid in order_ids:
                s.query(OutboxEvent).filter(OutboxEvent.aggregate_id == str(oid)).delete(synchronize_session=False)
            s.query(Order).filter(Order.id.in_(order_ids)).delete(synchronize_session=False)
        s.query(User).filter(User.id == user_id).delete(synchronize_session=False)
        s.commit()
    finally:
        s.close()

    for ev_oid in created_event_ids:
        mongo_db["events"].delete_one({"_id": ev_oid})


def test_oversell_concurrency_protection(setup_test_environment):
    """50 concurrent requests competing for 1 available seat.

    Proves the atomic $elemMatch + $inc filter in MongoDB prevents overselling under load.
    """
    env = setup_test_environment
    user_id = env["user_id"]
    event_id = env["make_event"]([{"name": "Last Seat Zone", "price": 1000.0, "capacity": 1, "booked_count": 0}])

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

    # Exactly 1 request must succeed, and 49 must be rejected with 409 Conflict
    assert success_count == 1, f"Expected exactly 1 order to succeed, got {success_count}"
    assert conflict_count == num_threads - 1, f"Expected {num_threads - 1} conflicts, got {conflict_count}"


def test_same_seat_concurrency_protection(setup_test_environment):
    """2 concurrent requests attempting to book the exact same seat number.

    Proves the PostgreSQL partial unique index uq_tickets_event_zone_seat prevents double-booking.
    """
    env = setup_test_environment
    user_id = env["user_id"]
    event_id = env["make_event"]([{"name": "VIP", "price": 2500.0, "capacity": 10, "booked_count": 0}])

    def claim_same_seat(worker_id: int):
        payload = {
            "user_id": user_id,
            "payment_method": "credit_card",
            "items": [
                {
                    "event_id": event_id,
                    "seat_zone": "VIP",
                    "seat_number": "VIP-A1",  # Same exact seat
                }
            ],
        }
        resp = client.post("/api/v1/orders", json=payload)
        return resp.status_code, resp.json()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(claim_same_seat, i) for i in range(2)]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]

    status_codes = [r[0] for r in results]
    assert 201 in status_codes, "One request must successfully claim the seat"
    assert 409 in status_codes, "Second request must be rejected with 409 Conflict (double-booking prevented)"


def test_idempotency_order_placement(setup_test_environment):
    """Submitting two identical requests with the same Idempotency-Key returns the original order."""
    env = setup_test_environment
    user_id = env["user_id"]
    ev_id = env["make_event"]([{"name": "GA", "price": 500.0, "capacity": 10, "booked_count": 0}])

    idemp_key = f"IDEMP-{uuid.uuid4().hex}"
    payload = {
        "user_id": user_id,
        "payment_method": "credit_card",
        "items": [{"event_id": ev_id, "seat_zone": "GA", "seat_number": "GA-10"}],
    }
    headers = {"Idempotency-Key": idemp_key}

    # First request
    resp1 = client.post("/api/v1/orders", json=payload, headers=headers)
    assert resp1.status_code == 201
    order1_id = resp1.json()["id"]

    # Second retry with identical key
    resp2 = client.post("/api/v1/orders", json=payload, headers=headers)
    assert resp2.status_code in [200, 201]
    order2_id = resp2.json()["id"]

    assert order1_id == order2_id


def test_idempotency_payload_mismatch(setup_test_environment):
    """Submitting the same Idempotency-Key with different payload parameters must return HTTP 409."""
    env = setup_test_environment
    user_id = env["user_id"]
    ev_id = env["make_event"]([{"name": "GA", "price": 500.0, "capacity": 10, "booked_count": 0}])

    idemp_key = f"IDEMP-MISMATCH-{uuid.uuid4().hex}"
    payload1 = {
        "user_id": user_id,
        "payment_method": "promptpay",
        "items": [{"event_id": ev_id, "seat_zone": "GA", "seat_number": "GA-21"}],
    }
    resp1 = client.post("/api/v1/orders", json=payload1, headers={"Idempotency-Key": idemp_key})
    assert resp1.status_code == 201

    # Replay with altered items
    payload2 = {
        "user_id": user_id,
        "payment_method": "promptpay",
        "items": [
            {"event_id": ev_id, "seat_zone": "GA", "seat_number": "GA-21"},
            {"event_id": ev_id, "seat_zone": "GA", "seat_number": "GA-22"},
        ],
    }
    resp2 = client.post("/api/v1/orders", json=payload2, headers={"Idempotency-Key": idemp_key})
    assert resp2.status_code == 409


def test_paying_twice_fails_with_400(setup_test_environment):
    """First payment succeeds; second payment on the same order returns HTTP 400 Bad Request."""
    env = setup_test_environment
    user_id = env["user_id"]
    ev_id = env["make_event"]([{"name": "Zone B", "price": 800.0, "capacity": 5, "booked_count": 0}])

    # 1. Create order
    order_resp = client.post(
        "/api/v1/orders",
        json={
            "user_id": user_id,
            "payment_method": "promptpay",
            "items": [{"event_id": ev_id, "seat_zone": "Zone B", "seat_number": "ZB-1"}],
        },
    )
    assert order_resp.status_code == 201
    order_id = order_resp.json()["id"]
    total = order_resp.json()["total_amount"]

    # 2. First payment succeeds
    pay_resp1 = client.post(
        f"/api/v1/orders/{order_id}/payments",
        json={"amount": total, "provider": "promptpay"},
    )
    assert pay_resp1.status_code == 201

    # 3. Second payment fails with 400 Bad Request
    pay_resp2 = client.post(
        f"/api/v1/orders/{order_id}/payments",
        json={"amount": total, "provider": "promptpay"},
    )
    assert pay_resp2.status_code == 400
    assert "already paid" in pay_resp2.json()["detail"].lower()


def test_expiry_then_pay_fails_with_410(setup_test_environment):
    """Paying for an expired seat hold triggers seat release and returns HTTP 410 Gone."""
    env = setup_test_environment
    user_id = env["user_id"]
    ev_id = env["make_event"]([{"name": "Zone C", "price": 600.0, "capacity": 5, "booked_count": 0}])

    # 1. Create order
    order_resp = client.post(
        "/api/v1/orders",
        json={
            "user_id": user_id,
            "payment_method": "promptpay",
            "items": [{"event_id": ev_id, "seat_zone": "Zone C", "seat_number": "ZC-1"}],
        },
    )
    assert order_resp.status_code == 201
    order_id = order_resp.json()["id"]
    total = order_resp.json()["total_amount"]

    # 2. Manually alter expires_at into the past
    s = SessionLocal()
    order = s.query(Order).filter(Order.id == order_id).first()
    assert order is not None
    order.expires_at = datetime.now(timezone.utc) - timedelta(minutes=15)
    s.commit()
    s.close()

    # 3. Payment attempt should detect expiry, release seats, and return HTTP 410 Gone
    pay_resp = client.post(
        f"/api/v1/orders/{order_id}/payments",
        json={"amount": total, "provider": "promptpay"},
    )
    assert pay_resp.status_code == 410
    assert "expired" in pay_resp.json()["detail"].lower()
