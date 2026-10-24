# Resilience test results: one stuck lock or one dead database must not take the API down

Author: Natthakritta (Backend Lead & Database Architecture). Date: 2026-10-23. Part of CP3 (README section 7.9).

## 1. Question and answer

**Question.** When one thing in the system is stuck or down, does the rest of the API keep answering, and does the failing part
answer fast and honestly (409 / 503 with `Retry-After`) instead of hanging or crashing (hang, 502, 500)?

**Answer in one table** (same scripts, same hardware, same data; "before" is the main branch at `2022ed9`, "after" is this change):

| # | Failure injected | Measure | Before | After |
| ---: | --- | --- | --- | --- |
| 1 | One order row stays locked 25 s, 120 clients try to pay it | unrelated endpoint `GET /users/1` | 2 errors (two 12 s timeouts), p99 12,011 ms | **0 errors**, slowest answer 5.1 s, p99 36 ms |
| | | the 120 payments | 119 x 409 + 1 x 201, slowest 24.7 s | 120 x 409 ("retry"), slowest 19.9 s |
| 2 | A replica has one connection and it is stuck | next request | `200` after 9.7 s (waited for the stuck query) | **`503` + `Retry-After: 2` after 2.0 s**; the stuck request itself gets `409` after 5.0 s |
| 3 | MongoDB stopped 25 s, 50 users active | requests that failed | **1,568 of 8,629 (18.2 %)**, on all 9 endpoints, mostly nginx `502` | 805 of 8,298 (9.7 %), only on the 3 endpoints that need MongoDB, all `503` + `Retry-After` |
| | | PostgreSQL-only endpoints (users, orders, payments) | **626 of 3,866 failed (16.2 %)** | **0 failed** |
| 4 | MongoDB stopped 25 s, one probe each | PostgreSQL-only endpoint | 0 errors | 0 errors |
| | | MongoDB-only endpoint | 5 x `500`, slowest 5.5 s | 27 x `503` + `Retry-After`, slowest 1.5 s |
| 5 | PostgreSQL stopped 25 s | PostgreSQL-only endpoint | 13 errors (`500` and a 12 s timeout) | 22 fast errors, all `503` + `Retry-After`, slowest 7.9 s |
| | | MongoDB-only endpoint | 0 errors | 0 errors |
| 6 | 6,000 expired holds (12,000 tickets) swept | `GET /users/1` during the sweep | 0 errors, max 179 ms | 0 errors, max 45 ms; all 6,000 expired after 13 s; `booked_count` back to 0 |
| 7 | 50 users, 180 s (the CP3 load profile) | requests / failures / p95 / p99 | 27,223 / 0 / 120 ms / 140 ms | 27,236 / 0 / 120 ms / 140 ms (**no regression**) |
| | | outbox rows still `pending` right after the run | **8,940** (2,852 processed) | **89** (11,835 processed) |
| | | `verify_consistency.py` after the run | ALL CHECKS PASSED | ALL CHECKS PASSED |

Earlier, on commit `ec2a165` (before the background worker ran in a thread), the same sweep froze a replica: one probe request
timed out at 12 s and another waited 5.5 s. That is already fixed in "before" above.

The CP3 load targets (>= 50 users, 3 min, p95 < 200 ms, p99 < 500 ms, errors < 1 %) are met with margin in both columns:
the protection costs nothing when everything is healthy.

## 2. What changed (all in the backend; files in the last section)

| Problem found by measuring | Change |
| --- | --- |
| A waiting statement had no limit; a stuck lock filled the thread pool and the connection pool until unrelated endpoints stopped | every connection carries `lock_timeout` 5 s and `statement_timeout` 15 s; the pool gives up after `pool_timeout` 5 s (the setting existed but was never passed to `create_engine`: the pool still waited 30 s); opening a connection gives up after 2 s |
| A lock wait that expires is a normal event, not a bug | SQLSTATE `55P03` -> `409` (retry); `57014` statement timeout, pool timeout and an unreachable PostgreSQL -> `503` + `Retry-After` |
| MongoDB outage held a worker thread 5 s per request | server selection timeout 1 s; any `ConnectionFailure` -> `503` + `Retry-After`; a booking that fails halfway gives back the zones it already reserved |
| nginx counted the API's deliberate `503` as "replica failed" and took both healthy replicas out of rotation (`502` for everything) | `proxy_next_upstream error timeout` (no `http_503`), `max_fails=3`, connect timeout 1 s |
| The sweeper loaded every expired hold in one query and called MongoDB once per ticket | batches of 500 holds per transaction, bulk SQL, ONE MongoDB update per (event, zone), at most 20 batches per tick |
| The outbox worker handled 50 rows per 15 s while about 1,350 rows per minute were created | one `UPDATE ... SKIP LOCKED` per 1,000 rows; processed rows older than 7 days are deleted |
| Feature-flag refresh ran under a global lock | one thread refreshes, everybody else keeps using the previous values |
| The sweep endpoint accepted anyone | not routed by nginx (`404`), hidden from Swagger |
| `docker stop` could kill a request that uvicorn was still draining | `stop_grace_period: 20s` |
| A rolling deploy had no migration step | `scripts/rolling_deploy.sh` applies pending (expand-only) migrations first |

