import asyncio
import logging
from collections.abc import Awaitable

from pymongo.errors import PyMongoError
from redis.exceptions import RedisError

from app.models import DependencyStatus, HealthOut
from app.services.mongo_service import MongoService
from app.services.redis_service import RedisService

logger = logging.getLogger(__name__)

PING_TIMEOUT_SECONDS = 2.0


class HealthService:
    def __init__(self, mongo: MongoService, redis: RedisService):
        self._mongo = mongo
        self._redis = redis

    async def check(self) -> HealthOut:
        mongo_status, redis_status = await asyncio.gather(
            self._ping("mongo", self._mongo.ping()),
            self._ping("redis", self._redis.ping()),
        )

        # Without Mongo nothing works. Without Redis everything still works,
        # just slower and with a weaker rate limit, so that's only "degraded".
        if mongo_status != "ok":
            overall = "unavailable"
        elif redis_status != "ok":
            overall = "degraded"
        else:
            overall = "ok"
        return HealthOut(status=overall, mongo=mongo_status, redis=redis_status)

    @staticmethod
    async def _ping(name: str, ping: Awaitable[None]) -> DependencyStatus:
        try:
            await asyncio.wait_for(ping, timeout=PING_TIMEOUT_SECONDS)
        except (PyMongoError, RedisError, TimeoutError) as exc:
            logger.warning(
                "health check failed",
                extra={"dependency": name, "error": str(exc) or type(exc).__name__},
            )
            return "unavailable"
        return "ok"
