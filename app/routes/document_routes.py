from fastapi import APIRouter, status

from app.controllers import document_controller
from app.models import DocumentResponse, ErrorOut

router = APIRouter(prefix="/documents", tags=["documents"])

router.add_api_route(
    "",
    document_controller.submit_document,
    methods=["POST"],
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

router.add_api_route(
    "/{document_id}",
    document_controller.get_document,
    methods=["GET"],
    summary="Poll a document",
    response_description="Only `document_id` and `status` while queued or processing. "
    "The whole document once completed or failed",
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorOut, "description": "Document not found"}},
)
