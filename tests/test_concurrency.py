"""Concurrency verification for the Ticket Booking API (CP3: race conditions must not happen).

How each shared resource is protected, and the test that proves it:

  Seat inventory (MongoDB)    one atomic conditional update per zone (booked_count <= capacity)
                              -> test_oversell_concurrency_protection, test_last_ticket_20_users,
                                 test_multi_ticket_orders_never_oversell (all-or-nothing per order),
                                 test_sold_out_second_zone_returns_the_first_zones_seats
  A single seat (PostgreSQL)  partial unique index uq_tickets_event_zone_seat + one global seat order
                              -> test_same_seat_concurrency_protection,
                                 test_event_id_spelling_cannot_bypass_the_seat_lock,
                                 test_every_order_inserts_its_seats_in_one_global_order,
                                 test_opposite_order_multi_seat_bookings_never_return_5xx
  An order being paid         pessimistic lock: SELECT ... FOR UPDATE on the order row
                              -> test_paying_twice_fails_with_409, test_parallel_payments_charge_the_order_once
  Pay vs expire vs cleanup    same row lock (+ SKIP LOCKED in the sweeper): every hold is released once
                              -> test_pay_vs_expiry_vs_cleanup_race_releases_each_seat_once,
                                 test_parallel_cleanup_releases_each_hold_once
  Retried requests            unique Idempotency-Key -> test_idempotency_*,
                              test_parallel_same_idempotency_key_creates_one_order,
                              test_idempotent_replay_works_with_any_zone_spelling
  Read-modify-write of a row  SELECT ... FOR UPDATE in PUT /users
                              -> test_concurrent_user_updates_do_not_lose_each_other
  A collision that slips by   PostgreSQL deadlock/serialization errors become HTTP 409, never 500
                              -> test_database_deadlock_is_answered_with_409_and_seats_are_returned
  An overloaded database      pool timeout / statement timeout become HTTP 503 + Retry-After, never 500
                              -> test_exhausted_connection_pool_is_answered_with_503,
                                 test_statement_timeout_during_booking_is_answered_with_503_and_seats_are_returned

Run it:
  docker compose exec api pytest tests/test_concurrency.py -v -s                  # in-process
  docker compose exec -e API_URL=http://nginx -e REQUIRE_DB=1 api pytest tests/test_concurrency.py -v -s
      # real HTTP through nginx and both API replicas (races between two Python processes)
  docker compose exec api python tests/test_concurrency.py                        # same, as a script

Status codes: a successful booking answers 201 (resource created), a lost race answers 409,
an overloaded database answers 503 (retry after Retry-After seconds).
""" 
import concurrent.futures
import importlib 
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.database import SessionLocal  # noqa: E402
from app.models.sql_models import Order, OutboxEvent # noqa: E402
from tests.support import (  # noqa: E402
    LIVE_MODE, build_environment, client, post_order, run_parallel, sweep_expired_holds,
)


@pytest.fixture(scope="module")
def setup_test_environment():
    """Ensure database connectivity, create a test user, and clean up safely on teardown."""
    env, teardown = build_environment(need_mongo=True)
    yield env
    teardown()


@pytest.fixture(scope="module")
def postgres_environment():
    """Same helpers for tests that never touch MongoDB."""
    env, teardown = build_environment(need_mongo=False)
    yield env
    teardown()


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


def test_paying_twice_fails_with_409(setup_test_environment):
    """First payment succeeds; second payment on the same order returns HTTP 409 Conflict.
     The request is well-formed (not a 400); it conflicts with the order's current state.
    """
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

    # 3. Second payment fails with 409 Conflict
    pay_resp2 = client.post(
        f"/api/v1/orders/{order_id}/payments",
        json={"amount": total, "provider": "promptpay"},
    )
    assert pay_resp2.status_code == 409
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


# ---------------------------------------------------------------------------------------------
# CP3 additions
# ---------------------------------------------------------------------------------------------


def test_last_ticket_20_users(setup_test_environment):
    """The CP3 specification example: 20 different users race for the LAST remaining ticket."""
    env = setup_test_environment
    event_id = env["make_event"]([{"name": "Last Ticket", "price": 1500.0, "capacity": 1, "booked_count": 0}])
    user_ids = env["make_users"](20)

    responses = run_parallel(lambda uid: post_order(uid, event_id, "Last Ticket"), user_ids)
    codes = [r.status_code for r in responses]
    successes, conflicts = codes.count(201), codes.count(409)
    print(f"Concurrent Test Results -> Successes: {successes} (Must be 1) | Conflicts: {conflicts}")

    assert successes == 1, "Race Condition Detected! Multiple users booked the same ticket!"
    assert conflicts == 19, f"every losing user must get 409 Conflict, got {sorted(codes)}"
    assert env["booked"](event_id) == 1, "MongoDB seat counter must show exactly one seat taken"
    assert env["active_tickets"](event_id) == 1, "PostgreSQL must hold exactly one active ticket"


