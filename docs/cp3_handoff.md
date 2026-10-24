# CP3 hand-off: what is left, who does it, who checks it

Status as of 2026-10-24: backend by Natthakritta (concurrency control, consistency audit, migrations 0006-0008, `pg_stat_statements`, limits on every wait and 409 / 503 mapping, README 7.9);
order summary endpoints and 503 handling in `main` by Nannapat.
Design and numbers: README section 7. This file is the checklist. Deadline: end of week 16 (40 % of the team grade).

## Order of work

| Step | Who | Needs | Output |
| --- | --- | --- | --- |
| M1 | Poonyaporn | nothing (backend is ready) | `loadtest/locustfile.py` + first run = **"before" numbers** |
| M1 | Pandara | README 7.7 baseline | report skeleton, CP3 demo script, packaging check |
| M1 | Nannapat | README 7.5 and 7.6 | list endpoints as projections + `tests/test_query_counts.py` |
| M2 | Poonyaporn | Nannapat's merge | **"after" run** + charts + `pg_stats` CSV |
| M2 | Natthakritta | Nannapat's merge | review against README 7.5, run full test suite + audit on a clean stack |
| M3 | Pandara | M2 outputs | `Performance_Audit_Report.pdf`, final README numbers |
| M4 | everyone | all merged | rehearsal of the defence on a clean checkout |

Poonyaporn and Pandara do not have to wait for Nannapat to start; only the "after" measurements do.

## Nannapat — API Engineering & Request Validation (+ web design)

Code that is yours today: `app/api/v1/orders.py`, `app/api/v1/events.py`, `app/services/event_service.py`, `app/schemas/order.py`, `app/schemas/event.py`, `frontend/`.

- [ ] **Orders list** `GET /api/v1/orders?user_id=`: paginated summary (`OrderSummary`: id, order_number, status, total_amount, expires_at, created_at, ticket_count, paid_amount), no ORM entities, no `joinedload`. Use the tested SQL in README 7.6. Today: 337 KB and 77 ms for a user with 300 orders.
- [ ] **Order detail** `GET /orders/{id}`: projected columns for tickets and payments, no lazy loads.
- [x] **Events list** `GET /api/v1/events` (and `/products`): `EventSummary` with `min_price` and `seats_left` computed in MongoDB, 31 KB for 100 events instead of 124 KB; the detail endpoint is unchanged (done by Natthakritta, README 7.10).
- [ ] **Trim round trips**: `POST /orders` runs 7 statements (3 only serialise the response). `POST /orders/{id}/payments` now runs 6 for any number of tickets (the per-ticket UPDATE is gone). Build the response from objects you hold; consider `BackgroundTasks` for the activity-log insert.
- [x] **Errors**: pool exhaustion / timeout -> 503 + `Retry-After`; collisions map to 409 through `app/core/db_errors.py`. The limits themselves (lock 5 s, statement 15 s, pool 5 s) are now in force, see README 7.9.
- [ ] **Web design**: booking demo page in `frontend/` that handles `409` ("someone took it, pick another seat") and pages through orders. Pair with Pandara.
- [x] **Tests**: `tests/test_query_counts.py` (round trips constant while data grows, payload bounds; done, README 7.10). Add a row to `READ_CASES` for every new list endpoint.
- [ ] **Original `GET /orders?user_id=`**: still returns whole orders (326 KB for 300 orders, no pagination); `test_original_orders_list_payload_is_bounded` is an `xfail` that passes by itself once it is bounded.

Contract changes others depend on: the summary endpoints are `GET /orders/summary?user_id=&limit=&offset=` and `GET /orders/{id}/summary` (plain JSON list / object). If `GET /events` items change, Poonyaporn's Locust tasks and Pandara's pages need the same change on the same day.
Status codes now: already paid `409`, expired hold `410`, cancelled `409`, payments of an unknown order `404`, busy or unreachable database `503` + `Retry-After` (README section 4 and 7.2). `docs/audit_demo_script.md` still shows `400` for paying twice.

Who checks you: Natthakritta reviews the diff against README 7.5; Poonyaporn's "after" run must show the lower payloads; `pytest tests` must stay green (151 tests today, 149 in-process, 1 xfail).

## Poonyaporn — Data Engineering

Code that is yours today: `scripts/backfill.py`, `scripts/traffic_gen.py` (CP2), `seed.py`.

