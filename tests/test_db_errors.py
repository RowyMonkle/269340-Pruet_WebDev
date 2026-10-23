"""PostgreSQL collisions (deadlock, serialization failure, lock timeout) must become HTTP 409, never 500.

No database needed: the errors are built by hand with the SQLSTATE a real psycopg2 error carries.
"""
import asyncio

import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy.exc import IntegrityError, OperationalError
from starlette.requests import Request
from starlette.testclient import TestClient

from app.core.db_errors import CONFLICT_MESSAGE, conflict_exception, is_retryable_conflict, pgcode
from app.main import app, retryable_db_conflict_handler


def db_error(code, cls=OperationalError):
    class Orig(Exception):
        pgcode = code

    return cls("SELECT 1", {}, Orig("boom"))


@pytest.mark.parametrize("code", ["40P01", "40001", "55P03"])
def test_deadlock_serialization_and_lock_timeout_are_retryable(code):
    assert is_retryable_conflict(db_error(code)) is True
    assert pgcode(db_error(code)) == code


@pytest.mark.parametrize("code", ["23505", "23503", "42P01", "08006", None])
def test_other_database_errors_are_not_retryable(code):
    assert is_retryable_conflict(db_error(code)) is False


def test_an_error_without_a_database_cause_is_not_retryable():
    assert is_retryable_conflict(ValueError("nope")) is False


def test_conflict_exception_is_http_409_with_the_retry_message():
    exc = conflict_exception(db_error("40P01"))
    assert isinstance(exc, HTTPException)
    assert exc.status_code == 409
    assert exc.detail == CONFLICT_MESSAGE
    assert "retry" in exc.detail.lower()


def test_the_global_handler_is_registered_on_the_real_app():
    from sqlalchemy.exc import DBAPIError

    assert app.exception_handlers.get(DBAPIError) is retryable_db_conflict_handler


def make_request():
    return Request({"type": "http", "method": "POST", "path": "/api/v1/orders", "headers": [], "query_string": b""})


def test_handler_answers_409_for_a_retryable_error():
    response = asyncio.run(retryable_db_conflict_handler(make_request(), db_error("40P01")))
    assert response.status_code == 409
    assert b"retry" in response.body.lower()


def test_handler_leaves_other_database_errors_unhandled():
    with pytest.raises(IntegrityError):
        asyncio.run(retryable_db_conflict_handler(make_request(), db_error("23505", IntegrityError)))


def test_handler_works_end_to_end_inside_a_fastapi_app():
    mini = FastAPI()
    mini.add_exception_handler(OperationalError, retryable_db_conflict_handler)

    @mini.get("/deadlock")
    def deadlock():
        raise db_error("40P01")

    response = TestClient(mini).get("/deadlock")
    assert response.status_code == 409
    assert response.json() == {"detail": CONFLICT_MESSAGE}


# ---- 503: the database is busy or unreachable (nothing was written, the client retries after a pause) -------------

def test_classify_maps_each_failure_to_the_right_status():
    from pymongo.errors import AutoReconnect, NetworkTimeout, OperationFailure, ServerSelectionTimeoutError
    from sqlalchemy.exc import TimeoutError as PoolTimeoutError
    from app.core.db_errors import RETRY_AFTER_SECONDS, classify_db_error

    expected_409 = [db_error("40P01"), db_error("40001"), db_error("55P03")]            # deadlock, serialization, lock timeout
    expected_503 = [db_error("57014"), db_error("53300"), db_error("57P03"),             # statement timeout, too many connections, starting up
                    PoolTimeoutError("QueuePool limit reached"),
                    ServerSelectionTimeoutError("no mongo"), NetworkTimeout("slow"), AutoReconnect("gone")]
    for exc in expected_409:
        assert classify_db_error(exc).status_code == 409, exc
    for exc in expected_503:
        result = classify_db_error(exc)
        assert result.status_code == 503, exc
        assert result.headers["Retry-After"] == str(RETRY_AFTER_SECONDS)
    # PostgreSQL itself unreachable: the driver error has no SQLSTATE because no server answered
    assert classify_db_error(db_error(None)).status_code == 503
    # a real bug stays a 500: constraint violation, bad SQL, a MongoDB command that is simply wrong
    assert classify_db_error(db_error("23505")) is None
    assert classify_db_error(db_error("42P01")) is None
    assert classify_db_error(OperationFailure("bad command")) is None


def test_503_answers_never_leak_database_internals():
    from sqlalchemy.exc import TimeoutError as PoolTimeoutError
    from app.core.db_errors import classify_db_error

    detail = classify_db_error(PoolTimeoutError("QueuePool limit of size 20 overflow 10 reached")).detail.lower()
    assert "queuepool" not in detail and "overflow" not in detail
