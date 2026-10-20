from typing import Annotated

from fastapi import APIRouter, Path, Query

from app.dependencies import DocumentServiceDep
from app.models import USER_ID_PATTERN, DocumentPage, DocumentStatus

MAX_PAGE_SIZE = 100

router = APIRouter(prefix="/users", tags=["users"])


@router.get(
    "/{user_id}/documents",
    summary="List a user's documents",
    response_description="One page of the user's documents, newest first, without their text",
)
async def list_user_documents(
    user_id: Annotated[str, Path(min_length=1, max_length=64, pattern=USER_ID_PATTERN)],
    service: DocumentServiceDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 20,
    status_filter: Annotated[DocumentStatus | None, Query(alias="status")] = None,
) -> DocumentPage:
    # There's no users collection, so an unknown user is just an empty list, not a 404.
    return await service.list_for_user(
        user_id, status=status_filter, page=page, page_size=page_size
    )
