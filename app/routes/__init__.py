"""Every URL the API serves.

Each routes module declares its endpoints with what OpenAPI needs (status codes,
summaries) and handles the request by calling a service. main.py mounts
`api_router`.
"""

from fastapi import APIRouter

from app.routes import document_routes, health_routes, user_routes

api_router = APIRouter()
api_router.include_router(document_routes.router)
api_router.include_router(user_routes.router)
api_router.include_router(health_routes.router)
