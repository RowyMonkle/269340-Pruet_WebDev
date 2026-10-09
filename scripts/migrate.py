"""Bring the PostgreSQL schema to Alembic head before the API starts.

Handles the three states a database can be in:
  1. Empty volume                      -> `alembic upgrade head` builds everything.
  2. Built by init-db/init.sql, never  -> init.sql matches revision 0002, so stamp
     stamped (no alembic_version)         0002 first, then upgrade the rest.
  3. Already tracked by Alembic        -> plain `alembic upgrade head`.

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


def main() -> None:
    cfg = Config(os.path.join(ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(ROOT, "alembic"))

    engine = create_engine(settings.sync_database_url)
    tables = set(inspect(engine).get_table_names())
    engine.dispose()

    if "alembic_version" not in tables and "users" in tables:
        print(f"[migrate] schema from init.sql detected, stamping {INIT_SQL_REVISION}")
        command.stamp(cfg, INIT_SQL_REVISION)

    print("[migrate] upgrading to head")
    command.upgrade(cfg, "head")


if __name__ == "__main__":
    main()
