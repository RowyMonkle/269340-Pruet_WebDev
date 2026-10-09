from datetime import datetime
from typing import Dict
from pydantic import BaseModel, Field


class FlagsResponse(BaseModel):
    flags: Dict[str, str]
    choices: Dict[str, list]


class FlagUpdate(BaseModel):
    value: str = Field(..., min_length=1, max_length=64)


class FlagUpdateResponse(BaseModel):
    key: str
    value: str
    updated_at: datetime