- [ ] **`loadtest/locustfile.py`**: 50 users, ramp 10/s, **180 s**, mix and thresholds in README 7.6. Exit code 1 when p95 >= 200 ms, p99 >= 500 ms or failures >= 1 %. Run with the official `locustio/locust` image (command in README 7.6).
- [ ] **Contention scenario**: hot event with N seats, 50 VUs racing; exactly N bookings must win; then `verify_consistency.py` must pass.
- [ ] **Bigger data**: `seed.py --users 5000 --orders 20000`, plus a few users with hundreds of orders.
- [ ] **Run protocol**: `pg_stats.py --reset` -> load -> `pg_stats.py --top 15 --requests N --csv docs/perf/pg_top.csv` -> `verify_consistency.py`. Save Locust `--csv` and `--html` in `docs/perf/`.
- [ ] **Before and after** with the same profile (before = commit before the list changes). Hand Pandara a table and charts. `docs/perf/reference_locustfile.py` is the profile used for the baseline numbers in README 7.7 and 7.9: start from it.
- [ ] **Mongo**: index for the events list (status + date_time) and an `explain()` screenshot/output.
- [ ] **`scripts/k6_script.js`** (merged in PR #24): `POST /api/v1/payments` now exists (order id in the body, `payment_method` accepted; same service as `POST /orders/{id}/payments`), so the payment requests no longer end in 404. Still to fix so the numbers mean something: pay the order id that `POST /orders` just returned (not a random 1..50: those are mostly already paid or expired and answer 409 / 410), count only 201 (and the intended 409 on a deliberate double payment) as success instead of accepting 400 / 404, add `GET /orders/summary?user_id=`, export p99 in the summary, seed bigger data, and save the command and the result in `docs/`.
- [ ] Warm up before measuring (first seconds after a restart are slower); keep `POST /users` at 3 % of the mix or less, or report it separately (bcrypt).

Who checks you: Natthakritta re-runs your locustfile on a clean stack; the audit must say ALL CHECKS PASSED after your load.

## Pandara — System Documentation & Frontend Design

Code that is yours today: `README.md`, `docs/`, `frontend/`.

- [ ] **`docs/Performance_Audit_Report.md` -> `Performance_Audit_Report.pdf`** (repository root, the name the spec asks for). Sections: objectives and rubric mapping; system under test (architecture, versions, machine); concurrency control (table in README 7.2 + test output); query audit table (endpoint, SQL statements, payload, before/after); load test setup and charts; consistency audit output; risks; reproduction commands.
- [ ] **`docs/audit_demo_script.md` for CP3**: live race demo (README 7.3), query-count demo, load run, who answers which question. **Fix the known bug in step 2.2**: the events endpoint answers `{"items":[{"id":...}]}` and the parameter is `page_size`, not `limit`.
- [ ] **README upkeep**: keep section 7 statuses and numbers current; every command in the report must work.
- [ ] **Packaging**: `git archive -o 269340_CP3.zip HEAD` (tracked files only, no `__pycache__`, `.env`, notes). Ask the instructor how and where to submit (the spec does not say).
- [ ] **Frontend**: pair with Nannapat on the booking demo page; update pages when the list responses change.

Who checks you: each teammate reads the report section about their part; Natthakritta runs the commands in the report on a clean stack.

## Natthakritta — Backend Lead & Database Architecture

Done: booking-path fixes, `app/core/db_errors.py` (409 / 503), `scripts/verify_consistency.py`, `scripts/pg_stats.py`, migration 0006, tests (`test_concurrency.py` 22, `test_consistency.py` 8, `test_resilience.py` 24, `test_db_errors.py` 16), README section 7.
Also done (7.9): lock / statement / pool / connect / MongoDB timeouts that really reach the engine and client, batched sweeper (one MongoDB update per zone), outbox drained in 1,000-row batches and trimmed after 7 days,
non-blocking flag refresh, nginx fail-over that no longer counts a deliberate 503 as a dead replica, internal sweep endpoint hidden from nginx and Swagger, `scripts/resilience_lab.py`, results in `docs/perf/resilience_results.md`.
Also done (7.11, 7.12): database guards (migration 0007: one completed payment per order, status `CHECK`s, MongoDB seat-inventory validator; a blocked second charge answers 409), index review (`scripts/index_report.py`, migration 0008 drops nine indexes, `docs/perf/index_review.md`), the sweeper query now uses its index (23 ms -> 0.13 ms), nginx `keepalive_timeout 4s` (a rare 502 on POST), tests `test_db_guards.py` 20, `test_index_report.py` 11, `test_index_usage.py` 14.
Rules for the others: tests that corrupt MongoDB on purpose need `bypass_document_validation=True`; a new status value needs a migration first; a new index comes with its query (`index_report.py` and `test_index_usage.py` fail on a redundant one).

- [ ] Review Nannapat's and Poonyaporn's changes against README 7.5.
- [ ] After every merge: `docker compose exec -e REQUIRE_DB=1 api pytest tests -q`, the live race run (`API_URL=http://nginx`) and `verify_consistency.py`.
- [ ] Re-run everything on a clean checkout before submitting (`scripts/resilience_lab.py all` takes about 7 minutes).
- [ ] Explain in the defence: concurrency table (README 7.2), why Option B, why two Alembic heads, the three booking bugs, the crash-window trade-off.

## Who tests what

| Change | Tested by | How |
| --- | --- | --- |
| List projections (Nannapat) | Nannapat, then Poonyaporn and Natthakritta | `tests/test_query_counts.py`; before/after payload and `pg_stats.py` statements per request |
| Write-path trimming (Nannapat) | Natthakritta | `tests/test_concurrency.py` in-process and with `API_URL=http://nginx` |
| Locust profile (Poonyaporn) | Natthakritta | run on a clean stack, thresholds must hold, audit must pass |
| Report numbers (Pandara) | Poonyaporn | every figure reproducible from a command in the README |
| Any merge | everyone | `pytest tests -q` green; `alembic current` shows `0008_drop_redundant_indexes (head)` |

## Final checklist (spec section 4)

- [x] Concurrency control implementation (pessimistic locks + atomic seat counters): `app/services/*`
- [x] Concurrency verification script: `tests/test_concurrency.py`
- [x] Audited queries with projection and zero N+1 in list endpoints: `tests/test_query_counts.py`, README 7.10 (open: the original `GET /orders?user_id=` payload)
- [ ] Automated load test script: `loadtest/locustfile.py` (Poonyaporn)
- [ ] `Performance_Audit_Report.pdf` with charts and metrics (Pandara)
- [ ] Clean zip built with `git archive`; submission channel confirmed with the instructor
