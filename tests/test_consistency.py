"""Tests for scripts/verify_consistency.py: it must pass on honest data and catch each kind of damage.

The audit is global, so each test only looks for lines that name the event or order it created.
Needs PostgreSQL and MongoDB (skipped when they are down; REQUIRE_DB=1 turns the skip into a failure).
"""
import importlib.util
import os
import sys

import pytest
from bson import ObjectId
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.database import SessionLocal, engine, mongo_db  # noqa: E402
from tests.support import build_environment, client, post_order  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("verify_consistency", os.path.join(ROOT, "scripts", "verify_consistency.py"))
audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit)


@pytest.fixture(scope="module")
def env():
    environment, teardown = build_environment(need_mongo=True)
    yield environment
    teardown()


@pytest.fixture(autouse=True)
def show_every_sample(monkeypatch):
    monkeypatch.setattr(audit, "SAMPLE_LIMIT", 10 ** 6)


def run():
    results, drift = audit.run_audit(engine, mongo_db)
    return {r.name: r for r in results}, drift


def mentions(result, needle) -> bool:
    return any(needle in sample for sample in result.samples)


def set_booked(event_id, value):
    mongo_db["events"].update_one({"_id": ObjectId(event_id)}, {"$set": {"zones.0.booked_count": value}})


def booked_event(env, capacity=10, seats=3):
    """An event with `seats` booked through the API and the first order paid."""
    event_id = env["make_event"]([{"name": "Audit", "price": 100.0, "capacity": capacity, "booked_count": 0}])
    orders = [post_order(env["user_id"], event_id, "Audit", seats=[f"AU-{i}"]).json() for i in range(seats)]
    paid = client.post(f"/api/v1/orders/{orders[0]['id']}/payments",
                       json={"amount": orders[0]["total_amount"], "provider": "promptpay"})
    assert paid.status_code == 201
    return event_id, orders


def test_honest_booking_flow_passes_every_check(env):
    event_id, orders = booked_event(env)
    checks, _ = run()
    for name, result in checks.items():
        assert not mentions(result, event_id), f"{name} flagged an honest event: {result.samples}"
        for order in orders:
            assert not mentions(result, f"order {order['id']}"), f"{name} flagged an honest order"


def test_detects_mongo_counter_above_and_below_postgres_and_repairs_it(env):
    event_id, _ = booked_event(env, seats=3)

    set_booked(event_id, 5)  # two seats Mongo thinks are taken but nobody holds
    checks, drift = run()
    assert mentions(checks["drift_mongo_above_pg"], event_id)

    set_booked(event_id, 1)  # fewer than the 3 tickets that exist: the dangerous direction
    checks, drift = run()
    assert mentions(checks["drift_mongo_below_pg"], event_id)

    mine = [d for d in drift if str(d[0]) == event_id]
    assert audit.repair(mongo_db, mine) == 1
    checks, _ = run()
    assert not mentions(checks["drift_mongo_below_pg"], event_id)
    assert not mentions(checks["drift_mongo_above_pg"], event_id)
    assert env["booked"](event_id) == 3


def test_detects_overselling_and_negative_counters(env):
    event_id, _ = booked_event(env, capacity=10, seats=2)

    set_booked(event_id, 11)
    checks, _ = run()
    assert mentions(checks["mongo_over_capacity"], event_id)

    set_booked(event_id, -1)
    checks, _ = run()
    assert mentions(checks["negative_booked_count"], event_id)
    set_booked(event_id, 2)


def test_detects_more_tickets_than_capacity_in_postgres(env):
    event_id, orders = booked_event(env, capacity=3, seats=3)
    mongo_db["events"].update_one({"_id": ObjectId(event_id)}, {"$set": {"zones.0.capacity": 2}})
    checks, _ = run()
    assert mentions(checks["pg_over_capacity"], event_id)


def test_detects_a_double_charge(env):
    event_id, orders = booked_event(env)
    with SessionLocal() as session:
        session.execute(
            text("INSERT INTO payments (order_id, payment_reference, amount, provider, status) "
                 "VALUES (:o, :ref, :a, 'promptpay', 'completed')"),
            {"o": orders[0]["id"], "ref": f"PAY-DUP-{orders[0]['id']}", "a": orders[0]["total_amount"]},
        )
        session.commit()
    checks, _ = run()
    assert mentions(checks["double_charged_orders"], f"order {orders[0]['id']}")
    assert mentions(checks["payment_amount_mismatch"], f"order {orders[0]['id']}")


def test_detects_a_dead_order_that_still_holds_seats(env):
    event_id, orders = booked_event(env)
    with SessionLocal() as session:
        session.execute(text("UPDATE orders SET status = 'expired' WHERE id = :o"), {"o": orders[1]["id"]})
        session.commit()
    checks, _ = run()
    assert mentions(checks["dead_order_with_active_tickets"], f"order {orders[1]['id']}")


def test_detects_a_confirmed_order_nobody_paid(env):
    event_id, orders = booked_event(env)
    with SessionLocal() as session:
        session.execute(text("UPDATE orders SET status = 'confirmed' WHERE id = :o"), {"o": orders[2]["id"]})
        session.commit()
    checks, _ = run()
    assert mentions(checks["confirmed_without_payment"], f"order {orders[2]['id']}")
    assert mentions(checks["ticket_state_mismatch"], f"order {orders[2]['id']}")


def test_the_database_itself_refuses_a_duplicate_seat(env):
    """duplicate_active_seats can never fire: the partial unique index rejects the second row first."""
    event_id, orders = booked_event(env, seats=1)
    with SessionLocal() as session:
        with pytest.raises(IntegrityError):
            session.execute(
                text("INSERT INTO tickets (order_id, event_id, ticket_code, seat_zone, seat_number, price, status) "
                     "VALUES (:o, :e, :c, 'Audit', 'AU-0', 100, 'held')"),
                {"o": orders[0]["id"], "e": event_id, "c": f"TKT-DUP-{orders[0]['id']}"},
            )
            session.commit()
