### Key Enhancements:
1. **Automated Schema Migration (`ticweb_migrate`)**:
   - Runs `scripts/migrate.py` automatically on `docker compose up`.
   - Automatically detects whether the database is fresh, initialized via `init.sql`, or already tracked by Alembic, then stamps and upgrades to `head`.
   - **No manual `alembic stamp head` required anymore!**
2. **Dual API Replicas & Load Balancing (`nginx`)**:
   - Nginx listens on port `8000` and balances traffic across two FastAPI instances (`api` and `api_2`).
   - If one container restarts or crashes, Nginx automatically retries and fails over to the healthy replica without downtime.
3. **Zero-Downtime Rolling Deploys (`scripts/rolling_deploy.sh`)**:
   - Rebuilds code and updates replicas sequentially (`api` $\rightarrow$ wait healthy $\rightarrow$ `api_2`), ensuring zero request drops.
4. **Runtime Feature Flags (`feature_flags` table & `/api/v1/admin/flags`)**:
   - Dynamically toggle features in PostgreSQL without redeploying code.
5. **Standardized Container Names**:
   - Containers are prefixed with `ticweb_*` under the `269340-pruet_ticweb` project namespace.

---

## Quick Start

### Step 1: Clone and Configure `.env`
```bash
git clone https://github.com/RowyMonkle/269340-Pruet_WebDev.git
cd 269340-Pruet_WebDev

# Copy template to active .env
cp .env.example .env
```

### Step 2: Start the Entire Stack
```bash
docker compose up --build -d
```
Docker will automatically:
1. Start PostgreSQL (`ticweb_postgres`) and MongoDB (`ticweb_mongo`).
2. Wait for database healthchecks to pass.
3. Run `ticweb_migrate` to bring the database schema to Alembic head.
4. Start both API replicas (`ticweb_api_1`, `ticweb_api_2`).
5. Start Nginx (`ticweb_nginx`) on port `8000`.

### Step 3: Verify Container Health
```bash
docker compose ps
```
You should see all containers running:
- `ticweb_postgres`: `Up (healthy)`
- `ticweb_mongo`: `Up (healthy)`
- `ticweb_migrate`: `Exited (0)` *(one-shot job succeeded)*
- `ticweb_api_1`: `Up (healthy)`
- `ticweb_api_2`: `Up (healthy)`
- `ticweb_nginx`: `Up`

### Step 4: Seed Realistic Test Data
Populate realistic mock data into both PostgreSQL and MongoDB:
```bash
docker compose exec api python seed.py
```
> **Output:** ~2,884 PostgreSQL rows (Users, Orders, Tickets, Payments, Outbox) and ~1,407 MongoDB documents (Events, ActivityLogs).

### Step 5: Run Concurrency & Stress Tests
```bash
docker compose exec api pytest tests/test_concurrency.py -v
```
All 6 concurrency test scenarios should pass with `6 passed`.

---

## 🌐 3. Service Ports & Useful URLs

