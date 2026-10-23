from typing import List, Optional
from fastapi import APIRouter, Depends, Header, Path, status
from sqlalchemy.orm import Session
from pymongo.database import Database

from app.core.database import get_db, get_mongo_db
from app.schemas.errors import ErrorResponse, ValidationErrorResponse
from app.schemas.payment import CleanupResponse, PaymentCreate, PaymentResponse
from app.services.payment_service import (
    process_order_payment,
    get_payments_by_order_id,
)
from app.services.order_service import cleanup_expired_seat_holds

router = APIRouter(prefix="/orders", tags=["Payments & Checkout (Relational Billing)"])

# 400 from the global validation handler in main.py (bad path / body / header)
VALIDATION_ERROR_RESPONSE = {
    400: {"model": ValidationErrorResponse, "description": "Invalid input (e.g. amount <= 0, unknown provider)"},
}
# errors any database-backed endpoint can return (global handlers in main.py)
DB_ERROR_RESPONSES = {
    409: {"model": ErrorResponse, "description": "Concurrent update collided, nothing was changed, retry"},
    503: {"model": ErrorResponse, "description": "Database busy (pool exhausted / timeout), retry after Retry-After"},
}


@router.post(
    "/{order_id}/payments",
    response_model=PaymentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Process Order Payment",
    description=(
        "Processes payment for an order currently holding seats. "
        "Transitions order from 'pending' to 'confirmed', validates hold expiry, "
        "and activates held tickets to 'valid'. "
        "Returns HTTP 410 Gone if the seat hold has expired."
    ),
    responses={  # document every error this endpoint can return
        **VALIDATION_ERROR_RESPONSE,
        404: {"model": ErrorResponse, "description": "Order not found"},
        410: {"model": ErrorResponse, "description": "Seat hold expired (or order already expired), the order must be placed again"},
        **DB_ERROR_RESPONSES,
        409: {"model": ErrorResponse, "description": "Order already paid or cancelled, or a concurrent update collided"},  # overrides the generic 409 (must come after **DB_ERROR_RESPONSES)
    },
)
def pay_for_order(
    order_id: int = Path(..., description="PostgreSQL Order ID"),
    payment_in: PaymentCreate = ...,
    db: Session = Depends(get_db),
    mongo_db: Database = Depends(get_mongo_db),
):
    return process_order_payment(
        db=db,
        mongo_db=mongo_db,
        order_id=order_id,
        payment_in=payment_in,
    )


@router.get(
    "/{order_id}/payments",
    response_model=List[PaymentResponse],
    status_code=status.HTTP_200_OK,
    summary="List Order Payments",
    description="Retrieve all payment attempt records associated with an order.",
    responses={
        **VALIDATION_ERROR_RESPONSE,
        404: {"model": ErrorResponse, "description": "Order not found"},  # ADD: service now returns 404 for an unknown order
        **DB_ERROR_RESPONSES,
    },
)
def list_order_payments(
    order_id: int = Path(..., description="PostgreSQL Order ID"),
    db: Session = Depends(get_db),
):
    return get_payments_by_order_id(db=db, order_id=order_id)


@router.post(
    "/cleanup-expired",
    response_model=CleanupResponse,  # typed response instead of an untyped dict
    status_code=status.HTTP_200_OK,
    summary="Reconcile & Clean Expired Seat Holds (Internal / Cron)",
    description=(
        "Internal reconciliation endpoint for background cron or worker. "
        "Finds all pending orders whose 10-minute hold window expired, "
        "cancels tickets, and atomically releases held seats back to MongoDB."
    ),
    responses=DB_ERROR_RESPONSES,
)
def trigger_cleanup_expired_holds(
    x_cron_secret: Optional[str] = Header(
        None,
        alias="X-Cron-Secret",
        description="Optional internal secret for cron/worker authentication",
    ),
    db: Session = Depends(get_db),
    mongo_db: Database = Depends(get_mongo_db),
):
    count = cleanup_expired_seat_holds(db=db, mongo_db=mongo_db)
    return CleanupResponse(
        status="success",
        message=f"Cleaned up {count} expired order seat holds.",
        expired_orders_count=count,
    )