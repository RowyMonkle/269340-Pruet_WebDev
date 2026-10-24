"""Resilience tests: one stuck lock, one busy pool or one dead database must not take the API down.

Every test here uses the REAL mechanism, nothing is faked: a row lock really held by another connection, a
pool really exhausted, a MongoDB address that really does not answer. What each protects against:

  database limits are in force   lock_timeout / statement_timeout / pool_timeout reach every connection
  a stuck lock                   a request waits lock_timeout, then HTTP 409 (retry), and frees its thread
  a slow statement               cancelled after statement_timeout, then HTTP 503 + Retry-After
  a busy pool                    a request waits pool_timeout, then HTTP 503 + Retry-After (not 30 s)
  MongoDB unreachable            HTTP 503 + Retry-After, and seats taken before the failure are given back
  the sweeper                    works in batches, one MongoDB update per (event, zone), skips rows being paid
  the outbox                     drained in big batches, old processed rows deleted
  the background worker          never blocks the event loop, one failing step does not stop the others
  the feature-flag cache         a slow refresh makes nobody else wait
  the gateway                    nginx hides the internal sweep endpoint

Tests marked in-process replace FastAPI dependencies, so they cannot run with API_URL (live mode).
Run:  docker compose exec -e REQUIRE_DB=1 api pytest tests/test_resilience.py -v
"""
import asyncio
import os
import sys
import threading
import time
import uuid
from contextlib import contextmanager

import pytest
from pymongo import MongoClient
from pymongo.errors import ServerSelectionTimeoutError
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core import flags  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.database import (  # noqa: E402
    SessionLocal, engine, get_db, get_mongo_db, mongo_client, mongo_db, relax_timeouts_for_scripts,
)
from app.core.db_errors import RETRY_AFTER_SECONDS  # noqa: E402
from app.services import order_service, outbox_worker  # noqa: E402
from tests.support import LIVE_MODE, build_environment, client, post_order, sweep_expired_holds  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
in_process_only = pytest.mark.skipif(LIVE_MODE, reason="replaces FastAPI dependencies, so it only works in-process")


@pytest.fixture(scope="module")
def env():
    environment, teardown = build_environment(need_mongo=True)
    yield environment
    teardown()


def pg_setting(connection, name: str) -> str:
    return connection.execute(text(f"SHOW {name}")).scalar()


def as_pg_duration(ms: int) -> str:
    """How PostgreSQL prints a millisecond setting: 5000 -> '5s', 300 -> '300ms', 0 -> '0'."""
    if ms == 0:
        return "0"
    return f"{ms // 1000}s" if ms % 1000 == 0 else f"{ms}ms"


@contextmanager
def api_with_engine(**limits):
    """Send the API's database traffic through a throw-away engine with its own limits.

    limits: lock_ms, statement_ms, pool_size, max_overflow, pool_timeout
    """
    options = []
    if limits.get("lock_ms"):
        options.append(f"-c lock_timeout={limits['lock_ms']}")
    if limits.get("statement_ms"):
        options.append(f"-c statement_timeout={limits['statement_ms']}")
    small = create_engine(
        settings.sync_database_url,
        pool_size=limits.get("pool_size", 5),
        max_overflow=limits.get("max_overflow", 0),
        pool_timeout=limits.get("pool_timeout", 5),
        connect_args={"options": " ".join(options)} if options else {},
    )
    maker = sessionmaker(bind=small, autocommit=False, autoflush=False)

    def override():
        db = maker()
        try:
            yield db
        finally:
            db.close()

    from app.main import app

    app.dependency_overrides[get_db] = override
    try:
        yield small
    finally:
        app.dependency_overrides.pop(get_db, None)
        small.dispose()


@contextmanager
def row_locked(table: str, row_id: int):
    """Hold SELECT ... FOR UPDATE on one row from a separate connection (a stuck transaction)."""
    holder = create_engine(settings.sync_database_url).connect()
    try:
        holder.execute(text(f"SELECT id FROM {table} WHERE id = :i FOR UPDATE"), {"i": row_id})
        yield
    finally:
        holder.rollback()
        holder.close()


