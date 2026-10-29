# CP3 hand-off: what is left, who does it, who checks it

Status as of 2026-10-25: backend by Natthakritta (concurrency control, consistency audit, migrations 0006-0008, `pg_stat_statements`, limits on every wait and 409 / 503 mapping, README 7.9);
order summary endpoints, the trimmed booking path (3 SQL statements), the redirect of the original orders list and 503 handling in `main` by Nannapat;
the k6 load test (`scripts/k6_script.js`) and the audit report with charts are committed (README 7.13). Open items are the ones still marked `[ ]` below.
Design and numbers: README section 7. This file is the checklist. Deadline: end of week 16 (40 % of the team grade).

## Order of work

| Step | Who | Needs | Output | Status |
| --- | --- | --- | --- | --- |
| M1 | Poonyaporn | nothing (backend is ready) | load test script (`scripts/k6_script.js`) + first run | done (README 7.13) |
| M1 | Pandara | README 7.7 baseline | report skeleton, CP3 demo script, packaging check | report committed; demo script and packaging open |
| M1 | Nannapat | README 7.5 and 7.6 | list endpoints as projections + `tests/test_query_counts.py` | done |
| M2 | Poonyaporn | Nannapat's merge | **"after" run** + charts + `pg_stats` CSV | k6 run and charts done; `pg_stats` CSV open (optional) |
| M2 | Natthakritta | Nannapat's merge | review against README 7.5, run full test suite + audit on a clean stack | done for what is merged |
| M3 | Pandara | M2 outputs | `Performance_Audit_Report.pdf`, final README numbers | Markdown report, `Performance_Audit_Report.pdf` and README 7.13 done |
| M4 | everyone | all merged | rehearsal of the defence on a clean checkout | open |

Only M4 and the open items below are left.

## Nannapat — API Engineering & Request Validation (+ web design)

Code that is yours today: `app/api/v1/orders.py`, `app/api/v1/events.py`, `app/services/event_service.py`, `app/schemas/order.py`, `app/schemas/event.py`, `frontend/`.

- [x] **Orders list**: paginated summary (`GET /orders/summary`, `OrderSummary`: id, order_number, status, total_amount, expires_at, created_at, ticket_count, paid_amount), no ORM entities, no `joinedload`; SQL in README 7.6. 3.8 KB instead of 337 KB for a user with 300 orders.
- [x] **Order detail** `GET /orders/{id}`: one SQL statement for 1 or for 50 tickets and payments, no lazy loads (README 7.10).
- [x] **Events list** `GET /api/v1/events` (and `/products`): `EventSummary` with `min_price` and `seats_left` computed in MongoDB, 27 KB for 100 events instead of 124 KB; the detail endpoint is unchanged (done by Natthakritta, README 7.10).
- [x] **Trim round trips**: `POST /orders` runs 3 SQL statements for any number of tickets (it ran 7, 3 of them only serialised the response). `POST /orders/{id}/payments` runs 6 for any number of tickets (the per-ticket UPDATE is gone).
- [x] **Errors**: pool exhaustion / timeout -> 503 + `Retry-After`; collisions map to 409 through `app/core/db_errors.py`. The limits themselves (lock 5 s, statement 15 s, pool 5 s) are now in force, see README 7.9.
- [ ] **Web design**: booking demo page in `frontend/` that handles `409` ("someone took it, pick another seat") and pages through orders. Pair with Pandara.
- [x] **Tests**: `tests/test_query_counts.py` (round trips constant while data grows, payload bounds; done, README 7.10). Add a row to `READ_CASES` for every new list endpoint.
- [x] **Original `GET /orders?user_id=`**: redirects (307) to the paginated summary; `test_original_orders_list_payload_is_bounded` (formerly an `xfail`) passes.

