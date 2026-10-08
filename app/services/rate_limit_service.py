import logging
import time

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.repositories.document_repository import DocumentRepository

logger = logging.getLogger(__name__)

# KEYS[1] = the user's active set
# ARGV = document id, now, stale cutoff, limit, key ttl
#
# Prune stale entries, then add the document only if the user is under the limit.
# Doing it in one script makes check-and-add atomic, so two requests from the same
# user can't both see 2 active jobs and both get in.
_ACQUIRE_SCRIPT = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[3])
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[4]) then
    return 0
end
redis.call('ZADD', KEYS[1], ARGV[2], ARGV[1])
redis.call('EXPIRE', KEYS[1], ARGV[5])
return 1
"""


def _active_key(user_id: str) -> str:
    return f"active-jobs:{user_id}"


class RateLimitService:
    """Caps how many documents a user can have queued or processing.

    Each user gets a sorted set of their active document ids, scored by when they
    were added. I went with a set over INCR/DECR on a counter because releasing
    by id is idempotent (releasing twice can't take the count negative), and the
    timestamps let us drop entries that a crashed worker never released.
    """

    def __init__(self, redis: Redis, repository: DocumentRepository, limit: int, ttl_seconds: int):
        self.limit = limit
        self._redis = redis
        self._repository = repository
        self._ttl_seconds = ttl_seconds
        self._acquire = redis.register_script(_ACQUIRE_SCRIPT)

    async def try_acquire(self, user_id: str, document_id: str) -> bool:
        now = time.time()
        try:
            added = await self._acquire(
                keys=[_active_key(user_id)],
                args=[document_id, now, now - self._ttl_seconds, self.limit, self._ttl_seconds],
            )
            return added == 1
        except RedisError as exc:
            # Mongo has the real statuses, it's just slower and not atomic.
            # Two requests landing at the same moment could both get through.
            logger.warning(
                "rate limiter unavailable, counting active jobs in mongo",
                extra={"user_id": user_id, "error": str(exc)},
            )
            return await self._repository.count_active(user_id) < self.limit

    async def release(self, user_id: str, document_id: str) -> None:
        try:
            await self._redis.zrem(_active_key(user_id), document_id)
        except RedisError as exc:
            logger.warning(
                "failed to release active job slot, it will expire on its own",
                extra={"user_id": user_id, "document_id": document_id, "error": str(exc)},
            )
