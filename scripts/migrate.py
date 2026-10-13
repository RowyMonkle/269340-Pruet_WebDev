"""Bring the PostgreSQL schema to the safe Alembic target (see MIGRATE_TARGET below).

Handles the three states a database can be in:
  1. Empty volume                      -> `alembic upgrade <target>` builds everything.
  2. Built by init-db/init.sql, never  -> init.sql matches revision 0002, so stamp
     stamped (no alembic_version)         0002 first, then upgrade the rest.
  3. Already tracked by Alembic        -> plain `alembic upgrade <target>`.

It stops at MIGRATE_TARGET (default: 0006_orders_list_index, the last non-destructive
revision), NOT at `head` (there are two heads on purpose), so `docker compose up` can never
run the CP2 Phase 5 contract revision.
Apply that one on purpose:  alembic upgrade 0005_contract_user_full_name

Usage: python scripts/migrate.py
"""
import os
import sys

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.core.config import settings  # noqa: E402

INIT_SQL_REVISION = "0002_add_payments_holds_outbox"
# 0005 drops users.full_name, so it is never applied automatically. 0006 (CP3 index) hangs off
# 0004 as a second head, so upgrading to it applies everything except the contract.
DEFAULT_TARGET = "0006_orders_list_index"


def main() -> None:
    cfg = Config(os.path.join(ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(ROOT, "alembic"))

    engine = create_engine(settings.sync_database_url)
    tables = set(inspect(engine).get_table_names())
    engine.dispose()

    if "alembic_version" not in tables and "users" in tables:
        print(f"[migrate] schema from init.sql detected, stamping {INIT_SQL_REVISION}")
        command.stamp(cfg, INIT_SQL_REVISION)

    target = os.environ.get("MIGRATE_TARGET", DEFAULT_TARGET)
    print(f"[migrate] upgrading to {target}")
    command.upgrade(cfg, target)


if __name__ == "__main__":
    main()