| Service | Host URL / Port | Credentials / Notes |
| :--- | :--- | :--- |
| **Public API Gateway (Nginx)** | [http://localhost:8000](http://localhost:8000) | Main entrypoint for clients, frontend, and tests |
| **Interactive Swagger UI** | [http://localhost:8000/docs](http://localhost:8000/docs) | Interactive API testing documentation |
| **ReDoc UI** | [http://localhost:8000/redoc](http://localhost:8000/redoc) | Clean API documentation |
| **Health Check** | [http://localhost:8000/health](http://localhost:8000/health) | Returns `{"status":"healthy","databases":{...}}` |
| **Nginx Status** | [http://localhost:8000/nginx-health](http://localhost:8000/nginx-health) | Verifies Nginx reverse proxy |
| **API Replica 1 (Direct Debug)** | [http://localhost:8001](http://localhost:8001) | Direct port to `ticweb_api_1` |
| **API Replica 2 (Direct Debug)** | [http://localhost:8002](http://localhost:8002) | Direct port to `ticweb_api_2` |
| **PostgreSQL 15** | `localhost:5432` | User: `dev_user` / Pass: `dev_password` / DB: `main_db` |
| **MongoDB 6.0** | `localhost:27017` | DB: `main_db` (No auth in dev) |

---

## 👥 4. Role-Specific Guidelines

### 🅰️ Backend Lead & DB Architecture (Natthakritta / Poonyaporn)
1. **Making Schema Changes (Expand & Contract)**:
   - Modify SQLAlchemy models in [`app/models/sql_models.py`](app/models/sql_models.py).
   - Generate migration:
     ```bash
     docker compose exec api alembic revision -m "expand_your_feature_name"
     ```
   - Edit the generated file in `alembic/versions/`. Follow Expand-and-Contract rules: columns must be `nullable=True` or have server defaults.
   - Apply migrations:
     ```bash
     docker compose exec api alembic upgrade head
     ```
2. **Feature Flags**:
   - Check if a flag is active:
     ```python
     from app.core.flags import is_flag_enabled
     if is_flag_enabled(db, "my_new_feature"):
         ...
     ```
   - Switch flags dynamically using the admin endpoint:
     ```bash
     curl -X POST "http://localhost:8000/api/v1/admin/flags/my_new_feature" \
       -H "X-Admin-Token: dev-admin-token" \
       -H "Content-Type: application/json" \
       -d '{"enabled": true}'
     ```
3. **Zero-Downtime Deployment**:
   - When updating backend logic, test the rolling deploy script:
     ```bash
     sh scripts/rolling_deploy.sh
     ```

### 🅱️ API Engineering & Validation (Nannapat)
1. **Adding New Endpoints**:
   - Create routers in [`app/api/v1/`](app/api/v1/).
   - Define request/response models in [`app/schemas/`](app/schemas/).
   - Register routers in [`app/api/v1/__init__.py`](app/api/v1/__init__.py).
2. **Error Handling**:
   - Ensure input validation errors return `400 Bad Request` or `409 Conflict` (for business/concurrency violations) rather than unhandled exceptions.
3. **Running Specific Tests**:
   ```bash
   # Run only idempotency tests:
   docker compose exec api pytest tests/test_concurrency.py -k "idempotency" -v
   ```

### 🅲 Frontend & System Documentation (Pandara)
1. **Connecting the Frontend**:
   - Point your API calls to base URL: `http://localhost:8000/api/v1`.
   - CORS is already enabled in `app/main.py` allowing all origins (`*`).
   - Example endpoints:
     - `GET /api/v1/events` (Catalog)
     - `POST /api/v1/orders` (Hold seats with `Idempotency-Key` header)
     - `POST /api/v1/orders/{id}/payments` (Confirm payment)
2. **Git Hygiene**:
   - Keep `node_modules/`, `dist/`, and `.env.local` ignored in `.gitignore`.
   - Do NOT add frontend service to `docker-compose.yml` until the frontend build is tested and verified.

---

## ❓ 5. Troubleshooting & FAQ

### Q1: `FATAL: role "dev_user" does not exist` on Windows
- **Cause:** You have a local Windows PostgreSQL service (`postgresql-x64-18`) running on port 5432 that intercepts `localhost:5432`.
- **Solution:** 
  1. Open PowerShell as Administrator.
  2. Temporarily stop the Windows service:
     ```powershell
     Stop-Service postgresql-x64-18
     ```
  3. Run the container command: `docker compose exec api python seed.py` (which routes internally, unaffected by Windows services).

### Q2: How do I do a complete factory reset?
If data or schema states get out of sync, wipe volumes and rebuild clean:
```bash
docker compose down -v
docker compose up --build -d
docker compose exec api python seed.py
```

### Q3: How do I view logs for a specific service?
```bash
# View combined logs:
docker compose logs -f

# View Nginx routing logs:
docker compose logs -f nginx

# View API replica logs:
docker compose logs -f api api_2
```
