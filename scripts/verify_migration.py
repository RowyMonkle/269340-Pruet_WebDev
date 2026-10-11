"""Run the CP2 data-consistency checks for the users name migration.

Reads scripts/sql/verify_users_migration.sql, runs the checks that belong to
--stage and prints PASS / FAIL for each. Exit code is 1 if any check fails.

  before-backfill  dual-write is on, backfill has not run (or is running)
  after-backfill   every user must have first_name + last_name and agree with full_name
  after-contract   full_name column is gone, new columns and index remain

Snapshot (proves no user was lost or renamed while migrating, ignores new signups):
  python scripts/verify_migration.py --stage before-backfill --save-snapshot snap.json
  python scripts/verify_migration.py --stage after-backfill  --compare-snapshot snap.json

Usage (inside the api container):
  python scripts/verify_migration.py --stage after-backfill
"""
import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from typing import List, Optional

from sqlalchemy import create_engine, text

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.core.config import settings  # noqa: E402

SQL_FILE = os.path.join(ROOT, "scripts", "sql", "verify_users_migration.sql")
STAGES = ("before-backfill", "after-backfill", "after-contract")


@dataclass
class Check:
    name: str
    stages: List[str]
    expect: str
    what: str
    sql: str


def parse_checks(path: str) -> List[Check]:
    """Read blocks of `-- @key value` marker lines followed by one SQL statement.

    A block ends at the first SQL line that finishes with `;`. Any other comment or
    blank line is ignored, so the file can carry free-form documentation.
    """
    checks: List[Check] = []
    header: dict = {}
    body: List[str] = []

    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.rstrip()
            marker = re.match(r"--\s*@(name|stages|expect|what)\s+(.*)$", line)
            if marker:
                key, value = marker.groups()
                if key == "name":
                    header, body = {}, []
                header[key] = value.strip()
                continue
            if not header.get("name") or not line.strip() or line.lstrip().startswith("--"):
                continue
            body.append(line)
            if line.endswith(";"):
                checks.append(
                    Check(
                        name=header["name"],
                        stages=header.get("stages", "-").split(),
                        expect=header.get("expect", "info"),
                        what=header.get("what", ""),
                        sql="\n".join(body).rstrip(";"),
                    )
                )
                header, body = {}, []
    return checks


def by_name(checks: List[Check], name: str) -> Check:
    return next(check for check in checks if check.name == name)


def scalar(conn, check: Check, **params):
    return conn.execute(text(check.sql), params).scalar_one()


def take_snapshot(conn, checks: List[Check]) -> dict:
    max_id = scalar(conn, by_name(checks, "snapshot_max_id"))
    return {
        "max_id": max_id,
        "count": scalar(conn, by_name(checks, "snapshot_count"), max_id=max_id),
        "checksum": scalar(conn, by_name(checks, "snapshot_checksum"), max_id=max_id),
    }


def compare_snapshot(conn, checks: List[Check], saved: dict) -> bool:
    now = take_snapshot_up_to(conn, checks, saved["max_id"])
    ok = now["count"] == saved["count"] and now["checksum"] == saved["checksum"]
    detail = (
        f"saved {saved['count']} users (id <= {saved['max_id']}), "
        f"now {now['count']}, checksum {'match' if now['checksum'] == saved['checksum'] else 'DIFFERENT'}"
    )
    print(f"  {'PASS' if ok else 'FAIL'}  no_user_lost_or_renamed  {detail}")
    return ok


def take_snapshot_up_to(conn, checks: List[Check], max_id: int) -> dict:
    return {
        "count": scalar(conn, by_name(checks, "snapshot_count"), max_id=max_id),
        "checksum": scalar(conn, by_name(checks, "snapshot_checksum"), max_id=max_id),
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stage", choices=STAGES, required=True)
    parser.add_argument("--save-snapshot", metavar="FILE", help="write a snapshot of existing users")
    parser.add_argument("--compare-snapshot", metavar="FILE", help="compare against a saved snapshot")
    args = parser.parse_args(argv)

    checks = parse_checks(SQL_FILE)
    engine = create_engine(settings.sync_database_url)
    failures = 0

    print(f"[verify] stage={args.stage}")
    with engine.connect() as conn:
        for check in checks:
            if args.stage not in check.stages:
                continue
            value = scalar(conn, check)
            if check.expect == "info":
                print(f"  INFO  {check.name:<26} {value}")
                continue
            ok = value == int(check.expect)
            failures += 0 if ok else 1
            note = "" if ok else f"  <- expected {check.expect}: {check.what}"
            print(f"  {'PASS' if ok else 'FAIL'}  {check.name:<26} {value}{note}")

        if args.save_snapshot:
            snapshot = take_snapshot(conn, checks)
            with open(args.save_snapshot, "w", encoding="utf-8") as handle:
                json.dump(snapshot, handle)
            print(f"  SAVED snapshot: {snapshot['count']} users up to id {snapshot['max_id']} -> {args.save_snapshot}")

        if args.compare_snapshot:
            with open(args.compare_snapshot, encoding="utf-8") as handle:
                saved = json.load(handle)
            if not compare_snapshot(conn, checks, saved):
                failures += 1

    engine.dispose()
    print(f"[verify] {'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
