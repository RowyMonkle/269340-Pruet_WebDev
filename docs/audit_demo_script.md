# 10-Minute Oral Defense & Audit Script
**Course:** 269340 Web Application Development  
**Project:** Dual-Database Ticket Booking Platform  
**Team:** 
- Natthakritta Aoktan (670615022) — Backend Lead & Project Management
- Nannapat Chaipoon (670615027) — API Engineering & Request Validation
- Poonyaporn Intaphrom (670615029) — Data Engineering & Database Architecture
- Pandara Yutiraksa (670615032) — System Documentation & Frontend Design

---

## ⏱ Time Allocation Overview (10 Minutes)

| Timing | Section | Lead Speaker / Presenter | Key Focus |
| :--- | :--- | :--- | :--- |
| **0:00 – 2:00** | 1. Dual-DB Rationale & Architecture | Natthakritta / Poonyaporn | Relational vs. Document boundaries, ACID vs. Flexibility |
| **2:00 – 4:30** | 2. Live Happy-Path Demo | Nannapat | Browsing catalog, seat holding, and payment confirmation |
| **4:30 – 7:30** | 3. Concurrency & Edge Cases | All Members | Overselling prevention, double-booking, idempotency, hold expiry |
| **7:30 – 9:00** | 4. Zero-Downtime Migrations | Poonyaporn / Pandara | Expand-and-Contract pattern via Alembic |
| **9:00 – 10:00** | 5. Verified Data Volumes & Q&A | Pandara / Team | Verified dataset scale (2,800+ PG, 1,400+ Mongo) |

---

## Part 1: Dual-Database Architectural Rationale (0:00 – 2:00)

### 🎙 Speaking Script:
> *"Our platform uses a dual-database architecture where each database solves a distinct persistence problem:*
>
> 1. ***PostgreSQL (Relational / ACID)** handles our transactional core: Users, Orders, Tickets, Payments, and OutboxEvents. In concert ticketing, seat reservations and financial records must guarantee strict consistency. We use foreign key constraints (`ON DELETE RESTRICT` for users, `ON DELETE CASCADE` for tickets), check constraints (`amount >= 0`), and partial unique indexes (`uq_tickets_event_zone_seat`) to mathematically prevent double-booking.*
> 2. ***MongoDB (Document / Schema-Flexible)** stores the Event catalog and high-volume telemetry ActivityLogs. Event seating configurations vary widely between stadium concerts, theater festivals, and open-field festivals. MongoDB handles heterogeneous zone arrays, dynamic festival attributes, and append-only activity streams without schema lockups.*
> 3. ***Eventual Consistency**: We bridge the two worlds using the **Transactional Outbox Pattern**. When a seat is held or paid in PostgreSQL, an outbox event is committed in the same relational transaction. An asynchronous worker reconciles state with MongoDB, eliminating distributed transactions (2PC).*

---

## Part 2: Live Happy-Path Demonstration (2:00 – 4:30)

Execute these live requests using Swagger UI (`http://localhost:8000/docs`) or terminal `curl`:

### Step 2.1: Verify System Health
```bash
curl -s http://localhost:8000/health | jq .
```
**Expected Response (`200 OK`):**
```json
{
  "status": "healthy",
  "databases": {
    "postgresql": "connected",
    "mongodb": "connected"
  }
}
```

### Step 2.2: Retrieve Concert Event & Authoritative Pricing (MongoDB)
```bash
# Fetch the first seeded event
EVENT_ID=$(curl -s "http://localhost:8000/api/v1/events?limit=1" | jq -r '.[0]._id')
echo "Target Event ID: $EVENT_ID"
```

### Step 2.3: Place Order with 10-Minute Seat Hold (Dual-DB Atomic Operation)
```bash
IDEMP_KEY="DEMO-ORDER-$(date +%s)"
ORDER_RESP=$(curl -s -X POST "http://localhost:8000/api/v1/orders" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $IDEMP_KEY" \
  -d "{
    \"user_id\": 1,
    \"payment_method\": \"promptpay\",
    \"items\": [
      {
        \"event_id\": \"$EVENT_ID\",
        \"seat_zone\": \"VIP\",
        \"seat_number\": \"VIP-A99\"
      }
    ]
  }")

ORDER_ID=$(echo $ORDER_RESP | jq -r '.id')
TOTAL=$(echo $ORDER_RESP | jq -r '.total_amount')
echo "Created Order #$ORDER_ID with Status: $(echo $ORDER_RESP | jq -r '.status'), Expires: $(echo $ORDER_RESP | jq -r '.expires_at')"
```
**Key Checkpoint**: 
- Order status is `pending`.
- Ticket status is `held`.
- MongoDB `booked_count` incremented by 1.

### Step 2.4: Settle Payment & Confirm Order
```bash
curl -s -X POST "http://localhost:8000/api/v1/orders/$ORDER_ID/payments" \
  -H "Content-Type: application/json" \
  -d "{
    \"amount\": $TOTAL,
    \"provider\": \"promptpay\"
  }" | jq .
```
**Key Checkpoint**:
- Order transitions to `confirmed`.
- Ticket transitions from `held` to `valid`.
- Payment record created with status `completed`.

