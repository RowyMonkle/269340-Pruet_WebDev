# 269340 Ticket Booking Platform — Checkpoint 1: Core Architecture & Dual-Database Model

A high-performance, production-grade Dual-Database backend API for concert, music event, and festival ticket booking. Built with **FastAPI**, **PostgreSQL** (ACID transactional core state), and **MongoDB** (flexible event catalog & telemetry).

---

## 1. Team Roster (4)

| Student ID | Full Name | Role |
| :--- | :--- | :--- |
| **670615022** | Natthakritta Aoktan | Backend Lead & Database Architecture |
| **670615027** | Nannapat Chaipoon | API Engineering & Request Validation |
| **670615029** | Poonyaporn Intaphrom | Data Engineering & Project Management |
| **670615032** | Pandara Yutiraksa | System Documentation & Frontend design |

---

## 2. System Architecture Diagram

```
 ┌────────────────┐
 │ Client Browser │
 └───────┬────────┘
         │ (HTTP / JSON API)
         ▼
 ┌────────────────┐
 │  Web API Tier  │
 │ (C# / Python)  │
 └────┬──────┬────┘
      │      │ 
(Relational) │ (Document)
 ┌────┘      └────┐
 ▼                ▼
┌──────────────┐ ┌──────────────┐
│  PostgreSQL  │ │   MongoDB    │
│(Transactional│ │  (Catalog/   │
│ Core State)  │ │Unstructured) │
└──────────────┘ └──────────────┘
```

### Detailed Component Architecture

```mermaid
graph TD
    Client["Client Browser / Mobile App / API Consumer"] -->|"HTTP / RESTful JSON"| FastAPI["FastAPI Backend Tier (Port 8000)"]
    
    subgraph Relational_Boundary ["PostgreSQL 15 (Port 5432) - Transactional Core State"]
        Users["users Table (Accounts, Auth, Roles)"]
        Orders["orders Table (Billing, Status, Payment)"]
        Tickets["tickets Table (Seat Allocation, QR Codes)"]
        
        Users -->|"1 : N"| Orders
        Orders -->|"1 : N (Cascade)"| Tickets
    end

    subgraph Document_Boundary ["MongoDB 6.0 (Port 27017) - Catalog & Unstructured"]
        Events["events Collection (Dynamic Stage Layouts, Artist Info, Tags)"]
        ActivityLogs["activity_logs Collection (High-Volume Telemetry & Auditing)"]
    end

    FastAPI -->|"SQLAlchemy 2.0 Pool"| Users
    FastAPI -->|"PyMongo Connection Pool"| Events
    Tickets -.->|"Cross-DB Reference: event_id"| Events
```

### Domain Boundary Separation:
1. **PostgreSQL (Port 5432)**:
   - **Users**: Core user credentials, security hashes, and role-based access (`fan`, `organizer`, `admin`).
   - **Orders**: ACID transactional records preventing double charging and maintaining exact financial state.
   - **Tickets**: Discrete seat/zone records strictly linked to orders with unique ticket codes to prevent double-booking.
   
2. **MongoDB (Port 27017)**:
   - **Events**: Flexible JSON documents supporting heterogeneous stage zones, nested artist profiles, and arbitrary `dynamic_attributes` (age policies, stage specs, festival passes).
   - **ActivityLogs**: Append-only telemetry recording search behavior, checkout funnels, and interaction metrics.

---

## 3. Quick Start & Setup Instructions

