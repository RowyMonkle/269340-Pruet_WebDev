"""scripts/index_report.py: the detector must flag exactly the indexes that are safe to call redundant, and nothing else.

Runs against a throwaway schema (created and dropped by the fixture), so it never depends on, or touches, the real tables.
Needs PostgreSQL (skipped when it is down; REQUIRE_DB=1 turns the skip into a failure).
"""
import importlib.util
import os
import sys
import uuid

import pytest
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.database import engine  # noqa: E402
from tests.support import build_environment  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("index_report", os.path.join(ROOT, "scripts", "index_report.py"))
assert _spec is not None and _spec.loader is not None  # spec_from_file_location may return None
index_report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(index_report)

DDL = """
CREATE TABLE {s}.parent (id int PRIMARY KEY);
CREATE TABLE {s}.child (id serial PRIMARY KEY, parent_id int REFERENCES {s}.parent (id), a int, b int, c text, status text);
CREATE TABLE {s}.child2 (id serial PRIMARY KEY, parent_id int REFERENCES {s}.parent (id), x int, y int);
CREATE TABLE {s}.child3 (id serial PRIMARY KEY, parent_id int REFERENCES {s}.parent (id), z int);

-- leading part of another index                      -> redundant (ix_a, covered by ix_a_b)
CREATE INDEX ix_a ON {s}.child (a);
CREATE INDEX ix_a_b ON {s}.child (a, b);
-- exact duplicate                                    -> exactly ONE of the two is reported (ix_dup2)
CREATE INDEX ix_dup1 ON {s}.child (c);
CREATE INDEX ix_dup2 ON {s}.child (c);
-- same column, different direction                   -> NOT redundant: it cannot serve ORDER BY b DESC the same way
CREATE INDEX ix_b ON {s}.child (b);
CREATE INDEX ix_b_desc_c ON {s}.child (b DESC, c);
-- partial index next to a full one                   -> NOT redundant: different predicate, different rows
CREATE INDEX ix_status ON {s}.child (status);
CREATE INDEX ix_status_partial ON {s}.child (status, a) WHERE a > 5;
-- expression index                                   -> never compared
CREATE INDEX ix_lower_c ON {s}.child (lower(c));
CREATE INDEX ix_lower_c_b ON {s}.child (lower(c), b);
-- a unique index is never redundant, but it MAY be the covering one
CREATE UNIQUE INDEX ux_x ON {s}.child2 (x);
CREATE INDEX ix_x_y ON {s}.child2 (x, y);
CREATE INDEX ix_y ON {s}.child2 (y);
CREATE UNIQUE INDEX ux_y_x ON {s}.child2 (y, x);
-- foreign keys: child has none indexed, child2 none, child3 is covered
CREATE INDEX ix_child3_parent ON {s}.child3 (parent_id, z);
"""


@pytest.fixture(scope="module")
def report():
    environment, teardown = build_environment(need_mongo=False)
    schema = f"idxrep_{uuid.uuid4().hex[:8]}"
    with engine.begin() as conn:
        conn.execute(text(f"CREATE SCHEMA {schema}"))
        conn.execute(text(DDL.format(s=schema)))
    try:
        with engine.connect() as conn:
            yield index_report.build_report(conn, schema, min_rows=0)
    finally:
        with engine.begin() as conn:
            conn.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        teardown()


def names(findings):
    return {f.index for f in findings}


def test_a_leading_part_of_another_index_is_redundant(report):
    finding = next(f for f in report.redundant if f.index == "ix_a")
    assert finding.covered_by == "ix_a_b"
    assert "leading part" in finding.detail


def test_of_two_identical_indexes_exactly_one_is_reported(report):
    assert names(report.redundant) & {"ix_dup1", "ix_dup2"} == {"ix_dup2"}
    assert next(f for f in report.redundant if f.index == "ix_dup2").covered_by == "ix_dup1"


def test_a_different_direction_is_not_redundant(report):
    assert "ix_b" not in names(report.redundant)
    assert "ix_b_desc_c" not in names(report.redundant)


def test_a_partial_index_is_not_compared_with_a_full_one(report):
    assert names(report.redundant) & {"ix_status", "ix_status_partial"} == set()


def test_expression_indexes_are_left_alone(report):
    assert names(report.redundant) & {"ix_lower_c", "ix_lower_c_b"} == set()


def test_unique_and_primary_key_indexes_are_never_reported(report):
    unique_or_pk = {i.idx for i in report.indexes if i.is_unique or i.is_primary}
    assert {"ux_x", "ux_y_x"} <= unique_or_pk
    assert not names(report.redundant) & unique_or_pk
    assert not names(report.unused) & unique_or_pk


def test_a_unique_index_can_be_the_covering_one(report):
    # ix_y (y) is the leading part of the UNIQUE index (y, x): the unique index answers y lookups too
    finding = next(f for f in report.redundant if f.index == "ix_y")
    assert finding.covered_by == "ux_y_x"
    # ix_x_y (x, y) is NOT a prefix of anything, and ux_x (x) is unique: neither may be reported
    assert not names(report.redundant) & {"ix_x_y", "ux_x"}


def test_exactly_the_expected_indexes_are_redundant(report):
    assert names(report.redundant) == {"ix_a", "ix_dup2", "ix_y"}


def test_foreign_keys_without_a_leading_index_are_found(report):
    unindexed = {(f.table, f.index) for f in report.unindexed}
    assert ("child", "child_parent_id_fkey") in unindexed
    assert ("child2", "child2_parent_id_fkey") in unindexed
    assert ("child3", "child3_parent_id_fkey") not in unindexed, "(parent_id, z) starts with the foreign key column"


def test_unused_needs_enough_rows(report):
    assert "ix_a" in names(report.unused), "fresh index, 0 scans, min_rows=0"
    with engine.connect() as conn:
        strict = index_report.build_report(conn, "public", min_rows=10 ** 12)
    assert strict.unused == [], "no real table has a trillion rows"


def test_report_fails_while_something_redundant_remains(report):
    assert report.ok is False
