import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.core.database import (
    Base,
    engine,
    mongo_db,
    check_postgres_connection,
    check_mongo_connection,
)
from app.models.sql_models import User, Order, Ticket  # Registers models on Base.metadata
from app.models.nosql_models import init_mongo_indexes
from app.api.v1 import api_v1_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("ticket_booking_app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan context for startup and shutdown routines."""
    logger.info("Initializing Ticket Booking Platform backend...")

    # Verify PostgreSQL and initialize tables
    if check_postgres_connection():
        logger.info("PostgreSQL connection established successfully. Creating tables if missing...")
        Base.metadata.create_all(bind=engine)
    else:
        logger.warning("PostgreSQL is not reachable at startup. Continuing (will retry on requests)...")

    # Verify MongoDB and initialize collection indexes
    if check_mongo_connection():
        logger.info("MongoDB connection established successfully. Creating collection indexes...")
        try:
            init_mongo_indexes(mongo_db)
        except Exception as e:
            logger.error(f"Error creating MongoDB indexes: {e}")
    else:
        logger.warning("MongoDB is not reachable at startup. Continuing (will retry on requests)...")

    yield

    logger.info("Shutting down Ticket Booking Platform backend...")


app = FastAPI(
    title=settings.PROJECT_NAME,
    version="1.0.0",
    description=(
        "Dual-Database Backend API for Concert & Festival Ticket Booking. "
        "Integrates PostgreSQL for transactional core state (Users, Orders, Tickets) "
        "and MongoDB for flexible documents (Events, ActivityLogs)."
    ),
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# Cross-Origin Resource Sharing (CORS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount API v1 Routers
app.include_router(api_v1_router, prefix=settings.API_V1_PREFIX)


@app.get("/", tags=["System"])
def root():
    """Root metadata endpoint."""
    return {
        "service": settings.PROJECT_NAME,
        "version": "1.0.0",
        "docs": "/docs",
        "api_v1": settings.API_V1_PREFIX,
        "status": "online",
    }


@app.get("/health", tags=["System"])
def health_check():
    """Health check endpoint verifying both PostgreSQL and MongoDB connectivity."""
    pg_healthy = check_postgres_connection()
    mongo_healthy = check_mongo_connection()

    all_healthy = pg_healthy and mongo_healthy
    status_code = status.HTTP_200_OK if all_healthy else status.HTTP_503_SERVICE_UNAVAILABLE

    return JSONResponse(
        status_code=status_code,
        content={
            "status": "healthy" if all_healthy else "unhealthy",
            "databases": {
                "postgresql": "connected" if pg_healthy else "disconnected",
                "mongodb": "connected" if mongo_healthy else "disconnected",
            },
        },
    )
