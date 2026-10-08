import asyncio
import time

import pytest
from bson import ObjectId

from app.models import DocumentStatus
from app.services import processing_service
from app.services.cache_service import CacheService
from app.services.rate_limit_service import RateLimitService
from app.services.summarizer_service import build_summary
from app.utils.hashing import hash_content


async def create_job(repository, *, user_id="alice", content="Some text."):
    doc = await repository.create(
        document_id=ObjectId(),
        user_id=user_id,
        title="t",
        content=content,
        content_hash=hash_content(content),
    )
    return doc["_id"]


async def raw(db, document_id):
    return await db["documents"].find_one({"_id": document_id})


async def test_job_is_completed_and_cached(client, submit, processor, db, redis):
    resp = await submit(content="One. Two. Three.")
    document_id = resp.json()["document_id"]
    assert await redis.zcard("active-jobs:alice") == 1

    assert await processor.run_once() is True

    doc = await raw(db, ObjectId(document_id))
    assert doc["status"] == DocumentStatus.COMPLETED
    assert doc["summary"]["text"] == "One. Two."
    assert doc["attempts"] == 1
    assert doc["completed_at"] is not None
    assert "lease_token" not in doc
    assert await redis.exists(f"summary:{hash_content('One. Two. Three.')}")
    # slot released
    assert await redis.zcard("active-jobs:alice") == 0


async def test_nothing_to_do(processor, repository):
    assert await processor.run_once() is False


@pytest.fixture
def settings_overrides(request):
    return getattr(request, "param", {})


@pytest.mark.parametrize(
    "settings_overrides", [{"failure_rate": 1.0, "max_attempts": 2}], indirect=True
)
async def test_failed_job_is_retried_then_marked_failed(submit, processor, db, redis):
    document_id = ObjectId((await submit()).json()["document_id"])

    await processor.run_once()
    doc = await raw(db, document_id)
    assert doc["status"] == DocumentStatus.QUEUED
    assert doc["attempts"] == 1
    assert "simulated" in doc["error"]
    assert await redis.zcard("active-jobs:alice") == 1

    await processor.run_once()
    doc = await raw(db, document_id)
    assert doc["status"] == DocumentStatus.FAILED
    assert doc["attempts"] == 2
    assert await redis.zcard("active-jobs:alice") == 0


@pytest.mark.parametrize(
    "settings_overrides", [{"failure_rate": 1.0, "retry_backoff_seconds": 30}], indirect=True
)
async def test_retry_waits_for_backoff(submit, processor, db):
    document_id = ObjectId((await submit()).json()["document_id"])

    await processor.run_once()
    doc = await raw(db, document_id)
    assert doc["status"] == DocumentStatus.QUEUED
    # not visible again until the backoff is over
    assert await processor.run_once() is False


async def test_worker_reuses_a_summary_from_mongo(repository, processor, db, redis):
    content = "Done once already."
    first = await create_job(repository, user_id="alice", content=content)
    await processor.run_once()
    await redis.flushdb()  # Redis has forgotten it, Mongo hasn't

    second = await create_job(repository, user_id="bob", content=content)
    await processor.run_once()

    doc = await raw(db, second)
    assert doc["status"] == DocumentStatus.COMPLETED
    assert doc["cache_hit"] is True
    assert doc["summary"] == (await raw(db, first))["summary"]


async def test_two_workers_cannot_claim_the_same_job(repository):
    await create_job(repository)

    claims = await asyncio.gather(*(repository.claim_next(lease_seconds=60) for _ in range(10)))

    assert len([c for c in claims if c is not None]) == 1


async def test_expired_lease_is_reclaimed_and_stale_worker_is_fenced_off(repository, db):
    await create_job(repository)
    stale = await repository.claim_next(lease_seconds=0)  # expires immediately
    fresh = await repository.claim_next(lease_seconds=60)

    assert fresh is not None and fresh.id == stale.id
    assert fresh.attempts == 2

    summary = build_summary("Some text.")
    assert await repository.mark_completed(stale, summary, cache_hit=False) is False
    assert await repository.mark_completed(fresh, summary, cache_hit=False) is True


