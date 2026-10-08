from fastapi import APIRouter, Response, status

from app.dependencies import HealthServiceDep
from app.models import HealthOut

router = APIRouter(tags=["health"])


@router.get(
    "/health",
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "MongoDB is unreachable"}},
)
async def health(response: Response, service: HealthServiceDep) -> HealthOut:
    result = await service.check()
    # 503 only when Mongo is down, so a load balancer pulls the instance out.
    # Redis being down is reported as "degraded" but stays 200, the API still works.
    if result.status == "unavailable":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return result
