"""Query audit (CP3 section 2.2): no N+1, and read endpoints fetch only what they return.

Two promises are checked for every endpoint, with the amount of data growing by orders of magnitude:

  1. The number of database round trips does NOT depend on how many rows the answer contains
     (a list of 100 users costs the same as a list of 1; an order with 50 tickets costs the same as one with 1).
  2. The payload is bounded: a list returns summary rows, never whole entity graphs.

The counter (tests/support.py: QueryCounter) is itself tested first, so a green result means something.
Run with -s to print the audit table that README section 7.10 quotes:

    docker compose exec -e REQUIRE_DB=1 api pytest tests/test_query_counts.py -v -s

In-process only: the counter watches this Python process, so it cannot run with API_URL (live mode).
"""
import os
import sys
import uuid

import pytest
from sqlalchemy import insert, text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.database import SessionLocal, mongo_db  # noqa: E402
from app.models.sql_models import OutboxEvent  # noqa: E402
from tests.support import LIVE_MODE, QueryCounter, build_environment, client, post_order  # noqa: E402

pytestmark = pytest.mark.skipif(LIVE_MODE, reason="counts the queries of this process, so it only works in-process")

AUDIT = []  # (endpoint, data size, SQL round trips, Mongo calls, KB) printed at the end with -s


def sql(query: str, **params):
    with SessionLocal() as session:
        result = session.execute(text(query), params)
        session.commit()
        return result


def add_orders(user_id: int, count: int, tickets: int = 2, payments: int = 2) -> int:
    """`count` confirmed orders for the user, each with `tickets` tickets and `payments` payment attempts. Returns the first order id.

    Realistic enough for the consistency audit: exactly one attempt is `completed` and it equals the order total, the others `failed`.
    """
    prefix = "QC" + uuid.uuid4().hex[:8] + "-"
    sql("INSERT INTO orders (order_number, user_id, total_amount, status, payment_method) "
        "SELECT :p || g, :u, :t, 'confirmed', 'promptpay' FROM generate_series(1, :n) g",
        p=prefix, u=user_id, n=count, t=10 * tickets)
    sql("INSERT INTO tickets (order_id, event_id, ticket_code, seat_zone, price, status) "
        "SELECT o.id, '000000000000000000000000', 'T' || o.id || '-' || k, 'GA', 10, 'valid' "
        "FROM orders o, generate_series(1, :k) k WHERE o.order_number LIKE :p || '%'", p=prefix, k=tickets)
    sql("INSERT INTO payments (order_id, payment_reference, amount, provider, status) "
        "SELECT o.id, 'P' || o.id || '-' || k, :t, 'promptpay', CASE WHEN k = 1 THEN 'completed' ELSE 'failed' END "
        "FROM orders o, generate_series(1, :k) k WHERE o.order_number LIKE :p || '%'", p=prefix, k=payments, t=10 * tickets)
    return sql("SELECT min(id) FROM orders WHERE order_number LIKE :p || '%'", p=prefix).scalar()


@pytest.fixture(scope="module")
def env():
    environment, teardown = build_environment(need_mongo=True)
    yield environment
    teardown()
    if AUDIT:
        print("\n\nQUERY AUDIT  (database round trips per request; payload of the answer)")
        print(f"{'endpoint':<36}{'data':<26}{'SQL':>5}{'Mongo':>7}{'KB':>9}")
        previous = None
        for endpoint, data, sql_count, mongo_count, kb in AUDIT:
            if previous and previous != endpoint:
                print()
            previous = endpoint
            print(f"{endpoint:<36}{data:<26}{sql_count:>5}{mongo_count:>7}{kb:>9.1f}")


@pytest.fixture(scope="module")
def data(env):
    """Datasets of very different sizes, all removed by the environment teardown."""
    users_with = {}
    for count in (1, 10, 100, 300):
        user_id = env["make_users"](1)[0]
        add_orders(user_id, count)
        users_with[count] = user_id

    children = {}  # tickets and payments per order
    for count in (1, 10, 50):
        user_id = env["make_users"](1)[0]
        children[count] = add_orders(user_id, 1, tickets=count, payments=count)

    if sql("SELECT count(*) FROM users").scalar() < 100:
        env["make_users"](100)

    category = "QC-" + uuid.uuid4().hex[:8]  # a private slice of the catalog, whatever else is in MongoDB
    for index in range(100):
        event_id = env["make_event"]([
            {"name": "Front", "price": 100.0 + index, "capacity": 10, "booked_count": 4},
            {"name": "Back", "price": 50.0, "capacity": 5, "booked_count": 5},
        ])
        mongo_db["events"].update_one(
            {"_id": __import__("bson").ObjectId(event_id)},
            {"$set": {"category": category, "dynamic_attributes": {"stage": "x" * 200}, "artist.bio": "b" * 300}},
        )
    return {"users_with": users_with, "children": children, "category": category}


