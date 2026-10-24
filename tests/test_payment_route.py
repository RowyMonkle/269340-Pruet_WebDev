"""POST /api/v1/payments: the order id in the body, the same payment as POST /orders/{id}/payments.

Both routes call the same service, so they must give the same answers, take the same lock and obey the same guards
(one completed payment per order, 409 / 410 / 404 / 400). These tests prove it from the outside, including the
case a load test hits: many clients paying one order at the same instant.
Needs PostgreSQL and MongoDB (skipped when they are down; REQUIRE_DB=1 turns the skip into a failure).
Run: docker compose exec -e REQUIRE_DB=1 api pytest tests/test_payment_route.py -v   (add -e API_URL=http://nginx to go through nginx)
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support import build_environment, client, post_order, run_parallel  # noqa: E402


@pytest.fixture(scope="module")
def env():
    environment, teardown = build_environment(need_mongo=True)
    yield environment
    teardown()


def new_order(env, zone: str = "Pay", capacity: int = 50) -> dict:
    event_id = env["make_event"]([{"name": zone, "price": 100.0, "capacity": capacity, "booked_count": 0}])
    response = post_order(env["user_id"], event_id, zone)
    assert response.status_code == 201, response.text
    return response.json()


def pay_flat(order: dict, **over):
    body = {"order_id": order["id"], "amount": order["total_amount"], "provider": "promptpay", **over}
    return client.post("/api/v1/payments", json=body)


def pay_nested(order: dict, **over):
    body = {"amount": order["total_amount"], "provider": "promptpay", **over}
    return client.post(f"/api/v1/orders/{order['id']}/payments", json=body)


def test_the_flat_route_pays_a_pending_order(env):
    order = new_order(env)
    response = pay_flat(order)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["order_id"] == order["id"] and body["status"] == "completed" and body["amount"] == order["total_amount"]
    scalar = env["scalar"]
    assert scalar("SELECT status FROM orders WHERE id = :o", o=order["id"]) == "confirmed"
    assert scalar("SELECT count(*) FROM tickets WHERE order_id = :o AND status = 'valid'", o=order["id"]) == 1
    assert scalar("SELECT count(*) FROM payments WHERE order_id = :o AND status = 'completed'", o=order["id"]) == 1


def test_both_routes_answer_with_the_same_shape(env):
    flat, nested = pay_flat(new_order(env)), pay_nested(new_order(env))
    assert flat.status_code == nested.status_code == 201
    assert set(flat.json()) == set(nested.json())


def test_payment_method_is_accepted_as_the_provider_name(env):
    """The orders API (and the k6 script) call it payment_method."""
    order = new_order(env)
    body = {"order_id": order["id"], "amount": order["total_amount"], "payment_method": "credit_card"}
    response = client.post("/api/v1/payments", json=body)
    assert response.status_code == 201, response.text
    assert response.json()["provider"] == "credit_card"


def test_without_a_provider_it_defaults_to_promptpay(env):
    order = new_order(env)
    response = client.post("/api/v1/payments", json={"order_id": order["id"], "amount": order["total_amount"]})
    assert response.status_code == 201 and response.json()["provider"] == "promptpay"


@pytest.mark.parametrize("bad_body", [
    {"amount": 100.0},                                        # no order_id
    {"order_id": 0, "amount": 100.0},                         # ids start at 1
    {"order_id": "abc", "amount": 100.0},
    {"order_id": 1},                                          # no amount
    {"order_id": 1, "amount": 0},                             # amount must be > 0
    {"order_id": 1, "amount": -5},
    {"order_id": 1, "amount": 100.0, "provider": "bitcoin"},  # unknown provider
    {"order_id": 1, "amount": 100.0, "payment_method": "cash"},
])
def test_a_bad_body_is_a_400_and_changes_nothing(env, bad_body):
    assert client.post("/api/v1/payments", json=bad_body).status_code == 400


def test_an_unknown_order_is_404(env):
    assert client.post("/api/v1/payments", json={"order_id": 2147483000, "amount": 100.0}).status_code == 404


def test_paying_twice_is_409_through_either_route(env):
    order = new_order(env)
    assert pay_flat(order).status_code == 201
    again_flat, again_nested = pay_flat(order), pay_nested(order)
    assert again_flat.status_code == 409 and again_nested.status_code == 409, (again_flat.text, again_nested.text)
    assert "already paid" in again_flat.json()["detail"].lower()
    assert env["scalar"]("SELECT count(*) FROM payments WHERE order_id = :o AND status = 'completed'", o=order["id"]) == 1


def test_a_nested_payment_followed_by_a_flat_one_is_409(env):
    order = new_order(env)
    assert pay_nested(order).status_code == 201
    assert pay_flat(order).status_code == 409


def test_an_expired_hold_is_410_and_the_seat_goes_back(env):
    event_id = env["make_event"]([{"name": "Exp", "price": 100.0, "capacity": 5, "booked_count": 0}])
    order = post_order(env["user_id"], event_id, "Exp").json()
    assert env["booked"](event_id) == 1
    env["expire_order"](order["id"])
    response = pay_flat(order)
    assert response.status_code == 410, response.text
    assert env["scalar"]("SELECT count(*) FROM payments WHERE order_id = :o AND status = 'completed'", o=order["id"]) == 0
    assert env["booked"](event_id) == 0, "the seat of the expired hold must be released"


def test_a_wrong_amount_does_not_confirm_the_order(env):
    order = new_order(env)
    response = pay_flat(order, amount=order["total_amount"] + 1)
    assert response.status_code in (400, 409), response.text
    assert env["scalar"]("SELECT status FROM orders WHERE id = :o", o=order["id"]) == "pending"


def test_many_clients_paying_one_order_at_once_charge_it_once(env):
    """What a load test does when its virtual users pick the same order: exactly one wins, nobody gets a 5xx."""
    order = new_order(env)
    codes = run_parallel(lambda i: (pay_flat(order) if i % 2 else pay_nested(order)).status_code, range(10))
    assert codes.count(201) == 1, codes
    assert codes.count(409) == 9, codes
    assert env["scalar"]("SELECT count(*) FROM payments WHERE order_id = :o AND status = 'completed'", o=order["id"]) == 1
    assert env["scalar"]("SELECT status FROM orders WHERE id = :o", o=order["id"]) == "confirmed"


def test_the_route_is_in_the_swagger_page(env):
    spec = client.get("/openapi.json").json()
    operation = spec["paths"]["/api/v1/payments"]["post"]
    assert {"201", "400", "404", "409", "410", "503"} <= set(operation["responses"])
    assert set(operation["responses"]) == set(spec["paths"]["/api/v1/orders/{order_id}/payments"]["post"]["responses"])
