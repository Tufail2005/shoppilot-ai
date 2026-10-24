from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, UploadFile, status
from psycopg_pool import AsyncConnectionPool
from supabase import Client

from app.auth import require_role
from app.db import get_db
from app.schemas.documents import DocumentOut, DocumentVisibility
from app.services import documents as documents_service
from app.storage import get_storage


router = APIRouter(
    prefix="/admin",
    tags=["admin"],
)


@router.post(
    "/documents",
    response_model=DocumentOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_document(
    current_user: Annotated[
        dict,
        Depends(require_role("support_agent", "admin")),
    ],
    file: Annotated[UploadFile, File()],
    pool: Annotated[AsyncConnectionPool, Depends(get_db)],
    storage: Annotated[Client, Depends(get_storage)],
    visibility: Annotated[
        DocumentVisibility | None,
        Form(),
    ] = None,
):
    try:
        return await documents_service.upload_document(
            pool,
            storage,
            user_role=current_user["role"],
            upload=file,
            visibility=visibility,
        )
    finally:
        await file.close()
