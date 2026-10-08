from fastapi import APIRouter

from app.controllers import document_controller, health_controller, user_controller

api_router = APIRouter()
api_router.include_router(document_controller.router)
api_router.include_router(user_controller.router)
api_router.include_router(health_controller.router)
