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
