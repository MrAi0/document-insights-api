"""Every URL the API serves.

A routes module says which URL and method go to which controller function, plus
what OpenAPI needs (status codes, summaries). The controllers in app/controllers
handle the request. main.py mounts `api_router`.
"""

from fastapi import APIRouter

from app.routes import document_routes, health_routes, user_routes

api_router = APIRouter()
api_router.include_router(document_routes.router)
api_router.include_router(user_routes.router)
api_router.include_router(health_routes.router)
