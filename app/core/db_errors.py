"""Classify database errors that mean "a concurrent transaction got in the way".

PostgreSQL aborts one side of a collision and tells the client to try again:
  40P01  deadlock_detected        two transactions waited on each other
  40001  serialization_failure    a snapshot/isolation conflict
  55P03  lock_not_available       a lock_timeout expired while waiting

Nothing was written in any of these cases, so the right answer is HTTP 409 (the
client retries), never HTTP 500. Services call is_retryable_conflict() to compensate
and raise 409; app/main.py registers a global handler as a safety net for every
other endpoint, so a collision can never surface as a server error.
"""
import logging
from typing import Optional

from fastapi import HTTPException, status

logger = logging.getLogger("db_errors")

DEADLOCK_DETECTED = "40P01"
SERIALIZATION_FAILURE = "40001"
LOCK_NOT_AVAILABLE = "55P03"
RETRYABLE_PGCODES = frozenset({DEADLOCK_DETECTED, SERIALIZATION_FAILURE, LOCK_NOT_AVAILABLE})

CONFLICT_MESSAGE = "The request collided with a concurrent update. Nothing was changed, please retry."


def pgcode(exc: BaseException) -> Optional[str]:
    """SQLSTATE of a SQLAlchemy/psycopg2 error, or None when it has none."""
    return getattr(getattr(exc, "orig", None), "pgcode", None)


def is_retryable_conflict(exc: BaseException) -> bool:
    return pgcode(exc) in RETRYABLE_PGCODES


def conflict_exception(exc: BaseException) -> HTTPException:
    """The HTTP 409 to raise for a retryable collision (also logs which code it was)."""
    logger.warning("Retryable database conflict (SQLSTATE %s): answering 409", pgcode(exc))
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=CONFLICT_MESSAGE)
