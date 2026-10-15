import asyncio

import pytest
from bson import ObjectId

from app.utils.hashing import hash_content


async def test_submit_document_is_queued(client, submit, redis):
    resp = await submit()

    assert resp.status_code == 201
    body = resp.json()
    # nothing to show yet but the status
    assert body == {"document_id": body["document_id"], "status": "queued"}
    assert resp.headers["location"] == f"/documents/{body['document_id']}"
    assert resp.headers["x-request-id"]

    resp = await client.get(f"/documents/{body['document_id']}")
    assert resp.status_code == 200
    assert resp.json() == body
    # and a sleeping worker has been told about it
    assert await redis.lrange("jobs:wakeup", 0, -1) == [body["document_id"]]


async def test_finished_document_is_returned_in_full(client, submit, processor):
    content = "Revenue grew 12% in Q3. Costs stayed flat. Headcount rose by 4."
    document_id = (await submit(content=content, title="Q3 report")).json()["document_id"]
    await processor.run_once()

    resp = await client.get(f"/documents/{document_id}")

    assert resp.status_code == 200
    doc = resp.json()
    assert doc["document_id"] == document_id
    assert doc["status"] == "completed"
    assert doc["user_id"] == "alice"
    assert doc["title"] == "Q3 report"
    assert doc["content"] == content
    assert doc["summary"]["text"] == "Revenue grew 12% in Q3. Costs stayed flat."
    assert doc["error"] is None
    assert doc["attempts"] == 1
    assert doc["cache_hit"] is False
    assert doc["completed_at"] is not None
    # the worker's bookkeeping stays internal
    assert not {"_id", "content_hash", "lease_token", "visible_at"} & doc.keys()


@pytest.mark.parametrize("settings_overrides", [{"failure_rate": 1, "max_attempts": 1}])
async def test_failed_document_is_returned_in_full_with_the_error(client, submit, processor):
    content = "Never gets summarized."
    document_id = (await submit(content=content)).json()["document_id"]
    await processor.run_once()

    doc = (await client.get(f"/documents/{document_id}")).json()

    assert doc["status"] == "failed"
    assert doc["content"] == content
    assert doc["summary"] is None
    assert "simulated" in doc["error"]
    assert doc["completed_at"] is not None


async def test_submit_rejects_invalid_payload(client):
    resp = await client.post("/documents", json={"user_id": "alice", "title": " ", "content": "x"})
    assert resp.status_code == 422


async def test_get_document_404s(client):
    assert (await client.get(f"/documents/{ObjectId()}")).status_code == 404
    # malformed ids are a 404 too, not a 500
    assert (await client.get("/documents/not-an-object-id")).status_code == 404


async def test_fourth_active_document_is_rejected(client, submit, processor):
    for _ in range(3):
        assert (await submit()).status_code == 201

    resp = await submit()
    assert resp.status_code == 429

    # other users aren't affected
    assert (await submit(user_id="bob")).status_code == 201

    # once one of alice's documents finishes she can submit again
    await processor.run_once()
    assert (await submit()).status_code == 201


async def test_duplicate_content_is_served_from_cache(client, submit, processor, redis):
    content = "The quarterly numbers look good. Costs are down."
    first = (await submit(content=content)).json()
    await processor.run_once()
    original = (await client.get(f"/documents/{first['document_id']}")).json()
    assert original["status"] == "completed"
    assert original["cache_hit"] is False

    resp = await submit(user_id="bob", content=content)

    assert resp.status_code == 201
    body = resp.json()
    # finished straight away, so it's the whole document
    assert body["status"] == "completed"
    assert body["summary"] == original["summary"]
    assert body["cache_hit"] is True
    assert body["user_id"] == "bob"
    assert body["content"] == content
    assert (await client.get(f"/documents/{body['document_id']}")).json() == body
    # nothing to process, so no worker gets woken for it
    assert await redis.lrange("jobs:wakeup", 0, -1) == [first["document_id"]]


async def test_summary_is_reused_after_it_drops_out_of_redis(client, submit, processor, redis):
    content = "Summarized a long time ago. Still the same text."
    await submit(content=content)
    await processor.run_once()
    key = f"summary:{hash_content(content)}"
    # what happens once the Redis TTL runs out
    await redis.delete(key)

    resp = await submit(user_id="bob", content=content)

    # found in Mongo instead of being processed again
    assert resp.status_code == 201
    assert resp.json()["status"] == "completed"
    assert resp.json()["summary"] is not None
    # and put back in Redis, with a fresh TTL, for the next request
    assert await redis.ttl(key) > 0