@pytest.mark.parametrize(
    "settings_overrides",
    [{"processing_min_seconds": 0.3, "processing_max_seconds": 0.3}],
    indirect=True,
)
async def test_same_content_in_flight_is_only_processed_once(
    repository, processor, db, monkeypatch
):
    monkeypatch.setattr(processing_service, "DUPLICATE_RETRY_DELAY_SECONDS", 0)
    a = await create_job(repository, user_id="alice", content="Same text.")
    b = await create_job(repository, user_id="bob", content="Same text.")

    # Two workers pick up both copies at the same time. One does the work, the
    # other notices and goes back to the queue.
    await asyncio.gather(processor.run_once(), processor.run_once())
    docs = [await raw(db, a), await raw(db, b)]
    assert sorted(d["status"] for d in docs) == ["completed", "queued"]
    waiting = next(d for d in docs if d["status"] == DocumentStatus.QUEUED)
    assert waiting["attempts"] == 0  # waiting isn't a failed attempt

    await processor.run_once()
    docs = [await raw(db, a), await raw(db, b)]
    assert all(d["status"] == DocumentStatus.COMPLETED for d in docs)
    assert sorted(d["cache_hit"] for d in docs) == [False, True]


@pytest.mark.parametrize(
    "settings_overrides",
    [{"processing_min_seconds": 5, "processing_max_seconds": 5}],
    indirect=True,
)
async def test_shutdown_puts_job_back_in_queue(repository, processor, db, redis):
    document_id = await create_job(repository)
    task = asyncio.create_task(processor.run_once())
    for _ in range(50):
        if (await raw(db, document_id))["status"] == DocumentStatus.PROCESSING:
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.1)  # let it get into the (slow) summarize call

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    doc = await raw(db, document_id)
    assert doc["status"] == DocumentStatus.QUEUED
    assert doc["attempts"] == 0
    # another worker is woken to take it over
    assert await redis.lrange("jobs:wakeup", 0, -1) == [str(document_id)]
    assert await processor.run_once() is True  # immediately available again


async def test_limiter_is_atomic_under_concurrency(redis, repository):
    limiter = RateLimitService(redis, repository, limit=3, ttl_seconds=60)

    results = await asyncio.gather(*(limiter.try_acquire("alice", f"doc-{i}") for i in range(20)))

    assert results.count(True) == 3


async def test_limiter_drops_stale_slots(redis, repository):
    limiter = RateLimitService(redis, repository, limit=1, ttl_seconds=60)
    # a slot a dead worker never released
    await redis.zadd("active-jobs:alice", {"ghost": time.time() - 3600})

    assert await limiter.try_acquire("alice", "doc-1") is True
    assert await limiter.try_acquire("alice", "doc-2") is False


async def test_limiter_release_is_idempotent(redis, repository):
    limiter = RateLimitService(redis, repository, limit=1, ttl_seconds=60)
    assert await limiter.try_acquire("alice", "doc-1") is True
    await limiter.release("alice", "doc-1")
    await limiter.release("alice", "doc-1")

    assert await limiter.try_acquire("alice", "doc-2") is True
    assert await limiter.try_acquire("alice", "doc-3") is False


async def test_cache_lock_only_released_by_owner(redis, repository):
    cache = CacheService(redis, repository, ttl_seconds=60)
    assert await cache.try_lock("abc", "owner", ttl_seconds=60) is True
    assert await cache.try_lock("abc", "someone-else", ttl_seconds=60) is False

    await cache.unlock("abc", "someone-else")
    assert await redis.get("summary-lock:abc") == "owner"
    await cache.unlock("abc", "owner")
    assert await redis.get("summary-lock:abc") is None