def measure(path: str, endpoint: str, data_label: str, method: str = "get", **kwargs):
    with QueryCounter() as counted:
        response = getattr(client, method)(path, **kwargs)
    assert response.status_code in (200, 201), f"{path} answered {response.status_code}: {response.text[:200]}"
    kb = len(response.content) / 1024
    AUDIT.append((endpoint, data_label, counted.sql, counted.mongo, kb))
    return response, counted, kb


# ----------------------------------------------------------------------------------------------------------
# 0. The counter itself
# ----------------------------------------------------------------------------------------------------------


def test_the_counter_sees_n_plus_one_and_does_not_invent_it(env):
    rows = [{"event_type": "QC_TEST", "aggregate_id": f"qc-{uuid.uuid4().hex[:6]}-{i}", "payload": "{}", "status": "processed"} for i in range(20)]
    try:
        with QueryCounter() as counted:  # 20 separate SELECTs = the classic N+1
            with SessionLocal() as session:
                for i in range(20):
                    session.execute(text("SELECT :i"), {"i": i})
        assert counted.sql == 20

        with QueryCounter() as counted:  # executemany of 20 rows: psycopg2 sends 20 UPDATEs
            with SessionLocal() as session:
                session.execute(text("UPDATE users SET role = role WHERE id = :i"), [{"i": i} for i in range(20)])
                session.rollback()
        assert counted.sql == 20, "an executemany of 20 rows is 20 round trips"

        with QueryCounter() as counted:  # one multi-row INSERT built by SQLAlchemy: 1 round trip, not 20
            with SessionLocal() as session:
                session.execute(insert(OutboxEvent).returning(OutboxEvent.id), rows)
                session.commit()
        assert counted.sql == 1, f"a multi-row INSERT is one round trip, counted {counted.sql}: {counted.statements}"

        with QueryCounter() as counted:  # find_one calls find inside pymongo: still one call
            for _ in range(3):
                mongo_db["events"].find_one({}, {"_id": 1})
            mongo_db["events"].count_documents({"category": "QC-none"})  # calls aggregate inside: still one call
        assert counted.mongo == 4, counted.mongo_calls
    finally:
        sql("DELETE FROM outbox_events WHERE event_type = 'QC_TEST'")


# ----------------------------------------------------------------------------------------------------------
# 1. Read endpoints: same round trips for 1 row and for 300
# ----------------------------------------------------------------------------------------------------------

READ_CASES = [
    # name, builder of (small path, small label, large path, large label), max SQL, max Mongo
    ("GET /users (list)", lambda d: ("/api/v1/users?page=1&page_size=1", "page_size=1", "/api/v1/users?page=1&page_size=100", "page_size=100"), 2, 0),
    ("GET /users/{id}", lambda d: (f"/api/v1/users/{d['users_with'][1]}", "1 user", f"/api/v1/users/{d['users_with'][300]}", "user with 300 orders"), 1, 0),
    ("GET /orders/summary", lambda d: (f"/api/v1/orders/summary?user_id={d['users_with'][1]}&limit=20", "1 order", f"/api/v1/orders/summary?user_id={d['users_with'][300]}&limit=20", "300 orders, limit 20"), 2, 0),
    ("GET /orders/{id}/summary", lambda d: (f"/api/v1/orders/{d['children'][1]}/summary", "1 ticket + 1 payment", f"/api/v1/orders/{d['children'][50]}/summary", "50 tickets + 50 payments"), 1, 0),
    ("GET /orders/{id} (detail)", lambda d: (f"/api/v1/orders/{d['children'][1]}", "1 ticket + 1 payment", f"/api/v1/orders/{d['children'][50]}", "50 tickets + 50 payments"), 1, 0),
    ("GET /orders/{id}/payments", lambda d: (f"/api/v1/orders/{d['children'][1]}/payments", "1 payment", f"/api/v1/orders/{d['children'][50]}/payments", "50 payments"), 2, 0),
    ("GET /orders?user_id= (original)", lambda d: (f"/api/v1/orders?user_id={d['users_with'][1]}", "1 order", f"/api/v1/orders?user_id={d['users_with'][300]}", "300 orders"), 1, 0),
    ("GET /events (list)", lambda d: (f"/api/v1/events?category={d['category']}&page_size=1", "page_size=1", f"/api/v1/events?category={d['category']}&page_size=100", "page_size=100"), 0, 2),
    ("GET /products (alias of events)", lambda d: (f"/api/v1/products?category={d['category']}&page_size=1", "page_size=1", f"/api/v1/products?category={d['category']}&page_size=100", "page_size=100"), 0, 2),
]


