from typing import Annotated

from fastapi import Depends, Request
from pymongo.asynchronous.database import AsyncDatabase
from redis.asyncio import Redis

from app.config import Settings
from app.repositories.document_repository import DocumentRepository
from app.services.cache_service import CacheService
from app.services.document_service import DocumentService
from app.services.health_service import HealthService
from app.services.mongo_service import mongo_service
from app.services.rate_limit_service import RateLimitService
from app.services.redis_service import redis_service
from app.services.wakeup_service import WakeupService

# The Mongo and Redis clients are process-wide singletons, connected in the
# lifespan (main.py). Everything built on top of them is cheap, so it's built
# per request. Tests can swap any of these with app.dependency_overrides.


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_db() -> AsyncDatabase:
    return mongo_service.db


def get_redis() -> Redis:
    return redis_service.client


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
DatabaseDep = Annotated[AsyncDatabase, Depends(get_db)]
RedisDep = Annotated[Redis, Depends(get_redis)]


def get_document_repository(db: DatabaseDep) -> DocumentRepository:
    return DocumentRepository(db)


DocumentRepositoryDep = Annotated[DocumentRepository, Depends(get_document_repository)]


def get_document_service(
    repository: DocumentRepositoryDep, redis: RedisDep, settings: SettingsDep
) -> DocumentService:
    return DocumentService(
        repository=repository,
        cache=CacheService(redis, repository, settings.summary_cache_ttl_seconds),
        rate_limiter=RateLimitService(
            redis, repository, settings.max_active_jobs_per_user, settings.active_job_ttl_seconds
        ),
        wakeups=WakeupService(redis),
    )


def get_health_service() -> HealthService:
    return HealthService(mongo_service, redis_service)


DocumentServiceDep = Annotated[DocumentService, Depends(get_document_service)]
HealthServiceDep = Annotated[HealthService, Depends(get_health_service)]