def test_parallel_payments_charge_the_order_once(setup_test_environment):
    """20 payments for the same pending order at the same instant: one charge, nineteen 409 refusals."""
    env = setup_test_environment
    event_id = env["make_event"]([{"name": "Pay Once", "price": 800.0, "capacity": 5, "booked_count": 0}])
    order = post_order(env["user_id"], event_id, "Pay Once", seats=["PO-1"]).json()

    responses = run_parallel(
        lambda _: client.post(
            f"/api/v1/orders/{order['id']}/payments",
            json={"amount": order["total_amount"], "provider": "promptpay"},
        ),
        range(20),
    )
    codes = [r.status_code for r in responses]
    print(f"Parallel payments -> {codes.count(201)} charged, {codes.count(409)} refused")

    assert codes.count(201) == 1, f"the order must be charged exactly once, got {sorted(codes)}"
    assert codes.count(409) == 19, f"every other payment must be refused with 409, got {sorted(codes)}" 
    scalar = env["scalar"]
    assert scalar("SELECT count(*) FROM payments WHERE order_id = :o AND status = 'completed'", o=order["id"]) == 1
    assert scalar("SELECT status FROM orders WHERE id = :o", o=order["id"]) == "confirmed"
    assert scalar("SELECT count(*) FROM tickets WHERE order_id = :o AND status = 'valid'", o=order["id"]) == 1


def test_parallel_same_idempotency_key_creates_one_order(setup_test_environment):
    """20 identical requests carrying the same Idempotency-Key: one order, one seat, one outbox event."""
    env = setup_test_environment
    event_id = env["make_event"]([{"name": "Idem", "price": 500.0, "capacity": 50, "booked_count": 0}])
    key = f"IDEMP-RACE-{uuid.uuid4().hex}"

    responses = run_parallel(lambda _: post_order(env["user_id"], event_id, "Idem", seats=["ID-1"], key=key), range(20))
    codes = [r.status_code for r in responses]
    order_ids = {r.json()["id"] for r in responses if r.status_code < 300}
    print(f"Same Idempotency-Key x20 -> codes {sorted(set(codes))}, distinct orders {len(order_ids)}")

    assert all(c in (200, 201) for c in codes), f"a replay must return the original order, got {sorted(codes)}"
    assert len(order_ids) == 1, "all 20 requests must resolve to the same order"
    scalar = env["scalar"]
    assert scalar("SELECT count(*) FROM orders WHERE idempotency_key = :k", k=key) == 1
    assert env["booked"](event_id) == 1, "the losers' reservations must have been given back"
    assert env["active_tickets"](event_id) == 1
    assert scalar("SELECT count(*) FROM outbox_events WHERE event_type = 'SEAT_HELD' AND aggregate_id = :a",
                  a=str(next(iter(order_ids)))) == 1


def test_opposite_order_multi_seat_bookings_never_return_5xx(setup_test_environment):
    """Two users want the same seats but list them in opposite order, round after round.

    Regression test for a real bug: each order inserted its seats in request order, so two orders
    could each hold part of the seats and wait for the other (PostgreSQL deadlock -> HTTP 500).
    Orders now take seats in one global order, so exactly one wins and the other gets a clean 409.

    Measured on the old code through nginx and both replicas: 13% of 2-seat rounds and 61% of
    30-seat rounds returned HTTP 500. Only a real multi-process run shows it reliably (in-process,
    the GIL lines requests up one after another), so run it with API_URL=http://nginx for the proof;
    test_every_order_inserts_its_seats_in_one_global_order pins the cause deterministically.
    """
    env = setup_test_environment
    event_id = env["make_event"]([{"name": "Deadlock", "price": 100.0, "capacity": 100000, "booked_count": 0}])
    user_a, user_b = env["make_users"](2)
    rounds, seats_per_order = (60, 10) if LIVE_MODE else (40, 10)

    server_errors, bad_rounds = [], []
    for i in range(rounds):
        seats = [f"R{i}-S{k:02d}" for k in range(seats_per_order)]
        pair = run_parallel(
            lambda arg: post_order(arg[0], event_id, "Deadlock", seats=arg[1]),
            [(user_a, seats), (user_b, list(reversed(seats)))],
        )
        codes = sorted(r.status_code for r in pair)
        server_errors += [(i, r.status_code, r.json().get("detail", "")[:80]) for r in pair if r.status_code >= 500]
        if codes != [201, 409]:
            bad_rounds.append((i, codes))
    print(f"Opposite-order bookings: {rounds} rounds, {len(server_errors)} server errors, {len(bad_rounds)} unexpected rounds")

    assert not server_errors, f"a collision must never be a 5xx, got {server_errors[:3]}"
    assert not bad_rounds, f"every round needs exactly one 201 and one 409, got {bad_rounds[:3]}"
    expected = rounds * seats_per_order
    assert env["booked"](event_id) == expected == env["active_tickets"](event_id)