Contract changes others depend on: the summary endpoints are `GET /orders/summary?user_id=&limit=&offset=` and `GET /orders/{id}/summary` (plain JSON list / object). If `GET /events` items change, the k6 and Locust scripts and Pandara's pages need the same change on the same day.
Status codes now: already paid `409`, expired hold `410`, cancelled `409`, payments of an unknown order `404`, busy or unreachable database `503` + `Retry-After` (README section 4 and 7.2). `docs/audit_demo_script.md` still shows `400` for paying twice.

Who checks you: Natthakritta reviews the diff against README 7.5; Poonyaporn's "after" run must show the lower payloads; `pytest tests` must stay green (218 pass + 1 skip in-process today).

## Poonyaporn — Data Engineering

Code that is yours today: `scripts/backfill.py`, `scripts/traffic_gen.py` (CP2), `seed.py`, `scripts/k6_script.js`.

- [x] **Load test** `scripts/k6_script.js`: 50 VUs through nginx, 10 s ramp up + **180 s** + 10 s ramp down, thresholds p95 < 200 ms, p99 < 500 ms, `http_req_failed` < 1 % (k6 exits with a non-zero code when one is broken). It creates its own events (a large zone and a 25-seat contested zone), pays the order it has just created and counts only the expected status as a success. Result and charts: `docs/Performance_Audit_Report.md`, README 7.13, `docs/k6_summary.json`, `docs/perf/`. Re-run it on your own machine before the defence (command in the script header and in README 7.13).
- [x] **Contention scenario**, inside the same script: 10 % of the writes race for the 25-seat zone, exactly 25 must win (`hot_zone_orders_won == 25`); `verify_consistency.py` passes afterwards (13 / 13).
- [ ] **Bigger data** (optional): `seed.py --users 5000 --orders 20000`, plus a few users with hundreds of orders. The official run used the 250 seeded users.
- [ ] **Run protocol** (optional): `pg_stats.py --reset` -> load -> `pg_stats.py --top 15 --requests N --csv docs/perf/pg_top.csv` -> `verify_consistency.py` for the k6 run.
- [x] **Before and after** with the same profile: the Locust numbers are in README 7.7 (`docs/perf/reference_locustfile.py` is the profile; it is also the independent cross-check of the k6 run, command in README 7.6). The k6 script was run on the final code only.
- [ ] **Mongo**: index for the events list (status + date_time) and an `explain()` screenshot/output.
- [ ] Warm up before measuring (first seconds after a restart are slower); keep `POST /users` at 3 % of the mix or less, or report it separately (bcrypt).

Who checks you: Natthakritta re-ran the k6 script on a clean stack (three full runs, the same result each time, audit ALL CHECKS PASSED after the load).

## Pandara — System Documentation & Frontend Design

Code that is yours today: `README.md`, `docs/`, `frontend/`.

- [x] **`docs/Performance_Audit_Report.md`** (Markdown, with charts): objectives and rubric mapping; load test results; concurrency control and its test results; query audit table; methodology and raw k6 output; consistency audit output; risks; reproduction commands. The summary is README 7.13 (the spec accepts a README section).
- [x] **`Performance_Audit_Report.pdf`** (repository root, the name the spec asks for): generated from the Markdown report with `docs/perf/make_report_pdf.py`; run it again after every change of the report so that every number matches README 7.13.
- [ ] **`docs/audit_demo_script.md` for CP3**: live race demo (README 7.3), query-count demo, load run, who answers which question. **Fix the known bugs in it**: step 2.2 (the events endpoint answers `{"items":[{"id":...}]}` and the parameter is `page_size`, not `limit`) and the "paying twice" scenario (it answers `409` now, not `400`).
- [ ] **README upkeep**: keep section 7 statuses and numbers current; every command in the report must work.
- [x] **Packaging**: `CP3_Concurrency_Control_and_Performance_Audit.zip` = `git archive --format=zip --prefix=CP3_Concurrency_Control_and_Performance_Audit/ -o CP3_Concurrency_Control_and_Performance_Audit.zip HEAD` (tracked files only, no `__pycache__`, `.env`, notes, raw k6 points). Still to do: ask the instructor how and where to submit (the spec does not say).
- [ ] **Frontend**: pair with Nannapat on the booking demo page; update pages when the list responses change.

