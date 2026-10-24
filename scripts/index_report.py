"""Index review for the PostgreSQL schema: which indexes are redundant, unused, or missing.

Every index speeds up some reads and slows down EVERY insert/update/delete of its table (one more B-tree to
maintain, more WAL, more disk, more cache pressure). So an index must earn its place. This report answers three
questions with facts from the catalog, never with guesses:

  redundant   A non-unique index whose columns are the leading columns of another index (same order, same
              predicate). The bigger index answers the same lookups, so the smaller one is pure write cost.
              This is STRUCTURAL: it holds whatever the workload is, so these are safe to drop.
              Example: INDEX (user_id) next to INDEX (user_id, created_at DESC).
  unused      idx_scan = 0 since the statistics were last reset, on a table that has real data. This is
              EVIDENCE, not proof: the workload may simply not have run the query yet, a nightly job may
              need it, a small table is read by seq scan anyway. Reported for a human to judge, never fatal.
  unindexed   A foreign key whose columns are not the leading columns of any index. Deleting or updating a
              parent row then scans the whole child table. This is the check that stops us from dropping
              an index that a foreign key is silently relying on.

Never reported as redundant: primary keys, unique indexes, indexes that back a constraint, partial indexes
(a different predicate answers different questions), expression indexes, invalid indexes.

Usage:
  python scripts/index_report.py                    # human readable, exit 1 when something redundant/unindexed remains
  python scripts/index_report.py --json             # machine readable
  python scripts/index_report.py --min-rows 50000   # "unused" only counts on tables with at least this many live rows
  python scripts/index_report.py --schema public
"""
import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Tuple

from sqlalchemy import create_engine, text

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

INDEX_SQL = """
SELECT t.relname                                           AS tbl,
       i.relname                                           AS idx,
       am.amname                                           AS method,
       ix.indisunique                                      AS is_unique,
       ix.indisprimary                                     AS is_primary,
       ix.indisvalid                                       AS is_valid,
       (ix.indexprs IS NOT NULL)                           AS has_expression,
       EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conindid = ix.indexrelid) AS backs_constraint,
       pg_get_expr(ix.indpred, ix.indrelid)                AS predicate,
       ARRAY(SELECT pg_get_indexdef(ix.indexrelid, k, true) FROM generate_series(1, ix.indnkeyatts) k ORDER BY k) AS keys,
       ARRAY(SELECT ix.indoption[k - 1]::int FROM generate_series(1, ix.indnkeyatts) k ORDER BY k)               AS options,
       string_to_array(ix.indclass::text, ' ')             AS opclasses,
       string_to_array(ix.indcollation::text, ' ')         AS collations,
       pg_relation_size(ix.indexrelid)                     AS size_bytes,
       COALESCE(s.idx_scan, 0)                             AS scans,
       -- n_live_tup is zeroed by pg_stat_reset(); reltuples (kept by VACUUM/ANALYZE) is not
       GREATEST(COALESCE(ts.n_live_tup, 0), GREATEST(t.reltuples, 0))::bigint AS table_rows
FROM pg_index ix
JOIN pg_class i  ON i.oid = ix.indexrelid
JOIN pg_class t  ON t.oid = ix.indrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
JOIN pg_am am    ON am.oid = i.relam
LEFT JOIN pg_stat_user_indexes s ON s.indexrelid = ix.indexrelid
LEFT JOIN pg_stat_user_tables ts ON ts.relid = t.oid
WHERE n.nspname = :schema AND t.relkind IN ('r', 'p') AND t.relname <> 'alembic_version'
ORDER BY t.relname, i.relname
"""

FOREIGN_KEY_SQL = """
SELECT t.relname AS tbl, c.conname AS fk,
       ARRAY(SELECT a.attname FROM unnest(c.conkey) WITH ORDINALITY k(attnum, ord)
             JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum ORDER BY k.ord) AS columns
FROM pg_constraint c
JOIN pg_class t ON t.oid = c.conrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
WHERE c.contype = 'f' AND n.nspname = :schema
ORDER BY t.relname, c.conname
"""

