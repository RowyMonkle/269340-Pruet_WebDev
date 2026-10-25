# Checkpoint 3: Defence Preparation (Questions and Answers)

## Purpose and Format

This document prepares the team for the instructor's code review (rubric: 35 concurrency, 35 query projection, 20 load test, 10 audit report and a clear team defence). Every question is written the way a reviewer is likely to ask it. Each one has three parts:

- **Answer**: what to say, in about 30 seconds.
- **Evidence**: the file, test or command to open if the reviewer asks for proof.
- **Owner**: who knows that part best. This is a suggestion; the team agrees the real split. Whoever answers should have read the files named under *Evidence*.

Every figure comes from the audit report (`docs/Performance_Audit_Report.md`), the README or a test, and the commands that reproduce them are in section 8.

---

## 1. Key Figures

### 1.1 Result Against the CP3 Specification

| Requirement | Observed | Audit Status |
| :--- | :--- | :--- |
| Concurrency control (Option A or B) and a dedicated `test_concurrency.py` | **Option B** (pessimistic): atomic conditional update for seats, `SELECT ... FOR UPDATE` for payments; **22 tests**; 20 users racing for the last ticket give 1 × `201` and 19 × `409` | **PASS** |
| Projection and $O(1)$ queries on list endpoints | Round trips stay constant while the data grows 50 to 300 times (`tests/test_query_counts.py`, **20 tests**); `GET /orders/summary` 3.8 KB for 300 orders instead of about 330 KB | **PASS** |
| $\ge 50$ VUs for 3 minutes, concurrent reads and writes | **50 VUs for 180 s** (+ 10 s ramps), 65 % read / 35 % write iterations, every write answered `201` | **PASS** |
| Stable requests per second | **216.1 req/s**; steady state 227.0 req/s, coefficient of variation 4.4 % | **PASS** |
| $p95 < 200$ ms | **60.04 ms** | **PASS** |
| $p99 < 500$ ms | **104.07 ms** (max 273.98 ms) | **PASS** |
| HTTP error rate $< 1$ % | **0.00 %** (0 of 43,253 requests, k6's own `http_req_failed`) | **PASS** |
| Audit report with charts and metrics | `docs/Performance_Audit_Report.md` with four charts, and README 7.13 (the specification accepts a README section; a PDF is optional) | **PASS** |

### 1.2 Other Figures to Know

| Figure | Value | Source |
| :--- | :--- | :--- |
| Writes in the official run | 11,098 orders and 7,773 payments committed, all `201` | report 2.2 |
| Contested zone (25 seats) | 1,156 attempts: exactly **25 won**, 1,131 got `409` | report 3.3 |
| Repeated payments (on purpose) | 378 sent, 378 × `409`, 0 double charges | report 3.3 |
| Repeatability | three runs: 214.7 / 215.4 / 216.1 req/s; $p95$ 64.06 / 60.57 / 60.04 ms | report 2.4 |
| Rerun from a clean `git archive` (2026-10-25) | 42,939 requests, 214.5 req/s, $p95$ 61.78 ms, $p99$ 102.02 ms, 0 failed, 25 wins | the commands of README 7.13 |
| Locust cross-check | 27,708 requests, 154.9 req/s, $p95$ 120 ms, $p99$ 130 ms, 0 failures; the $p95$ is carried by `POST /users` (bcrypt) | report 2.5 |
| Test suite | 219 tests: 218 pass + 1 skip in-process; `test_concurrency` 22, `test_query_counts` 20, `test_db_guards` 20 | `pytest tests -q`, README 7.3 |
| Consistency audit | 13 invariants, all pass after the load | report 7 |
| Deadlock before the fix | HTTP `500` in 13 % (2 seats), 44 % (10 seats) and 61 % (30 seats) of the opposite-order rounds | README 7.7 |
| Round trips | `GET /orders/summary` 2 SQL for 1 and for 300 orders; `GET /events` 2 MongoDB operations, 27 KB (was 124 KB); `POST /orders` 3 SQL + 3 MongoDB for 1 or 20 tickets; payment 6 SQL (was 11 for 1 ticket, 30 for 20) | README 7.10 |
| Index review | 33 to 24 indexes; sweeper query 23-28 ms to 0.13 ms | README 7.12 |
| Limits on every wait | lock 5 s, statement 15 s, pool 5 s, connect 2 s, MongoDB selection 1 s | README 7.9 |

---

## 2. The System in One Minute

The ticket platform uses two databases. PostgreSQL holds orders, tickets and payments; MongoDB holds the events and the seat counter of every zone (`booked_count`). Two API replicas run behind an nginx gateway. Checkpoint 3 asked three things:

1. **Concurrency control.** We use Option B (pessimistic). A seat is reserved by one atomic conditional update per (event, zone) in MongoDB, and a partial unique index in PostgreSQL protects a numbered seat. Payment and expiry take `SELECT ... FOR UPDATE` on the order row. `tests/test_concurrency.py` (22 tests) proves it, including the specification's case of 20 users racing for the last ticket: one succeeds, nineteen receive `409`.
2. **Query projection.** List endpoints return summaries and run a constant number of queries however much the data grows (`tests/test_query_counts.py`, 20 tests). The orders of a user with 300 orders weigh 3.8 KB instead of about 330 KB.
3. **Load test.** k6, 50 VUs, 3 minutes, real writes: 216 req/s, $p95$ 60 ms, $p99$ 104 ms, 0 % errors; the consistency audit passes 13 of 13 checks after the load.

---

## 3. Concurrency Control & Race Condition Prevention (35 points)

### Q1. Option A or Option B, and why?
**Answer.** The team chose **Option B** (pessimistic) and did not build a version column (Option A). Booking is a high-contention workload on a limited resource: when tickets run out, almost every loser has to receive `409` anyway. Letting the database decide in one statement (an atomic update or a row lock) is simpler and more direct than reading a version and writing it back. The decision was taken on 2026-10-13.
**Evidence.** README 7.1 (team decision) and 7.2 (one mechanism per shared resource).
**Owner.** Natthakritta.

### Q2. Where exactly is `SELECT ... FOR UPDATE`? The booking does not seem to use it.
**Answer.** Correct: **the seat reservation does not use it**. It uses a MongoDB conditional update, `booked_count <= capacity - qty` together with `$inc` in one command. That is a compare-and-set with no read-then-write window, so there is no lost update. A partial unique index `uq_tickets_event_zone_seat` in PostgreSQL stops the same numbered seat from being sold twice. `SELECT ... FOR UPDATE` is used (1) on the order row when paying, which prevents a double payment and a race with the sweeper, (2) in `PUT /users/{id}`, and (3) as `FOR UPDATE SKIP LOCKED` in the expired-hold sweeper and the outbox worker.
**Evidence.** `app/services/order_service.py` (atomic update around line 221, sweeper around line 418), `app/services/payment_service.py:47-52`, `app/services/user_service.py` (around line 272), README 7.2 table.
**Owner.** Natthakritta.

### Q3. Walk me through 20 users booking the last ticket.
**Answer.** Every client calls `POST /orders`. The API sends `update_one` with the condition `booked_count <= capacity - 1`. MongoDB executes commands on one document one at a time, so the first request gets `modified_count = 1`, creates the order and its tickets in PostgreSQL, commits, and answers `201`. The other 19 get `modified_count = 0` and answer `409` "sold out" without any write to PostgreSQL. The test checks three places: 1 × `201` and 19 × `409`, MongoDB `booked_count` = 1, and exactly one active ticket in PostgreSQL.
**Evidence.** `tests/test_concurrency.py::test_last_ticket_20_users`. Live: `docker compose exec -e API_URL=http://nginx -e REQUIRE_DB=1 api pytest tests/test_concurrency.py -k last_ticket -v -s` prints `Successes: 1 (Must be 1) | Conflicts: 19`.
**Owner.** Natthakritta.

### Q4. Why do you run the race tests through nginx and two replicas?
**Answer.** Inside one process the GIL lines the requests up and hides some bugs; the PostgreSQL deadlock appeared only when two processes hit the database at the same time. The same tests therefore also run through nginx to both replicas (`API_URL=http://nginx`), and that is the primary proof. 17 of the 22 tests run that way; the other 5 observe or patch the server process and can only run in-process.
**Evidence.** README 7.3; the skip messages "only works in-process" in the nginx run.
**Owner.** Natthakritta.

### Q5. You mention a deadlock. What happened?
**Answer.** Two users booked the same two seats in opposite order (A1, A2 against A2, A1). Each inserted its first row and waited for the second; PostgreSQL detected the deadlock and killed one side, and the API answered `500 DeadlockDetected`. Measured: 20 of 150 two-seat rounds (13 %), 44 of 100 ten-seat rounds and 49 of 80 thirty-seat rounds (61 %). The fix is to sort the seats by (event, zone, seat), the same global order for every order, before inserting. Now 60 opposite-order rounds give 0 server errors, and if a deadlock or lock timeout still occurs it is mapped to `409` and the seats are returned, never `500`.
**Evidence.** README 7.7 ("Races on the old booking code") and 7.2 rule 1; `validated_items.sort(key=_lock_order_key)` in `order_service.py`; tests `test_opposite_order_multi_seat_bookings_never_return_5xx`, `test_every_order_inserts_its_seats_in_one_global_order`, `test_database_deadlock_is_answered_with_409_and_seats_are_returned`.
**Owner.** Natthakritta.

### Q6. Why `409`, `410` and `503`, and why never `500`?
**Answer.** `409` means a collision: sold out, seat taken, already paid, deadlock, lock timeout; the client can choose again or retry. `410` means the 10-minute hold has expired and a new order is needed. `503` with `Retry-After` means the service itself is not available (pool exhausted, statement timeout, MongoDB or PostgreSQL down), which is not the request's fault. `500` would be an unknown bug, so one central handler maps SQLSTATE `40P01`, `40001` and `55P03` to `409`, and `57014`, `53300`, `57P03`, an exhausted pool and an unreachable MongoDB to `503`.
**Evidence.** `app/core/db_errors.py`, README 7.2 rules 4 and 6, `tests/test_db_errors.py` (16 tests).
**Owner.** Natthakritta, Nannapat.

### Q7. There are two databases and no shared transaction. What if the application crashes between them?
**Answer.** We do not use a distributed transaction. The order of operations is: reserve in MongoDB first, then write PostgreSQL; if the second step fails, the seats are given back (compensation). If the process dies between the two steps, one seat stays reserved with no ticket, which is a lost sale, but it can **never be an oversell**, because the counter can only be higher than the truth. `verify_consistency.py` detects that drift and `--repair` resets the counter from the PostgreSQL ticket count. The accepted limit is that nothing heals it automatically. On the release side (expired holds) PostgreSQL is committed first and MongoDB second, so a crash in between has the same effect: a seat stays held, no oversell.
**Evidence.** README 7.2 rule 3 and 7.8; `scripts/verify_consistency.py` (13 invariants); the comment "Commit PostgreSQL state FIRST before altering MongoDB" in `payment_service.py`.
**Owner.** Natthakritta.

### Q8. How do you stop a double payment?
**Answer.** Two layers. In the application, `SELECT ... FOR UPDATE` on the order row makes the second payment wait until the first commits, then it sees the status `confirmed` and receives `409` "already paid". In the database (migration 0007), the partial unique index `uq_payments_order_completed (order_id) WHERE status = 'completed'` refuses a second completed payment even if a script or a future bug writes directly. In the load test 378 repeated payments were sent on purpose: 378 × `409` and 0 double charges.
**Evidence.** `tests/test_concurrency.py::test_parallel_payments_charge_the_order_once` (20 at once: 1 × `201`, 19 × `409`), `tests/test_db_guards.py` (8 raw writers: 1 succeeds, 7 are refused), README 7.11.
**Owner.** Natthakritta.

### Q9. What about a client that retries?
**Answer.** The `Idempotency-Key` header is unique, and the request is fingerprinted by user, event and zone. The same key sent 20 times at once creates one order; the same key with a different payload is refused with `409`.
**Evidence.** `test_parallel_same_idempotency_key_creates_one_order`, `test_idempotency_payload_mismatch`, `test_idempotent_replay_works_with_any_zone_spelling`.
**Owner.** Natthakritta.

### Q10. You already have locks. What do the database guards add?
**Answer.** A lock or an `if` in the code protects only the code path that uses it; a new endpoint, a script, a hand-written `UPDATE` or a future bug can skip it. The guards move the rules into the databases: the unique index against a second charge, five `CHECK` constraints on the status vocabularies, and a MongoDB validator `0 <= booked_count <= capacity`. They were added without downtime (`CREATE INDEX CONCURRENTLY`, `CHECK ... NOT VALID` then `VALIDATE`); applying migration 0007 under 50 users took 2.9 s with 0 failures. The migration never repairs money data by itself: if existing rows already break a rule (for example a double charge) it stops, changes nothing and names the orders.
**Evidence.** README 7.11, `alembic/versions/0007_db_guards.py`, `tests/test_db_guards.py` (20 tests).
**Owner.** Natthakritta.

---

## 4. Query Projection & Elimination of $N+1$ Queries (35 points)

### Q11. How do you prove there is no $N+1$?
**Answer.** `tests/test_query_counts.py` counts the database round trips of one request at a small and a large data size (the data grows 50 to 300 times) and fails if the count changes. The counter itself is tested: a multi-row `INSERT` counts as 1, while an `executemany` of N rows counts as N because psycopg2 sends the rows one by one (which is what an $N+1$ looks like on a write path).
**Evidence.** `docker compose exec -e REQUIRE_DB=1 api pytest tests/test_query_counts.py -v -s` prints the audit table; README 7.10.
**Owner.** Natthakritta, Nannapat.

### Q12. Did you actually find an $N+1$?
**Answer.** Yes, on a write path. Paying an order used to run one `UPDATE` per ticket (11 round trips for 1 ticket, 30 for 20). It is now one `UPDATE ... WHERE order_id = ?`, so the cost is a constant 6. The test `test_payment_costs_the_same_round_trips_for_1_and_for_20_tickets` fails on the old code and passes now. The read endpoints had no $N+1$.
**Evidence.** README 7.10 ("Findings"); the query-budget comment in `payment_service.py`.
**Owner.** Natthakritta.

### Q13. What do the list endpoints return, and what does the detail endpoint return?
**Answer.** A list returns a summary schema with only the columns it needs. `GET /orders/summary` computes `ticket_count` and `paid_amount` with sub-selects in SQL and loads no entities. The events list is a MongoDB aggregation whose `$project` stage derives `min_price` and `seats_left` inside the database (124 KB to 27 KB per 100 events). Users are selected as plain columns, and `hashed_password` is never read. The detail view `GET /orders/{id}` returns the full order with its tickets and payments through one eager join: 1 SQL statement for 1 or for 50 tickets and payments. To be precise, that one is an eager join, not a column projection.
**Evidence.** README 7.10 table; the `OrderSummary` and `EventSummary` schemas.
**Owner.** Nannapat.

### Q14. What happened to the original `GET /orders?user_id=`?
**Answer.** It used to return every order of the user with all tickets and payments and no pagination (about 330 KB for 300 orders). It now answers a `307` redirect to `/orders/summary`, a bounded page of 3.8 KB, so old clients keep working. The test `test_original_orders_list_payload_is_bounded` (formerly an `xfail`) passes.
**Evidence.** `app/api/v1/orders.py` (`list_orders_by_user`), README 7.10.
**Owner.** Nannapat.

### Q15. Why did you drop indexes, and how do you know it was safe?
**Answer.** Every index is also written on every insert and update. Going from 33 to 24 indexes cut the cost of inserting 50,000 rows into each of four tables from 11.1-11.3 s to 10.2-10.3 s and the WAL from 152-154 MB to 90-120 MB. Two criteria were used: *redundant* (the index is the leading part of another one, which is true whatever the workload) and *no reader* (checked with `EXPLAIN ANALYZE` of the application's own queries after dropping the index inside a transaction that was rolled back). `idx_scan = 0` is only supporting evidence, because on a small table or before a query has run it proves nothing, so no index was dropped on that number alone. One real slow query was found on the way: the sweeper's `ORDER BY id LIMIT 500` walked the whole primary key (23-28 ms at 130,000 orders); `ORDER BY expires_at, id` takes 0.13 ms. A wrong call is cheap to undo: `alembic downgrade 0007_db_guards` recreates the nine indexes in 3.6 s.
**Evidence.** README 7.12, `docs/perf/index_review.md`, `scripts/index_report.py`, `tests/test_index_usage.py`.
**Owner.** Natthakritta.

---

## 5. Automated Load Test & Performance Metrics (20 points)

### Q16. Describe the load test.
**Answer.** It is a k6 script (`scripts/k6_script.js`): 10 s ramp to 50 VUs, 3 minutes at 50 VUs, 10 s ramp down, through nginx. The mix is 65 % reads (events list and detail, user, order summary) and 35 % writes. A write books one ticket with `POST /orders`, and 70 % of the buyers pay the order that was just returned with `POST /payments`; 5 % of the payments are repeated on purpose and must answer `409`, and 10 % of the writes race for a zone of 25 seats, where exactly 25 must win. Only the expected status counts as a success. Thresholds: $p95 < 200$ ms, $p99 < 500$ ms, `http_req_failed` $< 1$ %, checks $> 99$ %, `hot_zone_orders_won == 25`.
**Evidence.** The header of `scripts/k6_script.js`, report 5.1, README 7.13.
**Owner.** Whoever runs the demo.

### Q17. Your report says an earlier benchmark was withdrawn. Why?
**Answer.** The team audited its first run and found that k6's own `http_req_failed` was **34.86 %**: every write had been refused with a `4xx`. The script had fallen back to an event id that does not exist (the events list no longer carries zones) and paid random order ids, while its checks accepted `400`, `404`, `409` and `410` as valid. The script was rewritten: it creates its own events, pays only the order it has just created, and counts only the correct status as a success. Three runs of the new script agree within 1 % on throughput. The lesson is to trust a strict metric (`http_req_failed`) rather than a permissive check.
**Evidence.** Report 5.3 (methodology revision) and 2.4 (repeatability).
**Owner.** Whoever runs the demo.

### Q18. Why a synthetic zone with 1,000,000 seats?
**Answer.** So that writes keep succeeding for the whole 3 minutes. If the zone sold out half way, most answers would be `409` and the write path would no longer be measured. The scarce-inventory case is measured separately: the 25-seat contested zone in the same script, and `test_concurrency.py`.
**Evidence.** Report 5.1 (Setup) and section 8.
**Owner.** Whoever runs the demo.

### Q19. k6 shows $p95$ 60 ms and Locust shows $p95$ 120 ms. Which one is right?
**Answer.** Both, because the mixes differ. The Locust profile includes `POST /users`, whose bcrypt hashing costs about 130 ms and which is about 5 % of that mix, so it pulls the aggregate $p95$ up to about 120 ms; without it the write endpoints stay at $p95 \le 35$ ms. The k6 mix has no registration. Both are far below the 200 ms limit, and the report states which figure comes from which tool.
**Evidence.** Report 2.5, README 7.7 (the $p95$ note).
**Owner.** Whoever runs the demo.

### Q20. Is the throughput stable?
**Answer.** In the steady state it is 227.0 req/s with a minimum of 175, a maximum of 239 and a coefficient of variation of 4.4 %; there is no drift over the 180 s. There are a few short dips to about 175-185 req/s in the first minute, and their cause was not analysed. They did not touch the latency limits: 29 of 43,253 requests took longer than 200 ms and none took longer than 500 ms.
**Evidence.** `docs/perf/k6_throughput.png`, report 2.1 and 6.
**Owner.** Whoever runs the demo.

### Q21. What are the limits of this benchmark?
**Answer.** k6 and the whole stack ran on one 16-CPU machine, so the absolute numbers are optimistic compared with a separate load generator; the margins are large, though (3.3 times on $p95$, 4.8 times on $p99$). The big zone is synthetic, `POST /users` is not in the mix, 30 % of the buyers never pay (their holds expire after the test window), the seeded data set has only 250 users (the effect of large tables was measured separately in the index review), and PostgreSQL and MongoDB are single instances.
**Evidence.** Report section 8.
**Owner.** Whoever runs the demo.

---

## 6. Audit Report, Reproduction & Team (10 points)

### Q22. Where is `Performance_Audit_Report.pdf`, and where are the charts?
**Answer.** The specification accepts a PDF or a Markdown section in the README. We deliver `docs/Performance_Audit_Report.md` with four charts (throughput over time, latency over time, latency per endpoint, outcome of every request) and README 7.13, which has the PASS table against the specification and two charts. *(Update this answer if a PDF is exported before the defence; every number in it must match 7.13.)*
**Evidence.** `docs/perf/k6_*.png`, README 7.13.
**Owner.** Pandara.

### Q23. Your test uses `/orders` and `201`; the specification's sample uses `/tickets/book` and `200`.
**Answer.** A booking is an order, the unit that is held for 10 minutes, paid, expired or cancelled. Its tickets are created by the same request, so there is no separate `/tickets/book` route, and because the request creates a new resource it answers `201`. The body names the MongoDB `event_id` (an ObjectId) and the zone. The contract is the same: exactly one winner, every other user receives `409`, and the assertion `successes == 1` is the one in the specification's sample.
**Evidence.** Report 3.4, README 7.8.
**Owner.** Nannapat, Natthakritta.

### Q24. Can we reproduce this from a clean checkout?
**Answer.** Yes, and it was done from the `git archive` of the commit to be submitted, on a separate stack: build and migration pass (head `0008_drop_redundant_indexes`), the whole suite gives 218 passed and 1 skipped, the race tests pass through nginx and both replicas, `verify_consistency` passes 13 of 13 checks three times, the k6 command of the README passes every threshold, and the resilience lab runs to the end.
**Evidence.** Report section 9, README 7.3 and 7.13.
**Owner.** Natthakritta.

### Q25. Who did what?

| Member | Role | Questions they should be able to answer |
| :--- | :--- | :--- |
| Natthakritta | Backend Lead & Database Architecture | Q1-Q5, Q7-Q12, Q15, Q24 |
| Nannapat | API Engineering & Request Validation (+ web design) | Q6, Q11, Q13, Q14, Q23 |
| Poonyaporn | Data Engineering | Q16-Q21: the k6 script, the data volumes, how to run and read the load test |
| Pandara | Documentation & Frontend Design | Q22, the structure of the report, the demo script, packaging (Q26) |

### Q26. How is the submission packaged?
**Answer.** `git archive -o 269340_CP3.zip HEAD` contains tracked files only: no `__pycache__`, no `.env`, no local files and not the 56 MB `k6_points.csv`. A plain zip of the folder would include them. The specification does not say how or where to submit, so this must be confirmed with the instructor.
**Evidence.** README 7.6 (Pandara, item 4).
**Owner.** Pandara.

---

## 7. What We Did Not Do

Say these before the reviewer asks.

- **Option A** (version column, `412`) is not built; Option B is the team decision.
- **Crash window**: a crash between the MongoDB reservation and the PostgreSQL commit leaves one unheld seat (never an oversell). There is no automatic healing, only `verify_consistency.py --repair` (README 7.8).
- **Multi-event orders** are reserved one zone at a time and are all-or-nothing through compensation, not through one atomic step (README 7.8).
- **Single instances**: nginx, PostgreSQL and MongoDB are not replicated. A PostgreSQL outage gives `503` with `Retry-After` for what depends on it, some of them slow (up to about 8 s, there is no circuit breaker). A MongoDB outage gives `503` on the three endpoints that need it, with a $p99$ of about 1 s from the 1 s selection timeout (`docs/perf/resilience_results.md`).
- **Internal sweep endpoint** `POST /orders/cleanup-expired` has no check of its own: it is hidden by nginx (`404`) and left out of Swagger, and the replicas' debug ports 8001 and 8002 are published on the host (README 7.8, 7.9).
- **`POST /users`** (bcrypt) is not in the k6 mix and carries the aggregate $p95$ in the Locust mix.
- **The first-minute throughput dips** (about 175-185 req/s) were not analysed.
- **No PDF** of the report unless it is exported (the README section is accepted by the specification).

---

## 8. Live Demo Runbook

Run these steps in this order on the real stack (`docker compose up -d`, seeded). The whole demo takes about 7 minutes, three and a half of them the load test.

| Step | Time | Command | Expected result |
| :--- | ---: | :--- | :--- |
| 1. Race | 1 min | `docker compose exec -e API_URL=http://nginx -e REQUIRE_DB=1 api pytest tests/test_concurrency.py -v -s` | `Successes: 1 (Must be 1) \| Conflicts: 19`; 17 pass, 5 skipped ("only works in-process") |
| 2. Query counts | 1 min | `docker compose exec -e REQUIRE_DB=1 api pytest tests/test_query_counts.py -v -s` | 20 pass and the audit table (the same round trips at the small and the large size) |
| 3. Integrity | 30 s | `docker compose exec api python scripts/verify_consistency.py` | `ALL CHECKS PASSED` (13 lines `PASS`) |
| 4. Load | 3 min 20 s | `docker run --rm --network 269340-pruet_ticweb_default -v "$PWD/scripts:/scripts:ro" -v "$PWD/docs:/out" -e BASE_URL=http://nginx/api/v1 grafana/k6 run /scripts/k6_script.js --summary-export /out/k6_summary.json --out csv=/out/perf/k6_points.csv` | every threshold with a check mark; about 215 req/s, $p95$ about 60 ms, `hot_zone_orders_won` 25 |
| 5. Integrity again | 30 s | the command of step 3 | `ALL CHECKS PASSED` |

Notes:

- Run step 4 alone: a test running at the same time loads the same stack and blurs the numbers. Pull `grafana/k6` before the session and run a short warm-up first (the first seconds after a restart are slower).
- Step 4 overwrites `docs/k6_summary.json` and writes `docs/perf/k6_points.csv` (git-ignored). To keep the committed summary, run `git checkout docs/k6_summary.json` afterwards.
- Do **not** run `scripts/resilience_lab.py` on the real stack: it stops the databases. Show `docs/perf/resilience_results.md` instead.
- If Docker fails during the defence, the committed evidence is `docs/k6_summary.json`, the four charts in `docs/perf/`, the raw k6 output in report 5.2 and the consistency output in report 7.

---

## 9. Before the Defence

- [ ] `docker compose up -d` is healthy and `docker compose exec api alembic current` shows `0008_drop_redundant_indexes (head)`.
- [ ] The database is seeded (`docker compose exec api python seed.py`).
- [ ] `scripts/verify_consistency.py` is clean **before** any `docker compose up` on an existing database (migration 0007 refuses to run if a double charge exists, and then the API does not start).
- [ ] `docker pull grafana/k6`, and the network name is right (`docker network ls` shows `269340-pruet_ticweb_default`).
- [ ] Everyone has read their questions in sections 3 to 6 and the files named under *Evidence*; the person answering the load-test questions has read `scripts/k6_script.js` and report sections 2, 5 and 8.
- [ ] The submission channel is confirmed with the instructor and the zip is built with `git archive`.
