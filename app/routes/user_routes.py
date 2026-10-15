from fastapi import APIRouter

from app.controllers import user_controller

router = APIRouter(prefix="/users", tags=["users"])

router.add_api_route(
    "/{user_id}/documents",
    user_controller.list_user_documents,
    methods=["GET"],
    summary="List a user's documents",
    response_description="One page of the user's documents, newest first, without their text",
)
