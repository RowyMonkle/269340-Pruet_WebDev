# Index review (migration 0008)

**Question:** which of the 33 indexes of the PostgreSQL schema earn their place, and which only make every write slower?
**Answer:** nine can go (four are structurally redundant, five have no reader). One of the review's findings was a real slow query: the expired-hold sweeper could not use its index. Both are fixed, measured, and guarded by tests.

Everything below is reproducible with the commands in the last section. Numbers come from a throw-away rehearsal stack with 130,000 orders, 196,000 tickets, 94,000 payments and 100,000 outbox events.

## 1. Method: three kinds of evidence, in this order

| Step | Question | Why it can be trusted |
| --- | --- | --- |
| Structure (`scripts/index_report.py`) | Is the index only a leading part of another index? | Holds whatever the workload is. A B-tree on `(a, b)` answers every lookup a B-tree on `(a)` answers. |
| Usage (`pg_stat_user_indexes.idx_scan`) | Did the planner read it during 120 s of realistic load? | Real traffic, real planner, real data size. Counters were reset first (`pg_stat_reset()`), because the old counters were polluted by earlier experiments. |
| Hypothetical drop (`BEGIN; DROP INDEX ...; EXPLAIN ANALYZE ...; ROLLBACK;`) | If this index did not exist, would any query the application runs get worse? | Nothing is really dropped, the plan and the time of the *same* query are compared. |

Two rules came out of this and are the reason the report treats the groups differently:

* **A scan count of 0 is evidence, not proof.** It proves nothing on a small table (the planner reads 12 rows with a sequential scan anyway) and nothing for a query that has not run yet. So "unused" is only a note for a human, never a failure.
* **Structure is proof.** A redundant index is reported as a failure, and `tests/test_index_usage.py` keeps it from coming back.

## 2. Verdict for every index that was dropped

### A. Redundant (structural)

| Dropped | Table | Covered by | Real planner behaviour |
| --- | --- | --- | --- |
| `idx_orders_user_id (user_id)` | orders | `idx_orders_user_created (user_id, created_at DESC, id DESC)` | 0 scans in the load test; "my orders" page 0.104 ms with and 0.111 ms without it |
| `idx_orders_status (status)` | orders | `idx_orders_status_expires (status, expires_at)` | 0 scans |
| `idx_payments_order_id (order_id)` | payments | `idx_payments_order_status (order_id, status)` | payments of an order 0.110 ms with, 0.132 ms without |
| `idx_tickets_event_id (event_id)` | tickets | `idx_tickets_event_zone (event_id, seat_zone)` | tickets of an event 0.303 ms with, 0.314 ms without |

Honest cost: a bare `SELECT count(*) ... WHERE user_id = ?` went from 0.116 ms to 0.247 ms (it now reads the wider `(user_id, status)` index). The application never runs that query.

### B. No reader (evidence)

Rule applied: 0 scans in the load test **and** no code path filters or sorts by the column **and** the hypothetical drop changes no plan of a query the application runs.

| Dropped | Table | Why nobody reads it | What a query nobody runs would cost without it |
| --- | --- | --- | --- |
| `idx_orders_created_at` | orders | no endpoint lists orders across all users | newest 20 orders of everybody: 0.085 ms -> 26 ms (sequential scan + top-N sort) |
| `idx_orders_expires_at` | orders | the sweeper needs `(status, expires_at)`; alone it was only the slower fallback plan (see 3) | - |
| `idx_payments_status` | payments | 4 distinct values, never selective on its own | count of failed payments: 0.46 ms -> 3.5 ms |
| `idx_outbox_event_type` | outbox_events | the outbox is write-hot and only read by `status, created_at` | - |
| `idx_outbox_aggregate_id` | outbox_events | same | lookup by aggregate: 0.08 ms -> 9.6 ms (sequential scan of 100,000 rows) |

If one of those queries is ever added to the application, add the index in the same pull request. It costs one migration; the downgrade of 0008 shows the exact definitions.

## 3. Finding: the sweeper could not use its index

`idx_orders_status_expires` was created for the sweeper (`status = 'pending' AND expires_at < now`), and had **0 scans**. The hypothetical-drop test showed why: the query ended in `ORDER BY id LIMIT 500`, so the planner walked the whole primary key and filtered every order.

| Query | Plan | Time |
| --- | --- | --- |
| `... ORDER BY id LIMIT 500` (before) | Index Scan on `orders_pkey`, **Rows Removed by Filter: 132,280** | 23-28 ms |
| `... ORDER BY expires_at, id LIMIT 500` (now) | Index Scan on `idx_orders_status_expires`, incremental sort | **0.13 ms** |
| same, with `idx_orders_status_expires` dropped | Index Scan on `idx_orders_expires_at`, 123,622 rows filtered | 81 ms |

The old cost grows with the size of the table, on every sweep tick of every replica (a few seconds per tick at 10 million orders). The fix is one line in `app/services/order_service.py::_sweep_one_batch` (oldest overdue hold first, which is also the fairer order). The last row is the reason `idx_orders_expires_at` is dropped: it is the plan the planner falls back to when the right index is missing, and it is worse.

`tests/test_index_usage.py::test_the_sweeper_asks_for_the_order_its_index_can_deliver` fails if someone changes the order back.

## 4. What is kept on purpose

