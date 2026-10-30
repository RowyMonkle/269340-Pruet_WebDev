"""MongoDB Document Data Structures and Index Management.

Defines schemas and indexes for:
1. 'events': Catalog of concerts, music events, and festivals with flexible dynamic attributes.
2. 'activity_logs': User telemetry and audit logging for high-volume activity tracking.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional
from pymongo import ASCENDING, DESCENDING, TEXT, IndexModel
from pymongo.database import Database


# Database-side invariant for seat inventory: in every zone 0 <= booked_count <= capacity.
# The booking code already reserves seats with a conditional atomic update; this validator is the last line of
# defence, so even a wrong update (a bug, a script, a hand edit in mongosh) cannot oversell or go negative.
# validationLevel "moderate": documents that were already invalid are not blocked until they are updated.
EVENT_ZONES_VALIDATOR = {
    "$expr": {
        "$allElementsTrue": [
            {
                "$map": {
                    "input": {"$ifNull": ["$zones", []]},
                    "as": "z",
                    "in": {
                        "$and": [
                            {"$isNumber": "$$z.capacity"},
                            {"$isNumber": {"$ifNull": ["$$z.booked_count", 0]}},
                            {"$gte": [{"$ifNull": ["$$z.booked_count", 0]}, 0]},
                            {"$lte": [{"$ifNull": ["$$z.booked_count", 0]}, "$$z.capacity"]},
                        ]
                    },
                }
            }
        ]
    }
}


def apply_event_guards(db: Database) -> None:
    """Install the seat-inventory validator on `events` (idempotent, safe to run on every start)."""
    db.command({
        "collMod": "events",
        "validator": EVENT_ZONES_VALIDATOR,
        "validationLevel": "moderate",
        "validationAction": "error",
    })


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
        # "my events" for an organizer: equality on organizer_id, then the list's date sort, from one index
        IndexModel([("organizer_id", ASCENDING), ("date_time", ASCENDING)], name="idx_events_organizer_date"),
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

    # Seat inventory can never leave 0..capacity, whatever writes to it. A failure here must not stop the API.
    try:
        apply_event_guards(db)
    except Exception as exc:  # noqa: BLE001 - e.g. a user without collMod rights
        import logging
        logging.getLogger("mongo_guards").warning("Could not install the events validator: %s", exc)
