import time
import psycopg2
from psycopg2.extras import RealDictCursor

DB_CONFIG = {
    "dbname": "main_db",
    "user": "dev_user",
    "password": "dev_password",
    "host": "localhost",
    "port": 5432
}

BATCH_SIZE = 50          # Small bounded batches to prevent table lock contention
THROTTLE_DELAY = 0.05    # 50ms pause between batches to yield I/O to live traffic

def split_full_name(full_name: str):
    if not full_name:
        return "", ""
    parts = full_name.strip().split(" ", 1)
    first_name = parts[0]
    last_name = parts[1] if len(parts) > 1 else ""
    return first_name, last_name

def run_backfill():
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    total_updated = 0
    last_id = 0

    print("=== Starting Phase 3: Non-Blocking Batched Backfill Job ===")

    try:
        while True:
            # Bounded cursor query using indexed PK
            select_query = """
                SELECT id, full_name 
                FROM users 
                WHERE id > %s 
                  AND (first_name IS NULL OR last_name IS NULL)
                ORDER BY id ASC 
                LIMIT %s;
            """
            cursor.execute(select_query, (last_id, BATCH_SIZE))
            batch = cursor.fetchall()

            if not batch:
                print("\nNo more unmigrated user records found.")
                break

            for row in batch:
                uid = row["id"]
                raw_name = row["full_name"]
                first_name, last_name = split_full_name(raw_name)

                update_query = """
                    UPDATE users 
                    SET first_name = %s, last_name = %s 
                    WHERE id = %s;
                """
                cursor.execute(update_query, (first_name, last_name, uid))
                last_id = uid

            # Commit batch transaction to release row locks immediately
            conn.commit()
            total_updated += len(batch)
            print(f"Migrated: {total_updated} rows | Current ID: {last_id}", end="\r")

            time.sleep(THROTTLE_DELAY)

    except Exception as e:
        conn.rollback()
        print(f"\n[Error] Backfill failed: {e}")
        raise e
    finally:
        cursor.close()
        conn.close()
        print(f"\n=== Completed: {total_updated} rows backfilled successfully. ===")

if __name__ == "__main__":
    run_backfill()