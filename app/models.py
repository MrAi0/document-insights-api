from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

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


class DocumentBase(BaseModel):
    """A document without its text. Each item of GET /users/{user_id}/documents is one of these."""

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
    def from_mongo(cls, doc: dict[str, Any]) -> Self:
        # Internal fields (content_hash, lease_token, visible_at) are dropped here,
        # since models ignore fields they don't declare.
        return cls.model_validate({**doc, "document_id": str(doc["_id"])})

    @property
    def is_finished(self) -> bool:
        return self.status not in ACTIVE_STATUSES


class Document(DocumentBase):
    """The whole document, as the services return it."""

    content: str


class DocumentInProgress(BaseModel):
    """Single-document response while it's queued or processing.

    Nothing has been worked out yet, so a client polling every few seconds only
    gets what it needs to decide whether to poll again.
    """

    document_id: str
    status: Literal[DocumentStatus.QUEUED, DocumentStatus.PROCESSING]


class DocumentDetail(Document):
    """Single-document response once processing has finished: the whole document.

    `summary` is set when it completed, `error` when it failed.
    """

    status: Literal[DocumentStatus.COMPLETED, DocumentStatus.FAILED]


# What POST /documents and GET /documents/{document_id} return. `status` decides
# which of the two it is, and OpenAPI documents it as a oneOf on that field.
DocumentResponse = Annotated[DocumentInProgress | DocumentDetail, Field(discriminator="status")]


class DocumentPage(BaseModel):
    items: list[DocumentBase]
    page: int
    page_size: int
    total: int


class ErrorOut(BaseModel):
    """Body of every error response except 422, which is FastAPI's validation error."""

    detail: str


DependencyStatus = Literal["ok", "unavailable"]


class HealthOut(BaseModel):
    status: Literal["ok", "degraded", "unavailable"]
    mongo: DependencyStatus
    redis: DependencyStatus