@pytest.mark.skipif(LIVE_MODE, reason="observes the server process, so it only works in-process")
def test_every_order_inserts_its_seats_in_one_global_order(setup_test_environment):
    """Deterministic proof of the cause: whatever order the client lists seats in, they are inserted sorted.

    Two orders can only deadlock if they take overlapping seats in different orders. If every order
    takes its seats in the same global order, one of them simply waits and then gets a 409.
    """
    from sqlalchemy import event
    from app.models.sql_models import Ticket

    env = setup_test_environment
    event_id = env["make_event"]([{"name": "Order", "price": 10.0, "capacity": 1000, "booked_count": 0}])
    inserted = []

    def spy(mapper, connection, target):
        inserted.append(target.seat_number)

    event.listen(Ticket, "before_insert", spy)
    try:
        for seats in (["S3", "S1", "S2", "S10"], ["T2", "T3", "T1", "T0"], ["U1", "U2", "U3", "U4"]):
            inserted.clear()
            response = post_order(env["user_id"], event_id, "Order", seats=seats)
            assert response.status_code == 201, response.text
            assert inserted == sorted(seats), f"seats of {seats} were inserted in order {inserted}"
    finally:
        event.remove(Ticket, "before_insert", spy)


def test_multi_ticket_orders_never_oversell(setup_test_environment):
    """20 users each want 2 tickets from a zone of 5: exactly 2 orders fit, never fewer, never more.

    A multi-ticket order reserves its whole quantity in ONE atomic update, so it either gets all of
    its seats or none. (Reserving ticket by ticket allowed an unlucky interleaving where every order
    grabbed its first seat, found the zone full for the second and gave up, so fewer orders than fit won.)
    """
    env = setup_test_environment
    event_id = env["make_event"]([{"name": "Multi", "price": 200.0, "capacity": 5, "booked_count": 0}])
    user_ids = env["make_users"](20)

    responses = run_parallel(lambda uid: post_order(uid, event_id, "Multi", tickets=2), user_ids)
    codes = [r.status_code for r in responses]
    won = codes.count(201)
    print(f"2-ticket orders for 5 seats -> {won} won, {codes.count(409)} refused")

    assert all(c in (201, 409) for c in codes), f"only 201/409 are acceptable, got {sorted(set(codes))}"
    assert won == 2, f"floor(5 / 2) = 2 orders must win whatever the interleaving, got {won}"
    assert env["booked"](event_id) == 4, "the seat counter must equal the tickets of the winning orders"
    assert env["active_tickets"](event_id) == 4


def test_sold_out_second_zone_returns_the_first_zones_seats(setup_test_environment):
    """An order spanning two zones is all-or-nothing: if zone B is full, zone A's seats go back."""
    env = setup_test_environment
    event_id = env["make_event"]([
        {"name": "A Zone", "price": 100.0, "capacity": 5, "booked_count": 0},
        {"name": "B Zone", "price": 100.0, "capacity": 1, "booked_count": 1},  # already full
    ])
    response = client.post("/api/v1/orders", json={
        "user_id": env["user_id"],
        "items": [{"event_id": event_id, "seat_zone": "A Zone"}, {"event_id": event_id, "seat_zone": "B Zone"}],
    })
    assert response.status_code == 409, response.text
    assert "sold out" in response.json()["detail"].lower()
    assert env["booked"](event_id, 0) == 0, "zone A must not keep a seat for an order that failed"
    assert env["booked"](event_id, 1) == 1
    assert env["active_tickets"](event_id) == 0