def make_user(env) -> int:
    return env["make_users"](1)[0]


# ----------------------------------------------------------------------------------------------------------
# 1. The limits are really in force (this is what was missing: the settings existed but did nothing)
# ----------------------------------------------------------------------------------------------------------


def test_pool_timeout_setting_reaches_the_engine():
    assert engine.pool.timeout() == settings.POSTGRES_POOL_TIMEOUT


def test_every_pooled_connection_carries_the_lock_and_statement_limits(env):
    with engine.connect() as connection:
        assert pg_setting(connection, "lock_timeout") == as_pg_duration(settings.POSTGRES_LOCK_TIMEOUT_MS)
        assert pg_setting(connection, "statement_timeout") == as_pg_duration(settings.POSTGRES_STATEMENT_TIMEOUT_MS)


def test_default_limits_are_the_agreed_ones():
    from app.core.config import Settings

    declared = {name: Settings.model_fields[name].default for name in (
        "POSTGRES_LOCK_TIMEOUT_MS", "POSTGRES_STATEMENT_TIMEOUT_MS", "POSTGRES_POOL_TIMEOUT", "MONGO_SERVER_SELECTION_TIMEOUT_MS")}
    assert declared == {
        "POSTGRES_LOCK_TIMEOUT_MS": 5000,
        "POSTGRES_STATEMENT_TIMEOUT_MS": 15000,
        "POSTGRES_POOL_TIMEOUT": 5.0,
        "MONGO_SERVER_SELECTION_TIMEOUT_MS": 1000,
    }


def test_scripts_can_switch_the_limits_off(env):
    fresh = create_engine(settings.sync_database_url, connect_args={"options": "-c lock_timeout=5000 -c statement_timeout=15000"})
    try:
        with fresh.connect() as connection:
            assert pg_setting(connection, "statement_timeout") == "15s"
        fresh.dispose()
        relax_timeouts_for_scripts(fresh)
        with fresh.connect() as connection:
            assert pg_setting(connection, "statement_timeout") == "0"
            assert pg_setting(connection, "lock_timeout") == "0"
    finally:
        fresh.dispose()


def test_mongo_client_uses_the_short_timeouts():
    assert mongo_client.options.server_selection_timeout == settings.MONGO_SERVER_SELECTION_TIMEOUT_MS / 1000
    assert mongo_client.options.pool_options.connect_timeout == settings.MONGO_CONNECT_TIMEOUT_MS / 1000


# ----------------------------------------------------------------------------------------------------------
# 2. A stuck lock, a slow statement and a busy pool answer 409 / 503 quickly instead of hanging
# ----------------------------------------------------------------------------------------------------------


@in_process_only
def test_a_stuck_row_lock_answers_409_after_lock_timeout_and_frees_the_request(env):
    user_id = make_user(env)
    with api_with_engine(lock_ms=300), row_locked("users", user_id):
        started = time.perf_counter()
        response = client.put(f"/api/v1/users/{user_id}", json={"last_name": "Blocked"})
        waited = time.perf_counter() - started
    assert response.status_code == 409, response.text
    assert "retry" in response.json()["detail"].lower()
    assert 0.2 < waited < 3, f"should wait about lock_timeout (0.3 s), waited {waited:.2f} s"
    assert client.put(f"/api/v1/users/{user_id}", json={"last_name": "Free"}).status_code == 200, "works once the lock is gone"


@in_process_only
def test_a_slow_statement_is_cancelled_and_answers_503_with_retry_after(env):
    user_id = make_user(env)
    with api_with_engine(statement_ms=300), row_locked("users", user_id):
        response = client.put(f"/api/v1/users/{user_id}", json={"last_name": "Slow"})
    assert response.status_code == 503, response.text
    assert response.headers.get("retry-after") == str(RETRY_AFTER_SECONDS)
    assert "statement" not in response.json()["detail"].lower(), "no database internals in the answer"


