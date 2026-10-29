import http from 'k6/http';
import { check, sleep } from 'k6';
import { Rate, Counter } from 'k6/metrics';

// CP3 load benchmark: 50 VUs for 3 minutes of concurrent read/write transactions.
//
// How to run (the stack must be up and seeded: docker compose up -d && docker compose exec api python seed.py):
//   docker run --rm --add-host=host.docker.internal:host-gateway -v "$PWD/scripts:/scripts:ro" -v "$PWD/docs:/out" \
//     -e BASE_URL=http://host.docker.internal:8000/api/v1 grafana/k6 run /scripts/k6_script.js \
//     --summary-export /out/k6_summary.json --out csv=/out/perf/k6_points.csv
//   (nginx answers on host port 8000; this form needs no Docker network and does not depend on the name of the project folder)
//   (docs/k6_summary.json is the report's source; docs/perf/k6_points.csv is the raw data for the charts and is git-ignored)
// or, with k6 installed locally and the stack on port 8000:   k6 run scripts/k6_script.js
//
// What counts as a success: ONLY the status the operation is meant to return (200 for reads, 201 for a new order
// and for a payment). 409 is accepted in the two places where it is the correct answer (the contested hot zone
// and the deliberate double payment). Everything else is a failure, so http_req_failed and http_errors are honest.

export const errorRate = new Rate('http_errors');
export const ordersCreated = new Counter('orders_created');
export const paymentsCompleted = new Counter('payments_completed');
export const doublePaymentsRejected = new Counter('double_payments_rejected');
export const hotSeatsWon = new Counter('hot_zone_orders_won');
export const hotSeatsSoldOut = new Counter('hot_zone_orders_sold_out');

const HOT_ZONE_CAPACITY = 25; // seats of the contested zone: exactly this many orders may win, never more, never fewer

export const options = {
  stages: [
    { duration: '10s', target: 50 },    // Ramp-up to 50 VUs
    { duration: '3m', target: 50 },     // 50 VUs sustained load (CP3: at least 50 VUs for 3 minutes)
    { duration: '10s', target: 0 },     // Ramp-down
  ],
  summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(90)', 'p(95)', 'p(99)'],
  thresholds: {
    'http_req_duration': ['p(95)<200', 'p(99)<500'],   // Latency p95 < 200ms, p99 < 500ms
    'http_req_failed': ['rate<0.01'],                   // HTTP error rate < 1% (k6's own metric: anything but the expected status)
    'http_errors': ['rate<0.01'],                       // Strict error rate < 1% (checks)
    'checks': ['rate>0.99'],
    'hot_zone_orders_won': [`count==${HOT_ZONE_CAPACITY}`], // race condition prevention under load: no oversell, no lost seat
  },
};

const BASE_URL = __ENV.BASE_URL || 'http://127.0.0.1:8000/api/v1';

// By default a response is "expected" only when it is 2xx. A request that is SUPPOSED to be refused (409) says so itself.
http.setResponseCallback(http.expectedStatuses({ min: 200, max: 299 }));

const params = (name) => ({
  headers: {
    'Content-Type': 'application/json',
  },
  tags: { name }, // one label per endpoint instead of one per URL
});

const expecting = (name, ...statuses) => ({
  ...params(name),
  responseCallback: http.expectedStatuses(...statuses),
});

function createEvent(title, zoneName, capacity) {
  const when = new Date(Date.now() + 30 * 24 * 3600 * 1000).toISOString();
  const res = http.post(
    `${BASE_URL}/events`,
    JSON.stringify({
      title: `${title} ${Date.now()}`,
      category: 'Concert',
      artist: { name: 'k6 Load Band', genre: 'Rock', bio: '' },
      venue: { name: 'k6 Arena', city: 'Bangkok', country: 'Thailand', capacity: Math.max(capacity, 1000) },
      date_time: when,
      zones: [{ name: zoneName, price: 100.0, capacity, booked_count: 0 }],
      tags: ['k6'],
      dynamic_attributes: {},
    }),
    params('setup: POST /events'),
  );
  if (res.status !== 201) {
    throw new Error(`setup failed: POST /events answered ${res.status} ${res.body}`);
  }
  return res.json().id;
}

export function setup() {
  // The seeded events have a few hundred seats: a 3-minute run would sell them out and every later order would be
  // refused. So the benchmark creates its own events: one with a very large zone (the normal traffic) and one
  // small contested zone (the race: 50 VUs fight for HOT_ZONE_CAPACITY seats).
  const eventId = createEvent('k6 Benchmark Event', 'Load GA', 1000000);
  const hotEventId = createEvent('k6 Contested Event', 'Hot Zone', HOT_ZONE_CAPACITY);

  return {
    eventId,
    zone: 'Load GA',
    hotEventId,
    hotZone: 'Hot Zone',
  };
}

