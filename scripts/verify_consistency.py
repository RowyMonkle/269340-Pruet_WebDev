"""CP3 consistency audit: proves no oversell, no lost seat, no double charge.

Run it after a concurrency test or a load test. It reads PostgreSQL (orders, tickets,
payments = the source of truth) and MongoDB (seat inventory: zones[].booked_count) and
checks the invariants the booking flow must keep under any number of parallel writers:

  PostgreSQL only
    duplicate_active_seats          a seat is held/valid twice
    double_charged_orders           an order has more than one completed payment
    confirmed_without_payment       a confirmed order nobody paid for
    payment_amount_mismatch         completed payments do not add up to the order total
    order_total_mismatch            order total differs from the sum of its tickets
    dead_order_with_active_tickets  an expired/cancelled order still holds seats
    ticket_state_mismatch           pending order with valid tickets / confirmed with held
  PostgreSQL vs MongoDB (per event zone)
    negative_booked_count           booked_count < 0
    mongo_over_capacity             booked_count > capacity
    pg_over_capacity                more active tickets than the zone's capacity (real oversell)
    drift_mongo_below_pg            Mongo counts fewer seats than there are tickets (oversell risk)
    drift_mongo_above_pg            Mongo counts seats that have no ticket (lost sales)
    tickets_for_unknown_zone        active tickets whose event/zone is missing in Mongo

A request in flight makes Mongo and PostgreSQL differ for a few milliseconds (Mongo is
updated first), so the drift checks are repeated --retries times, --wait seconds apart,
before they are reported. Best practice: stop the load first.

Usage (inside the api container):
  python scripts/verify_consistency.py                 # report; exit code 1 if any check fails
  python scripts/verify_consistency.py --repair        # reset Mongo booked_count to the PostgreSQL count
"""
import argparse
import os
import sys
import time
from dataclasses import dataclass, field
from typing import List, Tuple

from sqlalchemy import text

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SAMPLE_LIMIT = 5

# (name, what a row means, SQL returning one text column per offending row)
PG_CHECKS = [
    (
        "duplicate_active_seats",
        "seat is held/valid more than once",
        """SELECT event_id || ' / ' || seat_zone || ' / ' || seat_number FROM tickets
           WHERE seat_number IS NOT NULL AND status IN ('held', 'valid')
           GROUP BY event_id, seat_zone, seat_number HAVING count(*) > 1""",
    ),
    (
        "double_charged_orders",
        "order with more than one completed payment (double charge)",
        """SELECT 'order ' || order_id FROM payments WHERE status = 'completed'
           GROUP BY order_id HAVING count(*) > 1""",
    ),
    (
        "confirmed_without_payment",
        "confirmed order without a completed payment",
        """SELECT 'order ' || o.id FROM orders o WHERE o.status = 'confirmed'
           AND NOT EXISTS (SELECT 1 FROM payments p WHERE p.order_id = o.id AND p.status = 'completed')""",
    ),
    (
        "payment_amount_mismatch",
        "completed payments do not add up to the order total",
        """SELECT 'order ' || o.id FROM orders o
           JOIN (SELECT order_id, sum(amount) s FROM payments WHERE status = 'completed' GROUP BY order_id) p
             ON p.order_id = o.id WHERE o.total_amount <> p.s""",
    ),
    (
        "order_total_mismatch",
        "order total differs from the sum of its ticket prices",
        """SELECT 'order ' || o.id FROM orders o
           LEFT JOIN (SELECT order_id, sum(price) s FROM tickets GROUP BY order_id) t ON t.order_id = o.id
           WHERE o.total_amount <> coalesce(t.s, 0)""",
    ),
    (
        "dead_order_with_active_tickets",
        "expired/cancelled order still holds seats",
        """SELECT DISTINCT 'order ' || o.id FROM orders o JOIN tickets t ON t.order_id = o.id
           WHERE o.status IN ('expired', 'cancelled') AND t.status IN ('held', 'valid')""",
    ),
    (
        "ticket_state_mismatch",
        "ticket status contradicts its order (pending needs held, confirmed needs valid)",
        """SELECT DISTINCT 'order ' || o.id FROM orders o JOIN tickets t ON t.order_id = o.id
           WHERE (o.status = 'pending' AND t.status = 'valid') OR (o.status = 'confirmed' AND t.status = 'held')""",
    ),
]

ACTIVE_TICKETS_SQL = """SELECT event_id, seat_zone, count(*) AS n FROM tickets
                        WHERE status IN ('held', 'valid') GROUP BY event_id, seat_zone"""


@dataclass
class Result:
    name: str
    what: str
    value: int
    samples: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.value == 0


def postgres_checks(conn) -> List[Result]:
    results = []
    for name, what, sql in PG_CHECKS:
        rows = [r[0] for r in conn.execute(text(sql)).all()]
        results.append(Result(name, what, len(rows), rows[:SAMPLE_LIMIT]))
    return results