@in_process_only
def test_an_exhausted_pool_answers_503_after_pool_timeout_not_30_seconds(env):
    user_id = make_user(env)
    with api_with_engine(pool_size=1, max_overflow=0, pool_timeout=0.5) as small:
        busy = small.connect()  # the only connection is in use
        try:
            started = time.perf_counter()
            response = client.get(f"/api/v1/orders/summary?user_id={user_id}")
            waited = time.perf_counter() - started
        finally:
            busy.close()
        assert response.status_code == 503, response.text
        assert response.headers.get("retry-after") == str(RETRY_AFTER_SECONDS)
        assert waited < 3, f"must give up after pool_timeout (0.5 s), waited {waited:.2f} s"
        assert client.get(f"/api/v1/orders/summary?user_id={user_id}").status_code == 200, "recovers by itself"


@in_process_only
def test_requests_that_need_no_lock_keep_working_while_another_row_is_locked(env):
    stuck, other = make_user(env), make_user(env)
    with api_with_engine(lock_ms=300), row_locked("users", stuck):
        assert client.get(f"/api/v1/users/{other}").status_code == 200
        assert client.put(f"/api/v1/users/{other}", json={"last_name": "Fine"}).status_code == 200


# ----------------------------------------------------------------------------------------------------------
# 3. MongoDB unreachable: 503, and nothing is left reserved
# ----------------------------------------------------------------------------------------------------------


@contextmanager
def dead_mongo():
    from app.main import app

    dead = MongoClient("mongodb://127.0.0.1:1", serverSelectionTimeoutMS=300, connectTimeoutMS=300)[settings.MONGO_DB]
    app.dependency_overrides[get_mongo_db] = lambda: dead
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_mongo_db, None)


@in_process_only
def test_mongo_down_answers_503_for_mongo_endpoints_and_postgres_endpoints_still_work(env):
    user_id = make_user(env)
    with dead_mongo():
        events = client.get("/api/v1/events?page_size=1")
        booking = client.post("/api/v1/orders", json={
            "user_id": user_id, "items": [{"event_id": "0" * 24, "seat_zone": "Z"}],
        })
        assert events.status_code == 503 and events.headers.get("retry-after") == str(RETRY_AFTER_SECONDS), events.text
        assert booking.status_code == 503, booking.text
        assert client.get(f"/api/v1/users/{user_id}").status_code == 200, "PostgreSQL-only endpoints must not care"
        assert client.get(f"/api/v1/orders/summary?user_id={user_id}").status_code == 200


@in_process_only
def test_mongo_failing_halfway_through_a_booking_returns_the_seats_already_taken(env, monkeypatch):
    from pymongo.collection import Collection

    event_id = env["make_event"]([
        {"name": "Zone A", "price": 10.0, "capacity": 5, "booked_count": 0},
        {"name": "Zone B", "price": 10.0, "capacity": 5, "booked_count": 0},
    ])
    real_update_one, calls = Collection.update_one, {"n": 0}

    def flaky(self, *args, **kwargs):
        if self.name == "events":
            calls["n"] += 1
            if calls["n"] == 2:  # zone A was reserved, MongoDB stops answering for zone B
                raise ServerSelectionTimeoutError("mongo went away")
        return real_update_one(self, *args, **kwargs)

    monkeypatch.setattr(Collection, "update_one", flaky)
    response = client.post("/api/v1/orders", json={
        "user_id": env["user_id"],
        "items": [{"event_id": event_id, "seat_zone": "Zone A"}, {"event_id": event_id, "seat_zone": "Zone B"}],
    })
    monkeypatch.undo()

    assert response.status_code == 503, response.text
    assert env["booked"](event_id, 0) == 0, "zone A must not keep a seat for an order that failed"
    assert env["booked"](event_id, 1) == 0
    assert env["active_tickets"](event_id) == 0


# ----------------------------------------------------------------------------------------------------------
# 4. The sweeper: batches, grouped MongoDB updates, rows being paid are skipped
# ----------------------------------------------------------------------------------------------------------


