from typing import Optional
from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.common import PaginatedResponse
from app.schemas.user import UserCreate, UserResponse
from app.services.user_service import (
    create_user as create_user_svc,
    get_user_by_id as get_user_by_id_svc,
    get_users_paginated,
)

router = APIRouter(prefix="/users", tags=["Users (PostgreSQL)"])


@router.post(
    "",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create User Account",
    description="Register a new user account in PostgreSQL. Enforces email/username uniqueness.",
)
def create_user(
    user_in: UserCreate,
    db: Session = Depends(get_db),
):
    return create_user_svc(db, user_in)


@router.get(
    "/{user_id}",
    response_model=UserResponse,
    status_code=status.HTTP_200_OK,
    summary="Get User Details",
    description="Fetch user account details by ID from PostgreSQL using selective database projection.",
)
def get_user_by_id(
    user_id: int,
    db: Session = Depends(get_db),
):
    return get_user_by_id_svc(db, user_id)


@router.get(
    "",
    response_model=PaginatedResponse[UserResponse],
    status_code=status.HTTP_200_OK,
    summary="List Users (Paginated)",
    description="Retrieve paginated list of users using database projection (no heavy entity over-fetching).",
)
def list_users(
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    db: Session = Depends(get_db),
):
    users, total = get_users_paginated(db, page=page, page_size=page_size)
    total_pages = (total + page_size - 1) // page_size if total > 0 else 0
    return PaginatedResponse[UserResponse](
        items=users,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )
