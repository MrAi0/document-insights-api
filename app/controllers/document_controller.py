from fastapi import APIRouter, HTTPException, Response, status

from app.dependencies import DocumentServiceDep
from app.models import DocumentCreate, DocumentOut, DocumentSubmitted

router = APIRouter(prefix="/documents", tags=["documents"])


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_429_TOO_MANY_REQUESTS: {
            "description": "The user already has the maximum number of documents in progress"
        }
    },
)
async def submit_document(
    payload: DocumentCreate, response: Response, service: DocumentServiceDep
) -> DocumentSubmitted:
    doc = await service.submit(payload)
    response.headers["Location"] = f"/documents/{doc.document_id}"
    return DocumentSubmitted(document_id=doc.document_id, status=doc.status, summary=doc.summary)


@router.get(
    "/{document_id}",
    responses={status.HTTP_404_NOT_FOUND: {"description": "Document not found"}},
)
async def get_document(document_id: str, service: DocumentServiceDep) -> DocumentOut:
    # A malformed id is a 404 as well. From the client's point of view there's
    # no document there either way.
    doc = await service.get(document_id)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Document not found")
    return doc