def make_expired_holds(env, event_id: str, zone: str, count: int, tickets_each: int = 2) -> str:
    prefix = f"RS{uuid.uuid4().hex[:8]}-"
    with SessionLocal() as session:
        session.execute(text(
            "INSERT INTO orders (order_number, user_id, total_amount, status, payment_method, expires_at) "
            "SELECT :p || g, :u, 20, 'pending', 'promptpay', now() - interval '20 minutes' FROM generate_series(1, :n) g"),
            {"p": prefix, "u": env["user_id"], "n": count})
        session.execute(text(
            "INSERT INTO tickets (order_id, event_id, ticket_code, seat_zone, price, status) "
            "SELECT o.id, :e, 'T' || o.id || '-' || k, :z, 10, 'held' FROM orders o, generate_series(1, :k) k "
            "WHERE o.order_number LIKE :p || '%'"),
            {"e": event_id, "z": zone, "k": tickets_each, "p": prefix})
        session.commit()
    return prefix


def count_orders(env, prefix: str, status: str) -> int:
    return env["scalar"]("SELECT count(*) FROM orders WHERE order_number LIKE :p || '%' AND status = :s", p=prefix, s=status)


def spy_on_event_updates(monkeypatch, event_id: str) -> dict:
    """Count MongoDB update_one calls (from this process) that touch one event."""
    from bson import ObjectId
    from pymongo.collection import Collection

    real_update_one, calls = Collection.update_one, {"n": 0}
    oid = ObjectId(event_id)

    def counting(self, filter, *args, **kwargs):
        if self.name == "events" and isinstance(filter, dict) and filter.get("_id") == oid:
            calls["n"] += 1
        return real_update_one(self, filter, *args, **kwargs)

    monkeypatch.setattr(Collection, "update_one", counting)
    return calls


def sweep_until_done(env, prefixes, batch_size=500) -> int:
    """Sweep one batch at a time until none of these holds is pending any more. Returns the calls made."""
    calls = 0
    while any(count_orders(env, prefix, "pending") for prefix in prefixes) and calls < 40:
        sweep_expired_holds(batch_size=batch_size, max_batches=1)
        calls += 1
    return calls


def test_a_flash_sale_of_expired_holds_is_released_in_batches_and_counted_once(env, monkeypatch):
    holds, per_order = 1200, 2
    event_id = env["make_event"]([{"name": "Flash", "price": 10.0, "capacity": 10 ** 6, "booked_count": holds * per_order}])
    prefix = make_expired_holds(env, event_id, "Flash", holds, per_order)
    mongo_calls = spy_on_event_updates(monkeypatch, event_id)

    first = sweep_expired_holds(batch_size=500, max_batches=1)
    assert first <= 500, "one call must never exceed one batch when max_batches=1"
    sweep_until_done(env, [prefix])
    monkeypatch.undo()

    assert count_orders(env, prefix, "expired") == holds, "every overdue hold is expired"
    assert env["booked"](event_id) == 0, "every seat is released exactly once (never negative, never left over)"
    assert env["active_tickets"](event_id) == 0
    assert 1 <= mongo_calls["n"] <= 6, (
        f"{holds * per_order} tickets must cost a handful of grouped MongoDB updates (about one per batch), not one per ticket: "
        f"saw {mongo_calls['n']}")


def test_holds_of_two_zones_are_released_with_one_update_per_zone(env, monkeypatch):
    event_id = env["make_event"]([
        {"name": "North", "price": 10.0, "capacity": 100, "booked_count": 6},
        {"name": "South", "price": 10.0, "capacity": 100, "booked_count": 4},
    ])
    north = make_expired_holds(env, event_id, "North", 3, 2)
    south = make_expired_holds(env, event_id, "South", 2, 2)
    mongo_calls = spy_on_event_updates(monkeypatch, event_id)

    sweep_until_done(env, [north, south])
    monkeypatch.undo()

    assert env["booked"](event_id, 0) == 0 and env["booked"](event_id, 1) == 0
    assert mongo_calls["n"] <= 4, f"10 tickets in 2 zones need about 2 updates (4 if a batch boundary splits them), saw {mongo_calls['n']}"


