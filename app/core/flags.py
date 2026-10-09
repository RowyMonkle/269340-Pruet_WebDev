"""Runtime feature flags stored in PostgreSQL (feature_flags table).

Every API replica reads flags through a small in-process cache that refreshes
every FLAG_CACHE_SECONDS, so flipping a flag reaches all replicas within a few
seconds without a restart. Used to step through the CP2 expand/contract phases.
"""
import logging
import threading
import time
from typing import Dict

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.sql_models import FeatureFlag

logger = logging.getLogger(__name__)

# key -> allowed values. The first value is the default when the row is missing.
FLAG_CHOICES: Dict[str, tuple] = {
    # legacy: write full_name only | dual: write full_name + first/last | new: first/last only
    "user_name_write_mode": ("legacy", "dual", "new"),
    # legacy: read full_name | new: read first_name/last_name
    "user_name_read_source": ("legacy", "new"),
}

_lock = threading.Lock()
_cache: Dict[str, str] = {}
_loaded_at = 0.0


def _defaults() -> Dict[str, str]:
    return {key: choices[0] for key, choices in FLAG_CHOICES.items()}


def _refresh() -> None:
    global _cache, _loaded_at
    try:
        with SessionLocal() as db:
            rows = db.execute(select(FeatureFlag.key, FeatureFlag.value)).all()
        values = _defaults()
        values.update({key: value for key, value in rows if key in FLAG_CHOICES})
        _cache = values
    except Exception as exc:
        # Keep serving the last known values (or defaults) if the DB blips
        logger.warning(f"Feature flag refresh failed, using cached values: {exc}")
        if not _cache:
            _cache = _defaults()
    _loaded_at = time.monotonic()


def get_all_flags() -> Dict[str, str]:
    with _lock:
        if time.monotonic() - _loaded_at > settings.FLAG_CACHE_SECONDS:
            _refresh()
        return dict(_cache)


def get_flag(key: str) -> str:
    return get_all_flags()[key]


def set_flag(db: Session, key: str, value: str) -> None:
    """Upsert a flag. Caller validates key/value against FLAG_CHOICES."""
    stmt = insert(FeatureFlag).values(key=key, value=value)
    stmt = stmt.on_conflict_do_update(
        index_elements=[FeatureFlag.key],
        set_={"value": value, "updated_at": func.now()},
    )
    db.execute(stmt)
    db.commit()
    invalidate_cache()


def invalidate_cache() -> None:
    global _loaded_at
    with _lock:
        _loaded_at = 0.0
