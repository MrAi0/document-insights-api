from fastapi import APIRouter, status

from app.controllers import health_controller
from app.models import HealthOut

router = APIRouter(tags=["health"])

router.add_api_route(
    "/health",
    health_controller.health,
    methods=["GET"],
    summary="Check MongoDB and Redis connectivity",
    response_description="`ok`, or `degraded` while Redis is down (the API still works)",
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": HealthOut,
            "description": "MongoDB is unreachable",
        }
    },
)
