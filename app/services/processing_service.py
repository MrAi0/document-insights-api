import asyncio
import logging
import random
import time

from pymongo.errors import PyMongoError

from app.config import Settings
from app.models import Summary
from app.repositories.document_repository import ClaimedJob, DocumentRepository
from app.services.cache_service import CacheService
from app.services.rate_limit_service import RateLimitService
from app.services.summarizer_service import SummarizationError, SummarizerService
from app.services.wakeup_service import WakeupService
from app.utils.clock import utcnow

logger = logging.getLogger(__name__)

# When another job is already summarizing the same content, check back this soon.
DUPLICATE_RETRY_DELAY_SECONDS = 2.0


class ProcessingService:
    """What the worker does with a single job: claim it, summarize, record the outcome.

    The loop around this (concurrency, sleeping and waking up, shutdown) lives
    in app/worker.py.
    """

    def __init__(
        self,
        settings: Settings,
        repository: DocumentRepository,
        cache: CacheService,
        rate_limiter: RateLimitService,
        summarizer: SummarizerService,
        wakeups: WakeupService,
    ):
        self._settings = settings
        self._repository = repository
        self._cache = cache
        self._rate_limiter = rate_limiter
        self._summarizer = summarizer
        self._wakeups = wakeups

    async def run_once(self) -> bool:
        """Claim and process one job. Returns False if there was nothing to do."""
        job = await self._repository.claim_next(self._settings.job_lease_seconds)
        if job is None:
            return False
        await self.process(job)
        return True

    async def seconds_until_next_job(self) -> float | None:
        """How long until some job becomes claimable: a retry coming off its
        backoff, or a crashed worker's lease running out. None if the queue is
        completely empty. 0 means something is ready now.
        """
        next_at = await self._repository.next_visible_at()
        if next_at is None:
            return None
        return max(0.0, (next_at - utcnow()).total_seconds())

    async def process(self, job: ClaimedJob) -> None:
        started = time.perf_counter()
        log_extra = {"document_id": str(job.id), "user_id": job.user_id, "attempt": job.attempts}

        if job.attempts > self._settings.max_attempts:
            # Only happens when the job keeps getting reclaimed after its lease
            # expired, i.e. it keeps killing workers. Stop retrying it.
            await self._fail(
                job, "gave up after the worker stopped responding too many times", log_extra
            )
            return

        locked = False
        try:
            summary = await self._cache.get(job.content_hash)
            if summary is None:
                locked = await self._cache.try_lock(
                    job.content_hash, job.lease_token, self._settings.job_lease_seconds
                )
                if not locked:
                    # Same content is being summarized by another job right now.
                    # Put this one back and let it pick up that result from the cache.
                    await self._repository.requeue(
                        job, delay_seconds=DUPLICATE_RETRY_DELAY_SECONDS, refund_attempt=True
                    )
                    # repeats every couple of seconds until the other job is done
                    logger.debug("same content already in progress, requeued", extra=log_extra)
                    return
                # check again, the previous lock holder may have just finished
                summary = await self._cache.get(job.content_hash)

            if summary is not None:
                await self._complete(
                    job, summary, cache_hit=True, started=started, log_extra=log_extra
                )
                return

            logger.info("summarizing document", extra=log_extra)
            summary = await self._summarizer.summarize(job.content)
            await self._cache.set(job.content_hash, summary)
            await self._complete(
                job, summary, cache_hit=False, started=started, log_extra=log_extra
            )

        except asyncio.CancelledError:
            # Shutting down. Put the job straight back instead of making it wait
            # out the lease, and don't count this as an attempt.
            try:
                await self._repository.requeue(job, delay_seconds=0, refund_attempt=True)
                logger.info("worker shutting down, job requeued", extra=log_extra)
                # so another worker picks it up now, not at its next idle timeout
                await self._wakeups.notify(str(job.id))
            except PyMongoError:
                logger.exception(
                    "could not requeue job on shutdown, it will be retried when its lease expires",
                    extra=log_extra,
                )
            raise

        except Exception as exc:  # noqa: BLE001 - one bad job must not kill the worker loop
            await self._handle_error(job, exc, log_extra)

        finally:
            if locked:
                await self._cache.unlock(job.content_hash, job.lease_token)

    async def _complete(
        self, job: ClaimedJob, summary: Summary, *, cache_hit: bool, started: float, log_extra: dict
    ) -> None:
        if not await self._repository.mark_completed(job, summary, cache_hit=cache_hit):
            logger.warning(
                "lost the lease before finishing, another worker owns this job now", extra=log_extra
            )
            return
        await self._rate_limiter.release(job.user_id, str(job.id))
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        logger.info(
            "document completed",
            extra={**log_extra, "cache_hit": cache_hit, "duration_ms": duration_ms},
        )

    async def _handle_error(self, job: ClaimedJob, exc: Exception, log_extra: dict) -> None:
        error = str(exc) or type(exc).__name__
        if not isinstance(exc, SummarizationError):
            # something we didn't expect, keep the traceback
            logger.exception("unexpected error while processing document", extra=log_extra)

        if job.attempts >= self._settings.max_attempts:
            await self._fail(job, error, log_extra)
            return

        delay = self._retry_delay(job.attempts)
        if not await self._repository.requeue(job, delay_seconds=delay, error=error):
            logger.warning(
                "lost the lease before scheduling a retry",
                extra={**log_extra, "error": error},
            )
            return
        logger.warning(
            "attempt failed, retry scheduled",
            extra={**log_extra, "error": error, "retry_in_seconds": round(delay, 1)},
        )

    async def _fail(self, job: ClaimedJob, error: str, log_extra: dict) -> None:
        if not await self._repository.mark_failed(job, error):
            logger.warning("lost the lease before marking failed", extra=log_extra)
            return
        await self._rate_limiter.release(job.user_id, str(job.id))
        logger.error("document failed", extra={**log_extra, "error": error})

    def _retry_delay(self, attempt: int) -> float:
        # exponential backoff with some jitter so retries don't line up
        base = self._settings.retry_backoff_seconds * 2 ** (attempt - 1)
        return base * random.uniform(0.8, 1.2)