async def test_cache_hits_do_not_count_towards_the_limit(client, submit, processor):
    content = "Already summarized."
    await submit(user_id="bob", content=content)
    await processor.run_once()

    for _ in range(3):
        await submit()
    assert (await submit()).status_code == 429
    # alice is at her limit but this one doesn't need processing
    resp = await submit(content=content)
    assert resp.status_code == 201
    assert resp.json()["status"] == "completed"


async def test_resubmitting_the_same_content_returns_the_existing_document(
    client, submit, processor, db, redis
):
    content = "Sent twice by the same user."
    first = await submit(content=content)
    assert first.status_code == 201
    document_id = first.json()["document_id"]

    # while it's still queued
    again = await submit(content=content, title="Notes (again)")
    assert again.status_code == 200
    assert again.json() == first.json()
    assert again.headers["location"] == f"/documents/{document_id}"

    # and once it's done, now with the summary
    await processor.run_once()
    again = await submit(content=content)
    assert again.status_code == 200
    assert again.json()["document_id"] == document_id
    assert again.json()["status"] == "completed"
    assert again.json()["summary"] is not None

    assert await db["documents"].count_documents({"user_id": "alice"}) == 1
    # the original title is kept
    assert (await client.get(f"/documents/{document_id}")).json()["title"] == "Notes"
    # only the first submission woke a worker
    assert await redis.lrange("jobs:wakeup", 0, -1) == [document_id]


async def test_resubmitting_at_the_limit_returns_the_existing_document(client, submit):
    first = (await submit(content="First of three.")).json()
    for _ in range(2):
        await submit()
    assert (await submit()).status_code == 429

    # not a new job, so the limit doesn't apply
    resp = await submit(content="First of three.")
    assert resp.status_code == 200
    assert resp.json()["document_id"] == first["document_id"]


@pytest.mark.parametrize("settings_overrides", [{"failure_rate": 1, "max_attempts": 1}])
async def test_content_that_failed_can_be_submitted_again(client, submit, processor):
    content = "Fails the first time."
    first = (await submit(content=content)).json()
    await processor.run_once()
    assert (await client.get(f"/documents/{first['document_id']}")).json()["status"] == "failed"

    resp = await submit(content=content)
    assert resp.status_code == 201
    assert resp.json()["document_id"] != first["document_id"]
    assert resp.json()["status"] == "queued"


async def test_identical_requests_at_the_same_time_create_one_document(submit, db, redis):
    content = "Double clicked."
    responses = await asyncio.gather(*(submit(content=content) for _ in range(3)))

    assert len({r.json()["document_id"] for r in responses}) == 1
    assert sorted(r.status_code for r in responses) == [200, 200, 201]
    assert await db["documents"].count_documents({"user_id": "alice"}) == 1
    # the requests that lost the race gave their rate limit slots back
    assert await redis.zcard("active-jobs:alice") == 1


async def test_list_documents_paginates_newest_first(client, submit, processor):
    ids = []
    for i in range(3):
        ids.append((await submit(title=f"doc {i}")).json()["document_id"])
        await processor.run_once()
    await submit(user_id="bob")

    resp = await client.get("/users/alice/documents", params={"page": 1, "page_size": 2})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert [d["document_id"] for d in body["items"]] == [ids[2], ids[1]]

    body = (await client.get("/users/alice/documents", params={"page": 2, "page_size": 2})).json()
    assert [d["document_id"] for d in body["items"]] == [ids[0]]


async def test_list_documents_filters_by_status(client, submit, processor):
    await submit()
    await processor.run_once()
    await submit()

    body = (await client.get("/users/alice/documents", params={"status": "queued"})).json()
    assert body["total"] == 1
    assert body["items"][0]["status"] == "queued"

    body = (await client.get("/users/alice/documents", params={"status": "completed"})).json()
    assert body["total"] == 1


async def test_list_documents_validates_query(client):
    assert (await client.get("/users/alice/documents", params={"page": 0})).status_code == 422
    assert (
        await client.get("/users/alice/documents", params={"page_size": 101})
    ).status_code == 422
    assert (
        await client.get("/users/alice/documents", params={"status": "nope"})
    ).status_code == 422


async def test_list_documents_for_unknown_user_is_empty(client):
    resp = await client.get("/users/nobody/documents")
    assert resp.status_code == 200
    assert resp.json()["items"] == []


async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "mongo": "ok", "redis": "ok"}