@pytest.mark.parametrize("name, build, max_sql, max_mongo", READ_CASES, ids=[case[0] for case in READ_CASES])
def test_read_endpoint_round_trips_do_not_grow_with_the_data(env, data, name, build, max_sql, max_mongo):
    small_path, small_label, large_path, large_label = build(data)
    client.get(small_path)  # warm up: feature-flag cache and connection pool, not part of the budget
    _, small, _ = measure(small_path, name, small_label)
    _, large, _ = measure(large_path, name, large_label)
    assert (large.sql, large.mongo) == (small.sql, small.mongo), (
        f"{name}: N+1. {small_label}: {small.sql} SQL / {small.mongo} Mongo, {large_label}: {large.sql} SQL / {large.mongo} Mongo\n"
        f"{large.statements}"
    )
    assert large.sql <= max_sql and large.mongo <= max_mongo, f"{name}: {large.sql} SQL / {large.mongo} Mongo, budget {max_sql} / {max_mongo}"


# ----------------------------------------------------------------------------------------------------------
# 2. Payload: summaries are small and bounded
# ----------------------------------------------------------------------------------------------------------


def test_order_summaries_are_small_and_do_not_grow_with_the_number_of_orders(env, data):
    _, _, few = measure(f"/api/v1/orders/summary?user_id={data['users_with'][10]}&limit=20", "payload: GET /orders/summary", "10 orders")
    _, _, many = measure(f"/api/v1/orders/summary?user_id={data['users_with'][300]}&limit=20", "payload: GET /orders/summary", "300 orders, limit 20")
    assert many < 8, f"a page of 20 summaries should be a few KB, got {many:.1f} KB"
    assert many <= few * 3 + 1, "the page size, not the number of orders, decides the payload"


def test_order_summary_has_no_nested_children(env, data):
    item = client.get(f"/api/v1/orders/summary?user_id={data['users_with'][10]}&limit=1").json()[0]
    assert set(item) == {"id", "order_number", "user_id", "status", "total_amount", "ticket_count", "paid_amount", "expires_at", "created_at"}
    assert item["ticket_count"] == 2 and item["paid_amount"] == 20.0, "counted and summed by the database"


def test_events_list_is_a_summary_without_the_heavy_fields(env, data):
    response, _, kb = measure(f"/api/v1/events?category={data['category']}&page_size=100", "payload: GET /events", "100 events")
    body = response.json()
    assert body["total"] == 100 and len(body["items"]) == 100
    allowed = {"id", "title", "category", "artist", "venue", "date_time", "status", "min_price", "seats_left"}
    for item in body["items"]:
        assert set(item) == allowed
        assert set(item["artist"]) == {"name", "genre"} and set(item["venue"]) == {"name", "city"}
    assert "stage" not in response.text and "bbbbbbbbbb" not in response.text, "dynamic_attributes and artist.bio must not be sent"
    assert kb < 40, f"100 summary rows should be about 30 KB, got {kb:.1f} KB (the full documents are about 5x that)"


def test_events_list_derives_lowest_price_and_seats_left_in_the_database(env, data):
    items = client.get(f"/api/v1/events?category={data['category']}&page_size=100").json()["items"]
    assert sorted(item["min_price"] for item in items) == [50.0] * 100, "the cheaper 'Back' zone is the lowest price"
    assert {item["seats_left"] for item in items} == {6}, "(10 - 4) + (5 - 5) seats"
    full = client.get(f"/api/v1/events/{items[0]['id']}").json()
    assert "zones" in full and "dynamic_attributes" in full, "the detail endpoint still returns the complete document"


def test_events_list_pages_do_not_overlap(env, data):
    first = client.get(f"/api/v1/events?category={data['category']}&page=1&page_size=40").json()
    second = client.get(f"/api/v1/events?category={data['category']}&page=2&page_size=40").json()
    third = client.get(f"/api/v1/events?category={data['category']}&page=3&page_size=40").json()
    ids = [item["id"] for page in (first, second, third) for item in page["items"]]
    assert len(ids) == 100 and len(set(ids)) == 100, "every event appears exactly once across the pages"
    assert first["total_pages"] == 3