STATS_RESET_SQL = "SELECT stats_reset FROM pg_stat_database WHERE datname = current_database()"


@dataclass
class IndexInfo:
    tbl: str
    idx: str
    method: str
    is_unique: bool
    is_primary: bool
    is_valid: bool
    has_expression: bool
    backs_constraint: bool
    predicate: Optional[str]
    keys: List[str]
    options: List[int]
    opclasses: List[str]
    collations: List[str]
    size_bytes: int
    scans: int
    table_rows: int

    @property
    def droppable_kind(self) -> bool:
        """Only a plain, non-unique, non-constraint, valid, expression-free btree index may ever be called redundant."""
        return (self.method == "btree" and self.is_valid and not self.is_unique and not self.is_primary
                and not self.backs_constraint and not self.has_expression)

    def columns_text(self) -> str:
        return ", ".join(f"{k} DESC" if o & 1 else k for k, o in zip(self.keys, self.options))

    def signature(self, length: Optional[int] = None) -> Tuple:
        n = len(self.keys) if length is None else length
        return (tuple(self.keys[:n]), tuple(self.options[:n]), tuple(self.opclasses[:n]), tuple(self.collations[:n]), self.predicate)


@dataclass
class Finding:
    kind: str          # redundant | unused | unindexed
    table: str
    index: str         # index name, or foreign key name for "unindexed"
    detail: str
    covered_by: Optional[str] = None
    size_bytes: int = 0
    scans: int = 0


