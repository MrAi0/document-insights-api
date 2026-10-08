import logging

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.models import Summary
from app.repositories.document_repository import DocumentRepository

logger = logging.getLogger(__name__)

# Only delete the lock if we still own it. Without the check, a worker whose lock
# already expired could delete the lock someone else is now holding.
_UNLOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


def _summary_key(content_hash: str) -> str:
    return f"summary:{content_hash}"


def _lock_key(content_hash: str) -> str:
    return f"summary-lock:{content_hash}"


class CacheService:
    """Summaries keyed by content hash, in two layers.

    Redis holds recently used summaries for SUMMARY_CACHE_TTL_SECONDS. Behind
    it is MongoDB, where every completed document keeps its summary for good.
    A Redis miss falls through to one indexed Mongo lookup, and a summary found
    there goes back into Redis, so text we've summarized before is never
    processed again. Mongo also covers for Redis being down.
    """

    def __init__(self, redis: Redis, repository: DocumentRepository, ttl_seconds: int):
        self._redis = redis
        self._repository = repository
        self._ttl_seconds = ttl_seconds
        self._unlock = redis.register_script(_UNLOCK_SCRIPT)

    async def get(self, content_hash: str) -> Summary | None:
        try:
            raw = await self._redis.get(_summary_key(content_hash))
        except RedisError as exc:
            logger.warning(
                "summary cache unavailable, falling back to mongo", extra={"error": str(exc)}
            )
            # no point writing it back, Redis is down
            return await self._repository.find_summary(content_hash)

        if raw is not None:
            try:
                return Summary.model_validate_json(raw)
            except ValidationError:
                # e.g. left over from an older Summary shape. Mongo has the real
                # one, and the write below replaces the bad entry.
                logger.warning(
                    "ignoring unreadable cache entry", extra={"content_hash": content_hash}
                )

        summary = await self._repository.find_summary(content_hash)
        if summary is not None:
            logger.info(
                "summary found in mongo, cached again", extra={"content_hash": content_hash}
            )
            await self.set(content_hash, summary)
        return summary

    async def set(self, content_hash: str, summary: Summary) -> None:
        try:
            await self._redis.set(
                _summary_key(content_hash), summary.model_dump_json(), ex=self._ttl_seconds
            )
        except RedisError as exc:
            # not fatal, get() can still find it in mongo
            logger.warning(
                "failed to write summary cache",
                extra={"content_hash": content_hash, "error": str(exc)},
            )

    # The lock stops two workers from summarizing the same content at the same
    # time (e.g. the same file submitted twice in a row). Whoever holds it does
    # the work, everyone else waits for the cached result.

    async def try_lock(self, content_hash: str, token: str, ttl_seconds: int) -> bool:
        try:
            acquired = await self._redis.set(
                _lock_key(content_hash), token, nx=True, ex=ttl_seconds
            )
        except RedisError as exc:
            # Without Redis we can't coordinate. Doing the work twice is better than not at all.
            logger.warning(
                "could not take summary lock, continuing without it", extra={"error": str(exc)}
            )
            return True
        return bool(acquired)

    async def unlock(self, content_hash: str, token: str) -> None:
        try:
            await self._unlock(keys=[_lock_key(content_hash)], args=[token])
        except RedisError as exc:
            # it has a TTL, so it goes away on its own
            logger.warning("failed to release summary lock", extra={"error": str(exc)})
