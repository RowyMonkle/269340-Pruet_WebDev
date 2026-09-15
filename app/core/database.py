import logging
from typing import Generator
from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from pymongo import MongoClient
from pymongo.database import Database

from app.core.config import settings

logger = logging.getLogger(__name__)

# ==============================================================================
# PostgreSQL Connection & Session Pool
# ==============================================================================
engine = create_engine(
    settings.sync_database_url,
    pool_size=settings.POSTGRES_POOL_SIZE,
    max_overflow=settings.POSTGRES_MAX_OVERFLOW,
    pool_pre_ping=True,  # Test connection liveness before checkout
    pool_recycle=1800,   # Recycle connections every 30 minutes
    echo=False,
)

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
    serverSelectionTimeoutMS=5000,
    connectTimeoutMS=5000,
    socketTimeoutMS=10000,
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