@dataclass
class Report:
    schema: str
    stats_reset: Optional[str]
    indexes: List[IndexInfo] = field(default_factory=list)
    redundant: List[Finding] = field(default_factory=list)
    unused: List[Finding] = field(default_factory=list)
    unindexed: List[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.redundant and not self.unindexed


def fetch_indexes(conn, schema: str) -> List[IndexInfo]:
    return [IndexInfo(**dict(row._mapping)) for row in conn.execute(text(INDEX_SQL), {"schema": schema})]


def find_redundant(indexes: List[IndexInfo]) -> List[Finding]:
    """A is redundant when another valid btree index B on the same table starts with exactly A's columns."""
    by_table: Dict[str, List[IndexInfo]] = {}
    for index in indexes:
        by_table.setdefault(index.tbl, []).append(index)

    findings = []
    for table, group in by_table.items():
        candidates: Dict[str, List[IndexInfo]] = {}
        for a in group:
            if not a.droppable_kind:
                continue
            for b in group:
                if a is b or b.method != "btree" or not b.is_valid or b.has_expression or len(b.keys) < len(a.keys):
                    continue
                if a.signature() != b.signature(len(a.keys)):
                    continue
                if len(b.keys) == len(a.keys) and b.droppable_kind and a.idx < b.idx:
                    continue  # exact duplicates: keep the alphabetically first name, report the other
                candidates.setdefault(a.idx, []).append(b)

        redundant_names = set(candidates)
        for a in group:
            if a.idx not in candidates:
                continue
            keepers = [b for b in candidates[a.idx] if b.idx not in redundant_names]
            keeper = max(keepers or candidates[a.idx], key=lambda b: (len(b.keys), b.is_unique, b.idx))
            exact = len(keeper.keys) == len(a.keys)
            detail = (f"duplicate of {keeper.idx}" if exact else
                      f"({a.columns_text()}) is the leading part of {keeper.idx} ({keeper.columns_text()})")
            findings.append(Finding("redundant", table, a.idx, detail, keeper.idx, a.size_bytes, a.scans))
    return sorted(findings, key=lambda f: (f.table, f.index))


def find_unused(indexes: List[IndexInfo], min_rows: int) -> List[Finding]:
    return sorted(
        (Finding("unused", i.tbl, i.idx, f"0 scans on a table with {i.table_rows} live rows", None, i.size_bytes, 0)
         for i in indexes
         if i.scans == 0 and i.droppable_kind and i.table_rows >= min_rows),
        key=lambda f: (f.table, f.index),
    )


def find_unindexed_foreign_keys(conn, schema: str, indexes: List[IndexInfo]) -> List[Finding]:
    findings = []
    for row in conn.execute(text(FOREIGN_KEY_SQL), {"schema": schema}):
        columns = list(row.columns)
        served = any(
            i.tbl == row.tbl and i.is_valid and not i.has_expression and i.predicate is None
            and i.keys[: len(columns)] == columns
            for i in indexes
        )
        if not served:
            findings.append(Finding("unindexed", row.tbl, row.fk,
                                    f"foreign key ({', '.join(columns)}) has no index starting with those columns"))
    return findings


def build_report(conn, schema: str = "public", min_rows: int = 10000) -> Report:
    indexes = fetch_indexes(conn, schema)
    reset = conn.execute(text(STATS_RESET_SQL)).scalar()
    return Report(
        schema=schema,
        stats_reset=str(reset) if reset else None,
        indexes=indexes,
        redundant=find_redundant(indexes),
        unused=find_unused(indexes, min_rows),
        unindexed=find_unindexed_foreign_keys(conn, schema, indexes),
    )


def pretty_size(n: int) -> str:
    value = float(n)
    for unit in ("B", "kB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit in ("B", "kB") else f"{value:.1f} {unit}"
        value /= 1024
    return f"{n} B"


def print_report(report: Report, min_rows: int) -> None:
    print(f"[index-report] schema={report.schema}  {len(report.indexes)} indexes, "
          f"{pretty_size(sum(i.size_bytes for i in report.indexes))} in total, statistics reset at {report.stats_reset or 'never'}")
    print("\n  index                                table            scans      size  kind")
    for i in report.indexes:
        kind = "primary key" if i.is_primary else "unique" if i.is_unique else "partial" if i.predicate else "plain"
        if not i.is_valid:
            kind += " INVALID"
        print(f"  {i.idx:<36} {i.tbl:<16} {i.scans:>6} {pretty_size(i.size_bytes):>9}  {kind}")

    print(f"\n  REDUNDANT (structural, safe to drop): {len(report.redundant)}")
    for f in report.redundant:
        print(f"    FAIL  {f.table}.{f.index}: {f.detail}  [{pretty_size(f.size_bytes)}, {f.scans} scans]")
    print(f"\n  FOREIGN KEYS WITHOUT AN INDEX: {len(report.unindexed)}")
    for f in report.unindexed:
        print(f"    FAIL  {f.table}.{f.index}: {f.detail}")
    print(f"\n  UNUSED since the last statistics reset (evidence only, judge by hand; tables >= {min_rows} rows): {len(report.unused)}")
    for f in report.unused:
        print(f"    note  {f.table}.{f.index}: {f.detail}  [{pretty_size(f.size_bytes)}]")
    print("\n[index-report] " + ("OK: nothing redundant, every foreign key is indexed" if report.ok else "FAILED: see the FAIL lines above"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Redundant / unused / missing index report")
    parser.add_argument("--schema", default="public")
    parser.add_argument("--min-rows", type=int, default=10000, help="'unused' only counts on tables with at least this many live rows")
    parser.add_argument("--json", action="store_true", help="machine readable output")
    args = parser.parse_args()

    from app.core.config import settings
    engine = create_engine(settings.sync_database_url)
    try:
        with engine.connect() as conn:
            report = build_report(conn, args.schema, args.min_rows)
    finally:
        engine.dispose()

    if args.json:
        payload = asdict(report)
        payload["ok"] = report.ok
        print(json.dumps(payload, indent=2, default=str))
    else:
        print_report(report, args.min_rows)
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
