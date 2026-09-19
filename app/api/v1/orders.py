from typing import List, Optional
from fastapi import APIRouter, Depends, Header, Query, status
from sqlalchemy.orm import Session, joinedload
from pymongo.database import Database

from app.core.database import get_db, get_mongo_db
from app.models.sql_models import Order
from app.schemas.order import OrderCreate, OrderResponse
from app.services.order_service import (
    create_order_dual_db,
    get_order_by_id as get_order_by_id_svc,
)

router = APIRouter(prefix="/orders", tags=["Orders (Dual-DB Transaction)"])


@router.post(
    "",
    response_model=OrderResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create Transaction Order (Dual-DB)",
    description=(
        "Executes a cross-database transaction: validates User in PostgreSQL, "
        "checks and atomically increments seat capacity in MongoDB, creates Order with "
        "a 10-minute seat hold window and Tickets with 'held' status in PostgreSQL, "
        "supports Idempotency-Key header, and records outbox and telemetry logs."
    ),
)
def create_order(
    order_in: OrderCreate,
    idempotency_key: Optional[str] = Header(
        None,
        alias="Idempotency-Key",
        description="Optional unique idempotency token to prevent duplicate order placement on retries",
    ),
    db: Session = Depends(get_db),
    mongo_db: Database = Depends(get_mongo_db),
):
    return create_order_dual_db(
        db=db,
        mongo_db=mongo_db,
        order_in=order_in,
        idempotency_key=idempotency_key,
    )


@router.get(
    "/{order_id}",
    response_model=OrderResponse,
    status_code=status.HTTP_200_OK,
    summary="Get Order Details",
    description="Fetch order with associated tickets and payment history (joinedload eliminates N+1 queries).",
)
def get_order_by_id(
    order_id: int,
    db: Session = Depends(get_db),
):
    return get_order_by_id_svc(db, order_id)


@router.get(
    "",
    response_model=List[OrderResponse],
    status_code=status.HTTP_200_OK,
    summary="List Orders by User",
    description="Fetch orders for a specific user ID with ticket and payment details.",
)
def list_orders_by_user(
    user_id: int = Query(..., description="User ID to filter orders"),
    db: Session = Depends(get_db),
):
    orders = (
        db.query(Order)
        .options(joinedload(Order.tickets), joinedload(Order.payments))
        .filter(Order.user_id == user_id)
        .order_by(Order.created_at.desc())
        .all()
    )
    return orders
