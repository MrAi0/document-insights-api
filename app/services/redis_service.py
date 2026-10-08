from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

from app.config import Settings


def create_redis_client(settings: Settings, *, socket_timeout: float | None = None) -> Redis:
    return Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_timeout=socket_timeout or settings.redis_timeout_seconds,
        socket_connect_timeout=settings.redis_timeout_seconds,
        # Every Redis call here has a fallback, so fail fast instead of using
        # redis-py's default retries with backoff. One immediate retry is still
        # worth it: after a Redis restart, pooled connections are dead and the
        # retry is what reconnects them.
        retry=Retry(NoBackoff(), retries=1),
    )


class RedisService:
    """Holds the Redis clients for the process, same idea as MongoService.

    Unlike Mongo, nothing here requires Redis to be up. Every caller has a
    fallback, so connect() never fails startup because of Redis.
    """

    def __init__(self) -> None:
        self._client: Redis | None = None
        self._blocking_client: Redis | None = None

    def connect(self, settings: Settings) -> None:
        if self._client is not None:
            return
        self._client = create_redis_client(settings)
        # The worker's BLPOP blocks for up to WORKER_MAX_IDLE_SECONDS, which the
        # normal 0.5s socket timeout would cut off. Clients connect lazily, so the
        # API never opens a connection for this one.
        self._blocking_client = create_redis_client(
            settings, socket_timeout=settings.worker_max_idle_seconds + 5
        )

    @property
    def client(self) -> Redis:
        if self._client is None:
            raise RuntimeError("Redis is not connected, call redis_service.connect() first")
        return self._client

    @property
    def blocking_client(self) -> Redis:
        """For blocking commands only (the worker waiting for jobs)."""
        if self._blocking_client is None:
            raise RuntimeError("Redis is not connected, call redis_service.connect() first")
        return self._blocking_client

    async def ping(self) -> None:
        await self.client.ping()

    async def close(self) -> None:
        for client in (self._client, self._blocking_client):
            if client is not None:
                await client.aclose()
        self._client = None
        self._blocking_client = None


redis_service = RedisService()
