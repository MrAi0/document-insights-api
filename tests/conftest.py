"""Shared fixtures.

Unit tests need nothing. Integration tests need Mongo and Redis:

    docker compose run --rm tests          # everything inside compose
    docker compose up -d mongo redis && pytest   # or locally

Each test gets its own Mongo database and uses Redis db 15, which is flushed
before and after, so don't point TEST_REDIS_URL at anything you care about.
"""

import asyncio
import contextlib
import os
import uuid
from collections.abc import AsyncIterator, Callable
from functools import cache

import httpx
import pytest
from pymongo import MongoClient
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import PyMongoError
from redis.asyncio import Redis

from app.config import Settings
from app.main import create_app
from app.repositories.document_repository import DocumentRepository
from app.services.mongo_service import create_mongo_client
from app.services.processing_service import ProcessingService
from app.services.redis_service import create_redis_client
from app.worker import Worker, build_processing_service, build_worker

TEST_MONGO_URL = os.getenv("TEST_MONGO_URL", "mongodb://localhost:27017")
TEST_REDIS_URL = os.getenv("TEST_REDIS_URL", "redis://localhost:6379/15")
# Nothing listens here. Used to test behaviour with Redis down.
DEAD_REDIS_URL = "redis://127.0.0.1:1/0"


@cache
def _mongo_reachable() -> bool:
    client = MongoClient(TEST_MONGO_URL, serverSelectionTimeoutMS=1000)
    try:
        client.admin.command("ping")
        return True
    except PyMongoError:
        return False
    finally:
        client.close()


def make_settings(**overrides) -> Settings:
    values = {
        "mongo_url": TEST_MONGO_URL,
        "mongo_db": f"test_{uuid.uuid4().hex[:12]}",
        "redis_url": TEST_REDIS_URL,
        "log_json": False,
        "processing_min_seconds": 0,
        "processing_max_seconds": 0,
        "failure_rate": 0,
        "retry_backoff_seconds": 0,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.fixture
def settings_overrides() -> dict:
    """Override in a test module (or with parametrize) to tweak settings."""
    return {}


@pytest.fixture
def settings(settings_overrides) -> Settings:
    if not _mongo_reachable():
        pytest.skip(f"MongoDB not reachable at {TEST_MONGO_URL}, start it with docker compose")
    return make_settings(**settings_overrides)


# The app itself uses the mongo_service / redis_service singletons (connected in
# its lifespan). These fixtures open separate clients so tests can look at the
# data directly and clean up after the app has shut down.


@pytest.fixture
async def db(settings) -> AsyncIterator[AsyncDatabase]:
    client = create_mongo_client(settings)
    yield client[settings.mongo_db]
    await client.drop_database(settings.mongo_db)
    await client.close()


@pytest.fixture
async def redis(settings) -> AsyncIterator[Redis]:
    client = create_redis_client(settings)
    if settings.redis_url != DEAD_REDIS_URL:
        await client.flushdb()
    yield client
    if settings.redis_url != DEAD_REDIS_URL:
        await client.flushdb()
    await client.aclose()


@pytest.fixture
async def repository(db) -> DocumentRepository:
    repo = DocumentRepository(db)
    await repo.ensure_indexes()
    return repo


@pytest.fixture
async def client(settings, db, redis) -> AsyncIterator[httpx.AsyncClient]:
    # depends on db/redis so they get cleaned up afterwards
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


@pytest.fixture
def processor(settings, db, redis) -> ProcessingService:
    """What the worker runs per job. Tests call processor.run_once() to process the next one."""
    return build_processing_service(settings, db, redis)


@pytest.fixture
async def listener(settings) -> AsyncIterator[Redis]:
    """Redis client for blocking waits, like RedisService.blocking_client."""
    client = create_redis_client(settings, socket_timeout=settings.worker_max_idle_seconds + 5)
    yield client
    await client.aclose()


@pytest.fixture
async def running_worker(settings, db, redis, listener) -> AsyncIterator[Worker]:
    """The real worker loop, running in the background for the length of the test."""
    worker = build_worker(settings, db, redis, listener)
    task = asyncio.create_task(worker.run())
    yield worker
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def wait_for_status(client, document_id: str, status: str, timeout: float = 5.0) -> float:
    """Poll the API until the document reaches `status`. Returns how long it took."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    while loop.time() - started < timeout:
        doc = (await client.get(f"/documents/{document_id}")).json()
        if doc["status"] == status:
            return loop.time() - started
        await asyncio.sleep(0.05)
    raise AssertionError(f"document {document_id} still {doc['status']!r} after {timeout}s")


@pytest.fixture
def submit(client) -> Callable:
    async def _submit(user_id: str = "alice", content: str | None = None, title: str = "Notes"):
        payload = {
            "user_id": user_id,
            "title": title,
            # unique by default so tests don't hit the cache by accident
            "content": content or f"Some document text {uuid.uuid4().hex}.",
        }
        return await client.post("/documents", json=payload)

    return _submit
