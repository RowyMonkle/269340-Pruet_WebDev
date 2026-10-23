from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Application Settings
    PROJECT_NAME: str = "Ticket Booking Dual-DB API"
    ENVIRONMENT: str = "development"
    DEBUG: bool = True
    API_V1_PREFIX: str = "/api/v1"
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000

    # PostgreSQL Settings (Transactional Core State)
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str = "dev_user"
    POSTGRES_PASSWORD: str = "dev_password"
    POSTGRES_DB: str = "main_db"
    POSTGRES_POOL_SIZE: int = 20
    POSTGRES_MAX_OVERFLOW: int = 10
    # Database protection. Without these a single stuck row lock makes every request queue behind it, the
    # thread pool and the connection pool fill up, and endpoints that never touch that row stop answering.
    POSTGRES_POOL_TIMEOUT: float = 5.0  # seconds to wait for a free pooled connection, then HTTP 503
    POSTGRES_LOCK_TIMEOUT_MS: int = 5000  # a statement waits at most this long for a lock, then HTTP 409 (SQLSTATE 55P03)
    POSTGRES_STATEMENT_TIMEOUT_MS: int = 15000  # a statement runs at most this long, then HTTP 503 (SQLSTATE 57014)
    POSTGRES_CONNECT_TIMEOUT_S: int = 2  # opening a new connection gives up after this long (same network: 2 s is generous)
    DATABASE_URL: Optional[str] = None

    @property
    def sync_database_url(self) -> str:
        if self.DATABASE_URL:
            return self.DATABASE_URL
        return (
            f"postgresql+psycopg2://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}@"
            f"{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    # MongoDB Settings (Catalog & Telemetry Documents)
    MONGO_HOST: str = "localhost"
    MONGO_PORT: int = 27017
    MONGO_USER: Optional[str] = None
    MONGO_PASSWORD: Optional[str] = None
    MONGO_DB: str = "main_db"
    MONGO_MAX_POOL_SIZE: int = 50
    MONGO_MIN_POOL_SIZE: int = 10
    MONGO_URI: Optional[str] = None
    # Short on purpose: every request that waits for an unreachable MongoDB holds one of the
    # server's worker threads, so a long wait turns a MongoDB outage into an outage of everything.
    MONGO_SERVER_SELECTION_TIMEOUT_MS: int = 1000
    MONGO_CONNECT_TIMEOUT_MS: int = 2000
    MONGO_SOCKET_TIMEOUT_MS: int = 10000

    # Background worker (runs on every replica, rows are claimed with SKIP LOCKED)
    SWEEP_BATCH_SIZE: int = 500  # expired holds released per transaction
    SWEEP_MAX_BATCHES: int = 20  # batches per 15 s tick (10,000 holds)
    OUTBOX_BATCH_SIZE: int = 1000  # outbox rows marked processed per statement
    OUTBOX_MAX_BATCHES: int = 20
    OUTBOX_RETENTION_DAYS: int = 7  # processed outbox rows older than this are deleted (0 = keep forever)

    # bcrypt cost factor for password hashing (each +1 doubles the time)
    BCRYPT_ROUNDS: int = 12

    # Runtime feature flags (CP2 expand/contract phases)
    FLAG_CACHE_SECONDS: float = 2.0
    # Required in the X-Admin-Token header for /admin endpoints. Empty disables them.
    ADMIN_TOKEN: str = ""

    @property
    def mongo_connection_uri(self) -> str:
        if self.MONGO_URI:
            return self.MONGO_URI
        if self.MONGO_USER and self.MONGO_PASSWORD:
            return f"mongodb://{self.MONGO_USER}:{self.MONGO_PASSWORD}@{self.MONGO_HOST}:{self.MONGO_PORT}"
        return f"mongodb://{self.MONGO_HOST}:{self.MONGO_PORT}"


settings = Settings()