---

## Part 3: Failure Cases & Concurrency Defense (4:30 – 7:30)

Show the examiner the exact error responses produced by the safety mechanisms:

### Scenario A: Same Seat Booked Concurrently (`409 Conflict`)
Attempt to book the exact same seat (`VIP-A99`) on the same event:
```bash
curl -s -X POST "http://localhost:8000/api/v1/orders" \
  -H "Content-Type: application/json" \
  -d "{
    \"user_id\": 2,
    \"payment_method\": \"promptpay\",
    \"items\": [
      {\"event_id\": \"$EVENT_ID\", \"seat_zone\": \"VIP\", \"seat_number\": \"VIP-A99\"}
    ]
  }" | jq .
```
**Expected Output (`409 Conflict`):**
```json
{
  "detail": "Double-booking prevented: One or more selected seats have already been reserved for this event."
}
```
*Architecture Defense*: Protected by PostgreSQL partial unique index:
`CREATE UNIQUE INDEX uq_tickets_event_zone_seat ON tickets (event_id, seat_zone, seat_number) WHERE seat_number IS NOT NULL AND status IN ('held', 'valid');`

---

### Scenario B: Paying Twice for the Same Order (`400 Bad Request`)
Re-submit payment for the confirmed order `$ORDER_ID`:
```bash
curl -s -X POST "http://localhost:8000/api/v1/orders/$ORDER_ID/payments" \
  -H "Content-Type: application/json" \
  -d "{\"amount\": $TOTAL, \"provider\": \"promptpay\"}" | jq .
```
**Expected Output (`400 Bad Request`):**
```json
{
  "detail": "Order ORD-... is already paid and confirmed"
}
```
*Architecture Defense*: Pessimistic row lock `.with_for_update(of=Order)` prevents concurrent double-spend races.

---

### Scenario C: Idempotency Key Replay vs. Payload Tampering (`409 Conflict`)
Submitting the same `Idempotency-Key` with an altered user or item count:
```bash
curl -s -X POST "http://localhost:8000/api/v1/orders" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $IDEMP_KEY" \
  -d "{
    \"user_id\": 999,
    \"payment_method\": \"promptpay\",
    \"items\": [{\"event_id\": \"$EVENT_ID\", \"seat_zone\": \"VIP\", \"seat_number\": \"VIP-DIFF\"}]
  }" | jq .
```
**Expected Output (`409 Conflict`):**
```json
{
  "detail": "Idempotency key reused with different request payload (user or item count mismatch)."
}
```

---

### Scenario D: Expired Seat Hold Settle Attempt (`410 Gone`)
When a user attempts to pay for an order whose 10-minute hold has expired:
```bash
# When simulated or naturally expired:
# Payment returns 410 Gone, releases seats in MongoDB, cancels tickets in PostgreSQL
```
**Expected Output (`410 Gone`):**
```json
{
  "detail": "Order ORD-... seat hold has expired and seats have been released."
}
```

---

## Part 4: Zero-Downtime Schema Evolution (7:30 – 9:00)

Demonstrate our use of the **Expand and Contract Pattern** via Alembic:

```text
1. EXPAND:
   Migration 0002 added nullable columns 'expires_at' and 'idempotency_key' to 'orders' without acquiring exclusive table locks.
   It created the 'payments' table (1:N) and 'outbox_events' table alongside legacy fields.

2. DUAL-WRITE:
   The API writes to both legacy and new structures concurrently.

3. CONTRACT:
   Legacy fields are only deprecated after all clients and services migrate.
```

Demonstrate live migration status:
```bash
docker compose exec api alembic current
# Shows: 0002_add_payments_holds_outbox (head)
```

---

## Part 5: Verified Dataset Scale & Automated Suite (9:00 – 10:00)

Show the examiner the clean test execution and realistic seeded dataset counts:

### Automated Concurrency Suite:
```bash
docker compose exec api pytest tests/test_concurrency.py -v
```
**Verification Result**: All 6 stress scenarios pass cleanly in ~3.2 seconds.

### Database Record Totals:
| Database | Collection / Table | Target Spec | Actual Seeded Count | Status |
| :--- | :--- | :--- | :--- | :--- |
| **PostgreSQL** | `users` | Accounts | **250** | Verified |
| **PostgreSQL** | `orders` | Billing Records | **500** | Verified |
| **PostgreSQL** | `tickets` | Seat Allocations | **1,257** | Verified |
| **PostgreSQL** | `payments` | Transactions | **377** | Verified |
| **PostgreSQL** | `outbox_events`| Eventual Consistency | **500** | Verified |
| **PostgreSQL** | **Total Relational Rows** | $\ge 1,000$ | **2,884** | **Exceeds Target (288%)** |
| **MongoDB** | `events` | Catalog & Stages | **200** | Verified |
| **MongoDB** | `activity_logs` | User Telemetry | **1,207** | Verified |
| **MongoDB** | **Total Documents** | $\ge 1,000$ | **1,407** | **Exceeds Target (140%)** |
