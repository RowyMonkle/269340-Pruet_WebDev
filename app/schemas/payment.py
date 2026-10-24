from datetime import datetime
from typing import Optional
from pydantic import AliasChoices, BaseModel, Field, ConfigDict


PROVIDER_PATTERN = r"^(promptpay|credit_card|stripe|bank_transfer)$"


class PaymentCreate(BaseModel):
    amount: float = Field(..., gt=0, description="Payment amount matching order total")
    provider: str = Field(default="promptpay", pattern=PROVIDER_PATTERN)
    provider_tx_id: Optional[str] = Field(None, max_length=128, description="External provider transaction reference")


class PaymentCreateByOrder(BaseModel):
    """Body of POST /payments: the same fields as PaymentCreate plus the order id (the nested route takes it from the path).

    `payment_method` is accepted as another name for `provider`, because POST /orders calls it that."""
    order_id: int = Field(..., gt=0, description="PostgreSQL Order ID to pay")
    amount: float = Field(..., gt=0, description="Payment amount matching order total")
    provider: str = Field(
        default="promptpay", pattern=PROVIDER_PATTERN,
        validation_alias=AliasChoices("provider", "payment_method"),
        description="promptpay | credit_card | stripe | bank_transfer (also accepted under the name payment_method)",
    )
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