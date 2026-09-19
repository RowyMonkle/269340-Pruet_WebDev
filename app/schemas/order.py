from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, Field, ConfigDict


class TicketItemCreate(BaseModel):
    event_id: str = Field(..., description="MongoDB Event Document ID")
    seat_zone: str = Field(..., min_length=1, max_length=50, description="Target zone e.g. VIP Standing, Front Zone A")
    seat_number: Optional[str] = Field(None, max_length=50, description="Optional seat number (e.g. VIP-12)")
    price: Optional[float] = Field(
        None,
        ge=0.0,
        description="Optional client price (server enforces authoritative MongoDB zone price to prevent tampering)",
    )


class OrderCreate(BaseModel):
    user_id: int = Field(..., gt=0, description="PostgreSQL User ID")
    payment_method: str = Field(default="credit_card", description="Payment method: credit_card, promptpay, etc.")
    items: List[TicketItemCreate] = Field(..., min_length=1, description="List of tickets to purchase")


class TicketResponse(BaseModel):
    id: int
    order_id: int
    event_id: str
    ticket_code: str
    seat_zone: str
    seat_number: Optional[str] = None
    price: float
    status: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class OrderResponse(BaseModel):
    id: int
    order_number: str
    user_id: int
    total_amount: float
    status: str
    payment_method: str
    tickets: List[TicketResponse] = []
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)
