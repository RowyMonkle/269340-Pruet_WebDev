"""Shared helpers for tests/test_concurrency.py and tests/test_consistency.py.

Two ways to reach the API:
  default              in-process (FastAPI TestClient): fast, needs only the two databases
  API_URL=http://nginx real HTTP through nginx and BOTH API replicas: the closest thing to
                       production and the only way to race two separate Python processes.
                       Example: docker compose exec -e API_URL=http://nginx api pytest tests/test_concurrency.py

Set REQUIRE_DB=1 to turn "databases not reachable" into a failure instead of a silent skip.
"""
import concurrent.futures
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, List, Optional

import pytest
from bson import ObjectId
from pymongo.collection import Collection
from sqlalchemy import event, insert, text
from sqlalchemy.engine.default import ExecuteStyle

from app.core.database import SessionLocal, engine, mongo_db, check_postgres_connection, check_mongo_connection
from app.models.sql_models import User
from app.services.user_service import hash_password

API_URL = os.environ.get("API_URL", "").rstrip("/")
REQUIRE_DB = os.environ.get("REQUIRE_DB", "") == "1"

if API_URL:
    import httpx

    client = httpx.Client(
        base_url=API_URL,
        timeout=60.0,
        limits=httpx.Limits(max_connections=300, max_keepalive_connections=100),
    )
else:
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)

LIVE_MODE = bool(API_URL)


def sweep_expired_holds(batch_size: Optional[int] = None, max_batches: Optional[int] = None) -> int:
    """Run the expired-hold sweeper exactly like the background worker does (own session). Returns holds expired.

    POST /orders/cleanup-expired is internal and blocked by nginx, so tests call the service function directly;
    in live mode this runs in the pytest process while the HTTP traffic hits the replicas, which is a real race.
    """
    from app.services.order_service import cleanup_expired_seat_holds

    with SessionLocal() as session:
        return cleanup_expired_seat_holds(session, mongo_db, batch_size=batch_size, max_batches=max_batches)


def run_parallel(fn: Callable, items: Iterable, workers: int = 0) -> list:
    """Call fn(item) for every item at the same instant (barrier) and return results in item order."""
    items = list(items)
    barrier = threading.Barrier(len(items))

    def task(item):
        barrier.wait(timeout=60)
        return fn(item)

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers or len(items)) as pool:
        return list(pool.map(task, items))


def post_order(user_id: int, event_id: str, zone: str, seats: Optional[List[str]] = None,
               key: Optional[str] = None, tickets: int = 1):
    """POST /orders for `seats` (explicit seat numbers) or for `tickets` seatless tickets."""
    seat_list = seats if seats is not None else [None] * tickets
    items = [
        {"event_id": event_id, "seat_zone": zone, **({"seat_number": s} if s else {})} for s in seat_list
    ]
    return client.post(
        "/api/v1/orders",
        json={"user_id": user_id, "payment_method": "promptpay", "items": items},
        headers={"Idempotency-Key": key} if key else None,
    )


