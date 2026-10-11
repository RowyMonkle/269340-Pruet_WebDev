"""CP2 users API under every migration phase (live PostgreSQL, skipped when it is down).

Covers Phase 2 (dual-write on POST and PUT), backward compatibility of the payloads,
Phase 4 (read switch) and the error codes: 400 / 404 / 409 and never 5xx.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core import flags
from app.core.database import SessionLocal, check_postgres_connection
from app.main import app

client = TestClient(app)

pytestmark = pytest.mark.skipif(
    not check_postgres_connection(), reason="PostgreSQL not accessible for live integration testing."
)


def set_modes(write: str, read: str) -> None:
    with SessionLocal() as db:
        flags.set_flag(db, "user_name_write_mode", write)
        flags.set_flag(db, "user_name_read_source", read)


@pytest.fixture(autouse=True)
def restore_flags():
    with SessionLocal() as db:
        before = dict(flags.get_all_flags())
    yield
    set_modes(before["user_name_write_mode"], before["user_name_read_source"])


@pytest.fixture
def created_ids():
    ids = []
    yield ids
    with SessionLocal() as db:
        for user_id in ids:
            db.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
        db.commit()


def uid() -> str:
    return uuid.uuid4().hex[:10]


def make_payload(**extra):
    tag = uid()
    payload = {"email": f"cp2_{tag}@example.com", "username": f"cp2_{tag}", "password": "secret12"}
    payload.update(extra)
    return payload


def post(created_ids, **extra):
    res = client.post("/api/v1/users", json=make_payload(**extra))
    if res.status_code == 201:
        created_ids.append(res.json()["id"])
    return res


def stored(user_id: int):
    with SessionLocal() as db:
        row = db.execute(
            text("SELECT full_name, first_name, last_name FROM users WHERE id = :id"), {"id": user_id}
        ).one()
    return tuple(row)


WRITE_EXPECT_LEGACY_PAYLOAD = {
    "legacy": ("John Smith", None, None),
    "dual": ("John Smith", "John", "Smith"),
    "new": (None, "John", "Smith"),
}


@pytest.mark.parametrize("write_mode", ["legacy", "dual", "new"])
def test_post_legacy_payload_is_stored_per_write_mode(write_mode, created_ids):
    set_modes(write_mode, "legacy" if write_mode != "new" else "new")
    res = post(created_ids, full_name="John Smith")
    assert res.status_code == 201, res.text
    assert stored(res.json()["id"]) == WRITE_EXPECT_LEGACY_PAYLOAD[write_mode]
    body = res.json()
    assert (body["full_name"], body["first_name"], body["last_name"]) == ("John Smith", "John", "Smith")


@pytest.mark.parametrize("write_mode", ["legacy", "dual", "new"])
def test_post_split_payload_is_stored_per_write_mode(write_mode, created_ids):
    set_modes(write_mode, "legacy" if write_mode != "new" else "new")
    res = post(created_ids, first_name="John", last_name="Smith")
    assert res.status_code == 201, res.text
    assert stored(res.json()["id"]) == WRITE_EXPECT_LEGACY_PAYLOAD[write_mode]


def test_post_single_word_name_stores_empty_last_name(created_ids):
    set_modes("dual", "legacy")
    res = post(created_ids, first_name="Cher")
    assert res.status_code == 201, res.text
    assert stored(res.json()["id"]) == ("Cher", "Cher", "")


def test_post_without_any_name_is_400_not_500():
    assert client.post("/api/v1/users", json=make_payload()).status_code == 400


def test_post_duplicates_are_409(created_ids):
    first = post(created_ids, full_name="Dup One")
    payload = make_payload(full_name="Dup Two")
    payload["email"] = first.json()["email"]
    assert client.post("/api/v1/users", json=payload).status_code == 409
    payload = make_payload(full_name="Dup Two")
    payload["username"] = first.json()["username"]
    assert client.post("/api/v1/users", json=payload).status_code == 409


def test_public_post_cannot_choose_a_role(created_ids):
    res = post(created_ids, full_name="Sneaky One", role="admin")
    assert res.status_code == 201
    assert res.json()["role"] == "fan"


@pytest.mark.parametrize("read_source", ["legacy", "new"])
def test_get_and_list_return_the_same_shape_for_either_read_source(read_source, created_ids):
    set_modes("dual", "legacy")
    user_id = post(created_ids, full_name="Anna Maria Lopez").json()["id"]
    set_modes("dual", read_source)

    one = client.get(f"/api/v1/users/{user_id}")
    assert one.status_code == 200
    assert (one.json()["full_name"], one.json()["first_name"], one.json()["last_name"]) == (
        "Anna Maria Lopez", "Anna", "Maria Lopez",
    )
    assert "hashed_password" not in one.json()

    listing = client.get("/api/v1/users?page=1&page_size=100")
    assert listing.status_code == 200
    assert listing.json()["items"], "list must not be empty"


def test_read_new_ignores_a_stale_full_name(created_ids):
    set_modes("dual", "legacy")
    user_id = post(created_ids, full_name="Old Name").json()["id"]
    with SessionLocal() as db:
        db.execute(
            text("UPDATE users SET first_name = 'New', last_name = 'Person' WHERE id = :id"), {"id": user_id}
        )
        db.commit()

    set_modes("dual", "new")
    assert client.get(f"/api/v1/users/{user_id}").json()["full_name"] == "New Person"
    set_modes("dual", "legacy")
    assert client.get(f"/api/v1/users/{user_id}").json()["full_name"] == "Old Name"


def test_row_written_in_new_mode_is_readable_with_legacy_read(created_ids):
    set_modes("new", "legacy")
    res = post(created_ids, first_name="Only", last_name="New")
    assert res.status_code == 201
    got = client.get(f"/api/v1/users/{res.json()['id']}").json()
    assert (got["full_name"], got["first_name"], got["last_name"]) == ("Only New", "Only", "New")


def test_get_unknown_user_is_404():
    assert client.get("/api/v1/users/2147483000").status_code == 404


@pytest.mark.parametrize("write_mode", ["legacy", "dual", "new"])
def test_put_legacy_shape_updates_per_write_mode(write_mode, created_ids):
    set_modes("dual", "legacy")
    user_id = post(created_ids, full_name="Before Change").json()["id"]
    set_modes(write_mode, "legacy" if write_mode != "new" else "new")

    res = client.put(f"/api/v1/users/{user_id}", json={"full_name": "After Change"})
    assert res.status_code == 200, res.text
    full, first, last = stored(user_id)
    if write_mode == "legacy":
        assert (full, first, last) == ("After Change", "Before", "Change")  # first/last untouched
    elif write_mode == "dual":
        assert (full, first, last) == ("After Change", "After", "Change")
    else:
        assert (full, first, last) == ("Before Change", "After", "Change")  # full_name untouched
    assert res.json()["full_name"] == "After Change"


def test_put_split_shape_keeps_the_part_that_was_not_sent(created_ids):
    set_modes("dual", "legacy")
    user_id = post(created_ids, full_name="Jane Smith").json()["id"]

    res = client.put(f"/api/v1/users/{user_id}", json={"last_name": "Jones"})
    assert res.status_code == 200, res.text
    assert stored(user_id) == ("Jane Jones", "Jane", "Jones")

    res = client.put(f"/api/v1/users/{user_id}", json={"first_name": "Janet"})
    assert stored(user_id) == ("Janet Jones", "Janet", "Jones")


def test_put_on_a_row_not_backfilled_yet_does_not_lose_the_other_part(created_ids):
    set_modes("legacy", "legacy")
    user_id = post(created_ids, full_name="Legacy Person").json()["id"]
    set_modes("dual", "legacy")
    assert stored(user_id) == ("Legacy Person", None, None)

    assert client.put(f"/api/v1/users/{user_id}", json={"last_name": "Renamed"}).status_code == 200
    assert stored(user_id) == ("Legacy Renamed", "Legacy", "Renamed")


def test_put_email_and_conflicts(created_ids):
    a = post(created_ids, full_name="Mail A").json()
    b = post(created_ids, full_name="Mail B").json()
    assert client.put(f"/api/v1/users/{a['id']}", json={"email": b["email"]}).status_code == 409
    new_email = f"changed_{uid()}@example.com"
    res = client.put(f"/api/v1/users/{a['id']}", json={"email": new_email})
    assert res.status_code == 200 and res.json()["email"] == new_email
    # same email as its own again is not a conflict
    assert client.put(f"/api/v1/users/{a['id']}", json={"email": new_email}).status_code == 200


def test_put_errors_are_4xx(created_ids):
    user_id = post(created_ids, full_name="Err User").json()["id"]
    assert client.put("/api/v1/users/2147483000", json={"full_name": "Nobody Here"}).status_code == 404
    assert client.put(f"/api/v1/users/{user_id}", json={}).status_code == 400
    assert client.put(f"/api/v1/users/{user_id}", json={"full_name": "   "}).status_code == 400
    assert client.put(f"/api/v1/users/{user_id}", json={"email": "not-an-email"}).status_code == 400


def test_put_cannot_change_role(created_ids):
    user_id = post(created_ids, full_name="Role User").json()["id"]
    res = client.put(f"/api/v1/users/{user_id}", json={"full_name": "Role User 2", "role": "admin"})
    assert res.status_code == 200
    assert res.json()["role"] == "fan"
