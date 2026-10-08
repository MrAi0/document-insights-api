import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING, IndexModel, ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase

from app.models import ACTIVE_STATUSES, DocumentStatus, Summary
from app.utils.clock import utcnow

# list endpoints never need the full text
_WITHOUT_CONTENT = {"content": 0}


@dataclass(frozen=True)
class ClaimedJob:
    id: ObjectId
    user_id: str
    content: str
    content_hash: str
    attempts: int
    lease_token: str


class DocumentRepository:
    def __init__(self, db: AsyncDatabase):
        self._collection = db["documents"]

    async def ensure_indexes(self) -> None:
        await self._collection.create_indexes(
            [
                # GET /users/{id}/documents, newest first
                IndexModel(
                    [("user_id", ASCENDING), ("created_at", DESCENDING), ("_id", DESCENDING)],
                    name="user_created",
                ),
                # same listing with ?status=, plus counting active jobs when Redis is down
                IndexModel(
                    [
                        ("user_id", ASCENDING),
                        ("status", ASCENDING),
                        ("created_at", DESCENDING),
                        ("_id", DESCENDING),
                    ],
                    name="user_status_created",
                ),
                # worker claim query
                IndexModel([("status", ASCENDING), ("visible_at", ASCENDING)], name="claim"),
                # find_summary: any earlier completed document with the same content
                IndexModel(
                    [
                        ("content_hash", ASCENDING),
                        ("status", ASCENDING),
                        ("completed_at", DESCENDING),
                    ],
                    name="content_hash",
                ),
            ]
        )

    async def create(
        self,
        *,
        document_id: ObjectId,
        user_id: str,
        title: str,
        content: str,
        content_hash: str,
        cached_summary: Summary | None = None,
    ) -> dict[str, Any]:
        now = utcnow()
        doc: dict[str, Any] = {
            "_id": document_id,
            "user_id": user_id,
            "title": title,
            "content": content,
            "content_hash": content_hash,
            "status": DocumentStatus.QUEUED,
            "summary": None,
            "error": None,
            "attempts": 0,
            "cache_hit": False,
            "visible_at": now,
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
        }
        if cached_summary is not None:
            doc.update(
                status=DocumentStatus.COMPLETED,
                summary=cached_summary.model_dump(),
                cache_hit=True,
                visible_at=None,
                completed_at=now,
            )
        await self._collection.insert_one(doc)
        return doc

    async def get(self, document_id: ObjectId) -> dict[str, Any] | None:
        return await self._collection.find_one({"_id": document_id}, _WITHOUT_CONTENT)

    async def list_for_user(
        self, user_id: str, *, status: DocumentStatus | None, page: int, page_size: int
    ) -> tuple[list[dict[str, Any]], int]:
        query: dict[str, Any] = {"user_id": user_id}
        if status is not None:
            query["status"] = status

        cursor = (
            self._collection.find(query, _WITHOUT_CONTENT)
            .sort([("created_at", DESCENDING), ("_id", DESCENDING)])
            .skip((page - 1) * page_size)
            .limit(page_size)
        )
        items = await cursor.to_list()
        total = await self._collection.count_documents(query)
        return items, total

    async def count_active(self, user_id: str) -> int:
        return await self._collection.count_documents(
            {"user_id": user_id, "status": {"$in": ACTIVE_STATUSES}}
        )

    async def find_summary(self, content_hash: str) -> Summary | None:
        """Summary of any earlier completed document with the same content.

        Served straight from the content_hash index: equality on content_hash and
        status, already sorted by completed_at, so it reads one index entry and
        one document however big the collection gets. A hash we've never seen
        costs a single index seek. tests/test_indexes.py checks the plan.
        """
        doc = await self._collection.find_one(
            {"content_hash": content_hash, "status": DocumentStatus.COMPLETED},
            {"summary": 1, "_id": 0},
            sort=[("completed_at", DESCENDING)],
        )
        return Summary.model_validate(doc["summary"]) if doc else None

    async def next_visible_at(self) -> datetime | None:
        """When the next job becomes claimable, or None if nothing is queued or running.

        That's either a queued job's retry time or a processing job's lease expiry.
        An idle worker sleeps until then (or until it's woken), so retries and
        crashed jobs are picked up on time without polling. Uses the claim index.
        """
        doc = await self._collection.find_one(
            {"status": {"$in": ACTIVE_STATUSES}},
            {"visible_at": 1},
            sort=[("visible_at", ASCENDING)],
        )
        return doc["visible_at"] if doc else None

    async def claim_next(self, lease_seconds: int) -> ClaimedJob | None:
        """Atomically take the oldest job that's ready to run.

        `visible_at` works like an SQS visibility timeout. For a queued job it's
        when the job may run (now, or later if it's backing off after a failure).
        For a processing job it's when the lease runs out. A processing job past
        its lease means the worker died, so it's fair game again.

        find_one_and_update is atomic on a single document, so two workers racing
        for the same job can't both get it. The loser just matches the next one.
        """
        now = utcnow()
        token = uuid.uuid4().hex
        doc = await self._collection.find_one_and_update(
            {"status": {"$in": ACTIVE_STATUSES}, "visible_at": {"$lte": now}},
            {
                "$set": {
                    "status": DocumentStatus.PROCESSING,
                    "lease_token": token,
                    "visible_at": now + timedelta(seconds=lease_seconds),
                    "updated_at": now,
                },
                "$inc": {"attempts": 1},
            },
            sort=[("visible_at", ASCENDING)],
            projection={"user_id": 1, "content": 1, "content_hash": 1, "attempts": 1},
            return_document=ReturnDocument.AFTER,
        )
        if doc is None:
            return None
        return ClaimedJob(
            id=doc["_id"],
            user_id=doc["user_id"],
            content=doc["content"],
            content_hash=doc["content_hash"],
            attempts=doc["attempts"],
            lease_token=token,
        )

    # The methods below only touch a job if the caller still holds its lease.
    # If a worker stalls past its lease and someone else picks the job up, the
    # stale worker's writes match nothing and it backs off. They return whether
    # the write went through.

    async def mark_completed(self, job: ClaimedJob, summary: Summary, *, cache_hit: bool) -> bool:
        now = utcnow()
        return await self._update_owned(
            job,
            {
                "$set": {
                    "status": DocumentStatus.COMPLETED,
                    "summary": summary.model_dump(),
                    "cache_hit": cache_hit,
                    "error": None,
                    "visible_at": None,
                    "completed_at": now,
                    "updated_at": now,
                },
                "$unset": {"lease_token": ""},
            },
        )

    async def mark_failed(self, job: ClaimedJob, error: str) -> bool:
        now = utcnow()
        return await self._update_owned(
            job,
            {
                "$set": {
                    "status": DocumentStatus.FAILED,
                    "error": error,
                    "visible_at": None,
                    "completed_at": now,
                    "updated_at": now,
                },
                "$unset": {"lease_token": ""},
            },
        )

    async def requeue(
        self,
        job: ClaimedJob,
        *,
        delay_seconds: float,
        error: str | None = None,
        refund_attempt: bool = False,
    ) -> bool:
        now = utcnow()
        update: dict[str, Any] = {
            "$set": {
                "status": DocumentStatus.QUEUED,
                "visible_at": now + timedelta(seconds=delay_seconds),
                "updated_at": now,
            },
            "$unset": {"lease_token": ""},
        }
        if error is not None:
            update["$set"]["error"] = error
        if refund_attempt:
            update["$inc"] = {"attempts": -1}
        return await self._update_owned(job, update)

    async def _update_owned(self, job: ClaimedJob, update: dict[str, Any]) -> bool:
        result = await self._collection.update_one(
            {"_id": job.id, "status": DocumentStatus.PROCESSING, "lease_token": job.lease_token},
            update,
        )
        return result.matched_count == 1
