from datetime import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, ConfigDict


class ZoneSchema(BaseModel):
    name: str = Field(..., min_length=1, max_length=50, description="Zone name, e.g., VIP, General Admission, Zone A")
    price: float = Field(..., ge=0.0, description="Ticket price for this zone")
    capacity: int = Field(..., gt=0, description="Maximum seats/tickets in this zone")
    booked_count: int = Field(default=0, ge=0, description="Currently reserved or booked tickets")


class ArtistSchema(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    genre: str = Field(..., min_length=1, max_length=50)
    bio: Optional[str] = Field(default="", max_length=1000)
    spotify_id: Optional[str] = None


class VenueSchema(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    city: str = Field(..., min_length=1, max_length=100)
    country: str = Field(default="Thailand", max_length=100)
    capacity: int = Field(..., gt=0)
    address: Optional[str] = None


class EventCreate(BaseModel):
    title: str = Field(..., min_length=3, max_length=200, description="Event title")
    category: str = Field(default="Concert", description="Concert, Music Festival, Live Performance")
    artist: ArtistSchema
    venue: VenueSchema
    date_time: datetime = Field(..., description="Date and time of the event")
    zones: List[ZoneSchema] = Field(..., min_length=1, description="Stage zones and seating configurations")
    tags: List[str] = Field(default_factory=list, description="Categorization tags, e.g. ['rock', 'live', 'festival']")
    dynamic_attributes: Dict[str, Any] = Field(
        default_factory=dict,
        description="Flexible MongoDB document fields: age restriction, festival passes, stage specs, merchandise info",
    )


class EventResponse(BaseModel):
    id: str
    title: str
    category: str
    artist: ArtistSchema
    venue: VenueSchema
    date_time: datetime
    zones: List[ZoneSchema]
    tags: List[str]
    dynamic_attributes: Dict[str, Any]
    status: str = "upcoming"
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)
