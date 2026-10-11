from datetime import datetime
from typing import Optional
from pydantic import BaseModel, EmailStr, Field, ConfigDict, model_validator


class UserBase(BaseModel):
    email: EmailStr
    username: str = Field(..., min_length=3, max_length=50, pattern=r"^[a-zA-Z0-9_-]+$")
    full_name: Optional[str] = Field(None, max_length=150)
    first_name: Optional[str] = Field(None, max_length=100)
    last_name: Optional[str] = Field(None, max_length=100)
    role: str = Field(default="fan", pattern=r"^(fan|organizer|admin)$")


class UserCreate(UserBase):
    password: str = Field(..., min_length=6, max_length=100, description="Plaintext password to be hashed")

    @model_validator(mode="after")
    def validate_name_presence(self):
        has_full = bool(self.full_name and self.full_name.strip())
        has_first = bool(self.first_name and self.first_name.strip())
        if not has_full and not has_first:
            raise ValueError("Either 'full_name' or 'first_name' must be provided.")
        return self


class UserUpdate(BaseModel):
    """PUT /users/{id}: send only the fields to change. Accepts the legacy and the split name shape.

    'role' is deliberately not accepted here: the endpoint is public, so it must not
    be a way to promote an account.
    """
    email: Optional[EmailStr] = None
    full_name: Optional[str] = Field(None, max_length=150)
    first_name: Optional[str] = Field(None, max_length=100)
    last_name: Optional[str] = Field(None, max_length=100)

    @model_validator(mode="after")
    def validate_update(self):
        if all(v is None for v in (self.email, self.full_name, self.first_name, self.last_name)):
            raise ValueError("At least one field must be provided.")
        if self.full_name is not None and not self.full_name.strip():
            raise ValueError("'full_name' must not be blank.")
        if self.first_name is not None and not self.first_name.strip():
            raise ValueError("'first_name' must not be blank.")
        return self


class UserResponse(BaseModel):
    """Response shape. Built by user_service from the active read source.

    Plain str types on purpose: a stored row must never fail response validation
    (an odd legacy email or a long name would otherwise turn a GET into a 500).
    """
    id: int
    email: str
    username: str
    full_name: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    role: str
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)
