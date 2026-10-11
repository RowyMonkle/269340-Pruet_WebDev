# CLAUDE.md – 269340 Ticket Booking (CP2: Zero-Downtime Migration)

Working branch: `natthakritta` (Natthakritta Aoktan, Backend Lead & DB Architecture).
Stack: FastAPI + PostgreSQL 15 (SQLAlchemy/Alembic) + MongoDB 6, nginx in front of two API replicas.
Commit style: short plain one-line messages, no `fix:`/`feat:` prefixes.

## CP2 goal (spec: Checkpoint_2_Zero_Downtime_Migration.pdf, 35% of team grade)
Live demo of an Expand-Contract migration under >= 20 req/s of POST + GET traffic with
0 HTTP 5xx, 0 data loss / missing-field errors, p99 < 500 ms.
Rubric: Architecture 35 / Traffic test 35 / Backfill safety 15 / Code hygiene + Q&A 15.
Scenario: `users.full_name` -> `first_name` + `last_name` (see `docs/cp2_migration_scenario.md`).

## Roles
| Member | Role | CP2 tasks |
|---|---|---|
| Natthakritta | Backend Lead & DB | scenario, Phase 1 DDL, Phase 5 DDL, verification queries |
| Nannapat | API & validation | Phase 2 dual-write (POST **and PUT**), DTO compat, Phase 4 switch read, error codes, p99 < 500 ms |
| Poonyaporn | Data engineering | Phase 3 `backfill.py` (cursor batches 50-100 + checkpoint), `traffic_gen.py`, dry-run load tests |
| Pandara | Docs & frontend | README 5-phase + diagram + run commands, test client, 3-min fallback video |

