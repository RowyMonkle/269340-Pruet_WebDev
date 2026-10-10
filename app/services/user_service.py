import hashlib
import os
from typing import List, Optional, Tuple
from fastapi import HTTPException, status
from sqlalchemy.orm import Session, load_only
from app.core.config import settings
from app.core.flags import get_flag
from app.models.sql_models import User
from app.schemas.user import UserCreate


def split_full_name(full_name: Optional[str]) -> Tuple[str, str]:
    """Split full name into (first_name, last_name)."""
    if not full_name:
        return "", ""
    parts = full_name.strip().split(maxsplit=1)
    first_name = parts[0] if parts else ""
    last_name = parts[1] if len(parts) > 1 else ""
    return first_name, last_name


def join_names(first_name: Optional[str], last_name: Optional[str]) -> str:
    """Combine first and last names into a single full name string."""
    parts = [p.strip() for p in (first_name, last_name) if p and p.strip()]
    return " ".join(parts)


def resolve_user_names(
    user_in: UserCreate, write_mode: str
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Determine (full_name, first_name, last_name) to store based on user_name_write_mode."""
    f_name = user_in.first_name.strip() if user_in.first_name else None
    l_name = user_in.last_name.strip() if user_in.last_name else None
    raw_full = user_in.full_name.strip() if user_in.full_name else None

    # Derive missing representations
    if not raw_full and (f_name or l_name):
        full_name = join_names(f_name, l_name)
    else:
        full_name = raw_full

    if full_name and not f_name:
        f_name, l_name = split_full_name(full_name)

    if write_mode == "legacy":
        # Legacy mode: write full_name only
        return full_name, None, None
    elif write_mode == "dual":
        # Dual Write (Phase 2): write BOTH legacy full_name and new first_name/last_name
        return full_name, f_name, l_name
    elif write_mode == "new":
        # New mode: write first_name/last_name only (full_name set to None)
        return None, f_name, l_name
    else:
        # Default fallback to dual write
        return full_name, f_name, l_name

try:
    import bcrypt

    def hash_password(password: str) -> str:
        salt = bcrypt.gensalt(rounds=settings.BCRYPT_ROUNDS)
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
                User.first_name,
                User.last_name,
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
            User.first_name,
            User.last_name,
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
    """Create a new user account with uniqueness validation, hashed password, and CP2 Dual-Write.

    Security & Schema Evolution:
    - Public registration strictly sets role to 'fan'. Administrative roles cannot be self-assigned.
    - Returns HTTP 409 Conflict if email or username already exists.
    - Zero-Downtime Dual-Write: writes to both full_name and first_name/last_name according to user_name_write_mode flag.
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

    # CP2 Phase 2: Dual Write resolution based on active runtime feature flag
    write_mode = get_flag("user_name_write_mode")
    target_full_name, target_first_name, target_last_name = resolve_user_names(user_in, write_mode)

    db_user = User(
        email=user_in.email,
        username=user_in.username,
        hashed_password=hash_password(user_in.password),
        full_name=target_full_name,
        first_name=target_first_name,
        last_name=target_last_name,
        role="fan",  # Strictly force 'fan' role for all public self-registrations
    )
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user
