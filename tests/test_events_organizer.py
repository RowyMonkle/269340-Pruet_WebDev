"""GET /api/v1/events?organizer_id= (live MongoDB, skipped when it is down).

Creates two events for two made-up organizer ids, then checks: the filter returns only that organizer's
events, organizer_id is in the list rows and in the detail document, and an event without one still works.
"""
import uuid

import pytest
from bson import ObjectId
from fastapi.testclient import TestClient

from app.core.database import check_mongo_connection, mongo_db
from app.main import app

client = TestClient(app)

pytestmark = pytest.mark.skipif(not check_mongo_connection(), reason="MongoDB not accessible for live integration testing.")

ORG_A, ORG_B = 900001, 900002  # ids no real organizer has


def payload(title, organizer_id=None):
    body = {
        "title": title,
        "artist": {"name": "Test Act", "genre": "Pop"},
        "venue": {"name": "Test Hall", "city": "Bangkok", "capacity": 100},
        "date_time": "2027-01-01T12:00:00Z",
        "zones": [{"name": "GA", "price": 100.0, "capacity": 50, "booked_count": 0}],
    }
    if organizer_id is not None:
        body["organizer_id"] = organizer_id
    return body


@pytest.fixture
def events():
    tag = uuid.uuid4().hex[:8]
    ids = {}
    for key, org in (("a", ORG_A), ("b", ORG_B), ("none", None)):
        res = client.post("/api/v1/events", json=payload(f"Organizer test {key} {tag}", org))
        assert res.status_code == 201, res.text
        ids[key] = res.json()["id"]
    yield ids
    mongo_db["events"].delete_many({"_id": {"$in": [ObjectId(i) for i in ids.values()]}})


def test_filter_returns_only_that_organizers_events(events):
    res = client.get(f"/api/v1/events?organizer_id={ORG_A}&page_size=100")
    assert res.status_code == 200
    items = res.json()["items"]
    assert [e["id"] for e in items] == [events["a"]]
    assert items[0]["organizer_id"] == ORG_A  # list rows carry the owner


def test_detail_has_organizer_and_old_events_still_work(events):
    assert client.get(f"/api/v1/events/{events['b']}").json()["organizer_id"] == ORG_B
    assert client.get(f"/api/v1/events/{events['none']}").json()["organizer_id"] is None


def test_bad_organizer_id_is_400():
    assert client.get("/api/v1/events?organizer_id=0").status_code == 400  # gt=0, validation handler answers 400