function placeOrder(userId, eventId, zone, name, ...accepted) {
  const orderPayload = JSON.stringify({
    user_id: userId,
    payment_method: 'credit_card',
    items: [
      {
        event_id: eventId,
        seat_zone: zone,
      },
    ],
  });
  return http.post(`${BASE_URL}/orders`, orderPayload, accepted.length ? expecting(name, ...accepted) : params(name));
}

function pay(order, name, ...accepted) {
  const paymentPayload = JSON.stringify({
    order_id: order.id,
    amount: order.total_amount,
    payment_method: 'credit_card',
  });
  return http.post(`${BASE_URL}/payments`, paymentPayload, accepted.length ? expecting(name, ...accepted) : params(name));
}

export default function (data) {
  const randUser = Math.floor(Math.random() * 250) + 1;
  const isWrite = Math.random() < 0.35; // 35% Write, 65% Read

  if (!isWrite) {
    // ---------------- READ OPERATIONS ----------------
    const pick = Math.random();
    if (pick < 0.35) {
      const res = http.get(`${BASE_URL}/events?page=${Math.floor(Math.random() * 3) + 1}&page_size=20`, params('GET /events'));
      const ok = check(res, { 'events status 200': (r) => r.status === 200 });
      errorRate.add(!ok);
    } else if (pick < 0.5) {
      const res = http.get(`${BASE_URL}/events/${data.eventId}`, params('GET /events/{id}'));
      const ok = check(res, { 'event detail status 200': (r) => r.status === 200 });
      errorRate.add(!ok);
    } else if (pick < 0.75) {
      const res = http.get(`${BASE_URL}/users/${randUser}`, params('GET /users/{id}'));
      const ok = check(res, { 'user status 200': (r) => r.status === 200 });
      errorRate.add(!ok);
    } else {
      const res = http.get(`${BASE_URL}/orders/summary?user_id=${randUser}&limit=20`, params('GET /orders/summary'));
      const ok = check(res, { 'order summary status 200': (r) => r.status === 200 });
      errorRate.add(!ok);
    }
  } else if (Math.random() < 0.1) {
    // ---------------- WRITE OPERATIONS: the contested hot zone ----------------
    // 201 (won a seat) and 409 (sold out) are both correct answers here; the threshold on hot_zone_orders_won
    // proves that exactly HOT_ZONE_CAPACITY orders won.
    const res = placeOrder(randUser, data.hotEventId, data.hotZone, 'POST /orders (hot zone)', 201, 409);
    const ok = check(res, { 'hot zone order is 201 or 409': (r) => r.status === 201 || r.status === 409 });
    errorRate.add(!ok);
    if (res.status === 201) {
      hotSeatsWon.add(1);
    } else if (res.status === 409) {
      hotSeatsSoldOut.add(1);
    }
  } else {
    // ---------------- WRITE OPERATIONS: book, then pay ----------------
    const res = placeOrder(randUser, data.eventId, data.zone, 'POST /orders');
    const created = check(res, { 'order status 201': (r) => r.status === 201 });
    errorRate.add(!created);

    if (created) {
      ordersCreated.add(1);
      const order = res.json();

      if (Math.random() < 0.7) { // 70% of the buyers pay, the rest abandon the cart (the hold expires and is swept)
        sleep(0.2);
        const payRes = pay(order, 'POST /payments');
        const paid = check(payRes, { 'payment status 201': (r) => r.status === 201 });
        errorRate.add(!paid);

        if (paid) {
          paymentsCompleted.add(1);

          if (Math.random() < 0.05) { // 5% of the payments are repeated on purpose: the second charge must be refused
            const again = pay(order, 'POST /payments (double payment)', 409);
            const refused = check(again, { 'double payment refused with 409': (r) => r.status === 409 });
            errorRate.add(!refused);
            if (refused) {
              doublePaymentsRejected.add(1);
            }
          }
        }
      }
    }
  }

  sleep(0.2); // 200ms think time
}

export function teardown(data) {
  // after the load: the contested zone must be sold out exactly, never beyond its capacity
  const res = http.get(`${BASE_URL}/events/${data.hotEventId}`, params('teardown: GET /events/{id}'));
  const zone = res.json().zones.find((z) => z.name === data.hotZone);
  console.log(`hot zone after the test: booked_count=${zone.booked_count} capacity=${zone.capacity}`);
  check(res, {
    'hot zone sold exactly its capacity (no oversell)': () => zone.booked_count === HOT_ZONE_CAPACITY,
  });
}
