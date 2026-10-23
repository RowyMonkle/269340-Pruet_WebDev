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
#add : error when pool time out
from sqlalchemy.exc import OperationalError, TimeoutError as PoolTimeoutError
# MongoDB unreachable: ServerSelectionTimeoutError, NetworkTimeout and AutoReconnect are all ConnectionFailure
from pymongo.errors import ConnectionFailure as MongoConnectionFailure

logger = logging.getLogger("db_errors")

DEADLOCK_DETECTED = "40P01"
SERIALIZATION_FAILURE = "40001"
LOCK_NOT_AVAILABLE = "55P03"
RETRYABLE_PGCODES = frozenset({DEADLOCK_DETECTED, SERIALIZATION_FAILURE, LOCK_NOT_AVAILABLE})

#add statement_timeout
QUERY_CANCELED = "57014" 
#when Posgress can't receieve the connection
TOO_MANY_CONNECTIONS = "53300"
#Postgres open/close
CANNOT_CONNECT_NOW = "57P03"
#respone 503 code error
UNAVAILABLE_PGCODES = frozenset({QUERY_CANCELED, TOO_MANY_CONNECTIONS, CANNOT_CONNECT_NOW})

CONFLICT_MESSAGE = "The request collided with a concurrent update. Nothing was changed, please retry."
#msg code 503 error
UNAVAILABLE_MESSAGE = "The database is busy right now. Nothing was changed, please retry shortly." 
#client need to wait 2 sec (header Retry-After)
RETRY_AFTER_SECONDS = 2


def pgcode(exc: BaseException) -> Optional[str]:
    """SQLSTATE of a SQLAlchemy/psycopg2 error, or None when it has none."""
    return getattr(getattr(exc, "orig", None), "pgcode", None)


def is_retryable_conflict(exc: BaseException) -> bool:
    return pgcode(exc) in RETRYABLE_PGCODES

#check this is 503 or not
def is_service_unavailable(exc: BaseException) -> bool:
    if isinstance(exc, (PoolTimeoutError, MongoConnectionFailure)): #check type of exception
        return True
    # PostgreSQL itself is unreachable (stopped, restarting, network): the driver raises OperationalError
    # without a SQLSTATE because no server ever answered. Real SQL errors always carry one.
    if isinstance(exc, OperationalError) and pgcode(exc) is None:
        return True
    return pgcode(exc) in UNAVAILABLE_PGCODES #error from Posgres when overload

def conflict_exception(exc: BaseException) -> HTTPException:
    """The HTTP 409 to raise for a retryable collision (also logs which code it was)."""
    logger.warning("Retryable database conflict (SQLSTATE %s): answering 409", pgcode(exc))
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=CONFLICT_MESSAGE)

#create HTTP 503 with log
def unavailable_exception(exc: BaseException) -> HTTPException:
    if isinstance(exc, PoolTimeoutError):
        reason = "pool timeout"
    elif isinstance(exc, MongoConnectionFailure):
        reason = f"MongoDB unreachable ({type(exc).__name__})"
    elif pgcode(exc) is None:
        reason = "PostgreSQL unreachable"
    else:
        reason = f"SQLSTATE {pgcode(exc)}"
    logger.warning("Database unavailable (%s): answering 503", reason)
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=UNAVAILABLE_MESSAGE,
        headers={"Retry-After": str(RETRY_AFTER_SECONDS)},  # HTTP standard : tell ckient that when can retry
    )

# single entry point for main.py and services to convert a DB error
def classify_db_error(exc: BaseException) -> Optional[HTTPException]:
    """Map a database error to the HTTPException to raise, or None if it is a real bug (-> 500)."""  
    if is_retryable_conflict(exc):
        return conflict_exception(exc)
    if is_service_unavailable(exc):
        return unavailable_exception(exc)
    return None 