### Prerequisites
- [Docker & Docker Compose](https://docs.docker.com/get-docker/)
- [Python 3.10+](https://www.python.org/)

### Step 1: Clone Repository & Configure Environment
```bash
git clone https://github.com/RowyMonkle/269340-Pruet_WebDev.git
cd 269340-Pruet_WebDev

# Copy environment file template
cp .env.example .env
```

### Step 2: Launch Services with Docker Compose

**Option A: Launch Database Containers Only (Local Python Development)**
```bash
docker compose up -d postgres_db mongo_db
docker compose ps
```

**Option B: Full-Stack Container Orchestration (API + Databases)**
```bash
docker compose up --build -d
docker compose ps
```

### Step 3: Set Up Python Virtual Environment
```bash
# Create and activate virtual environment
python -m venv .venv

# On Linux / macOS:
source .venv/bin/activate

# On Windows (PowerShell):
.venv\Scripts\Activate.ps1
```

### Step 4: Install Dependencies
```bash
pip install -r requirements.txt
```

### Step 5: Mark Baseline Database Migration
Since Docker automatically initializes the PostgreSQL schema via `init-db/init.sql` upon container startup, stamp the baseline version in Alembic:
```bash
alembic stamp head
```
*(For future zero-downtime schema evolution using the Expand-and-Contract pattern, create migrations via `alembic revision -m "..."` and apply them with `alembic upgrade head`).*

### Step 6: Seed Over 2,800 Realistic Records
Execute the automated database seeder to pre-populate realistic mock data in **both** databases:
```bash
python seed.py
```
**Output Summary:**
- **PostgreSQL**: ~2,875 records (250 Users, 500 Orders, ~1,250 Tickets, ~375 Payments, ~500 OutboxEvents).
- **MongoDB**: ~1,400 documents (200 Events with stage zones, 1,200 ActivityLogs referencing valid user IDs).

### Step 7: Run FastAPI Server (If running locally)
```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```
- Interactive Swagger UI: [http://localhost:8000/docs](http://localhost:8000/docs)
- Interactive ReDoc: [http://localhost:8000/redoc](http://localhost:8000/redoc)
- Health Check: [http://localhost:8000/health](http://localhost:8000/health)

### Step 8: Run Concurrency & Stress Tests
```bash
pytest tests/test_concurrency.py -v
```

---

## 4. API Endpoints Summary

| Method | Route | Target DB | Status | Description |
| :--- | :--- | :--- | :--- | :--- |
| `GET` | `/health` | Both | `200` / `503` | Verifies live connectivity to PostgreSQL and MongoDB |
| `POST` | `/api/v1/users` | PostgreSQL | `201`, `400`, `409` | Register user account (strictly forces fan role, hashes password) |
| `GET` | `/api/v1/users/{id}` | PostgreSQL | `200`, `404` | Fetch user details via `.options(load_only(...))` projection |
| `GET` | `/api/v1/users` | PostgreSQL | `200` | Paginated list of users (optimized projection, zero N+1 queries) |
| `GET` | `/api/v1/events` | MongoDB | `200` | Fetch paginated concert/festival catalog with field projections |
| `POST` | `/api/v1/events` | MongoDB | `201`, `400` | Create event document with nested dynamic attributes |
| `GET` | `/api/v1/events/{id}` | MongoDB | `200`, `404` | Fetch single event document by its ObjectId |
| `GET` | `/api/v1/products` | MongoDB | `200` | Catalog alias for /events |
| `POST` | `/api/v1/products` | MongoDB | `201` | Catalog alias for /events |
| `POST` | `/api/v1/orders` | Dual-DB | `201`, `400`, `409` | Atomic checkout with 10-min seat hold, `Idempotency-Key` header support |
| `GET` | `/api/v1/orders/{id}`| PostgreSQL | `200`, `404` | Fetch order details with associated tickets and payments (joinedload) |
| `POST` | `/api/v1/orders/{id}/payments` | PostgreSQL | `201`, `400` | Process payment, confirm order, activate tickets (`held` $\rightarrow$ `valid`) |
| `GET` | `/api/v1/orders/{id}/payments` | PostgreSQL | `200`, `404` | List payment transaction attempts for an order |
| `POST` | `/api/v1/orders/cleanup-expired` | Dual-DB | `200` | Reconcile and release expired seat holds back to MongoDB |

---

## 5. Architectural Directives & Patterns

### 1. Seat Holds & Expiry State Machine
Orders follow an industry-standard state machine:
$$\text{Pending (Held)} \xrightarrow{\text{Payment Complete}} \text{Confirmed (Valid)} \quad \Big| \quad \text{Pending (Held)} \xrightarrow{\text{10 min Timeout}} \text{Expired (Released)}$$
- During the 10-minute hold window, tickets are marked `held`.
- The PostgreSQL partial unique index `uq_tickets_event_zone_seat` protects seats in both `held` and `valid` states from double-booking.
- Background reconciliation automatically releases expired seats back to the MongoDB event capacity.

### 2. Transactional Outbox Pattern (Dual-DB Consistency)
To guarantee eventual consistency across PostgreSQL and MongoDB:
- Every state change writes an atomic event (`SEAT_HELD`, `ORDER_PAID`, `SEAT_HOLD_EXPIRED`) into the PostgreSQL `outbox_events` table within the same database transaction.
- An asynchronous worker processes outbox records to update MongoDB, ensuring network failures or crashes never leave the databases permanently out of sync.

### 3. Database Projections & Eliminating N+1 Bottlenecks
- **SQLAlchemy (PostgreSQL)**: All entity queries utilize `.options(load_only(...))` including `updated_at` so heavy or sensitive columns (e.g. `hashed_password`) are never loaded into memory, and no secondary lazy-load queries are triggered. Related ticket collections use `joinedload` to prevent N+1 query loops.
- **PyMongo (MongoDB)**: All collection queries use projection dictionaries (e.g. `{"_id": 1, "title": 1, "zones": 1, ...}`) to eliminate document over-fetching over the wire.

### 4. Zero-Downtime Schema Evolution (Expand and Contract Pattern)
All database schema alterations avoid table-locking operations:
1. **Expand**: Add new columns with `nullable=True` or safe default values alongside legacy fields.
2. **Dual Write**: API writes to both legacy and new schema versions concurrently.
3. **Backfill**: Background worker asynchronously migrates legacy records.
4. **Switch Read**: Read traffic shifts from old columns to new columns.
5. **Contract**: Safely drop deprecated columns only after previous application versions are fully decommissioned.

### 5. Git Hygiene & Security
- Strict `.gitignore` prevents tracking virtual environments (`.venv`), `.env` configuration files, database files (`*.db`, `*.sqlite`), and credentials.
- Pure ORM queries and parameterized SQL prevent SQL Injection vulnerabilities.