def test_an_order_being_paid_is_skipped_by_the_sweeper_not_waited_on(env):
    event_id = env["make_event"]([{"name": "Busy", "price": 10.0, "capacity": 100, "booked_count": 2}])
    prefix = make_expired_holds(env, event_id, "Busy", 1, 2)
    order_id = env["scalar"]("SELECT id FROM orders WHERE order_number LIKE :p || '%'", p=prefix)

    with row_locked("orders", order_id):
        started = time.perf_counter()
        sweep_expired_holds()
        elapsed = time.perf_counter() - started
        assert count_orders(env, prefix, "pending") == 1, "the locked order is left alone"
        assert elapsed < 3, "the sweeper must not wait for the lock"

    guard = 0
    while count_orders(env, prefix, "pending") and guard < 10:
        sweep_expired_holds()
        guard += 1
    assert count_orders(env, prefix, "expired") == 1, "picked up on the next pass"
    assert env["booked"](event_id) == 0


# ----------------------------------------------------------------------------------------------------------
# 5. The outbox keeps up and is trimmed
# ----------------------------------------------------------------------------------------------------------


def insert_outbox(prefix: str, count: int, status: str, age_days: int) -> None:
    with SessionLocal() as session:
        session.execute(text(
            "INSERT INTO outbox_events (event_type, aggregate_type, aggregate_id, payload, status, created_at, processed_at) "
            "SELECT 'RESILIENCE_TEST', 'order', :p || g, '{}', :s, now() - make_interval(days => :d), "
            "CASE WHEN :s = 'processed' THEN now() - make_interval(days => :d) END FROM generate_series(1, :n) g"),
            {"p": prefix, "s": status, "d": age_days, "n": count})
        session.commit()


def outbox_count(prefix: str, status: str) -> int:
    with SessionLocal() as session:
        return session.execute(text(
            "SELECT count(*) FROM outbox_events WHERE aggregate_id LIKE :p || '%' AND status = :s"), {"p": prefix, "s": status}).scalar()


@pytest.fixture
def outbox_prefix():
    prefix = f"rs-{uuid.uuid4().hex[:8]}-"
    yield prefix
    with SessionLocal() as session:
        session.execute(text("DELETE FROM outbox_events WHERE aggregate_id LIKE :p || '%'"), {"p": prefix})
        session.commit()


def drain_outbox() -> None:
    with SessionLocal() as session:
        while outbox_worker.process_pending_outbox_events(session, mongo_db, batch_size=1000, max_batches=20) >= 1000:
            pass


def test_the_outbox_is_drained_in_big_batches(env, outbox_prefix):
    insert_outbox(outbox_prefix, 2500, "pending", 0)
    with SessionLocal() as session:
        first = outbox_worker.process_pending_outbox_events(session, mongo_db, batch_size=1000, max_batches=1)
    assert first <= 1000, "one batch is capped"
    drain_outbox()
    assert outbox_count(outbox_prefix, "pending") == 0
    assert outbox_count(outbox_prefix, "processed") == 2500


def test_old_processed_outbox_rows_are_deleted_and_recent_ones_kept(env, outbox_prefix):
    insert_outbox(outbox_prefix + "old-", 30, "processed", 10)
    insert_outbox(outbox_prefix + "new-", 30, "processed", 1)
    insert_outbox(outbox_prefix + "pending-", 5, "pending", 10)
    with SessionLocal() as session:
        assert outbox_worker.purge_processed_outbox_events(session, retention_days=0) == 0, "0 days = keep everything"
    with SessionLocal() as session:
        while outbox_worker.purge_processed_outbox_events(session, retention_days=7, batch_size=5000) >= 5000:
            pass
    assert outbox_count(outbox_prefix + "old-", "processed") == 0
    assert outbox_count(outbox_prefix + "new-", "processed") == 30
    assert outbox_count(outbox_prefix + "pending-", "pending") == 5, "pending rows are never deleted, however old"


# ----------------------------------------------------------------------------------------------------------
# 6. The background worker
# ----------------------------------------------------------------------------------------------------------


