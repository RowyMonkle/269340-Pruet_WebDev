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
    email: Optional[EmailStr] = None
    full_name: Optional[str] = Field(None, max_length=150)
    first_name: Optional[str] = Field(None, max_length=100)
    last_name: Optional[str] = Field(None, max_length=100)
    role: Optional[str] = Field(None, pattern=r"^(fan|organizer|admin)$")


class UserResponse(UserBase):
    id: int
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode="after")
    def ensure_full_name(self):
        # Fallback to computing full_name if full_name is None in DB (Phase 4/5)
        if not self.full_name and (self.first_name or self.last_name):
            parts = [p.strip() for p in (self.first_name, self.last_name) if p and p.strip()]
            self.full_name = " ".join(parts)
        elif self.full_name and not self.first_name:
            # If reading legacy rows that don't have first/last yet
            parts = self.full_name.strip().split(maxsplit=1)
            self.first_name = parts[0] if parts else ""
            self.last_name = parts[1] if len(parts) > 1 else ""
        return self
