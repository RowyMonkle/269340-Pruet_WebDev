# CP2 Migration Scenario: split `users.full_name` into `first_name` + `last_name`

Owner: Natthakritta (scenario, Phase 1, Phase 5, verification). Engine: PostgreSQL only.

## Why this scenario
`users.full_name` is a single free-text column. Search and sorting by surname, and
formal salutations on tickets, need separate fields. The change touches the table every
signup and login uses, so it must happen while the API keeps taking writes.

## Naming rule (shared by dual-write, backfill and verification)
- `first_name` = text before the first whitespace.
- `last_name` = everything after it, or `''` (empty string, never NULL) for one-word names.
- A user is **migrated** when `first_name IS NOT NULL AND last_name IS NOT NULL`.
- Reference implementation: `split_full_name()` in `app/services/user_service.py`.

## Phases, flags and exit conditions

| Phase | Action | Flags (`user_name_write_mode` / `user_name_read_source`) | Exit condition | Rollback |
|---|---|---|---|---|
| 1 Expand | Alembic `0004_expand_user_names`: add nullable `first_name`, `last_name`, drop NOT NULL on `full_name`, `CREATE INDEX CONCURRENTLY idx_users_last_name` | `legacy` / `legacy` | `alembic current` lists `0008_drop_redundant_indexes` (its ancestor is 0004); old API still works | `alembic downgrade 0003_feature_flags` |
| 2 Dual write | API writes both representations | `dual` / `legacy` | every replica reports `dual`; new signups have all three columns | set write mode back to `legacy` |
| 3 Backfill | `scripts/backfill.py` fills old rows in batches of 50 to 100 with a checkpoint | `dual` / `legacy` | `verify_migration.py --stage after-backfill` passes | stop the job; re-run resumes from checkpoint |
| 4 Switch read | API reads `first_name` / `last_name` | `dual` / `new`, then `new` / `new` | 0 errors under `traffic_gen.py`; legacy and new responses agree | set read source back to `legacy` (needs write mode `dual`) |
| 5 Contract | Alembic `0005_contract_user_full_name` drops `full_name` | `new` / `new` | `verify_migration.py --stage after-contract` passes | `alembic downgrade 0005_contract_user_full_name@-1` (refills `full_name`, keeps the CP3 index) |

Flags change at runtime, no restart: `PUT /api/v1/admin/flags/{key}` with header
`X-Admin-Token` and body `{"value": "dual"}`. Replicas pick it up within
`FLAG_CACHE_SECONDS` (2 s).

## Rules that keep it zero-downtime
- `scripts/migrate.py` (run by `docker compose up`) stops at `0008_drop_redundant_indexes` (CP3), which hangs off
  `0004_expand_user_names` as a second head: it applies the expand step but never the contract.
  The contract revision is never applied by startup. Apply it on purpose:
  `docker compose exec api alembic upgrade 0005_contract_user_full_name`.
  `alembic upgrade head` refuses to run (two heads), so nobody can drop `full_name` by accident.
- `0005` refuses to run (drops nothing) unless both flags are `new` and no user is missing
  `first_name` or `last_name`.
- Deploy order for Phase 5: first ship an API build that no longer reads or writes
  `full_name` (ORM model, `load_only` lists, schemas), then run `0005`. Dropping the
  column under a replica that still selects it makes every user request fail.
- DDL takes short locks, so every statement sets `lock_timeout = '3s'` and gives up
  instead of queueing behind a long transaction. The index is built `CONCURRENTLY`.

## Verification
```bash
# baseline + snapshot of existing users (before backfill)
docker compose exec api python scripts/verify_migration.py --stage before-backfill --save-snapshot /tmp/snap.json
# after the backfill: nothing missing, nothing lost, legacy and new agree
docker compose exec api python scripts/verify_migration.py --stage after-backfill --compare-snapshot /tmp/snap.json
# after the contract
docker compose exec api python scripts/verify_migration.py --stage after-contract --compare-snapshot /tmp/snap.json
```
The queries live in `scripts/sql/verify_users_migration.sql` (plain SELECTs, also usable
in `psql`). The runner exits with code 1 if any check fails.
