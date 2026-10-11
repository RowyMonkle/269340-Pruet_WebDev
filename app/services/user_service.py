import hashlib
import os
from typing import List, Optional, Tuple
from types import SimpleNamespace
from fastapi import HTTPException, status
from sqlalchemy import func, insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, load_only
from app.core.config import settings
from app.core.flags import get_flag
from app.models.sql_models import User
from app.schemas.user import UserCreate, UserResponse, UserUpdate


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


def _clean(value: Optional[str]) -> Optional[str]:
    """Strip a name part; blank becomes None."""
    value = value.strip() if value else None
    return value or None


def resolve_names(
    full_name: Optional[str],
    first_name: Optional[str],
    last_name: Optional[str],
    write_mode: str,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Return (full_name, first_name, last_name) to store for the active user_name_write_mode.

    Name rule shared with the backfill and the verification queries: first_name is the
    text before the first whitespace, last_name the rest, and last_name is '' (never
    NULL) when there is only one word. A column the mode does not write comes back None.
    """
    f_name, l_name, raw_full = _clean(first_name), _clean(last_name), _clean(full_name)

    # Derive the representation that is missing
    full = raw_full or join_names(f_name, l_name) or None
    if full and not f_name:
        head, tail = split_full_name(full)
        f_name, l_name = head, (l_name if l_name is not None else tail)
    if f_name and l_name is None:
        l_name = ""
        if full:
            head, tail = split_full_name(full)
            if head == f_name:
                l_name = tail

    if write_mode == "legacy":
        # Legacy mode: write full_name only
        return full, None, None
    if write_mode == "new":
        # New mode: write first_name/last_name only (full_name is left alone)
        return None, f_name, l_name
    # "dual" (Phase 2) writes BOTH; any unknown value falls back to dual as well
    return full, f_name, l_name


def resolve_user_names(
    user_in: UserCreate, write_mode: str
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Determine (full_name, first_name, last_name) to store for a new user."""
    return resolve_names(user_in.full_name, user_in.first_name, user_in.last_name, write_mode)


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


# Columns every user query needs. full_name is added only while something still
# reads it, so with both flags at "new" no query mentions the column (drop-safe).
_BASE_COLUMNS = (
    User.id,
    User.email,
    User.username,
    User.first_name,
    User.last_name,
    User.role,
    User.created_at,
    User.updated_at,  # Included to prevent N+1 query triggers
)


def _user_columns(include_legacy: bool):
    columns = _BASE_COLUMNS + ((User.full_name,) if include_legacy else ())
    return load_only(*columns)


def build_user_response(user: User, read_source: str) -> UserResponse:
    """Phase 4 switch: build the public response from the active read source.

    new:    first_name/last_name come from the new columns, full_name is derived from them.
    legacy: full_name comes from the legacy column (derived from first/last for rows
            written in "new" mode); first/last are split from it when not stored yet.
    """
    first, last = user.first_name, user.last_name
    if read_source == "new":
        full = join_names(first, last) or None
    else:
        full = user.full_name or join_names(first, last) or None
        if first is None and full:
            first, last = split_full_name(full)
    return UserResponse(
        id=user.id,
        email=user.email,
        username=user.username,
        full_name=full,
        first_name=first,
        last_name=last,
        role=user.role,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


def get_user_by_id(db: Session, user_id: int) -> UserResponse:
    """Fetch user by primary key using load_only projection.

    Security & Performance:
    - Never loads 'hashed_password' into memory.
    - Prevents N+1 query bottlenecks via explicit column projections.
    CP2 Phase 4: the active user_name_read_source flag decides which name columns are read.
    """
    read_source = get_flag("user_name_read_source")
    user = (
        db.query(User)
        .options(_user_columns(include_legacy=read_source != "new"))
        .filter(User.id == user_id)
        .first()
    )
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User with ID {user_id} was not found",
        )
    return build_user_response(user, read_source)


def get_users_paginated(
    db: Session, page: int = 1, page_size: int = 20
) -> Tuple[List[UserResponse], int]:
    """Return paginated list of users using database projection.

    Fixes N+1 issue:
    Includes all fields serialized by UserResponse (including updated_at) to avoid deferred column loads.
    """
    read_source = get_flag("user_name_read_source")
    # count() on the projected query would wrap a SELECT of every mapped column
    # (hashed_password, full_name...), so count ids directly.
    total = db.query(func.count(User.id)).scalar()
    query = db.query(User).options(_user_columns(include_legacy=read_source != "new"))
    offset = (page - 1) * page_size
    items = query.order_by(User.id.asc()).offset(offset).limit(page_size).all()
    return [build_user_response(user, read_source) for user in items], total


def _conflict(field: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=f"A user with this {field} already exists",
    )


def create_user(db: Session, user_in: UserCreate) -> UserResponse:
    """Create a new user account with uniqueness validation, hashed password, and CP2 Dual-Write.

    Security & Schema Evolution:
    - Public registration strictly sets role to 'fan'. Administrative roles cannot be self-assigned.
    - Returns HTTP 409 Conflict if email or username already exists (also when two
      identical requests race past the pre-check and hit the unique index).
    - Zero-Downtime Dual-Write: writes to both full_name and first_name/last_name according to user_name_write_mode flag.
    """
    existing_user = (
        db.query(User)
        .options(load_only(User.id, User.email, User.username))
        .filter((User.email == user_in.email) | (User.username == user_in.username))
        .first()
    )
    if existing_user:
        raise _conflict("email" if existing_user.email == user_in.email else "username")

    # CP2 Phase 2: Dual Write resolution based on active runtime feature flag
    write_mode = get_flag("user_name_write_mode")
    read_source = get_flag("user_name_read_source")
    target_full_name, target_first_name, target_last_name = resolve_user_names(user_in, write_mode)

    columns = dict(
        email=user_in.email,
        username=user_in.username,
        hashed_password=hash_password(user_in.password),
        first_name=target_first_name,
        last_name=target_last_name,
        role="fan",  # Strictly force 'fan' role for all public self-registrations
    )
    if write_mode != "new":
        # In "new" mode full_name is not mentioned at all, so the column can be dropped
        columns["full_name"] = target_full_name
    # Core INSERT names only the columns above; an ORM INSERT would also list
    # full_name (as NULL) and fail once the column is gone.
    try:
        row = db.execute(
            insert(User).values(**columns).returning(User.id, User.created_at, User.updated_at)
        ).one()
        db.commit()
    except IntegrityError:
        db.rollback()
        raise _conflict("email or username")
    created = SimpleNamespace(
        id=row.id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        full_name=columns.get("full_name"),
        **{k: columns[k] for k in ("email", "username", "first_name", "last_name", "role")},
    )
    return build_user_response(created, read_source)


def update_user(db: Session, user_id: int, user_in: UserUpdate) -> UserResponse:
    """PUT /users/{id}: partial update with the same Dual-Write rules as create.

    - 404 if the user does not exist, 409 if the new email is taken.
    - Name fields may arrive in the legacy shape (full_name) or the split shape
      (first_name / last_name); unspecified parts keep their stored value.
    - The row is locked (SELECT ... FOR UPDATE) so two concurrent PUTs cannot interleave.
    """
    write_mode = get_flag("user_name_write_mode")
    read_source = get_flag("user_name_read_source")
    include_legacy = write_mode != "new" or read_source != "new"

    user = (
        db.query(User)
        .options(_user_columns(include_legacy))
        .filter(User.id == user_id)
        .with_for_update()
        .first()
    )
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User with ID {user_id} was not found",
        )

    if user_in.email is not None and user_in.email != user.email:
        taken = (
            db.query(User)
            .options(load_only(User.id))
            .filter(User.email == user_in.email, User.id != user_id)
            .first()
        )
        if taken:
            db.rollback()
            raise _conflict("email")
        user.email = user_in.email

    if any(v is not None for v in (user_in.full_name, user_in.first_name, user_in.last_name)):
        if user_in.first_name is not None or user_in.last_name is not None:
            # Split shape: fill the part not sent from what is stored
            cur_first, cur_last = user.first_name, user.last_name
            if cur_first is None and include_legacy and user.full_name:
                cur_first, cur_last = split_full_name(user.full_name)
            first = user_in.first_name if user_in.first_name is not None else cur_first
            last = user_in.last_name if user_in.last_name is not None else cur_last
        else:
            first = last = None  # legacy shape: derive both from full_name
        full, f_name, l_name = resolve_names(user_in.full_name, first, last, write_mode)
        if write_mode != "new":
            user.full_name = full
        if write_mode != "legacy":
            user.first_name, user.last_name = f_name, l_name

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise _conflict("email")
    db.refresh(user)
    return build_user_response(user, read_source)