## Phase status (update on every sync)
| Phase | Owner | Status | Where |
|---|---|---|---|
| 1 Expand | Natthakritta | DONE, tested | `alembic/versions/0004_expand_user_names.py` |
| 2 Dual write | Nannapat | DONE (POST + PUT, fixed by Natthakritta's session, awaiting Nannapat review) | `app/services/user_service.py`, `app/api/v1/users.py` |
| 3 Backfill | Poonyaporn | Pushed (`scripts/backfill.py`), **has bugs** (see bugs 9-10) | `scripts/backfill.py` |
| 4 Switch read | Nannapat | DONE – `user_name_read_source` now drives GET/list (`build_user_response`) | `app/services/user_service.py` |
| 5 Contract | Natthakritta | DONE, tested (guards, rollback) | `alembic/versions/0005_contract_user_full_name.py` |
| Verification queries | Natthakritta | DONE, tested | `scripts/sql/verify_users_migration.sql`, `scripts/verify_migration.py` |
| Traffic generator | Poonyaporn | Pushed (`scripts/traffic_gen.py`), **every POST returns 400** (bug 9) | `scripts/traffic_gen.py` |
| README / test client | Pandara | client done; README stale | `README.md`, `frontend/` |
| 3-min video | Pandara | not done | – |

## Runbook (inside the stack: `docker compose up --build -d`)
```bash
docker compose exec api alembic current                   # 0004_expand_user_names
docker compose exec api python seed.py
docker compose exec api python scripts/verify_migration.py --stage before-backfill --save-snapshot /tmp/snap.json
# flip flags (Phase 2): write mode dual
curl -X PUT http://localhost:8000/api/v1/admin/flags/user_name_write_mode -H 'X-Admin-Token: dev-admin-token' -H 'Content-Type: application/json' -d '{"value":"dual"}'
# Phase 3: backfill (Poonyaporn) -> then
docker compose exec api python scripts/verify_migration.py --stage after-backfill --compare-snapshot /tmp/snap.json
# Phase 4: read_source=new, then write_mode=new
docker compose exec api alembic upgrade 0005_contract_user_full_name   # refuses unless flags are new/new and 0 users unmigrated
docker compose exec api python scripts/verify_migration.py --stage after-contract --compare-snapshot /tmp/snap.json
```
- `scripts/migrate.py` (run by compose) stops at `MIGRATE_TARGET`, default `0004_expand_user_names`.
  **Never use `alembic upgrade head` in the demo** – it would apply the contract.
- Do not `docker compose down -v` on a stack with data you want; volumes are named `ticket_postgres_data` / `ticket_mongo_data`.
- Name rule: `first_name` = text before first whitespace, `last_name` = rest or `''` (never NULL). Migrated = both NOT NULL.

## Last sync with teammates
Update this table every time anything is fetched / pulled / merged from another member's branch.

| Date | Source | Commit | What came in / notes |
|---|---|---|---|
| 2026-10-11 | `origin/famenan` (Nannapat), merged via PR #9 | 90d0507 | `/users` API + `user_service.py` dual-write by `user_name_write_mode`; **requirements.txt switched to psycopg v3 (broke driver, see bug 1)** |
| 2026-10-11 | `origin/main` (Pandara PR #8 + PR #10) | 38649b5 | README migration doc + schema-aware test client (`frontend/`), `/test-client` mount; `model_validator` on `UserCreate` |
| 2026-10-11 (later) | `origin/main` (Poonyaporn PR #11) | 020434e | Added `scripts/backfill.py` + `scripts/traffic_gen.py`. Merged into `natthakritta` by the user (merge commit 1abc99c). |
| 2026-10-11 | `git fetch --all --prune` | – | (earlier fetch) No new commits. `origin/poonyaporn` still at 1b3bbfd (no backfill/traffic_gen). Old `origin/Nannapat`, `origin/Pandara` branches were deleted upstream |

## Known bugs / issues (short notes; remove when fixed)
1. FIXED: `requirements.txt` had `psycopg[binary]` (v3) while URLs use `postgresql+psycopg2://` -> rebuild would crash migrate + API. Reverted to `psycopg2-binary`. (Introduced by 4aba48a.)
2. FIXED: `last_name` was stored as NULL when only `first_name` was sent. `resolve_names()` now stores `''`.
3. FIXED: no `PUT /users/{id}`. Added (dual-write rules, 400/404/409). `role` removed from `UserUpdate` so the public PUT cannot promote an account.
4. FIXED: `user_name_read_source` was never read. `build_user_response()` now switches GET and list.
5. FIXED: after `0005`, the API must not mention `full_name`. `User.full_name` is now `deferred`, create uses a Core INSERT naming only written columns, and the list `count()` no longer selects every column. Tested with the column physically dropped: POST/GET/list/PUT all work in new/new mode.
6. FIXED: `UserResponse` re-validated stored rows (`EmailStr`, length caps) -> could 500 on GET. Now plain `str` fields.
7. FIXED: two racing identical POSTs hit the unique index and returned 500. `IntegrityError` now becomes 409.
8. OPEN (Pandara): README says "API accepts full_name only" / "service does not consume flags" (stale). Also still says "Checkpoint 1".
9. OPEN (Poonyaporn) `scripts/traffic_gen.py`: POST payload has no `username` -> **100% of POSTs get 400**, so no write traffic is exercised. Also reports p95 not p99, no PUT, no exit code, no success rate, GET /users/1..300 produces 404s counted as errors.
10. OPEN (Poonyaporn) `scripts/backfill.py`: (a) host hard-coded to `localhost:5432`, fails inside the container (use `settings`); (b) selects `first_name IS NULL OR last_name IS NULL`, and for a row with `first_name` set but `full_name` NULL it overwrites with `('','')` (data loss in new mode); (c) UPDATE has no `AND first_name IS NULL` guard, so it can overwrite a concurrent PUT; (d) `split(" ", 1)` differs from `split_full_name()` (double spaces leave a leading space in `last_name`); (e) does not use `backfill_checkpoints`, so it is not resumable (spec: checkpointing).
11. MINOR: `seed.py` creates only 250 users with `full_name` only -> backfill shows ~5 batches; seed more for a convincing demo. `app/main.py:19` does not import `FeatureFlag`/`BackfillCheckpoint` (harmless).
12. NOTE: in write mode `new`, `PUT` leaves the old `full_name` stale (it cannot be touched once dropped), so `legacy_new_disagree` is only meaningful in stages before-backfill/after-backfill (dual mode).
13. NOTE: nginx retries POST on 502/503 only; a request that timed out mid-flight can leave a created user. Traffic gen should use unique usernames per request.

## Conventions for Claude in this repo
- After any fetch/pull/merge of another member's work: update "Last sync" and "Phase status" above.
- Each reply ends with a short "Bug notes" list of any bug or syntax error noticed.
- Tests: `docker compose exec api pytest tests/ -v` (DB tests skip if databases are down).
