"""Database-level guards (migration 0007 and the MongoDB events validator).

The application already prevents these things (row locks, conditional atomic updates). These tests prove the
second line of defence: even when the application code is bypassed, the DATABASE refuses.

  PostgreSQL  at most one completed payment per order       (partial unique index uq_payments_order_completed)
  PostgreSQL  status and role vocabularies                   (CHECK constraints chk_*_status, chk_users_role)
  MongoDB     0 <= booked_count <= capacity in every zone    (collection validator on `events`)
  API         a blocked double charge answers 409, not 500; a zone with booked_count > capacity answers 400

Every test writes through raw SQL / raw pymongo calls on purpose: that is what "bypassing the application" means.
Run:  docker compose exec -e REQUIRE_DB=1 api pytest tests/test_db_guards.py -v
"""
import os
import sys
import threading
import uuid

import pytest
from bson import ObjectId
from pymongo.errors import WriteError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.database import SessionLocal, mongo_db  # noqa: E402
from app.models.nosql_models import init_mongo_indexes  # noqa: E402
from tests.support import build_environment, client, post_order  # noqa: E402

DOCUMENT_VALIDATION_FAILED = 121


@pytest.fixture(scope="module")
def env():
    environment, teardown = build_environment(need_mongo=True)
    yield environment
    teardown()


def run_sql(sql: str, **params):
    with SessionLocal() as session:
        result = session.execute(text(sql), params)
        session.commit()
        return result


def new_order(env) -> dict:
    event_id = env["make_event"]([{"name": "Guard", "price": 100.0, "capacity": 50, "booked_count": 0}])
    return post_order(env["user_id"], event_id, "Guard").json()


def insert_payment(order_id: int, status: str = "completed", tag: str = "") -> None:
    run_sql(
        "INSERT INTO payments (order_id, payment_reference, amount, provider, status) "
        "VALUES (:o, :r, 100, 'promptpay', :s)",
        o=order_id, r=f"G-{uuid.uuid4().hex[:12]}{tag}", s=status,
    )


# ----------------------------------------------------------------------------------------------------------
# PostgreSQL: one completed payment per order
# ----------------------------------------------------------------------------------------------------------


def test_the_database_refuses_a_second_completed_payment(env):
    order = new_order(env)
    insert_payment(order["id"])
    with pytest.raises(IntegrityError) as refused:
        insert_payment(order["id"])
    assert "uq_payments_order_completed" in str(refused.value)
    count = env["scalar"]("SELECT count(*) FROM payments WHERE order_id = :o AND status = 'completed'", o=order["id"])
    assert count == 1


def test_failed_and_pending_attempts_are_still_allowed_alongside_one_completed(env):
    order = new_order(env)
    for status in ("failed", "failed", "pending", "refunded"):
        insert_payment(order["id"], status)
    insert_payment(order["id"], "completed")
    assert env["scalar"]("SELECT count(*) FROM payments WHERE order_id = :o", o=order["id"]) == 5


