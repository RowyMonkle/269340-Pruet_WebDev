"""MongoDB Document Data Structures and Index Management.

Defines schemas and indexes for:
1. 'events': Catalog of concerts, music events, and festivals with flexible dynamic attributes.
2. 'activity_logs': User telemetry and audit logging for high-volume activity tracking.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional
from pymongo import ASCENDING, DESCENDING, TEXT, IndexModel
from pymongo.database import Database


def init_mongo_indexes(db: Database) -> None:
    """Ensure indexes are created for MongoDB collections for optimal query performance."""
    # Indexes for 'events' collection
    event_indexes = [
        IndexModel([("date_time", ASCENDING)], name="idx_events_date_time"),
        IndexModel([("status", ASCENDING)], name="idx_events_status"),
        IndexModel([("category", ASCENDING)], name="idx_events_category"),
        IndexModel(
            [
                ("title", TEXT),
                ("artist.name", TEXT),
                ("venue.city", TEXT),
                ("tags", TEXT),
            ],
            name="idx_events_text_search",
        ),
        IndexModel([("created_at", DESCENDING)], name="idx_events_created_at"),
    ]
    db["events"].create_indexes(event_indexes)

    # Indexes for 'activity_logs' telemetry collection
    log_indexes = [
        IndexModel([("user_id", ASCENDING)], name="idx_logs_user_id"),
        IndexModel([("action", ASCENDING)], name="idx_logs_action"),
        IndexModel([("timestamp", DESCENDING)], name="idx_logs_timestamp"),
        IndexModel([("resource_type", ASCENDING), ("resource_id", ASCENDING)], name="idx_logs_resource"),
    ]
    db["activity_logs"].create_indexes(log_indexes)
