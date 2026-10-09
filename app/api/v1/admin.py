import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.core import flags
from app.core.config import settings
from app.core.database import get_db
from app.schemas.admin import FlagsResponse, FlagUpdate, FlagUpdateResponse


def require_admin_token(x_admin_token: str = Header(default="")) -> None:
    if not settings.ADMIN_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin endpoints are disabled (ADMIN_TOKEN is not set)",
        )
    if not secrets.compare_digest(x_admin_token, settings.ADMIN_TOKEN):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-Admin-Token header",
        )


router = APIRouter(
    prefix="/admin",
    tags=["Admin (Feature Flags)"],
    dependencies=[Depends(require_admin_token)],
)


@router.get(
    "/flags",
    response_model=FlagsResponse,
    summary="List Feature Flags",
    description="Current runtime flag values as seen by this replica, plus the allowed values.",
)
def list_flags():
    return FlagsResponse(
        flags=flags.get_all_flags(),
        choices={key: list(choices) for key, choices in flags.FLAG_CHOICES.items()},
    )


@router.put(
    "/flags/{key}",
    response_model=FlagUpdateResponse,
    summary="Change Feature Flag",
    description=(
        "Switch a migration phase at runtime. Every replica picks up the new value "
        "within FLAG_CACHE_SECONDS, no restart needed."
    ),
)
def update_flag(key: str, body: FlagUpdate, db: Session = Depends(get_db)):
    choices = flags.FLAG_CHOICES.get(key)
    if choices is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown flag '{key}'")
    if body.value not in choices:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid value '{body.value}' for '{key}'. Allowed: {', '.join(choices)}",
        )
    flags.set_flag(db, key, body.value)
    return FlagUpdateResponse(key=key, value=body.value, updated_at=datetime.now(timezone.utc))
