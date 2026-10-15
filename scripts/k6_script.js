import http from 'k6/http';
import { check, sleep } from 'k6';
import { Rate } from 'k6/metrics';

// กำหนด Metric สำหรับ Error Rate
export const errorRate = new Rate('http_errors');

export const options = {
  // จำลอง 50 VUs รันต่อเนื่องอย่างน้อย 3 นาที
  stages: [
    { duration: '20s', target: 50 },  // Ramp-up สู่ 50 VUs
    { duration: '2m20s', target: 50 }, // แช่โหลดที่ 50 VUs
    { duration: '20s', target: 0 },   // Ramp-down
  ],
  // กำหนดเงื่อนไขผ่านเกณฑ์ตามโจทย์ CP3
  thresholds: {
    'http_req_duration': ['p(95)<200', 'p(99)<500'], // p95 < 200ms, p99 < 500ms
    'http_errors': ['rate<0.01'],                    // Error rate < 1%
  },
};

const BASE_URL = 'http://localhost:8000/api/v1';

export default function () {
  const randUser = Math.floor(Math.random() * 250) + 1;
  const isWrite = Math.random() < 0.3; // ผสมโหลด Write ~30%, Read ~70%

  if (!isWrite) {
    // 1. Read Operations (GET /events, GET /users)
    if (Math.random() < 0.5) {
      const res = http.get(`${BASE_URL}/events`);
      const success = check(res, { 'events status 200': (r) => r.status === 200 });
      errorRate.add(!success);
    } else {
      const res = http.get(`${BASE_URL}/users/${randUser}`);
      const success = check(res, { 'user status 200': (r) => r.status === 200 });
      errorRate.add(!success);
    }
  } else {
    // 2. Write Operations (POST /orders, POST /payments)
    const params = { headers: { 'Content-Type': 'application/json' } };

    if (Math.random() < 0.5) {
      const orderPayload = JSON.stringify({
        user_id: randUser,
        event_id: Math.floor(Math.random() * 10) + 1,
        ticket_quantity: 1,
      });
      const res = http.post(`${BASE_URL}/orders`, orderPayload, params);
      // 200/201 หรือ 409 (Concurrency Conflict) ถือว่าระบบรองรับได้ถูกต้อง
      const success = check(res, { 'order valid status': (r) => [200, 201, 409].includes(r.status) });
      errorRate.add(!success);
    } else {
      const paymentPayload = JSON.stringify({
        order_id: Math.floor(Math.random() * 500) + 1,
        amount: 500.0,
        payment_method: 'credit_card',
      });
      const res = http.post(`${BASE_URL}/payments`, paymentPayload, params);
      const success = check(res, { 'payment valid status': (r) => [200, 201, 400, 404].includes(r.status) });
      errorRate.add(!success);
    }
  }

  sleep(0.2); // หน่วงเล็กน้อยเพื่อจำลองพฤติกรรมผู้ใช้
}