"""Error body schemas, used in responses= so Swagger documents every error shape the API returns."""
from typing import List
from pydantic import BaseModel, Field


class ErrorResponse(BaseModel):
    """Body of every HTTPException and of the global 409 / 503 database handlers in app/main.py."""
    detail: str = Field(..., description="Human-readable reason for the error")


class ValidationErrorItem(BaseModel):
    """One invalid field, as built by validation_exception_handler in app/main.py."""
    field: str = Field(..., description="Location of the bad value, e.g. 'body -> amount'")
    message: str = Field(..., description="What is wrong with it")
    type: str = Field(..., description="Pydantic error type, e.g. 'greater_than'")


class ValidationErrorResponse(BaseModel):
    """HTTP 400 body (main.py turns FastAPI's default 422 into this shape)."""
    error: str = Field(..., description="Always 'Bad Request'")
    message: str = Field(..., description="Summary of the failure")
    details: List[ValidationErrorItem] = Field(..., description="One entry per invalid field")