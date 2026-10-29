"""POST /api/v1/users/login (live PostgreSQL, skipped when it is down).

Registers a throw-away user, then checks: right password -> 200 with the public user (no hash),
email in other case -> 200, wrong password and unknown email -> the same 401, bad body -> 400.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.database import SessionLocal, check_postgres_connection
from app.main import app

client = TestClient(app)

pytestmark = pytest.mark.skipif(
    not check_postgres_connection(), reason="PostgreSQL not accessible for live integration testing."
)

PASSWORD = "Encore#2026"


@pytest.fixture
def account():
    tag = uuid.uuid4().hex[:10]
    payload = {"email": f"login_{tag}@example.com", "username": f"login_{tag}", "password": PASSWORD, "first_name": "Jamie"}
    res = client.post("/api/v1/users", json=payload)
    assert res.status_code == 201, res.text
    user = res.json()
    yield user
    with SessionLocal() as db:  # remove the throw-away account
        db.execute(text("DELETE FROM users WHERE id = :id"), {"id": user["id"]})
        db.commit()


def test_login_ok_returns_public_user(account):
    res = client.post("/api/v1/users/login", json={"email": account["email"], "password": PASSWORD})
    assert res.status_code == 200
    body = res.json()
    assert body["id"] == account["id"]
    assert body["email"] == account["email"]
    assert "hashed_password" not in body and "password" not in body  # the hash never leaves the server


def test_login_email_is_case_insensitive(account):
    res = client.post("/api/v1/users/login", json={"email": account["email"].upper(), "password": PASSWORD})
    assert res.status_code == 200
    assert res.json()["id"] == account["id"]


def test_wrong_password_and_unknown_email_look_the_same(account):
    wrong_pwd = client.post("/api/v1/users/login", json={"email": account["email"], "password": "nope"})
    no_user = client.post("/api/v1/users/login", json={"email": f"nobody_{uuid.uuid4().hex[:8]}@example.com", "password": PASSWORD})
    assert wrong_pwd.status_code == no_user.status_code == 401
    assert wrong_pwd.json()["detail"] == no_user.json()["detail"]  # no hint about which part was wrong


@pytest.mark.parametrize("body", [{}, {"email": "not-an-email", "password": "x"}, {"email": "a@example.com", "password": ""}])
def test_bad_body_is_400(body):
    res = client.post("/api/v1/users/login", json=body)
    assert res.status_code == 400  # the global validation handler in main.py answers 400, never 5xx