def test_event_id_spelling_cannot_bypass_the_seat_lock(setup_test_environment):
    """The same event written in upper case is still the same event, so the seat is still taken.

    Regression: tickets stored the event id exactly as the client typed it, so "ABC..." and "abc..."
    looked like two events to the unique seat index and one seat could be sold twice.
    """
    env = setup_test_environment
    event_id = env["make_event"]([{"name": "Case", "price": 10.0, "capacity": 10, "booked_count": 0}])
    first_user, second_user = env["make_users"](2)

    assert post_order(first_user, event_id, "Case", seats=["A1"]).status_code == 201
    assert post_order(second_user, event_id.upper(), "Case", seats=["A1"]).status_code == 409
    assert post_order(second_user, event_id, "Case", seats=[" A1 "]).status_code == 409, "padding must not hide a seat"
    assert env["active_tickets"](event_id) == 1
    assert env["booked"](event_id) == 1


def test_idempotent_replay_works_with_any_zone_spelling(setup_test_environment):
    """Replaying the very same request returns the original order, even if the zone name is not in the catalog's case."""
    env = setup_test_environment
    event_id = env["make_event"]([{"name": "Zone Case", "price": 10.0, "capacity": 10, "booked_count": 0}])
    key = f"IDEMP-CASE-{uuid.uuid4().hex}"

    first = post_order(env["user_id"], event_id, "zone case", seats=["Z-1"], key=key)
    replay = post_order(env["user_id"], event_id.upper(), "ZONE CASE", seats=["Z-1"], key=key)

    assert first.status_code == 201, first.text
    assert replay.status_code in (200, 201), replay.text
    assert replay.json()["id"] == first.json()["id"]
    assert env["booked"](event_id) == 1


@pytest.mark.skipif(LIVE_MODE, reason="counts calls made by the server process, so it only works in-process")
def test_a_four_ticket_order_costs_one_mongo_read_and_one_reservation(setup_test_environment, monkeypatch):
    """No query inside a per-ticket loop: 4 tickets in one zone = 1 catalog read + 1 atomic reservation."""
    from pymongo.collection import Collection

    env = setup_test_environment
    event_id = env["make_event"]([{"name": "Batch", "price": 10.0, "capacity": 50, "booked_count": 0}])
    calls = {"find": 0, "find_one": 0, "update_one": 0}

    def counting(name):
        real = getattr(Collection, name)

        def wrapper(self, *args, **kwargs):
            if self.name == "events":
                calls[name] += 1
            return real(self, *args, **kwargs)

        return wrapper

    for name in calls:
        monkeypatch.setattr(Collection, name, counting(name))

    response = post_order(env["user_id"], event_id, "Batch", seats=["B1", "B2", "B3", "B4"])
    assert response.status_code == 201, response.text
    print(f"Mongo calls on the events collection for a 4-ticket order: {calls}")
    assert calls["find"] + calls["find_one"] == 1, f"the catalog must be read once, got {calls}"
    assert calls["update_one"] == 1, f"the zone must be reserved with one update, got {calls}"
    assert env["booked"](event_id) == 4


def test_pay_vs_expiry_vs_cleanup_race_releases_each_seat_once(setup_test_environment):
    """3 expired holds are paid and swept at the same instant; 2 live holds must stay untouched.

    An expired hold can never be paid, and its seat must go back to the pool exactly once
    (releasing twice would hand out a seat that another order still holds). Whoever wins the
    row lock, the payment answers 410 Gone: either it finds the hold past expires_at, or the
    sweeper already marked the order 'expired'.
    """ 
    env = setup_test_environment
    user_id = env["user_id"]
    event_id = env["make_event"]([{"name": "Expiry", "price": 300.0, "capacity": 50, "booked_count": 0}])
    expiring = [post_order(user_id, event_id, "Expiry").json() for _ in range(3)]
    staying = [post_order(user_id, event_id, "Expiry").json() for _ in range(2)]
    assert env["booked"](event_id) == 5
    for order in expiring:
        env["expire_order"](order["id"])

    jobs = [("pay", order) for order in expiring] + [("sweep", None)] * 4

    def act(job):
        kind, order = job
        if kind == "pay":
            return kind, client.post(f"/api/v1/orders/{order['id']}/payments",
                                     json={"amount": order["total_amount"], "provider": "promptpay"})
        return kind, sweep_expired_holds()  # raises if the sweeper fails

    results = run_parallel(act, jobs)
    pay_codes = [r.status_code for kind, r in results if kind == "pay"]
    swept = [n for kind, n in results if kind == "sweep"]
    print(f"Pay vs sweep -> payments {sorted(pay_codes)}, holds expired per sweeper {sorted(swept)}")

    assert all(c == 410 for c in pay_codes), f"an expired hold must answer 410 Gone, got {pay_codes}" 
    assert len(swept) == 4, "every sweeper must finish without error"

    scalar = env["scalar"]
    expired_ids = [o["id"] for o in expiring]
    assert env["booked"](event_id) == 2, "exactly the 3 expired seats must be released, each once"
    assert scalar("SELECT count(*) FROM orders WHERE id = ANY(:i) AND status = 'expired'", i=expired_ids) == 3
    assert scalar("SELECT count(*) FROM payments WHERE order_id = ANY(:i)", i=expired_ids) == 0
    assert scalar("SELECT count(*) FROM tickets WHERE order_id = ANY(:i) AND status = 'cancelled'", i=expired_ids) == 3
    assert scalar("SELECT count(*) FROM orders WHERE id = ANY(:i) AND status = 'pending'",
                  i=[o["id"] for o in staying]) == 2
    assert env["active_tickets"](event_id) == 2


