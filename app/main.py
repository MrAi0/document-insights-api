import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import Settings, get_settings
from app.middleware.exception_handlers import register_exception_handlers
from app.middleware.request_context import RequestContextMiddleware
from app.repositories.document_repository import DocumentRepository
from app.routes import api_router
from app.services.mongo_service import mongo_service
from app.services.redis_service import redis_service
from app.utils.logger import setup_logging

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    setup_logging(settings.log_level, settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        mongo_service.connect(settings)
        redis_service.connect(settings)
        try:
            # Fails startup if Mongo isn't reachable. Better than starting up
            # and returning 503s for everything.
            await DocumentRepository(mongo_service.db).ensure_indexes()
            logger.info("api started")
            yield
        finally:
            await redis_service.close()
            await mongo_service.close()

    app = FastAPI(title="Document Insights API", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)
    app.include_router(api_router)
    return app


app = create_app()
