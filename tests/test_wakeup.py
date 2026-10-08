"""Idle workers sleep, and wake up as soon as there's something to do.

The idle timeout is the default 30s in all of these, so anything that finishes
within a couple of seconds was woken up on purpose, not by the timeout.
"""

import asyncio

import pytest
from bson import ObjectId

from app.models import DocumentStatus
from app.services.wakeup_service import MAX_PENDING_WAKEUPS, WAKEUP_KEY, WakeupService
from app.utils.hashing import hash_content
from app.worker import build_worker
from tests.conftest import wait_for_status


@pytest.fixture
def settings_overrides(request):
    return getattr(request, "param", {})


async def test_notify_wakes_a_waiting_worker(redis, listener):
    wakeups = WakeupService(redis, listener=listener)
    waiting = asyncio.create_task(wakeups.wait(timeout=5))
    await asyncio.sleep(0.1)

    await wakeups.notify("doc-1")

    assert await asyncio.wait_for(waiting, timeout=1) is True


async def test_wakeup_sent_before_the_worker_waits_is_not_lost(redis, listener):
    # A document can arrive between a worker finding the queue empty and it
    # starting to wait. The list holds on to the wake-up until it gets there.
    wakeups = WakeupService(redis, listener=listener)
    await wakeups.notify("doc-1")

    assert await wakeups.wait(timeout=1) is True


async def test_wait_times_out_when_nothing_arrives(redis, listener):
    wakeups = WakeupService(redis, listener=listener)
    loop = asyncio.get_running_loop()
    started = loop.time()

    assert await wakeups.wait(timeout=0.3) is False
    assert 0.25 < loop.time() - started < 2


async def test_pending_wakeups_are_capped(redis):
    wakeups = WakeupService(redis)
    for i in range(MAX_PENDING_WAKEUPS + 5):
        await wakeups.notify(f"doc-{i}")

    assert await redis.llen(WAKEUP_KEY) == MAX_PENDING_WAKEUPS


async def test_idle_worker_picks_up_a_new_document_straight_away(running_worker, client, submit):
    await asyncio.sleep(0.3)  # let the worker find the queue empty and go to sleep

    document_id = (await submit()).json()["document_id"]

    took = await wait_for_status(client, document_id, "completed", timeout=3)
    assert took < 1.5


@pytest.mark.parametrize(
    "settings_overrides",
    [{"failure_rate": 1.0, "max_attempts": 2, "retry_backoff_seconds": 0.5}],
    indirect=True,
)
async def test_worker_wakes_up_for_a_retry_without_being_told(running_worker, client, submit):
    # Nothing announces a retry. The worker has to work out when the backoff
    # ends and sleep exactly that long.
    document_id = (await submit()).json()["document_id"]

    took = await wait_for_status(client, document_id, "failed", timeout=5)

    assert took < 3
    assert (await client.get(f"/documents/{document_id}")).json()["attempts"] == 2


async def test_worker_wakes_up_when_a_dead_workers_lease_runs_out(
    settings, db, redis, listener, repository
):
    content = "Claimed by a worker that then died."
    document_id = ObjectId()
    await repository.create(
        document_id=document_id,
        user_id="alice",
        title="t",
        content=content,
        content_hash=hash_content(content),
    )
    # claimed with a 1s lease and never finished
    assert await repository.claim_next(lease_seconds=1) is not None

    worker = build_worker(settings, db, redis, listener)
    task = asyncio.create_task(worker.run())
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        while (await db["documents"].find_one({"_id": document_id}))[
            "status"
        ] != DocumentStatus.COMPLETED:
            assert loop.time() - started < 5, "job was never reclaimed"
            await asyncio.sleep(0.05)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    # it waited for the lease to run out, but not for the 30s idle timeout
    assert 0.5 < loop.time() - started < 3
