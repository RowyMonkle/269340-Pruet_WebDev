from typing import List, Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
#use to create COUNT/SUM in database
from sqlalchemy import func,select 
from sqlalchemy.orm import Session, joinedload
from pymongo.database import Database

from app.core.database import get_db, get_mongo_db
from app.models.sql_models import Order,Payment,Ticket,User
from app.schemas.order import ErrorResponse, OrderCreate, OrderResponse, OrderSummary
from app.services.order_service import (
    create_order_dual_db,
    get_order_by_id as get_order_by_id_svc,
)

router = APIRouter(prefix="/orders", tags=["Orders (Dual-DB Transaction)"])

#status of payment (already paid)
PAID_PAYMENT_STATUS="completed"

#error:every endpoint  --datavase res to Swagger show schema of error
DB_ERROR_RESPONSES = {  
    409: {"model": ErrorResponse, "description": "Concurrent update collided, nothing was changed, retry"},
    503: {"model": ErrorResponse, "description": "Database busy (pool exhausted / timeout), retry after Retry-After"},
}

def _order_summary_query(db: Session):
    # ticket count by subquery, no direct join (joining tickets + payments duplicates rows and breaks SUM)
    ticket_count = (  # FIX: indent 4 spaces (was 5 -> IndentationError)
        select(func.count(Ticket.id))  # FIX: indent 8 spaces (was 10)
        .where(Ticket.order_id == Order.id)
        .correlate(Order)
        .scalar_subquery()
    )
    # only completed payments, COALESCE -> 0 when nothing has been paid yet
    paid_amount = (
        select(func.coalesce(func.sum(Payment.amount), 0))
        .where(Payment.order_id == Order.id, Payment.status == PAID_PAYMENT_STATUS)
        .correlate(Order)
        .scalar_subquery()
    )
    return db.query(  
        Order.id,
        Order.order_number,
        Order.user_id,
        Order.status,
        Order.total_amount,
        Order.expires_at,
        Order.created_at,
        ticket_count.label("ticket_count"),
        paid_amount.label("paid_amount"),
    )


    
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
    #declaer an error, Swagger can see
    responses={  
        404: {"model": ErrorResponse, "description": "User or event not found"},
        **DB_ERROR_RESPONSES,
    },
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

# must be declared BEFORE GET /{order_id}, otherwise "summary" is parsed as order_id -> 422
@router.get(
    "/summary",
    response_model=List[OrderSummary],
    status_code=status.HTTP_200_OK,
    summary="List Order Summaries by User",
    description=(
        "Lightweight order list: ticket_count and paid_amount are computed in PostgreSQL "
        "(COUNT / SUM), no tickets or payments are loaded. Newest first, paginated."
    ),
    responses={404: {"model": ErrorResponse, "description": "User not found"}, **DB_ERROR_RESPONSES},
)
def list_order_summaries(  #summary list for "my orders" page
    user_id: int = Query(..., gt=0, description="User ID to filter orders"),  #  gt=0 -> invalid id gives 422
    limit: int = Query(20, ge=1, le=100, description="Page size"),  # cap page size
    offset: int = Query(0, ge=0, description="Rows to skip"),  # pagination
    db: Session = Depends(get_db),
):
    # unknown user -> 404, known user without orders -> 200 with []
    if db.query(User.id).filter(User.id == user_id).first() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"User {user_id} not found")
    rows = (
        _order_summary_query(db)
        .filter(Order.user_id == user_id)
        .order_by(Order.created_at.desc(), Order.id.desc())  # matches index idx_orders_user_created, stable paging
        .limit(limit)
        .offset(offset)
        .all()
    )
    return [OrderSummary.model_validate(row._mapping) for row in rows]  # Row -> dict -> OrderSummary

@router.get(
    "/{order_id}/summary",
    response_model=OrderSummary,
    status_code=status.HTTP_200_OK,
    summary="Get Order Summary",
    description="Single order summary (ticket_count, paid_amount) without loading tickets or payments.",
    responses={404: {"model": ErrorResponse, "description": "Order not found"}, **DB_ERROR_RESPONSES},
)
def get_order_summary(  # summary of one order
    order_id: int,
    db: Session = Depends(get_db),
):
    row = _order_summary_query(db).filter(Order.id == order_id).first()
    if row is None:  # order not found -> 404 instead of returning null
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Order {order_id} not found")
    return OrderSummary.model_validate(row._mapping)


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
    responses=DB_ERROR_RESPONSES,  #show errors in Swagger
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
