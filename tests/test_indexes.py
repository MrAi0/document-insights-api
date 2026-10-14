"""The read queries have to stay index lookups as the collection grows.

These call the real repository methods with MongoDB's profiler on, then check
what Mongo actually did: which index it used, how many documents it read, and
whether it had to sort in memory. If a query changes shape or an index goes
missing, these fail instead of the API quietly getting slower.
"""

from datetime import timedelta

import pytest
from bson import ObjectId

from app.models import DocumentStatus
from app.repositories.document_repository import DocumentRepository
from app.utils.clock import utcnow

USERS = 10
DOCS_PER_USER = 30


@pytest.fixture
async def profiled(db, repository):
    now = utcnow()
    statuses = [DocumentStatus.COMPLETED] * 3 + [DocumentStatus.QUEUED, DocumentStatus.FAILED]
    docs = []
    for i in range(USERS * DOCS_PER_USER):
        # cycle statuses per user, so every user has a mix of them
        status = statuses[(i // USERS) % len(statuses)]
        docs.append(
            {
                "_id": ObjectId(),
                "user_id": f"user{i % USERS}",
                "title": "t",
                "content": "x",
                # 47 rather than 50 so no user has the same content twice (user_content is unique)
                "content_hash": f"hash{i % 47}",
                "status": status,
                "summary": {
                    "text": "s",
                    "keywords": [],
                    "word_count": 1,
                    "reading_time_seconds": 1,
                    "model": "m",
                },
                "error": None,
                "attempts": 1,
                "cache_hit": False,
                "visible_at": now + timedelta(seconds=i)
                if status == DocumentStatus.QUEUED
                else None,
                "created_at": now - timedelta(seconds=i),
                "updated_at": now,
                "completed_at": now - timedelta(seconds=i),
            }
        )
    await db["documents"].insert_many(docs)
    await db.command("profile", 2)
    yield
    await db.command("profile", 0)


async def last_find(db) -> dict:
    """What Mongo recorded for the most recent find on the documents collection."""
    entry = await db["system.profile"].find_one(
        {"ns": f"{db.name}.documents", "command.find": "documents"}, sort=[("ts", -1)]
    )
    assert entry is not None, "no find was profiled"
    return entry


def assert_index_only(entry: dict, index_field: str) -> None:
    assert "COLLSCAN" not in entry["planSummary"], entry["planSummary"]
    assert index_field in entry["planSummary"], entry["planSummary"]
    assert not entry.get("hasSortStage"), "sorted in memory instead of using the index"


async def test_summary_lookup_reads_a_single_document(profiled, repository, db):
    assert await repository.find_summary("hash7") is not None

    entry = await last_find(db)
    assert_index_only(entry, "content_hash")
    assert entry["keysExamined"] == 1
    assert entry["docsExamined"] == 1


async def test_summary_lookup_for_new_content_reads_nothing(profiled, repository, db):
    assert await repository.find_summary("never-seen") is None

    entry = await last_find(db)
    assert_index_only(entry, "content_hash")
    assert entry["docsExamined"] == 0


async def test_resubmission_lookup_reads_a_single_document(profiled, repository, db):
    assert await repository.find_submitted("user3", "hash3") is not None

    entry = await last_find(db)
    assert_index_only(entry, "user_id: 1, content_hash: 1")
    assert entry["keysExamined"] == 1
    assert entry["docsExamined"] == 1


@pytest.mark.parametrize("status", [None, DocumentStatus.COMPLETED])
async def test_listing_reads_only_the_requested_page(profiled, repository, db, status):
    items, _ = await repository.list_for_user("user3", status=status, page=1, page_size=5)
    assert len(items) == 5

    entry = await last_find(db)
    assert_index_only(entry, "user_id")
    assert entry["docsExamined"] == 5


async def test_finding_the_next_due_job_reads_a_single_document(profiled, repository, db):
    assert await repository.next_visible_at() is not None

    entry = await last_find(db)
    assert_index_only(entry, "visible_at")
    assert entry["docsExamined"] == 1


async def test_startup_survives_duplicates_from_before_resubmissions_were_deduplicated(db):
    docs = [{"user_id": "alice", "content_hash": "abc", "status": "completed"} for _ in range(2)]
    await db["documents"].insert_many(docs)

    await DocumentRepository(db).ensure_indexes()

    indexes = await db["documents"].index_information()
    assert "user_content" not in indexes
    # the rest are still there
    assert {"user_created", "user_status_created", "claim", "content_hash"} <= indexes.keys()