def build_environment(need_mongo: bool = True):
    """Create one test user plus the factory helpers; returns (env dict, teardown function).

    Everything created through env is removed by teardown(), in foreign-key order.
    """
    ready = check_postgres_connection() and (not need_mongo or check_mongo_connection())
    if not ready:
        message = "Databases not accessible for live integration testing."
        if REQUIRE_DB:
            pytest.fail(message, pytrace=False)
        pytest.skip(message)

    password_hash = hash_password("Pass123!")  # hash once, bcrypt is slow on purpose
    user_ids: List[int] = []
    event_oids: List[ObjectId] = []

    def make_users(count: int) -> List[int]:
        rows = [
            dict(
                email=f"cp3_{uuid.uuid4().hex[:12]}@example.com",
                username=f"cp3_{uuid.uuid4().hex[:14]}",
                hashed_password=password_hash,
                first_name="Concurrency",
                last_name=f"Tester{i}",
                role="fan",
            )
            for i in range(count)
        ]
        table = User.__table__  # Core insert: never mentions full_name, so it also works after the CP2 contract
        with SessionLocal() as session:
            ids = [row[0] for row in session.execute(insert(table).returning(table.c.id), rows)]
            session.commit()
        user_ids.extend(ids)
        return ids

    def track_user(user_id: int) -> None:
        user_ids.append(user_id)

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
        oid = mongo_db["events"].insert_one(doc).inserted_id
        event_oids.append(oid)
        return str(oid)

    def booked(event_id: str, zone_index: int = 0) -> int:
        doc = mongo_db["events"].find_one({"_id": ObjectId(event_id)}, {"zones": 1})
        return doc["zones"][zone_index]["booked_count"]

    def scalar(sql: str, **params):
        with SessionLocal() as session:
            return session.execute(text(sql), params).scalar()

    def active_tickets(event_id: str) -> int:
        return scalar("SELECT count(*) FROM tickets WHERE event_id = :e AND status IN ('held', 'valid')", e=event_id)

    def expire_order(order_id: int, minutes: int = 15) -> None:
        with SessionLocal() as session:
            session.execute(
                text("UPDATE orders SET expires_at = :t WHERE id = :o"),
                {"t": datetime.now(timezone.utc) - timedelta(minutes=minutes), "o": order_id},
            )
            session.commit()

    first_user = make_users(1)[0]

    def teardown() -> None:
        # payments, tickets, outbox and orders reference the users (RESTRICT), so they go first
        with SessionLocal() as session:
            order_ids = [r[0] for r in session.execute(
                text("SELECT id FROM orders WHERE user_id = ANY(:u)"), {"u": user_ids})]
            if order_ids:
                session.execute(text("DELETE FROM payments WHERE order_id = ANY(:o)"), {"o": order_ids})
                session.execute(text("DELETE FROM tickets WHERE order_id = ANY(:o)"), {"o": order_ids})
                session.execute(text("DELETE FROM outbox_events WHERE aggregate_id = ANY(:o)"),
                                {"o": [str(o) for o in order_ids]})
                session.execute(text("DELETE FROM orders WHERE id = ANY(:o)"), {"o": order_ids})
            session.execute(text("DELETE FROM users WHERE id = ANY(:u)"), {"u": user_ids})
            session.commit()
        for oid in event_oids:
            mongo_db["events"].delete_one({"_id": oid})

    env = {
        "user_id": first_user,
        "make_event": make_event,
        "make_users": make_users,
        "track_user": track_user,
        "booked": booked,
        "active_tickets": active_tickets,
        "expire_order": expire_order,
        "scalar": scalar,
    }
    return env, teardown


class QueryCounter:
    """Counts the database round trips THIS process makes while the block runs (in-process mode only).

    SQL    every cursor.execute. A multi-row statement SQLAlchemy builds itself (INSERT ... VALUES (..),(..),
           "insertmanyvalues") is ONE round trip; an executemany of N parameter sets is N, because psycopg2
           sends those one by one. That second kind is what an N+1 looks like on a write path.
    Mongo  every public Collection call (find, find_one, count_documents, aggregate, update_one, insert_one ...).
           A call that calls another public method itself (find_one -> find, count_documents -> aggregate) counts
           once. getMore round trips of a cursor are not counted: no list here is bigger than one batch.

    with QueryCounter() as counted:
        client.get(...)
    counted.sql, counted.mongo, counted.statements, counted.mongo_calls
    """

    MONGO_METHODS = (
        "find", "find_one", "count_documents", "estimated_document_count", "aggregate", "distinct",
        "insert_one", "insert_many", "update_one", "update_many", "replace_one", "delete_one", "delete_many",
        "find_one_and_update", "find_one_and_delete", "bulk_write",
    )
    _IGNORED_SQL = {"SELECT 1", "ROLLBACK", "COMMIT", "BEGIN"}

    def __init__(self):
        self.sql = 0
        self.mongo = 0
        self.statements = []
        self.mongo_calls = []
        self._depth = threading.local()
        self._originals = {}

    def _on_sql(self, conn, cursor, statement, parameters, context, executemany):
        if statement.strip().upper() in self._IGNORED_SQL:
            return
        rows = len(parameters) if getattr(context, "execute_style", None) == ExecuteStyle.EXECUTEMANY else 1
        self.sql += rows
        self.statements.append((" ".join(statement.split())[:100], rows))

    def _wrap(self, name, original):
        counter = self

        def wrapper(collection, *args, **kwargs):
            depth = getattr(counter._depth, "n", 0)
            if depth == 0:
                counter.mongo += 1
                counter.mongo_calls.append(f"{collection.name}.{name}")
            counter._depth.n = depth + 1
            try:
                return original(collection, *args, **kwargs)
            finally:
                counter._depth.n = depth

        wrapper.__name__ = name
        return wrapper

    def __enter__(self):
        event.listen(engine, "before_cursor_execute", self._on_sql)
        for name in self.MONGO_METHODS:
            self._originals[name] = getattr(Collection, name)
            setattr(Collection, name, self._wrap(name, self._originals[name]))
        return self

    def __exit__(self, *exc_info):
        event.remove(engine, "before_cursor_execute", self._on_sql)
        for name, original in self._originals.items():
            setattr(Collection, name, original)
        return False
