from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from bson import ObjectId
from bson.errors import InvalidId
from fastapi import HTTPException, status
from pymongo.database import Database

from app.schemas.event import EventCreate, EventResponse, ZoneSchema, ArtistSchema, VenueSchema


# Explicit MongoDB Projection Document (eliminates over-fetching unneeded fields)
EVENT_PROJECTION = {
    "_id": 1,
    "title": 1,
    "category": 1,
    "artist": 1,
    "venue": 1,
    "date_time": 1,
    "zones": 1,
    "tags": 1,
    "dynamic_attributes": 1,
    "status": 1,
    "created_at": 1,
    "updated_at": 1,
}


def _format_event_doc(doc: Dict[str, Any]) -> EventResponse:
    """Format raw MongoDB document into EventResponse schema."""
    doc_id = str(doc["_id"])
    return EventResponse(
        id=doc_id,
        title=doc.get("title", ""),
        category=doc.get("category", "Concert"),
        artist=ArtistSchema(**doc.get("artist", {})),
        venue=VenueSchema(**doc.get("venue", {})),
        date_time=doc.get("date_time"),
        zones=[ZoneSchema(**z) for z in doc.get("zones", [])],
        tags=doc.get("tags", []),
        dynamic_attributes=doc.get("dynamic_attributes", {}),
        status=doc.get("status", "upcoming"),
        created_at=doc.get("created_at"),
        updated_at=doc.get("updated_at"),
    )


def get_events_paginated(
    mongo_db: Database,
    page: int = 1,
    page_size: int = 20,
    category: Optional[str] = None,
    status_filter: Optional[str] = None,
) -> Tuple[List[EventResponse], int]:
    """Fetch paginated event catalog using database projection."""
    query: Dict[str, Any] = {}
    if category:
        query["category"] = category
    if status_filter:
        query["status"] = status_filter

    events_collection = mongo_db["events"]
    total = events_collection.count_documents(query)

    skip = (page - 1) * page_size
    cursor = (
        events_collection.find(query, projection=EVENT_PROJECTION)
        .sort("date_time", 1)
        .skip(skip)
        .limit(page_size)
    )

    items = [_format_event_doc(doc) for doc in cursor]
    return items, total


def get_event_by_id(mongo_db: Database, event_id: str) -> EventResponse:
    """Fetch single event by its MongoDB ObjectId using projection."""
    try:
        oid = ObjectId(event_id)
    except InvalidId:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid event ID format: {event_id}",
        )

    doc = mongo_db["events"].find_one({"_id": oid}, projection=EVENT_PROJECTION)
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Event with ID '{event_id}' not found",
        )
    return _format_event_doc(doc)


def create_event(mongo_db: Database, event_in: EventCreate) -> EventResponse:
    """Insert new event with dynamic attributes into MongoDB."""
    now = datetime.now(timezone.utc)
    event_dict = event_in.model_dump()
    event_dict["status"] = "upcoming"
    event_dict["created_at"] = now
    event_dict["updated_at"] = now

    result = mongo_db["events"].insert_one(event_dict)
    event_dict["_id"] = result.inserted_id

    # Log action to activity_logs collection
    mongo_db["activity_logs"].insert_one(
        {
            "action": "create_event",
            "resource_type": "event",
            "resource_id": str(result.inserted_id),
            "metadata": {
                "title": event_in.title,
                "zones_count": len(event_in.zones),
            },
            "timestamp": now,
        }
    )

    return _format_event_doc(event_dict)
