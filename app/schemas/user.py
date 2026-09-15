from datetime import datetime
from typing import Optional
from pydantic import BaseModel, EmailStr, Field, ConfigDict


class UserBase(BaseModel):
    email: EmailStr
    username: str = Field(..., min_length=3, max_length=50, pattern=r"^[a-zA-Z0-9_-]+$")
    full_name: str = Field(..., min_length=2, max_length=150)
    role: str = Field(default="fan", pattern=r"^(fan|organizer|admin)$")


class UserCreate(UserBase):
    password: str = Field(..., min_length=6, max_length=100, description="Plaintext password to be hashed")


class UserUpdate(BaseModel):
    email: Optional[EmailStr] = None
    full_name: Optional[str] = Field(None, min_length=2, max_length=150)
    role: Optional[str] = Field(None, pattern=r"^(fan|organizer|admin)$")


class UserResponse(UserBase):
    id: int
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)
