"""Handlers for /documents. The URLs that lead here are in app/routes/document_routes.py."""

from fastapi import HTTPException, Response, status

from app.dependencies import DocumentServiceDep
from app.models import (
    Document,
    DocumentCreate,
    DocumentDetail,
    DocumentInProgress,
    DocumentResponse,
)


async def submit_document(
    payload: DocumentCreate, response: Response, service: DocumentServiceDep
) -> DocumentResponse:
    doc, created = await service.submit(payload)
    if not created:
        response.status_code = status.HTTP_200_OK
    response.headers["Location"] = f"/documents/{doc.document_id}"
    return _to_response(doc)


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
