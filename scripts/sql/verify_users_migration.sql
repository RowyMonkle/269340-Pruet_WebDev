-- CP2 verification queries for the users.full_name -> first_name + last_name migration.
--
-- Single source of truth: scripts/verify_migration.py parses this file, runs the
-- checks that belong to the chosen --stage and prints PASS / FAIL. Every block is
-- also a plain SELECT, so it can be pasted into psql on its own:
--   docker compose exec postgres_db psql -U dev_user -d main_db
--
-- Each block starts with four marker lines, written as a comment, an at-sign and the key:
--   name    id shown in the report
--   stages  which --stage values run it: before-backfill, after-backfill, after-contract
--   expect  the number the single value n must equal, or "info" (printed, never fails)
--   what    what a non-matching result means (one line)
--   skip_when  optional "flag=value": skip the check while that runtime flag has that value
-- A block ends at the first line that finishes with a semicolon.
--
-- Name rule shared by dual-write, backfill and these queries:
--   first_name = text before the first whitespace, last_name = the rest ('' when
--   there is only one word). A user is "migrated" when first_name AND last_name
--   are NOT NULL.

-- @name total_users
-- @stages before-backfill after-backfill after-contract
-- @expect info
-- @what how many users exist right now (grows while traffic_gen is running)
SELECT count(*) AS n FROM users;

-- @name backfill_remaining
-- @stages before-backfill
-- @expect info
-- @what users the backfill still has to migrate (first_name or last_name is NULL)
SELECT count(*) AS n FROM users WHERE first_name IS NULL OR last_name IS NULL;

-- @name legacy_only_total
-- @stages before-backfill
-- @expect info
-- @what users that have the legacy full_name but no first_name yet
SELECT count(*) AS n FROM users WHERE full_name IS NOT NULL AND first_name IS NULL;

-- @name users_without_any_name
-- @stages before-backfill after-backfill
-- @expect 0
-- @what DATA LOSS - users with neither full_name nor first_name
SELECT count(*) AS n FROM users WHERE full_name IS NULL AND first_name IS NULL;

-- @name legacy_new_disagree
-- @stages before-backfill after-backfill
-- @expect 0
-- @skip_when user_name_write_mode=new
-- @what full_name and first_name+last_name are both set but spell different names (whitespace ignored)
SELECT count(*) AS n
FROM users
WHERE full_name IS NOT NULL
  AND first_name IS NOT NULL
  AND regexp_replace(btrim(full_name), '\s+', ' ', 'g')
      <> regexp_replace(btrim(concat_ws(' ', first_name, last_name)), '\s+', ' ', 'g');

-- @name not_migrated
-- @stages after-backfill after-contract
-- @expect 0
-- @what users still missing first_name or last_name after the backfill
SELECT count(*) AS n FROM users WHERE first_name IS NULL OR last_name IS NULL;

-- @name empty_first_name
-- @stages after-backfill after-contract
-- @expect 0
-- @what users whose first_name is an empty string (every user needs a first name)
SELECT count(*) AS n FROM users WHERE btrim(first_name) = '';

-- @name full_name_column_dropped
-- @stages after-contract
-- @expect 0
-- @what the legacy column must be gone after Phase 5
SELECT count(*) AS n
FROM information_schema.columns
WHERE table_schema = current_schema() AND table_name = 'users' AND column_name = 'full_name';

-- @name new_columns_present
-- @stages after-contract
-- @expect 2
-- @what first_name and last_name must both still exist
SELECT count(*) AS n
FROM information_schema.columns
WHERE table_schema = current_schema() AND table_name = 'users'
  AND column_name IN ('first_name', 'last_name');

-- @name last_name_index_present
-- @stages after-contract
-- @expect 1
-- @what idx_users_last_name (Phase 1) must survive the contract
SELECT count(*) AS n
FROM pg_indexes
WHERE schemaname = current_schema() AND tablename = 'users' AND indexname = 'idx_users_last_name';

-- Snapshot trio, used by --save-snapshot FILE and --compare-snapshot FILE. It proves no
-- existing user was lost or renamed while the migration ran. Only ids up to the saved
-- max_id are compared, so users created by live traffic do not matter.

-- @name snapshot_max_id
-- @stages -
-- @expect info
-- @what highest user id at snapshot time
SELECT coalesce(max(id), 0) AS n FROM users;

-- @name snapshot_checksum
-- @stages -
-- @expect info
-- @what md5 over (id, username) of every user with id <= :max_id
SELECT coalesce(md5(string_agg(id::text || ':' || username, ',' ORDER BY id)), '') AS n
FROM users
WHERE id <= :max_id;

-- @name snapshot_count
-- @stages -
-- @expect info
-- @what number of users with id <= :max_id
SELECT count(*) AS n FROM users WHERE id <= :max_id;
