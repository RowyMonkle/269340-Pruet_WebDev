from fastapi import APIRouter
from app.api.v1.users import router as users_router
from app.api.v1.events import router as events_router, products_router
from app.api.v1.orders import router as orders_router

api_v1_router = APIRouter()
api_v1_router.include_router(users_router)
api_v1_router.include_router(events_router)
api_v1_router.include_router(products_router)
api_v1_router.include_router(orders_router)

__all__ = ["api_v1_router"]