def test_parallel_cleanup_releases_each_hold_once(setup_test_environment):
    """8 sweepers run at once over 10 expired holds: every seat is released once, every hold logged once."""
    env = setup_test_environment
    event_id = env["make_event"]([{"name": "Sweep", "price": 100.0, "capacity": 100, "booked_count": 0}])
    orders = [post_order(env["user_id"], event_id, "Sweep").json() for _ in range(10)]
    assert env["booked"](event_id) == 10
    for order in orders:
        env["expire_order"](order["id"])

    swept = run_parallel(lambda _: sweep_expired_holds(), range(8))  # 8 sweepers claim rows with SKIP LOCKED
    assert len(swept) == 8, "every sweeper must finish without error"

    assert env["booked"](event_id) == 0, "all 10 seats released, never more than once"
    assert env["active_tickets"](event_id) == 0
    ids = [str(o["id"]) for o in orders]
    assert env["scalar"]("SELECT count(*) FROM outbox_events WHERE event_type = 'SEAT_HOLD_EXPIRED' "
                         "AND aggregate_id = ANY(:i)", i=ids) == 10


def test_concurrent_user_updates_do_not_lose_each_other(postgres_environment):
    """10 PUTs change only first_name and 10 change only last_name, all at once.

    Each PUT is a read-modify-write of the whole name. Without the row lock an update that read the
    old last name would write it back over a newer one (a lost update); with SELECT ... FOR UPDATE
    the final name is the last first_name update plus the last last_name update.
    """
    env = postgres_environment
    tag = uuid.uuid4().hex[:10]
    created = client.post("/api/v1/users", json={
        "email": f"lost_{tag}@example.com", "username": f"lost_{tag}", "password": "secret12",
        "first_name": "Start", "last_name": "Name",
    })
    assert created.status_code == 201, created.text
    user_id = created.json()["id"]
    env["track_user"](user_id)

    jobs = [("first_name", f"A{i}") for i in range(10)] + [("last_name", f"B{i}") for i in range(10)]
    responses = run_parallel(lambda job: client.put(f"/api/v1/users/{user_id}", json={job[0]: job[1]}), jobs)
    assert all(r.status_code == 200 for r in responses), [r.status_code for r in responses]

    final = client.get(f"/api/v1/users/{user_id}").json()
    print(f"Concurrent PUTs -> final name {final['first_name']} {final['last_name']}")
    assert final["first_name"].startswith("A"), f"a first_name update was lost: {final}"
    assert final["last_name"].startswith("B"), f"a stale update put the old last name back: {final}"
    assert final["full_name"] == f"{final['first_name']} {final['last_name']}"

