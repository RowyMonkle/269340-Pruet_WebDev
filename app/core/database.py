import logging
from typing import Generator
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from pymongo import MongoClient
from pymongo.database import Database

from app.core.config import settings

logger = logging.getLogger(__name__)

# ==============================================================================
# PostgreSQL Connection & Session Pool
# ==============================================================================
def _pg_connect_args() -> dict:
    """Server-side limits applied to every pooled connection (see Settings for the reasoning).

    lock_timeout      a statement that waits this long for a lock fails with SQLSTATE 55P03 -> HTTP 409
    statement_timeout a statement that runs this long is cancelled with SQLSTATE 57014      -> HTTP 503
    A value of 0 switches that limit off.
    """
    options = []
    if settings.POSTGRES_LOCK_TIMEOUT_MS > 0:
        options.append(f"-c lock_timeout={settings.POSTGRES_LOCK_TIMEOUT_MS}")
    if settings.POSTGRES_STATEMENT_TIMEOUT_MS > 0:
        options.append(f"-c statement_timeout={settings.POSTGRES_STATEMENT_TIMEOUT_MS}")
    args = {"connect_timeout": settings.POSTGRES_CONNECT_TIMEOUT_S}
    if options:
        args["options"] = " ".join(options)
    return args


engine = create_engine(
    settings.sync_database_url,
    pool_size=settings.POSTGRES_POOL_SIZE,
    max_overflow=settings.POSTGRES_MAX_OVERFLOW,
    pool_timeout=settings.POSTGRES_POOL_TIMEOUT,  # all connections busy: fail after this long (-> HTTP 503) instead of 30 s
    pool_pre_ping=True,  # Test connection liveness before checkout
    pool_recycle=1800,   # Recycle connections every 30 minutes
    connect_args=_pg_connect_args(),
    echo=False,
)


def relax_timeouts_for_scripts(target_engine=None) -> None:
    """Call first thing in a long-running command line script (seed, audit): no lock/statement limits.

    The limits protect the web API. A script that legitimately runs one big statement (a seed or an
    audit over millions of rows) must not be cancelled by them.
    """

    @event.listens_for(target_engine or engine, "connect")
    def _no_limits(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("SET statement_timeout = 0")
            cursor.execute("SET lock_timeout = 0")
        finally:
            cursor.close()
        dbapi_connection.commit()


SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)

Base = declarative_base()


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a transactional SQLAlchemy session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def check_postgres_connection() -> bool:
    """Check connectivity to PostgreSQL database."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        logger.error(f"PostgreSQL connection check failed: {exc}")
        return False


# ==============================================================================
# MongoDB Client & Connection Pool
# ==============================================================================
mongo_client: MongoClient = MongoClient(
    settings.mongo_connection_uri,
    maxPoolSize=settings.MONGO_MAX_POOL_SIZE,
    minPoolSize=settings.MONGO_MIN_POOL_SIZE,
    serverSelectionTimeoutMS=settings.MONGO_SERVER_SELECTION_TIMEOUT_MS,
    connectTimeoutMS=settings.MONGO_CONNECT_TIMEOUT_MS,
    socketTimeoutMS=settings.MONGO_SOCKET_TIMEOUT_MS,
)

mongo_db: Database = mongo_client[settings.MONGO_DB]


def get_mongo_db() -> Database:
    """FastAPI dependency returning MongoDB database instance."""
    return mongo_db


def check_mongo_connection() -> bool:
    """Check connectivity to MongoDB database."""
    try:
        mongo_client.admin.command("ping")
        return True
    except Exception as exc:
        logger.error(f"MongoDB connection check failed: {exc}")
        return False
