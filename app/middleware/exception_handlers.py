import logging

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from pymongo.errors import ConnectionFailure

from app.services.document_service import TooManyActiveJobs

logger = logging.getLogger(__name__)

# Services raise plain exceptions and don't know about HTTP. This is where
# those exceptions get turned into status codes. Anything unexpected becomes a
# 500 in middleware/request_context.py.


async def too_many_active_jobs(request: Request, exc: TooManyActiveJobs) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_429_TOO_MANY_REQUESTS, content={"detail": str(exc)})


async def database_unavailable(request: Request, exc: ConnectionFailure) -> JSONResponse:
    # no traceback, it's the same connection error on every request until Mongo is back
    logger.error("database unavailable", extra={"error": str(exc)})
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": "Database unavailable, please retry shortly"},
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(TooManyActiveJobs, too_many_active_jobs)
    app.add_exception_handler(ConnectionFailure, database_unavailable)