def test_users_list_returns_plain_columns_and_no_secrets(env, data):
    response, _, kb = measure("/api/v1/users?page=1&page_size=100", "payload: GET /users", "page_size=100")
    assert "hashed_password" not in response.text
    assert set(response.json()["items"][0]) == {"id", "email", "username", "full_name", "first_name", "last_name", "role", "created_at", "updated_at"}
    assert kb < 60


@pytest.mark.xfail(
    reason="the ORIGINAL GET /orders?user_id= still loads whole entity graphs and is not paginated (326 KB for 300 orders). "
           "Use GET /orders/summary; this test passes by itself once the original endpoint is bounded too.",
    strict=False,
)
def test_original_orders_list_payload_is_bounded(env, data):
    _, _, kb = measure(f"/api/v1/orders?user_id={data['users_with'][300]}", "payload: GET /orders?user_id=", "300 orders")
    assert kb < 60, f"{kb:.0f} KB for 300 orders"


# ----------------------------------------------------------------------------------------------------------
# 3. Write endpoints: the number of tickets must not change the number of round trips
# ----------------------------------------------------------------------------------------------------------


def book(env, event_id: str, tickets: int):
    book.counter = getattr(book, "counter", 0) + 1
    seats = [f"QC{book.counter}-{i}" for i in range(tickets)]
    response = post_order(env["user_id"], event_id, "Big", seats=seats)
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture(scope="module")
def big_event(env):
    return env["make_event"]([{"name": "Big", "price": 10.0, "capacity": 100000, "booked_count": 0}])


def test_booking_costs_the_same_round_trips_for_1_and_for_20_tickets(env, big_event):
    book(env, big_event, 2)  # warm up
    counts = {}
    for tickets in (1, 20):
        with QueryCounter() as counted:
            book(env, big_event, tickets)
        counts[tickets] = counted
        AUDIT.append(("POST /orders (booking)", f"{tickets} ticket(s)", counted.sql, counted.mongo, 0.0))
    assert (counts[20].sql, counts[20].mongo) == (counts[1].sql, counts[1].mongo), (
        f"booking is N+1: 1 ticket {counts[1].sql}/{counts[1].mongo}, 20 tickets {counts[20].sql}/{counts[20].mongo}\n{counts[20].statements}")
    assert counts[20].sql <= 8 and counts[20].mongo <= 4


def test_payment_costs_the_same_round_trips_for_1_and_for_20_tickets(env, big_event):
    """Regression: the payment used to run one UPDATE per ticket (11 + N round trips)."""
    orders = {tickets: [book(env, big_event, tickets) for _ in range(2)] for tickets in (1, 20)}
    counts = {}
    for tickets in (1, 20):
        warm, measured = orders[tickets]
        client.post(f"/api/v1/orders/{warm['id']}/payments", json={"amount": warm["total_amount"], "provider": "promptpay"})
        with QueryCounter() as counted:
            response = client.post(f"/api/v1/orders/{measured['id']}/payments", json={"amount": measured["total_amount"], "provider": "promptpay"})
        assert response.status_code == 201, response.text
        counts[tickets] = counted
        AUDIT.append(("POST /orders/{id}/payments", f"{tickets} ticket(s)", counted.sql, counted.mongo, 0.0))
    assert counts[20].sql == counts[1].sql, f"payment is N+1: {counts[1].sql} -> {counts[20].sql} round trips\n{counts[20].statements}"
    assert counts[20].sql <= 8, f"{counts[20].sql} round trips: {counts[20].statements}"
    assert env["scalar"]("SELECT count(*) FROM tickets WHERE order_id = :o AND status = 'valid'", o=orders[20][1]["id"]) == 20, "all 20 tickets became valid"


def test_paying_an_expired_hold_also_costs_the_same_round_trips(env, big_event):
    counts = {}
    for tickets in (1, 20):
        order = book(env, big_event, tickets)
        env["expire_order"](order["id"])
        with QueryCounter() as counted:
            response = client.post(f"/api/v1/orders/{order['id']}/payments", json={"amount": order["total_amount"], "provider": "promptpay"})
        assert response.status_code == 410, response.text
        counts[tickets] = counted
        AUDIT.append(("POST payments (expired hold)", f"{tickets} ticket(s)", counted.sql, counted.mongo, 0.0))
        assert env["scalar"]("SELECT count(*) FROM tickets WHERE order_id = :o AND status = 'cancelled'", o=order["id"]) == tickets
    assert (counts[20].sql, counts[20].mongo) == (counts[1].sql, counts[1].mongo), f"{counts[1].sql} -> {counts[20].sql}\n{counts[20].statements}"
    assert env["booked"](big_event) >= 0, "the counter never goes below zero"