def test_the_worker_never_blocks_the_event_loop(monkeypatch):
    monkeypatch.setattr(outbox_worker, "run_worker_cycle", lambda: time.sleep(0.8))  # a slow cycle

    async def scenario() -> int:
        stop, ticks = asyncio.Event(), 0
        worker = asyncio.create_task(outbox_worker.start_outbox_and_cleanup_worker(stop, poll_interval=1))
        deadline = asyncio.get_running_loop().time() + 0.6
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.02)
            ticks += 1
        stop.set()
        await worker
        return ticks

    ticks = asyncio.run(scenario())
    assert ticks >= 15, f"the loop only got {ticks} turns in 0.6 s: it was blocked by the worker"


def test_one_failing_worker_step_does_not_stop_the_others(env, outbox_prefix, monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("MongoDB is down")

    monkeypatch.setattr(outbox_worker, "cleanup_expired_seat_holds", broken)
    insert_outbox(outbox_prefix, 10, "pending", 0)
    outbox_worker.run_worker_cycle()  # must not raise
    assert outbox_count(outbox_prefix, "pending") == 0, "the outbox was still drained"


# ----------------------------------------------------------------------------------------------------------
# 7. Feature-flag cache: a slow refresh makes nobody else wait
# ----------------------------------------------------------------------------------------------------------


def test_a_slow_flag_refresh_does_not_make_other_requests_wait(monkeypatch):
    stale = {"user_name_write_mode": "dual", "user_name_read_source": "legacy"}
    monkeypatch.setattr(flags, "_cache", dict(stale))
    monkeypatch.setattr(flags, "_loaded_at", 0.0)  # expired
    started, release = threading.Event(), threading.Event()

    def slow_refresh():
        started.set()
        release.wait(5)
        flags._loaded_at = time.monotonic()

    monkeypatch.setattr(flags, "_refresh", slow_refresh)
    refresher = threading.Thread(target=flags.get_all_flags)
    refresher.start()
    assert started.wait(2), "the first caller starts the refresh"

    begun = time.perf_counter()
    values = flags.get_all_flags()  # a second request while the refresh is still running
    elapsed = time.perf_counter() - begun
    release.set()
    refresher.join(5)

    assert values == stale, "answered from the previous values"
    assert elapsed < 0.2, f"waited {elapsed:.2f} s behind the refresh"


# ----------------------------------------------------------------------------------------------------------
# 8. The gateway and the docs
# ----------------------------------------------------------------------------------------------------------


@pytest.mark.skipif(not LIVE_MODE, reason="needs the real nginx in front of the replicas (API_URL=http://nginx)")
def test_nginx_does_not_expose_the_internal_sweep_endpoint():
    assert client.post("/api/v1/orders/cleanup-expired").status_code == 404


def test_nginx_config_blocks_the_sweep_endpoint_and_tolerates_a_slow_answer():
    config = open(os.path.join(ROOT, "nginx", "nginx.conf"), encoding="utf-8").read()
    assert "location = /api/v1/orders/cleanup-expired" in config
    assert "max_fails=3" in config and "max_fails=1" not in config, "one slow answer must not mark a replica down"


def test_nginx_does_not_treat_a_deliberate_503_as_a_dead_replica():
    """The API answers 503 when a DATABASE is down. If nginx counted that as a replica failure it would take both
    healthy replicas out of rotation and answer 502 for everything, including endpoints that never use that database."""
    config = open(os.path.join(ROOT, "nginx", "nginx.conf"), encoding="utf-8").read()
    directive = next(line for line in config.splitlines() if line.strip().startswith("proxy_next_upstream ") )
    assert "http_503" not in directive and "http_502" not in directive, directive
    assert "error" in directive and "timeout" in directive, "connection errors and timeouts must still fail over"


def test_the_sweep_endpoint_is_not_in_the_public_swagger_page():
    from app.main import app

    assert "/api/v1/orders/cleanup-expired" not in app.openapi()["paths"]


@in_process_only
def test_the_sweep_endpoint_still_works_on_a_replica(env):
    response = client.post("/api/v1/orders/cleanup-expired")
    assert response.status_code == 200
    assert set(response.json()) == {"status", "message", "expired_orders_count"}
