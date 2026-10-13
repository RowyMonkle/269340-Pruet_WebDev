"""CP3 helper: read PostgreSQL's pg_stat_statements = what the API really ran and how fast.

Use it around a load test to get database-side evidence for the audit report:
  1. python scripts/pg_stats.py --reset                 start a clean measurement window
  2. ... run the load test / hit the endpoint under audit ...
  3. python scripts/pg_stats.py --top 15                heaviest statements (by total time)
     python scripts/pg_stats.py --like "FROM orders" --requests 2000
                                                        statements touching orders, with
                                                        "calls per request" (calls / 2000)

A list endpoint with no N+1 shows ~1 call per request for its SELECT, however many rows it returns.
Add --csv FILE to keep the table for the report.

Needs the library preloaded (docker-compose.yml does it) and the extension created (migration 0006).
"""
import argparse
import csv
import os
import sys

from sqlalchemy import create_engine, text

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.core.config import settings  # noqa: E402

ORDER_BY = {"total": "total_exec_time", "mean": "mean_exec_time", "calls": "calls", "max": "max_exec_time"}

SQL = """
SELECT calls, round(total_exec_time::numeric, 1) AS total_ms, round(mean_exec_time::numeric, 2) AS mean_ms,
       round(max_exec_time::numeric, 1) AS max_ms, rows,
       left(regexp_replace(query, '\\s+', ' ', 'g'), :width) AS query
FROM pg_stat_statements
WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
  AND query NOT ILIKE '%pg_stat_statements%'
  AND query NOT ILIKE 'SET %' AND query NOT ILIKE 'BEGIN%' AND query NOT ILIKE 'COMMIT%' AND query NOT ILIKE 'ROLLBACK%'
  AND (:like = '' OR query ILIKE :like_pattern)
ORDER BY {order} DESC
LIMIT :n
"""

HINT = ("pg_stat_statements is not available. Recreate the database container so the library is preloaded "
        "(`docker compose up -d postgres_db`) and apply migration 0006 (`docker compose run --rm migrate`).")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reset", action="store_true", help="clear the counters and exit")
    parser.add_argument("--top", type=int, default=15, help="how many statements to show (default 15)")
    parser.add_argument("--by", choices=sorted(ORDER_BY), default="total", help="sort column (default total time)")
    parser.add_argument("--like", default="", help="only statements containing this text, e.g. 'FROM orders'")
    parser.add_argument("--requests", type=int, default=0, help="HTTP requests in the window: adds calls per request")
    parser.add_argument("--width", type=int, default=110, help="characters of the query to print")
    parser.add_argument("--csv", metavar="FILE", help="also write the table to a CSV file")
    args = parser.parse_args(argv)

    engine = create_engine(settings.sync_database_url)
    try:
        with engine.begin() as conn:
            if args.reset:
                conn.execute(text("SELECT pg_stat_statements_reset()"))
                print("[pg_stats] counters reset, run the load now")
                return 0
            rows = conn.execute(
                text(SQL.format(order=ORDER_BY[args.by])),
                {"n": args.top, "like": args.like, "like_pattern": f"%{args.like}%", "width": args.width},
            ).all()
    except Exception as exc:  # noqa: BLE001
        print(f"[pg_stats] {HINT}\n           ({type(exc).__name__}: {str(exc).splitlines()[0]})")
        return 2
    finally:
        engine.dispose()

    header = ["calls", "total_ms", "mean_ms", "max_ms", "rows"] + (["calls/request"] if args.requests else []) + ["query"]
    table = [
        [r.calls, r.total_ms, r.mean_ms, r.max_ms, r.rows]
        + ([round(r.calls / args.requests, 2)] if args.requests else [])
        + [r.query]
        for r in rows
    ]
    print("  ".join(f"{h:>10}" for h in header[:-1]) + "  query")
    for row in table:
        print("  ".join(f"{v:>10}" for v in row[:-1]) + "  " + row[-1])
    if not table:
        print("(no statements recorded: did you run --reset and then some traffic?)")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(header)
            writer.writerows(table)
        print(f"[pg_stats] wrote {len(table)} row(s) to {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