def test_two_writers_racing_with_no_application_lock_charge_the_order_once(env):
    """The row lock is what normally serialises payments. Take it away: the database alone must still hold."""
    order = new_order(env)
    barrier = threading.Barrier(8)
    outcomes = []

    def writer():
        barrier.wait(timeout=10)
        try:
            insert_payment(order["id"])
            outcomes.append("charged")
        except IntegrityError:
            outcomes.append("refused")

    threads = [threading.Thread(target=writer) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert outcomes.count("charged") == 1 and outcomes.count("refused") == 7, outcomes
    assert env["scalar"]("SELECT count(*) FROM payments WHERE order_id = :o AND status = 'completed'", o=order["id"]) == 1


def test_a_charge_the_database_blocked_is_answered_409_and_saves_nothing(env):
    """The application check passes (the order still looks pending) but a completed payment already exists:
    the situation of a request that slipped past the row lock. Answer 409 'already paid', never 500."""
    order = new_order(env)
    insert_payment(order["id"])  # the other, earlier charge
    response = client.post(f"/api/v1/orders/{order['id']}/payments", json={"amount": order["total_amount"], "provider": "promptpay"})
    assert response.status_code == 409, response.text
    assert "already paid" in response.json()["detail"].lower()
    scalar = env["scalar"]
    assert scalar("SELECT count(*) FROM payments WHERE order_id = :o", o=order["id"]) == 1, "no second payment row"
    assert scalar("SELECT status FROM orders WHERE id = :o", o=order["id"]) == "pending", "the order was not confirmed by the refused charge"
    assert scalar("SELECT count(*) FROM tickets WHERE order_id = :o AND status = 'held'", o=order["id"]) == 1, "tickets untouched"


# ----------------------------------------------------------------------------------------------------------
# PostgreSQL: vocabularies
# ----------------------------------------------------------------------------------------------------------

BAD_VALUES = [
    ("orders", "status", "chk_orders_status", "UPDATE orders SET status = 'bogus' WHERE id = :id", "order"),
    ("tickets", "status", "chk_tickets_status", "UPDATE tickets SET status = 'bogus' WHERE order_id = :id", "order"),
    ("payments", "status", "chk_payments_status", "INSERT INTO payments (order_id, payment_reference, amount, provider, status) VALUES (:id, 'G-' || md5(random()::text), 1, 'promptpay', 'bogus')", "order"),
    ("outbox_events", "status", "chk_outbox_status", "INSERT INTO outbox_events (event_type, aggregate_type, aggregate_id, payload, status) VALUES ('X', 'order', 'g', '{}', 'bogus')", None),
    ("users", "role", "chk_users_role", "UPDATE users SET role = 'superuser' WHERE id = :id", "user"),
]


@pytest.mark.parametrize("table, column, constraint, statement, target", BAD_VALUES, ids=[b[2] for b in BAD_VALUES])
def test_the_database_refuses_a_value_outside_the_known_vocabulary(env, table, column, constraint, statement, target):
    order = new_order(env)
    target_id = {"order": order["id"], "user": env["user_id"], None: 0}[target]
    with pytest.raises(IntegrityError) as refused:
        run_sql(statement, id=target_id)
    assert constraint in str(refused.value)


def test_every_status_the_application_writes_is_still_accepted(env):
    """Guard against a constraint that is stricter than the code: walk an order through its real life."""
    order = new_order(env)
    for status in ("pending", "confirmed", "expired", "cancelled", "paid"):
        run_sql("UPDATE orders SET status = :s WHERE id = :id", s=status, id=order["id"])
    for status in ("held", "valid", "cancelled", "used", "refunded"):
        run_sql("UPDATE tickets SET status = :s WHERE order_id = :id", s=status, id=order["id"])
    for role in ("fan", "organizer", "admin"):
        run_sql("UPDATE users SET role = :r WHERE id = :id", r=role, id=env["user_id"])
    run_sql("UPDATE users SET role = 'fan' WHERE id = :id", id=env["user_id"])


def test_the_guards_are_installed_and_fully_validated(env):
    rows = run_sql(
        "SELECT conname, convalidated FROM pg_constraint WHERE conname IN "
        "('chk_users_role', 'chk_orders_status', 'chk_tickets_status', 'chk_payments_status', 'chk_outbox_status')"
    ).all()
    assert {r.conname for r in rows} == {"chk_users_role", "chk_orders_status", "chk_tickets_status", "chk_payments_status", "chk_outbox_status"}
    assert all(r.convalidated for r in rows), "a NOT VALID constraint only checks new rows: VALIDATE must have run"
    index = run_sql(
        "SELECT i.indisvalid, i.indisunique, pg_get_expr(i.indpred, i.indrelid) AS predicate FROM pg_index i "
        "JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = 'uq_payments_order_completed'"
    ).one()
    assert index.indisvalid and index.indisunique and "completed" in index.predicate


# ----------------------------------------------------------------------------------------------------------
# MongoDB: seat inventory
# ----------------------------------------------------------------------------------------------------------


def seat_event(env, capacity=10, booked=0) -> str:
    return env["make_event"]([{"name": "A", "price": 10.0, "capacity": capacity, "booked_count": booked}])


def test_mongodb_validator_is_installed():
    info = mongo_db.command("listCollections", filter={"name": "events"})["cursor"]["firstBatch"][0]["options"]
    assert "$expr" in info["validator"]
    assert (info["validationLevel"], info["validationAction"]) == ("moderate", "error")


def test_mongodb_refuses_a_zone_booked_beyond_its_capacity(env):
    event_id = seat_event(env, capacity=10, booked=10)
    with pytest.raises(WriteError) as refused:  # a wrong update with NO conditional filter: what a bug would do
        mongo_db["events"].update_one({"_id": ObjectId(event_id)}, {"$inc": {"zones.0.booked_count": 1}})
    assert refused.value.code == DOCUMENT_VALIDATION_FAILED
    assert env["booked"](event_id) == 10


def test_mongodb_refuses_a_negative_counter(env):
    event_id = seat_event(env, capacity=10, booked=0)
    with pytest.raises(WriteError) as refused:
        mongo_db["events"].update_one({"_id": ObjectId(event_id)}, {"$inc": {"zones.0.booked_count": -1}})
    assert refused.value.code == DOCUMENT_VALIDATION_FAILED
    assert env["booked"](event_id) == 0


def test_mongodb_refuses_to_shrink_a_zone_below_what_is_sold(env):
    event_id = seat_event(env, capacity=10, booked=6)
    with pytest.raises(WriteError):
        mongo_db["events"].update_one({"_id": ObjectId(event_id)}, {"$set": {"zones.0.capacity": 5}})


def test_mongodb_refuses_a_new_event_that_is_already_oversold(env):
    doc = {"title": "Oversold", "zones": [{"name": "A", "price": 1.0, "capacity": 5, "booked_count": 6}]}
    with pytest.raises(WriteError) as refused:
        mongo_db["events"].insert_one(doc)
    assert refused.value.code == DOCUMENT_VALIDATION_FAILED


def test_mongodb_accepts_every_legitimate_state(env):
    event_id = seat_event(env, capacity=3, booked=0)
    oid = ObjectId(event_id)
    for _ in range(3):  # fill the zone exactly: booked_count == capacity is allowed
        mongo_db["events"].update_one({"_id": oid}, {"$inc": {"zones.0.booked_count": 1}})
    assert env["booked"](event_id) == 3
    mongo_db["events"].update_one({"_id": oid}, {"$inc": {"zones.0.booked_count": -3}})  # and empty it again
    assert env["booked"](event_id) == 0
    mongo_db["events"].update_one({"_id": oid}, {"$set": {"title": "Renamed", "zones.0.price": 99.0}})  # unrelated edits
    no_zones = mongo_db["events"].insert_one({"title": "No zones yet"}).inserted_id  # an event without zones is valid
    mongo_db["events"].delete_one({"_id": no_zones})


def test_installing_the_validator_twice_is_harmless():
    init_mongo_indexes(mongo_db)
    init_mongo_indexes(mongo_db)


def test_the_booking_flow_still_works_with_the_validator(env):
    """The conditional atomic reservation never produces a state the validator refuses."""
    event_id = env["make_event"]([{"name": "Tight", "price": 5.0, "capacity": 3, "booked_count": 0}])
    codes = [post_order(env["user_id"], event_id, "Tight").status_code for _ in range(5)]
    assert codes.count(201) == 3 and codes.count(409) == 2, codes
    assert env["booked"](event_id) == 3


def test_the_api_rejects_an_oversold_zone_with_400_before_it_reaches_the_database(env):
    body = {
        "title": "Bad Zone Event", "category": "Concert", "artist": {"name": "A", "genre": "G"},
        "venue": {"name": "V", "city": "C", "capacity": 10}, "date_time": "2030-01-01T00:00:00Z",
        "zones": [{"name": "Z", "price": 10, "capacity": 5, "booked_count": 9}],
    }
    response = client.post("/api/v1/events", json=body)
    assert response.status_code == 400, response.text
    assert "booked_count" in response.text
