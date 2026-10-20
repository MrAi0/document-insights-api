"""Tests that don't need Mongo or Redis."""

import random

import httpx
import pytest
from bson import ObjectId
from pydantic import ValidationError

from app.config import Settings
from app.dependencies import get_document_service
from app.main import create_app
from app.models import DocumentCreate
from app.routes import document_routes, health_routes, user_routes
from app.services.mongo_service import MongoService
from app.services.redis_service import RedisService
from app.services.summarizer_service import SummarizationError, SummarizerService, build_summary
from app.utils.clock import utcnow
from app.utils.hashing import hash_content
from app.utils.ids import parse_object_id

VALID = {"user_id": "alice", "title": "Q3 report", "content": "Revenue went up."}


def test_document_create_strips_whitespace():
    doc = DocumentCreate(**{**VALID, "title": "  Q3 report  "})
    assert doc.title == "Q3 report"


@pytest.mark.parametrize(
    "overrides",
    [
        {"title": "   "},
        {"content": "\n\t "},
        {"user_id": "alice smith"},
        {"user_id": "../etc"},
        {"user_id": "x" * 65},
        {"content": "x" * 100_001},
        {"unexpected": "field"},
    ],
)
def test_document_create_rejects_bad_input(overrides):
    with pytest.raises(ValidationError):
        DocumentCreate(**{**VALID, **overrides})


def test_content_hash_is_stable_and_case_sensitive():
    assert hash_content("hello") == hash_content("hello")
    assert hash_content("hello") != hash_content("Hello")


def test_build_summary():
    content = (
        "Redis is used for caching. Redis also tracks rate limits. "
        "MongoDB stores the documents. This sentence is not in the summary."
    )
    summary = build_summary(content)

    assert summary.text == "Redis is used for caching. Redis also tracks rate limits."
    assert summary.keywords[0] == "redis"
    assert "is" not in summary.keywords
    assert summary.word_count == 21
    assert summary.reading_time_seconds >= 1
    # same input, same output. The cache depends on this.
    assert build_summary(content) == summary


def test_build_summary_truncates_long_text():
    summary = build_summary("word " * 1000)
    assert len(summary.text) <= 500
    assert summary.text.endswith("...")


async def test_summarizer_fails_at_configured_rate():
    always_fails = SummarizerService(0, 0, failure_rate=1.0)
    with pytest.raises(SummarizationError):
        await always_fails.summarize("anything")

    never_fails = SummarizerService(0, 0, failure_rate=0.0)
    assert (await never_fails.summarize("anything")).text == "anything"


async def test_summarizer_failure_rate_is_roughly_right():
    summarizer = SummarizerService(0, 0, failure_rate=0.1, rng=random.Random(42))
    failures = 0
    for _ in range(1000):
        try:
            await summarizer.summarize("text")
        except SummarizationError:
            failures += 1
    assert 60 < failures < 140


def test_settings_reject_lease_shorter_than_processing():
    with pytest.raises(ValidationError, match="JOB_LEASE_SECONDS"):
        Settings(_env_file=None, job_lease_seconds=20, processing_max_seconds=30)


def test_settings_reject_inverted_processing_range():
    with pytest.raises(ValidationError, match="PROCESSING_MIN_SECONDS"):
        Settings(_env_file=None, processing_min_seconds=40, processing_max_seconds=30)


def test_parse_object_id():
    assert parse_object_id("6ac76da774f52a459b1d7136") is not None
    assert parse_object_id("not-an-id") is None
    assert parse_object_id("") is None


def test_connection_singletons_complain_if_used_before_connect():
    with pytest.raises(RuntimeError, match="not connected"):
        _ = MongoService().db
    with pytest.raises(RuntimeError, match="not connected"):
        _ = RedisService().client


def make_app():
    # Never started, so it doesn't connect to anything. Fine for anything that
    # doesn't reach Mongo or Redis.
    return create_app(Settings(_env_file=None, log_json=False))


def test_every_route_goes_to_its_handler():
    routes = {
        (method, route.path): route.endpoint
        for module in (document_routes, user_routes, health_routes)
        for route in module.router.routes
        for method in route.methods
    }

    assert routes == {
        ("POST", "/documents"): document_routes.submit_document,
        ("GET", "/documents/{document_id}"): document_routes.get_document,
        ("GET", "/users/{user_id}/documents"): user_routes.list_user_documents,
        ("GET", "/health"): health_routes.health,
    }
    # and the app serves exactly these
    served = make_app().openapi()["paths"]
    assert {(method.upper(), path) for path, ops in served.items() for method in ops} == set(routes)


def test_openapi_documents_both_document_shapes():
    paths = make_app().openapi()["paths"]

    def schema(path, method, code):
        return paths[path][method]["responses"][code]["content"]["application/json"]["schema"]

    polled = schema("/documents/{document_id}", "get", "200")
    assert polled["discriminator"]["mapping"] == {
        "queued": "#/components/schemas/DocumentInProgress",
        "processing": "#/components/schemas/DocumentInProgress",
        "completed": "#/components/schemas/DocumentDetail",
        "failed": "#/components/schemas/DocumentDetail",
    }
    # submitting returns the same thing, whether it's new (201) or a resubmission (200)
    assert schema("/documents", "post", "201")["oneOf"] == polled["oneOf"]
    assert schema("/documents", "post", "200")["oneOf"] == polled["oneOf"]


async def test_unexpected_error_is_a_json_500_with_the_request_id(capsys):
    class BrokenService:
        async def get(self, document_id: str):
            raise RuntimeError("nobody saw this coming")

    app = make_app()
    app.dependency_overrides[get_document_service] = BrokenService
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/documents/{ObjectId()}", headers={"X-Request-ID": "req-123"})

    assert resp.status_code == 500
    # JSON like every other error, without the exception's details
    assert resp.json() == {"detail": "Internal server error"}
    assert resp.headers["x-request-id"] == "req-123"
    # the traceback is logged instead, tagged with the request id
    logged = capsys.readouterr().out
    assert "unhandled error request_id=req-123" in logged
    assert "RuntimeError: nobody saw this coming" in logged


def test_utcnow_has_mongo_precision():
    assert utcnow().microsecond % 1000 == 0
