import logging

from bson import ObjectId

from app.models import DocumentCreate, DocumentOut, DocumentPage, DocumentStatus
from app.repositories.document_repository import DocumentRepository
from app.services.cache_service import CacheService
from app.services.rate_limit_service import RateLimitService
from app.services.wakeup_service import WakeupService
from app.utils.hashing import hash_content
from app.utils.ids import parse_object_id

logger = logging.getLogger(__name__)


class TooManyActiveJobs(Exception):
    def __init__(self, limit: int):
        super().__init__(
            f"You already have {limit} documents queued or processing. "
            "Wait for one to finish and try again."
        )


class DocumentService:
    def __init__(
        self,
        repository: DocumentRepository,
        cache: CacheService,
        rate_limiter: RateLimitService,
        wakeups: WakeupService,
    ):
        self._repository = repository
        self._cache = cache
        self._rate_limiter = rate_limiter
        self._wakeups = wakeups

    async def submit(self, payload: DocumentCreate) -> DocumentOut:
        # The id is generated here rather than by Mongo so the rate limit slot
        # can be reserved under it before the insert.
        document_id = ObjectId()
        content_hash = hash_content(payload.content)
        log_extra = {"document_id": str(document_id), "user_id": payload.user_id}

        # Cache first. A cache hit never sits in the queue, so it shouldn't count
        # against the limit or get rejected by it.
        cached = await self._cache.get(content_hash)
        if cached is not None:
            doc = await self._repository.create(
                document_id=document_id,
                user_id=payload.user_id,
                title=payload.title,
                content=payload.content,
                content_hash=content_hash,
                cached_summary=cached,
            )
            logger.info("document completed from cache", extra=log_extra)
            return DocumentOut.from_mongo(doc)

        if not await self._rate_limiter.try_acquire(payload.user_id, str(document_id)):
            logger.info("document rejected, too many active jobs", extra=log_extra)
            raise TooManyActiveJobs(self._rate_limiter.limit)

        try:
            doc = await self._repository.create(
                document_id=document_id,
                user_id=payload.user_id,
                title=payload.title,
                content=payload.content,
                content_hash=content_hash,
            )
        except BaseException:
            # Give the slot back or the user stays blocked until it expires.
            await self._rate_limiter.release(payload.user_id, str(document_id))
            raise

        # The insert above is what queues the job. This just tells a sleeping
        # worker to come and get it now rather than at its next idle timeout.
        await self._wakeups.notify(str(document_id))
        logger.info("document queued", extra=log_extra)
        return DocumentOut.from_mongo(doc)

    async def get(self, document_id: str) -> DocumentOut | None:
        oid = parse_object_id(document_id)
        if oid is None:
            return None
        doc = await self._repository.get(oid)
        return DocumentOut.from_mongo(doc) if doc else None

    async def list_for_user(
        self, user_id: str, *, status: DocumentStatus | None, page: int, page_size: int
    ) -> DocumentPage:
        docs, total = await self._repository.list_for_user(
            user_id, status=status, page=page, page_size=page_size
        )
        return DocumentPage(
            items=[DocumentOut.from_mongo(d) for d in docs],
            page=page,
            page_size=page_size,
            total=total,
        )