#test deadlock
@pytest.mark.skipif(LIVE_MODE, reason="patches the server process, so it only works in-process")
def test_database_deadlock_is_answered_with_409_and_seats_are_returned(setup_test_environment, monkeypatch):
    """If PostgreSQL ever aborts a booking as a deadlock victim, the client gets 409 and keeps no seat."""
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session

    env = setup_test_environment
    event_id = env["make_event"]([{"name": "Safety Net", "price": 100.0, "capacity": 3, "booked_count": 0}])

    class FakeDeadlock(Exception):
        pgcode = "40P01"  # SQLSTATE deadlock_detected

    real_commit = Session.commit
    calls = {"n": 0}

    def failing_commit(self):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("COMMIT", {}, FakeDeadlock("deadlock detected"))
        return real_commit(self)

    monkeypatch.setattr(Session, "commit", failing_commit)
    response = post_order(env["user_id"], event_id, "Safety Net")

    assert response.status_code == 409, response.text
    assert "retry" in response.json()["detail"].lower()
    assert env["booked"](event_id) == 0, "the reserved seat must be given back"
    assert env["active_tickets"](event_id) == 0, "nothing may be persisted"
    assert post_order(env["user_id"], event_id, "Safety Net").status_code == 201, "the retry must succeed"
# ---------------------------------------------------------------------------------------------
# Overload: the database is busy, not the request's fault -> HTTP 503 + Retry-After, never 500
# ---------------------------------------------------------------------------------------------


@pytest.mark.skipif(LIVE_MODE, reason="patches the server process, so it only works in-process")
def test_exhausted_connection_pool_is_answered_with_503(setup_test_environment, monkeypatch):  # ADD
    """Every pooled connection is busy: SQLAlchemy raises its pool TimeoutError, the client gets 503.

    sqlalchemy.exc.TimeoutError is NOT a DBAPIError, so this proves the dedicated handler in
    app/main.py catches it (before, it fell through to a 500). Faking the error keeps the test
    deterministic; the manual proof is pool size 1 + LOCK TABLE orders in psql.
    """
    from sqlalchemy.exc import TimeoutError as PoolTimeoutError
    from app.core.db_errors import RETRY_AFTER_SECONDS

    orders_api = importlib.import_module("app.api.v1.orders")  # the module itself, not the router object

    def pool_exhausted(db):
        raise PoolTimeoutError("QueuePool limit of size 1 overflow 0 reached, connection timed out, timeout 2.00")

    monkeypatch.setattr(orders_api, "_order_summary_query", pool_exhausted)  # only this endpoint fails, no other request is affected
    response = client.get("/api/v1/orders/1/summary")

    assert response.status_code == 503, response.text
    assert response.headers.get("retry-after") == str(RETRY_AFTER_SECONDS), "the client must be told when to retry"
    assert "busy" in response.json()["detail"].lower()


@pytest.mark.skipif(LIVE_MODE, reason="patches the server process, so it only works in-process")
def test_statement_timeout_during_booking_is_answered_with_503_and_seats_are_returned(setup_test_environment, monkeypatch):  # ADD
    """PostgreSQL cancels a slow booking (statement_timeout, SQLSTATE 57014): 503, no seat kept, retry works.

    Before, create_order_dual_db only recognised deadlocks, so a timeout inside its transaction
    became a 500 that also leaked the raw database error to the client.
    """
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session

    env = setup_test_environment
    event_id = env["make_event"]([{"name": "Timeout", "price": 100.0, "capacity": 3, "booked_count": 0}])

    class FakeStatementTimeout(Exception):
        pgcode = "57014"  # SQLSTATE query_canceled (statement_timeout fired)

    real_commit = Session.commit
    calls = {"failed": False}

    def failing_commit(self):
        # Fail only the booking's own commit (it carries the new SEAT_HELD outbox row), so the
        # background worker or another session committing at the same moment is never hit by mistake.
        is_booking = any(isinstance(obj, OutboxEvent) and obj.event_type == "SEAT_HELD" for obj in self.new)
        if is_booking and not calls["failed"]:
            calls["failed"] = True
            raise OperationalError("COMMIT", {}, FakeStatementTimeout("canceling statement due to statement timeout"))
        return real_commit(self)

    monkeypatch.setattr(Session, "commit", failing_commit)
    response = post_order(env["user_id"], event_id, "Timeout")

    assert calls["failed"], "the fake timeout was never triggered, the test proved nothing"
    assert response.status_code == 503, response.text
    assert response.headers.get("retry-after"), "a 503 must carry Retry-After"
    detail = response.json()["detail"].lower()
    assert "busy" in detail
    assert "statement" not in detail and "sql" not in detail, "internal database errors must not leak to the client"
    assert env["booked"](event_id) == 0, "the reserved seat must be given back"
    assert env["active_tickets"](event_id) == 0, "nothing may be persisted"
    assert post_order(env["user_id"], event_id, "Timeout").status_code == 201, "the retry must succeed"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v", "-s"]))