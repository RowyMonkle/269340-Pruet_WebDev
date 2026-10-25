"""Reference Locust profile: the one used to measure the numbers in README section 7.7 and 7.9.

The independent cross-check of scripts/k6_script.js (the official load test, README section 7.13). 50 users, mixed read/write, through nginx:

  GET  /events (5)  /events/{id} (3)  /orders?user_id (3)  /orders/{id} (2)  /users (1)  /users/{id} (1)
  POST /orders (3)  /orders/{id}/payments (1)  /users (1)

Run (the official image, no pip install needed), from the repository root, against a stack named "rehearsal":

  docker run --rm --network rehearsal_default -v "$PWD/docs/perf:/mnt/locust" locustio/locust \
      -f /mnt/locust/reference_locustfile.py --headless -u 50 -r 10 -t 180s --host http://nginx \
      --csv /mnt/locust/out/reference

Needs a seeded database (python seed.py): users 1..250 and a few events already exist.
"""
import datetime
import random
import uuid

import requests
from locust import HttpUser, between, events, task

BIG = {"event_id": None}
SEEDED_USERS = 250


@events.test_start.add_listener
def create_booking_event(environment, **kwargs):
    """One event with a huge zone, so bookings never sell out during the run."""
    when = (datetime.datetime.utcnow() + datetime.timedelta(days=30)).isoformat()
    body = {
        "title": f"Load Test Event {uuid.uuid4().hex[:6]}",
        "category": "Concert",
        "artist": {"name": "Load Band", "genre": "Rock", "bio": ""},
        "venue": {"name": "Load Arena", "city": "Bangkok", "country": "Thailand", "capacity": 2000000},
        "date_time": when,
        "zones": [{"name": "Load GA", "price": 100.0, "capacity": 1000000, "booked_count": 0}],
        "tags": ["load"],
        "dynamic_attributes": {},
    }
    res = requests.post(environment.host + "/api/v1/events", json=body, timeout=10)
    res.raise_for_status()
    BIG["event_id"] = res.json()["id"]


class Shopper(HttpUser):
    wait_time = between(0.1, 0.5)

    def on_start(self):
        self.uid = random.randint(1, SEEDED_USERS)
        self.pending = []  # (order_id, total)

    @task(5)
    def list_events(self):
        self.client.get(f"/api/v1/events?page={random.randint(1, 5)}&page_size=20", name="GET /events")

    @task(3)
    def event_detail(self):
        self.client.get(f"/api/v1/events/{BIG['event_id']}", name="GET /events/{id}")

    @task(3)
    def my_orders(self):
        self.client.get(f"/api/v1/orders?user_id={self.uid}", name="GET /orders?user_id")

    @task(3)
    def book(self):
        with self.client.post(
            "/api/v1/orders",
            json={"user_id": self.uid, "payment_method": "promptpay",
                  "items": [{"event_id": BIG["event_id"], "seat_zone": "Load GA"}]},
            name="POST /orders", catch_response=True,
        ) as res:
            if res.status_code == 201:
                body = res.json()
                self.pending.append((body["id"], body["total_amount"]))
                res.success()
            else:
                res.failure(f"HTTP {res.status_code}")

    @task(1)
    def pay(self):
        if not self.pending:
            return
        order_id, total = self.pending.pop()
        self.client.post(f"/api/v1/orders/{order_id}/payments", json={"amount": total, "provider": "promptpay"},
                         name="POST /orders/{id}/payments")

    @task(2)
    def get_order(self):
        if self.pending:
            self.client.get(f"/api/v1/orders/{self.pending[-1][0]}", name="GET /orders/{id}")

    @task(1)
    def users_page(self):
        self.client.get(f"/api/v1/users?page={random.randint(1, 5)}&page_size=20", name="GET /users")

    @task(1)
    def user_detail(self):
        self.client.get(f"/api/v1/users/{self.uid}", name="GET /users/{id}")

    @task(1)
    def register(self):
        tag = uuid.uuid4().hex[:14]
        self.client.post("/api/v1/users", json={"email": f"lt_{tag}@example.com", "username": f"lt_{tag}",
                                                "password": "Password123!", "first_name": "Load", "last_name": "Tester"},
                         name="POST /users")
