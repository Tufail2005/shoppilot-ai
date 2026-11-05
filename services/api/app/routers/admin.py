import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, UploadFile, status
from psycopg_pool import AsyncConnectionPool
from storage3 import SyncStorageClient

from app.auth import require_role
from app.core.provider_concurrency import ProviderLimiters, get_provider_limiters
from app.db import get_db
from app.schemas.documents import (
    DocumentOut,
    DocumentVisibility,
    DocumentVisibilityUpdate,
)
from app.services import documents as documents_service
from app.services.embeddings import EmbeddingProvider, get_embedding_provider
from app.storage import get_storage


router = APIRouter(
    prefix="/admin",
    tags=["admin"],
)

# Module-level so tests can override the resolved user through
# FastAPI's dependency_overrides without depending on the database.
metrics_access = require_role("admin")


@router.get(
    "/metrics",
    summary="Aggregate, process-local provider and pipeline metrics",
)
async def get_metrics_snapshot(
    current_user: Annotated[
        dict,
        Depends(metrics_access),
    ],
) -> dict:
    """Return the aggregate Phase 6.8 metrics snapshot.

    Admin-only (the same trusted ``require_role("admin")`` dependency
    as the destructive document routes). The payload is the aggregate
    registry snapshot ONLY: bounded label values plus counts, sums,
    extremes, and histogram buckets. There are no user IDs, document
    IDs, request IDs, queries/prompts, or provider error messages in
    the registry, so none can appear in the response. Snapshot
    construction is fail-fast by design: an internal copy failure
    surfaces as an error, never as an empty, misleading snapshot.
    """
    # Deferred import keeps the router import graph identical to
    # pre-6.8 modules (observability is started by main.py at import
    # time, and the registry itself must not be constructed eagerly
    # here).
    from app.core.observability import metrics_payload
    from app.core.observability import provider_metrics

    return metrics_payload(provider_metrics.snapshot())


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
    storage: Annotated[SyncStorageClient, Depends(get_storage)],
    embedding_provider: Annotated[
        EmbeddingProvider,
        Depends(get_embedding_provider),
    ],
    limiters: Annotated[
        ProviderLimiters,
        Depends(get_provider_limiters),
    ],
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
            embedding_provider=embedding_provider,
            limiters=limiters,
        )
    finally:
        await file.close()


@router.delete(
    "/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_document(
    document_id: uuid.UUID,
    current_user: Annotated[
        dict,
        Depends(require_role("admin")),
    ],
    pool: Annotated[AsyncConnectionPool, Depends(get_db)],
    storage: Annotated[SyncStorageClient, Depends(get_storage)],
):
    # Admin-only: deletion is destructive and irreversible, and the
    # documents table has no creator column to scope a support agent's
    # deletion right to their own uploads.
    await documents_service.delete_document(
        pool,
        storage,
        document_id=document_id,
    )


@router.patch(
    "/documents/{document_id}",
    response_model=DocumentOut,
)
async def update_document_visibility(
    document_id: uuid.UUID,
    payload: DocumentVisibilityUpdate,
    current_user: Annotated[
        dict,
        Depends(require_role("admin")),
    ],
    pool: Annotated[AsyncConnectionPool, Depends(get_db)],
):
    # Admin-only. Flipping `internal -> public` is a content-exposure
    # grant, and the documents table has no creator column to scope a
    # support agent's update right to their own uploads (the same
    # rationale as the admin-only deletion in Phase 5.8). Changing
    # visibility never touches Storage, chunks, or embeddings.
    return await documents_service.update_document_visibility(
        pool,
        document_id=document_id,
        visibility=payload.visibility,
    )