A first version of this change made scenario 3 **worse** (37 % failures instead of 18 %): the new `503` answers tripped nginx's
failure counting, which is the nginx row above. The same experiment caught it, and `test_nginx_does_not_treat_a_deliberate_503_as_a_dead_replica`
now guards it. This is why the numbers above come from re-running the whole set on the final code.

## 3. Method

* **Stack:** `docker compose` with nginx + 2 API replicas (uvicorn, 1 process each) + PostgreSQL 15 + MongoDB 6, Docker Desktop on a
  16-CPU / 15 GiB host. Seeded with `seed.py` (250 users, 500 orders, 200 events). Each set started from a fresh volume.
* **Before / after:** "before" is built from `git archive 2022ed9` (its own `docker-compose.yml`, `nginx.conf` and image), "after" from the
  working tree. The measurements are the same scripts against both. No other load runs at the same time.
* **Probe:** one client sending about 20 `GET`/s to one endpoint. A request over 500 ms counts as a stall; a non-200 answer counts as an error.
  Probe scenarios are in `scripts/resilience_lab.py`; load scenarios use `docs/perf/reference_locustfile.py` (50 users, ramp 10/s, mixed
  read/write through nginx).
* **Failure injection:** a real `SELECT ... FOR UPDATE` held by another session (`pg_sleep`), `docker stop` / `docker start` of the database
  containers, a replica started with `POSTGRES_POOL_SIZE=1`, rows inserted directly for the sweep.
* **Repeatability:** scenario 1 was run four times on the "before" code with the same outcome (one or two 12 s timeouts each time); the whole set was
  run once per code version, apart from a first "after" run that exposed the nginx problem (not used in the table).
* **Not faked:** the tests in `tests/test_resilience.py` use the real mechanisms (a held row lock, an exhausted pool of size 1, an address that
  does not answer). Only two tests replace a component, and say so in their docstring.

## 4. Reading the results honestly

* Row 1: the unrelated endpoint still has two answers that took about 5 s, because while 120 payments wait for the lock they hold all
  worker threads for up to `lock_timeout`. It is bounded by the limit now; before, it was bounded only by the client's 12 s timeout, and
  nginx's 30 s. Lowering `POSTGRES_LOCK_TIMEOUT_MS` shortens it further, at the price of more `409` under heavy legitimate contention.
* Row 3: 9.7 % of requests still fail because they need MongoDB; the point is that they fail fast and honestly and that nothing else does.
  Their p95 is about 1 s (the 1 s MongoDB selection timeout).
* Row 5: during a PostgreSQL outage the requests that need PostgreSQL cannot succeed. Slowest answer 7.9 s comes from pool connections being
  re-tested one after another; a circuit breaker would cut it, we did not build one.
* Row 6: "13 s" is bounded by the worker's 15 s polling interval, not by the work (6,000 holds in 12 batches).
* Not covered: nginx, PostgreSQL and MongoDB are single instances (their loss is an outage by design); the replicas' debug ports 8001/8002 are
  published on the host and still reach the internal sweep endpoint; a crash between reserving seats in MongoDB and committing in PostgreSQL
  leaves a seat reserved with no ticket (never an oversell): `scripts/verify_consistency.py --repair`.

## 5. Reproduce

