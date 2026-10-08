"""Tests that don't need Mongo or Redis."""

import random

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.models import DocumentCreate
from app.services.mongo_service import MongoService
from app.services.redis_service import RedisService
from app.services.summarizer_service import SummarizationError, SummarizerService, build_summary
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
