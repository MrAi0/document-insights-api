import logging

from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

WAKEUP_KEY = "jobs:wakeup"
# Wake-ups are only hints, so there's no point keeping thousands of them around
# (e.g. when no worker has been running for a while).
MAX_PENDING_WAKEUPS = 1000
# BLPOP treats 0 as "wait forever", so we never go below this.
MIN_WAIT_SECONDS = 0.01


class WakeupService:
    """Lets the API wake idle workers the moment a document is queued.

    It's a doorbell, not the queue. The job itself is always in Mongo and is
    claimed from there, so a lost or duplicate wake-up does no harm: at worst a
    worker wakes up and finds nothing, or sleeps until its idle timeout.

    It uses a Redis list (RPUSH/BLPOP) rather than pub/sub on purpose. With
    pub/sub, a message published while a worker is busy, or in the gap between
    "queue is empty" and starting to wait, is gone. A list keeps it until a
    worker comes back for it.
    """

    def __init__(self, redis: Redis, *, listener: Redis | None = None):
        self._redis = redis
        # the client with the long socket timeout, see RedisService
        self._listener = listener

    async def notify(self, document_id: str) -> None:
        try:
            async with self._redis.pipeline(transaction=False) as pipe:
                pipe.rpush(WAKEUP_KEY, document_id)
                pipe.ltrim(WAKEUP_KEY, -MAX_PENDING_WAKEUPS, -1)
                await pipe.execute()
        except RedisError as exc:
            # The job is safe in Mongo. Workers find it at their next idle
            # timeout, or straight away if they've fallen back to polling.
            logger.warning(
                "could not send wake-up to workers",
                extra={"document_id": document_id, "error": str(exc)},
            )

    async def wait(self, timeout: float) -> bool:
        """Block until a job is announced or `timeout` seconds pass.

        Returns True if woken by a job. Raises RedisError if Redis is down, so the
        caller can fall back to polling.
        """
        if self._listener is None:
            raise RuntimeError("WakeupService needs a listener client to wait")
        woken = await self._listener.blpop([WAKEUP_KEY], timeout=max(timeout, MIN_WAIT_SECONDS))
        return woken is not None
