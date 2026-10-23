from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field, ConfigDict


class PaymentCreate(BaseModel):
    amount: float = Field(..., gt=0, description="Payment amount matching order total")
    provider: str = Field(default="promptpay", pattern=r"^(promptpay|credit_card|stripe|bank_transfer)$")
    provider_tx_id: Optional[str] = Field(None, max_length=128, description="External provider transaction reference")


class PaymentResponse(BaseModel):
    id: int
    order_id: int
    payment_reference: str
    amount: float
    provider: str
    status: str
    provider_tx_id: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)

 # response_model for POST /orders/cleanup-expired (it returned an untyped dict)
class CleanupResponse(BaseModel): 
    status: str = Field(..., description="'success' when the reconciliation pass finished") 
    message: str = Field(..., description="Human-readable summary of the pass")
    expired_orders_count: int = Field(..., ge=0, description="Pending orders whose seat hold expired and were released")  