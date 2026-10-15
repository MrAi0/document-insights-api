import logging
import re
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.status import HTTP_500_INTERNAL_SERVER_ERROR

from app.utils.logger import request_id_var

logger = logging.getLogger(__name__)

# Accept the caller's request id if it looks sane, so it can be traced across services.
# Anything else gets replaced; it ends up in every log line.
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# Docker's healthcheck hits this every few seconds. Only log it when it isn't healthy
# (HealthService also logs which dependency failed).
_HEALTH_PATH = "/health"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Tags every log line of a request with a request id, and writes one access log line."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        incoming = request.headers.get("x-request-id", "")
        request_id = incoming if _REQUEST_ID_RE.match(incoming) else uuid.uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status_code = 500
        try:
            response = await self._call_app(request, call_next)
            status_code = response.status_code
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            path = request.url.path
            quiet = path == _HEALTH_PATH and status_code == 200
            logger.log(
                logging.DEBUG if quiet else logging.INFO,
                f"{request.method} {path}",
                # also as fields, so JSON logs can be filtered and grouped by them
                extra={
                    "method": request.method,
                    "path": path,
                    "status_code": status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                },
            )
            request_id_var.reset(token)

    @staticmethod
    async def _call_app(request: Request, call_next: RequestResponseEndpoint) -> Response:
        try:
            return await call_next(request)
        except Exception:
            # Errors we expect already have handlers (middleware/exception_handlers.py),
            # so anything that gets here is a bug. It's handled here rather than with
            # an Exception handler because by the time that runs, this middleware has
            # finished: the log line would have no request id and the response no
            # X-Request-ID. The client gets JSON like every other error, not
            # Starlette's plain-text 500, and never the exception's details.
            logger.exception("unhandled error")
            return JSONResponse(
                status_code=HTTP_500_INTERNAL_SERVER_ERROR,
                content={"detail": "Internal server error"},
            )
