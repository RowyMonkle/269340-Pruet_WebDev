from typing import Optional
from fastapi import APIRouter, Depends, Query, status
from pymongo.database import Database

from app.core.database import get_mongo_db
from app.schemas.common import PaginatedResponse
from app.schemas.event import EventCreate, EventResponse
from app.services.event_service import (
    create_event as create_event_svc,
    get_events_paginated,
    get_event_by_id as get_event_by_id_svc,
)

# Main Events router
router = APIRouter(prefix="/events", tags=["Events & Festivals (MongoDB)"])

# Products catalog alias router
products_router = APIRouter(prefix="/products", tags=["Products / Catalog Alias (MongoDB)"])


@router.get(
    "",
    response_model=PaginatedResponse[EventResponse],
    status_code=status.HTTP_200_OK,
    summary="Fetch Paginated Event Catalog",
    description="Retrieve paginated concert and festival catalog from MongoDB using field projections.",
)
def list_events(
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    category: Optional[str] = Query(None, description="Optional category filter (e.g., Concert, Festival)"),
    status: Optional[str] = Query(None, description="Optional status filter (e.g., upcoming, completed)"),
    mongo_db: Database = Depends(get_mongo_db),
):
    items, total = get_events_paginated(
        mongo_db=mongo_db,
        page=page,
        page_size=page_size,
        category=category,
        status_filter=status,
    )
    total_pages = (total + page_size - 1) // page_size if total > 0 else 0
    return PaginatedResponse[EventResponse](
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


@router.post(
    "",
    response_model=EventResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create Event with Dynamic Attributes",
    description="Insert a new event/festival into MongoDB. Supports dynamic unstructured fields.",
)
def create_event(
    event_in: EventCreate,
    mongo_db: Database = Depends(get_mongo_db),
):
    return create_event_svc(mongo_db, event_in)


@router.get(
    "/{event_id}",
    response_model=EventResponse,
    status_code=status.HTTP_200_OK,
    summary="Get Event Details",
    description="Fetch single event document by MongoDB ObjectId.",
)
def get_event_by_id(
    event_id: str,
    mongo_db: Database = Depends(get_mongo_db),
):
    return get_event_by_id_svc(mongo_db, event_id)


# Mirror endpoints to products_router as catalog alias
products_router.add_api_route(
    "",
    endpoint=list_events,
    methods=["GET"],
    response_model=PaginatedResponse[EventResponse],
    status_code=status.HTTP_200_OK,
    summary="Fetch Paginated Catalog (Alias)",
    description="Catalog alias for GET /api/v1/events.",
)

products_router.add_api_route(
    "",
    endpoint=create_event,
    methods=["POST"],
    response_model=EventResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create Product with Dynamic Attributes (Alias)",
    description="Catalog alias for POST /api/v1/events.",
)

products_router.add_api_route(
    "/{event_id}",
    endpoint=get_event_by_id,
    methods=["GET"],
    response_model=EventResponse,
    status_code=status.HTTP_200_OK,
    summary="Get Product Details (Alias)",
    description="Catalog alias for GET /api/v1/events/{id}.",
)
