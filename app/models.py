from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

USER_ID_PATTERN = r"^[A-Za-z0-9_.-]+$"
MAX_TITLE_LENGTH = 200
MAX_CONTENT_LENGTH = 100_000


class DocumentStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


ACTIVE_STATUSES = [DocumentStatus.QUEUED, DocumentStatus.PROCESSING]

UserId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=64, pattern=USER_ID_PATTERN),
]


class Summary(BaseModel):
    text: str
    keywords: list[str]
    word_count: int
    reading_time_seconds: int
    model: str


class DocumentCreate(BaseModel):
    # str_strip_whitespace runs before the length checks, so "   " fails min_length
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    user_id: UserId
    title: str = Field(min_length=1, max_length=MAX_TITLE_LENGTH)
    content: str = Field(min_length=1, max_length=MAX_CONTENT_LENGTH)


class DocumentSubmitted(BaseModel):
    document_id: str
    status: DocumentStatus
    # only set when the summary came straight from the cache
    summary: Summary | None = None


class DocumentOut(BaseModel):
    document_id: str
    user_id: str
    title: str
    status: DocumentStatus
    summary: Summary | None = None
    error: str | None = None
    attempts: int
    cache_hit: bool
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None

    @classmethod
    def from_mongo(cls, doc: dict[str, Any]) -> "DocumentOut":
        return cls.model_validate({**doc, "document_id": str(doc["_id"])})


class DocumentPage(BaseModel):
    items: list[DocumentOut]
    page: int
    page_size: int
    total: int


DependencyStatus = Literal["ok", "unavailable"]


class HealthOut(BaseModel):
    status: Literal["ok", "degraded", "unavailable"]
    mongo: DependencyStatus
    redis: DependencyStatus
