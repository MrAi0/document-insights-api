from fastapi import APIRouter, HTTPException, Response, status

from app.dependencies import DocumentServiceDep
from app.models import (
    Document,
    DocumentCreate,
    DocumentDetail,
    DocumentInProgress,
    DocumentResponse,
    ErrorOut,
)

router = APIRouter(prefix="/documents", tags=["documents"])


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Submit a document for summarization",
    response_description="The new document. Already completed if the content was summarized before",
    responses={
        status.HTTP_200_OK: {
            "model": DocumentResponse,
            "description": "The user already submitted this content. Returns that document "
            "instead of creating another",
        },
        status.HTTP_429_TOO_MANY_REQUESTS: {
            "model": ErrorOut,
            "description": "The user already has the maximum number of documents in progress",
        },
    },
)
async def submit_document(
    payload: DocumentCreate, response: Response, service: DocumentServiceDep
) -> DocumentResponse:
    doc, created = await service.submit(payload)
    if not created:
        response.status_code = status.HTTP_200_OK
    response.headers["Location"] = f"/documents/{doc.document_id}"
    return _to_response(doc)


@router.get(
    "/{document_id}",
    summary="Poll a document",
    response_description="Only `document_id` and `status` while queued or processing. "
    "The whole document once completed or failed",
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorOut, "description": "Document not found"}},
)
async def get_document(document_id: str, service: DocumentServiceDep) -> DocumentResponse:
    # A malformed id is a 404 as well. From the client's point of view there's
    # no document there either way.
    doc = await service.get(document_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Document not found")
    return _to_response(doc)


def _to_response(doc: Document) -> DocumentInProgress | DocumentDetail:
    # Same rule for submitting and polling, so a client handles one shape either way.
    if doc.is_finished:
        return DocumentDetail.model_validate(doc.model_dump())
    return DocumentInProgress(document_id=doc.document_id, status=doc.status)
