import hashlib
import os
from typing import List, Optional, Tuple
from fastapi import HTTPException, status
from sqlalchemy.orm import Session, load_only
from app.models.sql_models import User
from app.schemas.user import UserCreate

try:
    import bcrypt

    def hash_password(password: str) -> str:
        salt = bcrypt.gensalt()
        return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")

    def verify_password(plain_password: str, hashed_password: str) -> bool:
        return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))

except ImportError:  # Fallback for lightweight / test environments
    def hash_password(password: str) -> str:
        salt = os.urandom(16).hex()
        digest = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
        return f"sha256${salt}${digest}"

    def verify_password(plain_password: str, hashed_password: str) -> bool:
        if not hashed_password.startswith("sha256$"):
            return False
        _, salt, digest = hashed_password.split("$")
        expected = hashlib.sha256((salt + plain_password).encode("utf-8")).hexdigest()
        return expected == digest


def get_user_by_id(db: Session, user_id: int) -> User:
    """Fetch user by primary key using load_only projection.

    Security & Performance:
    - Never loads 'hashed_password' into memory.
    - Prevents N+1 query bottlenecks via explicit column projections.
    """
    user = (
        db.query(User)
        .options(
            load_only(
                User.id,
                User.email,
                User.username,
                User.full_name,
                User.role,
                User.created_at,
                User.updated_at,
            )
        )
        .filter(User.id == user_id)
        .first()
    )
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User with ID {user_id} was not found",
        )
    return user


def get_users_paginated(
    db: Session, page: int = 1, page_size: int = 20
) -> Tuple[List[User], int]:
    """Return paginated list of users using database projection.

    Fixes N+1 issue:
    Includes all fields serialized by UserResponse (including updated_at) to avoid deferred column loads.
    """
    query = db.query(User).options(
        load_only(
            User.id,
            User.email,
            User.username,
            User.full_name,
            User.role,
            User.created_at,
            User.updated_at,  # Included to prevent N+1 query triggers
        )
    )
    total = query.count()
    offset = (page - 1) * page_size
    items = query.order_by(User.id.asc()).offset(offset).limit(page_size).all()
    return items, total


def create_user(db: Session, user_in: UserCreate) -> User:
    """Create a new user account with uniqueness validation and hashed password.

    Security Enforcement:
    - Public registration strictly sets role to 'fan'. Administrative roles cannot be self-assigned.
    - Returns HTTP 409 Conflict if email or username already exists.
    """
    existing_user = (
        db.query(User)
        .options(load_only(User.id, User.email, User.username))
        .filter((User.email == user_in.email) | (User.username == user_in.username))
        .first()
    )
    if existing_user:
        conflict_field = "email" if existing_user.email == user_in.email else "username"
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A user with this {conflict_field} already exists",
        )

    db_user = User(
        email=user_in.email,
        username=user_in.username,
        hashed_password=hash_password(user_in.password),
        full_name=user_in.full_name,
        role="fan",  # Strictly force 'fan' role for all public self-registrations
    )
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user
