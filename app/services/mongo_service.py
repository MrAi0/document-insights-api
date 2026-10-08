from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

from app.config import Settings


def create_mongo_client(settings: Settings) -> AsyncMongoClient:
    return AsyncMongoClient(
        settings.mongo_url,
        serverSelectionTimeoutMS=settings.mongo_timeout_ms,
        tz_aware=True,
    )


class MongoService:
    """Holds the one MongoDB client for the process.

    AsyncMongoClient keeps its own connection pool and is meant to be created
    once and shared, so the API and the worker both go through `mongo_service`
    below instead of opening clients of their own. connect() is called on
    startup and close() on shutdown.
    """

    def __init__(self) -> None:
        self._client: AsyncMongoClient | None = None
        self._db: AsyncDatabase | None = None

    def connect(self, settings: Settings) -> None:
        if self._client is not None:
            return
        # The client connects lazily, so this doesn't fail if Mongo is down.
        # The first real query (ensure_indexes on startup) does.
        self._client = create_mongo_client(settings)
        self._db = self._client[settings.mongo_db]

    @property
    def db(self) -> AsyncDatabase:
        if self._db is None:
            raise RuntimeError("MongoDB is not connected, call mongo_service.connect() first")
        return self._db

    async def ping(self) -> None:
        await self.db.command("ping")

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
        self._client = None
        self._db = None


mongo_service = MongoService()
