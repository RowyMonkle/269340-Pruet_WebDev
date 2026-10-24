"""The index set of the real schema (migration 0008): nothing redundant, every foreign key indexed, and the queries the
application really runs still use an index.

The plan checks use `SET LOCAL enable_seqscan = off`, so they say "an index CAN answer this", not "the planner picks it
on a table of 12 rows". The dataset-sized proof (130,000 orders, before/after timings) is in docs/perf/index_review.md.
Needs PostgreSQL migrated to 0008 (skipped when it is down; REQUIRE_DB=1 turns the skip into a failure).
"""
import importlib.util
import inspect
import json
import os
import sys

import pytest
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.database import engine  # noqa: E402
from app.services import order_service  # noqa: E402
from tests.support import build_environment  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("index_report", os.path.join(ROOT, "scripts", "index_report.py"))
assert _spec is not None and _spec.loader is not None  # spec_from_file_location may return None
index_report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(index_report)

DROPPED_BY_0008 = [
    "idx_orders_user_id", "idx_orders_status", "idx_payments_order_id", "idx_tickets_event_id",  # redundant
    "idx_orders_created_at", "idx_orders_expires_at", "idx_payments_status",                      # nobody reads them
    "idx_outbox_event_type", "idx_outbox_aggregate_id",
]
KEPT = [
    "idx_orders_user_created", "idx_orders_user_status", "idx_orders_status_expires",
    "idx_payments_order_status", "uq_payments_order_completed",
    "idx_tickets_order_id", "idx_tickets_event_zone", "uq_tickets_event_zone_seat",
    "idx_outbox_status_created", "idx_users_last_name",
]


@pytest.fixture(scope="module", autouse=True)
def database():
    _, teardown = build_environment(need_mongo=False)
    yield
    teardown()


def present_indexes() -> set:
    with engine.connect() as conn:
        return {r[0] for r in conn.execute(text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"))}


def index_names_used_by(sql: str) -> set:
    """Names of every index the planner would use for `sql` when a sequential scan is the last resort."""
    with engine.connect() as conn:
        conn.execute(text("SET LOCAL enable_seqscan = off"))
        conn.execute(text("SET LOCAL enable_bitmapscan = off"))
        plan = conn.execute(text("EXPLAIN (FORMAT JSON) " + sql)).scalar()
        conn.rollback()
    plan = plan if isinstance(plan, list) else json.loads(plan)
    found = set()

    def walk(node):
        if "Index Name" in node:
            found.add(node["Index Name"])
        for child in node.get("Plans", []):
            walk(child)

    walk(plan[0]["Plan"])
    return found


def test_no_redundant_index_and_every_foreign_key_is_indexed():
    with engine.connect() as conn:
        report = index_report.build_report(conn, "public")
    assert report.redundant == [], [f"{f.table}.{f.index}: {f.detail}" for f in report.redundant]
    assert report.unindexed == [], [f"{f.table}.{f.index}: {f.detail}" for f in report.unindexed]
    assert all(i.is_valid for i in report.indexes), "an INVALID index is a failed CREATE INDEX CONCURRENTLY"


def test_the_nine_dropped_indexes_are_gone():
    assert not set(DROPPED_BY_0008) & present_indexes()


def test_the_indexes_that_carry_the_application_are_kept():
    assert not set(KEPT) - present_indexes()


@pytest.mark.parametrize("label, sql, expected", [
    ("my orders page", "SELECT id FROM orders WHERE user_id = 17 ORDER BY created_at DESC, id DESC LIMIT 20", {"idx_orders_user_created"}),
    ("orders of a user, no ordering", "SELECT count(*) FROM orders WHERE user_id = 17", {"idx_orders_user_status", "idx_orders_user_created"}),
    ("tickets of an order", "SELECT * FROM tickets WHERE order_id = 5", {"idx_tickets_order_id"}),
    ("tickets of an event", "SELECT count(*) FROM tickets WHERE event_id = 'e'", {"idx_tickets_event_zone", "uq_tickets_event_zone_seat"}),
    ("payments of an order", "SELECT * FROM payments WHERE order_id = 5 ORDER BY created_at DESC", {"idx_payments_order_status", "uq_payments_order_completed"}),
    ("paid amount of an order", "SELECT sum(amount) FROM payments WHERE order_id = 5 AND status = 'completed'", {"uq_payments_order_completed", "idx_payments_order_status"}),
    ("the sweeper", "SELECT id FROM orders WHERE status = 'pending' AND expires_at < now() ORDER BY expires_at, id LIMIT 500", {"idx_orders_status_expires"}),
    ("the outbox worker", "SELECT id FROM outbox_events WHERE status = 'pending' ORDER BY created_at, id LIMIT 50", {"idx_outbox_status_created"}),
    ("an order by idempotency key", "SELECT id FROM orders WHERE idempotency_key = 'k'", {"orders_idempotency_key_key"}),
    ("a user by e-mail", "SELECT id FROM users WHERE email = 'a@b.c'", {"users_email_key"}),
])
def test_the_queries_the_application_runs_can_use_an_index(label, sql, expected):
    used = index_names_used_by(sql)
    assert used & expected, f"{label}: planner used {used or 'no index'}, expected one of {expected}"


def test_the_sweeper_asks_for_the_order_its_index_can_deliver():
    """ORDER BY id made the planner walk the whole primary key (23 ms at 130k orders). The index only helps when the
    query sorts by the index's own order, so the code and the index must change together."""
    source = inspect.getsource(order_service._sweep_one_batch)
    assert ".order_by(Order.expires_at, Order.id)" in source
    assert ".order_by(Order.id)" not in source
