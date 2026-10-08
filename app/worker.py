"""Background worker. Run with `python -m app.worker`.

Each worker loop does this:

    forever:
        process jobs back to back until none are ready     (ProcessingService.run_once)
        ask Mongo when the next job will be ready           (a retry backing off, an expired lease)
        sleep until whichever of these comes first:
            - the API announces a new document              (WakeupService, Redis BLPOP)
            - the next job is ready
            - WORKER_MAX_IDLE_SECONDS pass                   (safety net for a lost wake-up)

An idle worker makes no queries at all, it's blocked on Redis. While Redis is
down there are no wake-ups, so it polls Mongo every WORKER_POLL_INTERVAL_SECONDS
until Redis is back.

WORKER_CONCURRENCY loops run in one process, and several worker processes can
run side by side. Claiming a job is atomic (DocumentRepository.claim_next), so
no two loops ever get the same job.
"""

import asyncio
import logging
import signal

from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import PyMongoError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import Settings, get_settings
from app.repositories.document_repository import DocumentRepository
from app.services.cache_service import CacheService
from app.services.mongo_service import mongo_service
from app.services.processing_service import ProcessingService
from app.services.rate_limit_service import RateLimitService
from app.services.redis_service import redis_service
from app.services.summarizer_service import SummarizerService
from app.services.wakeup_service import WakeupService
from app.utils.logger import setup_logging

# not __name__, that's "__main__" when started with `python -m app.worker`
logger = logging.getLogger("app.worker")

# Pause after a Mongo error so we don't spin while it's down.
ERROR_BACKOFF_SECONDS = 5.0


class Worker:
    def __init__(
        self,
        processing: ProcessingService,
        wakeups: WakeupService,
        *,
        concurrency: int,
        max_idle_seconds: float,
        poll_interval_seconds: float,
    ):
        self._processing = processing
        self._wakeups = wakeups
        self._concurrency = concurrency
        self._max_idle_seconds = max_idle_seconds
        self._poll_interval_seconds = poll_interval_seconds
        # Shared by all loops, so a Redis outage gets logged once, not once per loop.
        self._wakeups_working = True

    async def run(self) -> None:
        """Runs until cancelled."""
        async with asyncio.TaskGroup() as tg:
            for slot in range(self._concurrency):
                tg.create_task(self._loop(slot))

    async def _loop(self, slot: int) -> None:
        while True:
            try:
                await self._process_ready_jobs()
                sleep_for = await self._time_until_next_job()
            except PyMongoError:
                # If this hit a status write, the job's lease will run out and
                # it gets picked up again, so nothing is lost.
                logger.exception("mongo error in worker loop", extra={"slot": slot})
                await asyncio.sleep(ERROR_BACKOFF_SECONDS)
                continue
            await self._sleep_until_woken(sleep_for)

    async def _process_ready_jobs(self) -> None:
        while await self._processing.run_once():
            pass

    async def _time_until_next_job(self) -> float:
        next_job_in = await self._processing.seconds_until_next_job()
        if next_job_in is None:
            return self._max_idle_seconds
        return min(next_job_in, self._max_idle_seconds)

    async def _sleep_until_woken(self, timeout: float) -> None:
        if timeout <= 0:
            return  # a job is ready right now
        if not self._wakeups_working:
            # Redis was down last time. Keep checking Mongo on the poll interval
            # until a wait goes through, which is also how we notice it's back.
            timeout = min(timeout, self._poll_interval_seconds)
        try:
            woken = await self._wakeups.wait(timeout)
        except RedisError as exc:
            self._set_wakeups_working(False, exc)
            # No doorbell without Redis, so check Mongo on a short interval instead.
            await asyncio.sleep(min(timeout, self._poll_interval_seconds))
            return
        self._set_wakeups_working(True)
        logger.debug("worker woke up", extra={"reason": "new document" if woken else "timeout"})

    def _set_wakeups_working(self, working: bool, error: Exception | None = None) -> None:
        if working == self._wakeups_working:
            return
        self._wakeups_working = working
        if working:
            logger.info("redis is back, idle workers sleep until woken again")
        else:
            logger.warning(
                "redis unavailable, polling mongo for jobs until it's back",
                extra={"poll_interval_seconds": self._poll_interval_seconds, "error": str(error)},
            )


def build_processing_service(
    settings: Settings, db: AsyncDatabase, redis: Redis
) -> ProcessingService:
    repository = DocumentRepository(db)
    return ProcessingService(
        settings=settings,
        repository=repository,
        cache=CacheService(redis, repository, settings.summary_cache_ttl_seconds),
        rate_limiter=RateLimitService(
            redis, repository, settings.max_active_jobs_per_user, settings.active_job_ttl_seconds
        ),
        summarizer=SummarizerService(
            settings.processing_min_seconds,
            settings.processing_max_seconds,
            settings.failure_rate,
        ),
        wakeups=WakeupService(redis),
    )


def build_worker(settings: Settings, db: AsyncDatabase, redis: Redis, listener: Redis) -> Worker:
    """`listener` is the Redis client for blocking waits (RedisService.blocking_client)."""
    return Worker(
        build_processing_service(settings, db, redis),
        WakeupService(redis, listener=listener),
        concurrency=settings.worker_concurrency,
        max_idle_seconds=settings.worker_max_idle_seconds,
        poll_interval_seconds=settings.worker_poll_interval_seconds,
    )


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_json)

    mongo_service.connect(settings)
    redis_service.connect(settings)
    try:
        await DocumentRepository(mongo_service.db).ensure_indexes()
        worker = build_worker(
            settings, mongo_service.db, redis_service.client, redis_service.blocking_client
        )

        task = asyncio.create_task(worker.run())
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, task.cancel)

        logger.info("worker started", extra={"concurrency": settings.worker_concurrency})
        try:
            await task
        except asyncio.CancelledError:
            logger.info("worker stopped")
    finally:
        await redis_service.close()
        await mongo_service.close()


if __name__ == "__main__":
    asyncio.run(main())