def inventory_checks(conn, mongo_db) -> Tuple[List[Result], List[tuple]]:
    """Compare Mongo seat counters with PostgreSQL tickets.

    Returns the results and the drifted zones as (event ObjectId, zone name, postgres count).
    """
    active = {(r.event_id, r.seat_zone): r.n for r in conn.execute(text(ACTIVE_TICKETS_SQL)).all()}
    zones = {}
    for doc in mongo_db["events"].find({}, {"zones.name": 1, "zones.capacity": 1, "zones.booked_count": 1}):
        for zone in doc.get("zones", []):
            zones[(str(doc["_id"]), zone["name"])] = (
                int(zone.get("capacity", 0)), int(zone.get("booked_count", 0)), doc["_id"],
            )

    def line(key):
        cap, booked, _ = zones[key]
        return f"{key[0]} / {key[1]}: mongo={booked} postgres={active.get(key, 0)} capacity={cap}"

    negative = [k for k, (_, booked, _) in zones.items() if booked < 0]
    over_cap = [k for k, (cap, booked, _) in zones.items() if booked > cap]
    pg_over = [k for k, n in active.items() if k in zones and n > zones[k][0]]
    below = [k for k, (_, booked, _) in zones.items() if booked < active.get(k, 0)]
    above = [k for k, (_, booked, _) in zones.items() if booked > active.get(k, 0)]
    orphans = [k for k in active if k not in zones]

    def result(name, what, keys, fmt=line):
        return Result(name, what, len(keys), [fmt(k) for k in keys[:SAMPLE_LIMIT]])

    results = [
        result("negative_booked_count", "booked_count below zero", negative),
        result("mongo_over_capacity", "booked_count above the zone capacity", over_cap),
        result("pg_over_capacity", "more active tickets than capacity (oversell)", pg_over),
        result("drift_mongo_below_pg", "Mongo counts fewer seats than tickets exist (oversell risk)", below),
        result("drift_mongo_above_pg", "Mongo counts seats that have no ticket (lost sales)", above),
        result("tickets_for_unknown_zone", "active tickets whose event/zone is not in Mongo", orphans,
               fmt=lambda k: f"{k[0]} / {k[1]}: postgres={active[k]}"),
    ]
    drift = [(zones[k][2], k[1], active.get(k, 0)) for k in dict.fromkeys(below + above)]
    return results, drift


def run_audit(engine, mongo_db, wait: float = 0.0, retries: int = 0) -> Tuple[List[Result], List[tuple]]:
    """Run every check. Drift is re-read up to `retries` times so in-flight requests settle."""
    attempt = 0
    while True:
        with engine.connect() as conn:
            results = postgres_checks(conn)
            inventory, drift = inventory_checks(conn, mongo_db)
        results += inventory
        drifting = any(r.name.startswith("drift_") and not r.ok for r in results)
        if not drifting or attempt >= retries:
            return results, drift
        attempt += 1
        time.sleep(wait)


def repair(mongo_db, drift: List[tuple]) -> int:
    """Make the Mongo counter equal the PostgreSQL ticket count (PostgreSQL is the truth)."""
    for oid, zone, expected in drift:
        mongo_db["events"].update_one({"_id": oid, "zones.name": zone}, {"$set": {"zones.$.booked_count": expected}})
        print(f"  REPAIR event {oid} zone {zone!r}: booked_count set to {expected}")
    return len(drift)


def print_results(results: List[Result]) -> int:
    failed = 0
    for r in results:
        print(f"  {'PASS' if r.ok else 'FAIL'}  {r.name:<32} {r.value}" + ("" if r.ok else f"  <- {r.what}"))
        for sample in r.samples if not r.ok else []:
            print(f"          e.g. {sample}")
        failed += 0 if r.ok else 1
    return failed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--wait", type=float, default=2.0, help="seconds between drift re-checks (default 2)")
    parser.add_argument("--retries", type=int, default=2, help="drift re-checks before reporting (default 2)")
    parser.add_argument("--repair", action="store_true",
                        help="set Mongo booked_count to the PostgreSQL ticket count for drifted zones (stop traffic first)")
    args = parser.parse_args(argv)

    from app.core.database import engine, mongo_db, relax_timeouts_for_scripts  # imported late so --help works without a database

    relax_timeouts_for_scripts()  # an audit over millions of rows must not be cancelled by the API's 15 s statement limit

    print("[consistency] PostgreSQL vs MongoDB audit")
    results, drift = run_audit(engine, mongo_db, args.wait, args.retries)
    failed = print_results(results)

    if failed and args.repair and drift:
        print(f"[consistency] repairing {len(drift)} drifted zone(s)")
        repair(mongo_db, drift)
        results, drift = run_audit(engine, mongo_db, args.wait, args.retries)
        print("[consistency] audit after repair")
        failed = print_results(results)

    print(f"[consistency] {'ALL CHECKS PASSED' if failed == 0 else f'{failed} CHECK(S) FAILED'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
