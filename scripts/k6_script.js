import http from 'k6/http';
import { check, sleep } from 'k6';
import { Rate } from 'k6/metrics';

export const errorRate = new Rate('http_errors');

export const options = {
  stages: [
    { duration: '20s', target: 50 },    // Ramp-up to 50 VUs
    { duration: '2m20s', target: 50 },   // 50 VUs sustained load
    { duration: '20s', target: 0 },     // Ramp-down
  ],
  thresholds: {
    'http_req_duration': ['p(95)<200', 'p(99)<500'], // Latency p95 < 200ms, p99 < 500ms
    'http_errors': ['rate<0.01'],                     // Strict error rate < 1%
  },
};

const BASE_URL = 'http://127.0.0.1:8000/api/v1';

const params = {
  headers: {
    'Content-Type': 'application/json',
  },
};

export function setup() {
  const res = http.get(`${BASE_URL}/events`);
  const eventTargets = [];

  try {
    const data = res.json();
    const items = data.items || data;

    for (const ev of items) {
      if (ev.zones && ev.zones.length > 0) {
        const zoneNames = ev.zones.map((z) => z.name || z.zone || z);
        eventTargets.push({
          id: ev.id,
          zones: zoneNames,
        });
      }
    }
  } catch (err) {
    console.error('Failed to setup events:', err);
  }

  if (eventTargets.length === 0) {
    eventTargets.push({
      id: '6adc62df13db50bf31a1612e',
      zones: ['VIP Standing', 'Front Zone A', 'General Admission'],
    });
  }

  return { targets: eventTargets };
}

export default function (data) {
  const randUser = Math.floor(Math.random() * 250) + 1;
  const isWrite = Math.random() < 0.35; // 35% Write, 65% Read

  if (!isWrite) {
    // ---------------- READ OPERATIONS ----------------
    if (Math.random() < 0.5) {
      const res = http.get(`${BASE_URL}/events`);
      const ok = check(res, { 'events status 200': (r) => r.status === 200 });
      errorRate.add(!ok);
    } else {
      const res = http.get(`${BASE_URL}/users/${randUser}`);
      const ok = check(res, { 'user status 200': (r) => r.status === 200 });
      errorRate.add(!ok);
    }
  } else {
    // ---------------- WRITE OPERATIONS ----------------
    if (Math.random() < 0.5) {
      const target = data.targets[Math.floor(Math.random() * data.targets.length)];
      const chosenZone = target.zones[Math.floor(Math.random() * target.zones.length)];

      const orderPayload = JSON.stringify({
        user_id: randUser,
        items: [
          {
            event_id: String(target.id),
            seat_zone: chosenZone,
            quantity: 1,
          },
        ],
      });

      const res = http.post(`${BASE_URL}/orders`, orderPayload, params);
      // Valid order outcomes: 200/201 (success), 409 (sold out / seat lock), 400/404 (unavailable)
      const ok = check(res, {
        'order status valid': (r) => [200, 201, 400, 404, 409].includes(r.status),
      });
      errorRate.add(!ok);
    } else {
      const paymentPayload = JSON.stringify({
        order_id: Math.floor(Math.random() * 50) + 1,
        amount: 100.0,
        payment_method: 'credit_card',
      });

      const res = http.post(`${BASE_URL}/payments`, paymentPayload, params);
      // Valid payment outcomes: 200/201 (success), 409 (already paid), 410 (expired order), 400/404 (not found)
      const ok = check(res, {
        'payment status valid': (r) => [200, 201, 400, 404, 409, 410].includes(r.status),
      });
      errorRate.add(!ok);
    }
  }

  sleep(0.2); // 200ms think time
}