| Kept | Why |
| --- | --- |
| every primary key, unique and partial unique index | they are constraints (idempotency key, order number, payment reference, seat uniqueness, one completed payment per order) |
| `idx_orders_user_created` | the "my orders" page (7.4) |
| `idx_orders_user_status (user_id, status)` | 161 scans in the load test; not structurally redundant. Next candidate if write cost ever matters: `user_created` would take over |
| `idx_orders_status_expires` | the sweeper |
| `idx_payments_order_status` | payments of an order; it now also serves `order_id` alone |
| `idx_tickets_order_id` | the hottest index of the schema (205,000 scans in 120 s): tickets of an order, and the foreign key |
| `idx_tickets_event_zone` | the only event-level lookup of tickets (also serves `event_id` alone). 0 scans in the test, kept because it is the one index a sales report per event would use |
| `idx_outbox_status_created` | the outbox worker |
| `idx_users_last_name` | CP2: `scripts/sql/verify_users_migration.sql` requires it after the contract |
| `idx_users_created_at` | users is a small table that is written rarely (bcrypt dominates a registration); not worth the argument |

`scripts/index_report.py` also checks the opposite mistake: a **foreign key without an index** makes a delete or update of the parent row scan the whole child table. Result today: 0 such keys.

## 5. Measured effect

**Write cost** (one transaction inserting 50,000 orders, 50,000 tickets, 50,000 payments and 50,000 outbox events, two runs each, same data, indexes switched by `alembic downgrade/upgrade`):

| Indexes | Time | WAL written |
| --- | ---: | ---: |
| 33 (before) | 11.1 s and 11.3 s | 152 MB and 154 MB |
| 24 (after) | 10.3 s and 10.2 s | 90 MB and 120 MB |

About 9 % faster inserts and 20-40 % less WAL (WAL is noisy because of full-page writes after a checkpoint; the direction was the same in all four runs). The saving is paid on every booking: a `POST /orders` writes to `orders`, `tickets` and `outbox_events`.

**Index size** on the same data: 140 MB -> 124 MB (the nine dropped indexes were 16 MB, freshly built).

**Reads and the whole system.** 50 users, 90 s per run, mixed profile including the summary endpoints, 130,000 orders in the table. The two schemas were run **alternately on the same stack** (A = 33 indexes, B = 24 indexes), because single runs vary: p95 of one run ranged from 59 to 90 ms with no change at all in the schema.

| Run | Indexes | Requests/s | p50 | p95 | p99 | max | Failures |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| A1 | 33 | 152.8 | 11 | 67 | 130 | 238 | 0 |
| B1 | 24 | 152.0 | 9 | 65 | 130 | 238 | 0 |
| A2 | 33 | 152.7 | 10 | 81 | 140 | 237 | 0 |
| B2 | 24 | 152.3 | 10 | 68 | 130 | 201 | 0 |

No regression: the difference between the two schemas is smaller than the difference between two runs of the same schema. The read paths do not change (section 2 shows 0.0-0.1 ms differences), and the load generator caps the rate (about 152 req/s) in every run. The load test is the safety net, not the benefit: the benefit is the write cost, the disk, and the sweeper.

**Applying 0008 to a running system** (130,000 orders, 50 users writing, upgrade started 20 s into the run): **2.5 s, 0 failures**, 152 req/s, p95 77 ms, p99 130 ms, max 238 ms, no invalid index afterwards. `DROP INDEX CONCURRENTLY` takes no lock that blocks reads or writes. The downgrade rebuilds all nine indexes with `CREATE INDEX CONCURRENTLY` in 3.6 s and leaves no invalid index behind.

## 6. A side finding: the 502 that was not the database

The first load run showed 1 failed request in 18,192: `POST /orders` -> **502**. nginx's error log had `recv() failed (104: Connection reset by peer) while reading response header from upstream`. Cause: nginx keeps idle connections to the replicas for 60 s, uvicorn closes an idle connection after 5 s. Now and then nginx sends a request down a connection that uvicorn is closing, and nginx never retries a POST once it was sent.

Reproduced on purpose (`one POST every 2.5 s through nginx`, so each replica sees a request every ~5 s): **before 1 of 200 requests answered 502; after the fix 0 of 200**. One failure in 200 is a thin sample, so read the after-fix result as consistent with the fix; the cause itself is shown by the nginx error log and by the setting (nginx now closes an idle connection after 4 s, uvicorn after 5 s). Fix: `keepalive_timeout 4s;` in the `upstream` block of `nginx/nginx.conf` (nginx gives up on an idle connection first).

## 7. Reproduce

```bash
docker compose exec api python scripts/index_report.py                 # redundant / unindexed fail the run; "unused" is a note
docker compose exec api python scripts/index_report.py --json          # machine readable
docker compose exec -e REQUIRE_DB=1 api pytest tests/test_index_report.py tests/test_index_usage.py -v
```

For the dataset-sized evidence use a rehearsal stack (README 7.4), load a few hundred thousand rows, `SELECT pg_stat_reset()`, run the Locust profile for 2 minutes, then run `index_report.py --min-rows 20000`. The "what if this index did not exist" check is a plain transaction:

```sql
BEGIN;
DROP INDEX idx_orders_status_expires;
EXPLAIN (ANALYZE, COSTS OFF) SELECT id FROM orders WHERE status = 'pending' AND expires_at < now() ORDER BY expires_at, id LIMIT 500;
ROLLBACK;   -- nothing was dropped
```

## 8. Rules for the team

1. A new index comes with the query that needs it (the `EXPLAIN` in the pull request). No query, no index.
2. Do not add `(a)` next to `(a, b)`: `tests/test_index_usage.py` and `scripts/index_report.py` fail.
3. A query that sorts differently from its index cannot use it for the order (section 3). Check the plan, not the existence of the index.
4. Never trust `idx_scan = 0` on a small table or right after a restart.
5. Create and drop indexes `CONCURRENTLY` in an Alembic revision with `autocommit_block()`, as 0006 and 0008 do.