Who checks you: each teammate reads the report section about their part; Natthakritta runs the commands in the report on a clean stack.

## Natthakritta — Backend Lead & Database Architecture

Done: booking-path fixes, `app/core/db_errors.py` (409 / 503), `scripts/verify_consistency.py`, `scripts/pg_stats.py`, migration 0006, tests (`test_concurrency.py` 22, `test_consistency.py` 8, `test_resilience.py` 24, `test_db_errors.py` 16), README section 7.
Also done (7.9): lock / statement / pool / connect / MongoDB timeouts that really reach the engine and client, batched sweeper (one MongoDB update per zone), outbox drained in 1,000-row batches and trimmed after 7 days,
non-blocking flag refresh, nginx fail-over that no longer counts a deliberate 503 as a dead replica, internal sweep endpoint hidden from nginx and Swagger, `scripts/resilience_lab.py`, results in `docs/perf/resilience_results.md`.
Also done (7.11, 7.12): database guards (migration 0007: one completed payment per order, status `CHECK`s, MongoDB seat-inventory validator; a blocked second charge answers 409), index review (`scripts/index_report.py`, migration 0008 drops nine indexes, `docs/perf/index_review.md`), the sweeper query now uses its index (23 ms -> 0.13 ms), nginx `keepalive_timeout 4s` (a rare 502 on POST), tests `test_db_guards.py` 20, `test_index_report.py` 11, `test_index_usage.py` 14.
Rules for the others: tests that corrupt MongoDB on purpose need `bypass_document_validation=True`; a new status value needs a migration first; a new index comes with its query (`index_report.py` and `test_index_usage.py` fail on a redundant one).

- [x] Review Nannapat's and Poonyaporn's changes against README 7.5 (done for what is merged).
- [ ] After every merge: `docker compose exec -e REQUIRE_DB=1 api pytest tests -q`, the live race run (`API_URL=http://nginx`) and `verify_consistency.py`.
- [ ] Re-run everything on a clean checkout before submitting (`scripts/resilience_lab.py all` takes about 7 minutes).
- [ ] Explain in the defence: concurrency table (README 7.2), why Option B, why two Alembic heads, the three booking bugs, the crash-window trade-off. Prepared questions, answers and the demo runbook: [defence_qa.md](defence_qa.md).

## Who tests what

| Change | Tested by | How |
| --- | --- | --- |
| List projections (Nannapat) | Nannapat, then Poonyaporn and Natthakritta | `tests/test_query_counts.py`; before/after payload and `pg_stats.py` statements per request |
| Write-path trimming (Nannapat) | Natthakritta | `tests/test_concurrency.py` in-process and with `API_URL=http://nginx` |
| k6 script (Poonyaporn) | Natthakritta | three full runs on a clean stack: thresholds held, audit passed (README 7.13) |
| Report numbers (Pandara) | Poonyaporn | every figure reproducible from a command in the README |
| Any merge | everyone | `pytest tests -q` green; `alembic current` shows `0008_drop_redundant_indexes (head)` |

## Final checklist (spec section 4)

- [x] Concurrency control implementation (pessimistic locks + atomic seat counters): `app/services/*`
- [x] Concurrency verification script: `tests/test_concurrency.py`
- [x] Audited queries with projection and zero N+1 in list endpoints: `tests/test_query_counts.py`, README 7.10 (the original `GET /orders?user_id=` redirects to the bounded summary)
- [x] Automated load test script: `scripts/k6_script.js` (Poonyaporn's k6 script, rewritten); Locust reference profile in `docs/perf/reference_locustfile.py`
- [x] Performance audit report with charts and metrics: `docs/Performance_Audit_Report.md` + README 7.13 (the spec accepts a README section). The PDF is `Performance_Audit_Report.pdf` (repository root)
- [ ] Clean zip built with `git archive`; submission channel confirmed with the instructor