```bash
# 1. a disposable stack (names, ports and volumes are separate from the dev stack)
export STACK_PREFIX=rehearsal API_IMAGE=rehearsal_api:local NGINX_PORT=18000 API_1_PORT=18001 API_2_PORT=18002 \
       MONGO_HOST_PORT=27018 POSTGRES_PORT=5433 PG_VOLUME=rehearsal_pg MONGO_VOLUME=rehearsal_mongo
docker compose -p rehearsal up --build -d --wait
docker compose -p rehearsal exec api python seed.py

# 2. probe scenarios (rows 1, 2, 3 probes, 4, 5, 6): about 7 minutes
python scripts/resilience_lab.py --prefix rehearsal --url http://localhost:18000 \
       --network rehearsal_default --image rehearsal_api:local all

# 3. load profile (row 7), 3 minutes. For row 3 use -t 75s instead, then 15 s after it starts run
#    docker stop rehearsal_mongo   and 25 s later   docker start rehearsal_mongo
mkdir -p docs/perf/out
docker run --rm --network rehearsal_default -v "$PWD/docs/perf:/mnt/locust" locustio/locust \
    -f /mnt/locust/reference_locustfile.py --headless -u 50 -r 10 -t 180s --host http://nginx --csv /mnt/locust/out/reference
docker compose -p rehearsal exec api python scripts/pg_stats.py --top 5
docker compose -p rehearsal exec api python scripts/verify_consistency.py
#    outbox backlog:
docker compose -p rehearsal exec postgres_db psql -U dev_user -d main_db -c "select status, count(*) from outbox_events group by 1"

# 4. the "before" column: build the old commit in its own folder and use the same commands
git archive 2022ed9 | tar -x -C /tmp/before && cd /tmp/before && docker compose -p rehearsal up --build -d --wait

# 5. tests
docker compose -p rehearsal exec -e REQUIRE_DB=1 api pytest tests -q                                 # 130 pass, 1 skipped (live only)
docker compose -p rehearsal exec -e API_URL=http://nginx -e REQUIRE_DB=1 api pytest tests/test_concurrency.py tests/test_consistency.py tests/test_resilience.py -q
docker compose -p rehearsal down -v
```

`scripts/resilience_lab.py` stops database containers and inserts test rows: use it only on a disposable stack.

## 6. Tests that guard each behaviour

| Behaviour | Test (`tests/test_resilience.py` unless noted) |
| --- | --- |
| limits really reach every connection | `test_pool_timeout_setting_reaches_the_engine`, `test_every_pooled_connection_carries_the_lock_and_statement_limits`, `test_default_limits_are_the_agreed_ones` |
| stuck lock -> 409 after `lock_timeout` | `test_a_stuck_row_lock_answers_409_after_lock_timeout_and_frees_the_request` |
| slow statement -> 503 | `test_a_slow_statement_is_cancelled_and_answers_503_with_retry_after` |
| pool exhausted -> 503 | `test_an_exhausted_pool_answers_503_after_pool_timeout_not_30_seconds` |
| unrelated rows keep working | `test_requests_that_need_no_lock_keep_working_while_another_row_is_locked` |
| MongoDB down -> 503, PostgreSQL endpoints fine | `test_mongo_down_answers_503_for_mongo_endpoints_and_postgres_endpoints_still_work` |
| seats returned when MongoDB fails halfway | `test_mongo_failing_halfway_through_a_booking_returns_the_seats_already_taken` |
| sweeper: batches, one update per zone, skip locked | `test_a_flash_sale_of_expired_holds_is_released_in_batches_and_counted_once`, `test_holds_of_two_zones_are_released_with_one_update_per_zone`, `test_an_order_being_paid_is_skipped_by_the_sweeper_not_waited_on` |
| outbox keeps up and is trimmed | `test_the_outbox_is_drained_in_big_batches`, `test_old_processed_outbox_rows_are_deleted_and_recent_ones_kept` |
| worker never blocks the loop; isolated steps | `test_the_worker_never_blocks_the_event_loop`, `test_one_failing_worker_step_does_not_stop_the_others` |
| flag refresh does not make others wait | `test_a_slow_flag_refresh_does_not_make_other_requests_wait` |
| gateway | `test_nginx_does_not_expose_the_internal_sweep_endpoint` (live), `test_nginx_config_blocks_the_sweep_endpoint_and_tolerates_a_slow_answer`, `test_nginx_does_not_treat_a_deliberate_503_as_a_dead_replica` |
| status mapping | `tests/test_db_errors.py::test_classify_maps_each_failure_to_the_right_status` |

## 7. Files

`app/core/{config,database,db_errors,flags}.py`, `app/main.py`, `app/services/{order_service,outbox_worker}.py`, `app/api/v1/payments.py`,
`nginx/nginx.conf`, `docker-compose.yml`, `.env.example`, `scripts/{resilience_lab.py,rolling_deploy.sh,verify_consistency.py}`, `seed.py`,
`tests/{test_resilience.py,test_db_errors.py,test_concurrency.py,support.py}`, `docs/perf/reference_locustfile.py`.
