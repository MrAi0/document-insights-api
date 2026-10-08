"""Everything should keep working with Redis unreachable, falling back to Mongo."""

import pytest

from tests.conftest import DEAD_REDIS_URL, wait_for_status


@pytest.fixture
def settings_overrides():
    return {"redis_url": DEAD_REDIS_URL}


async def test_health_reports_degraded(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "degraded", "mongo": "ok", "redis": "unavailable"}


async def test_rate_limit_falls_back_to_mongo(client, submit, processor):
    for _ in range(3):
        assert (await submit()).status_code == 201
    assert (await submit()).status_code == 429

    await processor.run_once()
    assert (await submit()).status_code == 201


async def test_cache_falls_back_to_mongo(client, submit, processor):
    content = "Cached in Mongo only."
    await submit(content=content)
    await processor.run_once()

    resp = await submit(user_id="bob", content=content)
    assert resp.status_code == 201
    assert resp.json()["status"] == "completed"


async def test_worker_falls_back_to_polling(running_worker, client, submit):
    # The wake-up can't be sent and the worker can't wait on Redis, so it
    # checks Mongo every WORKER_POLL_INTERVAL_SECONDS (1s) instead.
    document_id = (await submit()).json()["document_id"]

    took = await wait_for_status(client, document_id, "completed", timeout=5)
    assert took < 3
