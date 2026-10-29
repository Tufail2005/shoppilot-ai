import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


DocumentVisibility = Literal["public", "internal"]


class DocumentVisibilityUpdate(BaseModel):
    """Phase 5.9 request body for PATCH /admin/documents/{document_id}.

    Only the visibility field is updateable: this endpoint never
    touches status, title, or failure fields, so the schema accepts
    nothing else. ``extra="forbid"`` rejects unexpected fields with a
    422 instead of silently ignoring them.
    """

    model_config = ConfigDict(extra="forbid")

    visibility: DocumentVisibility


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    status: str
    visibility: str
    version: int
    # Phase 5.7: sanitized failure information, present (with a
    # controlled stage slug and static message) only when `status`
    # is `failed`; a `ready` or `processing` document always has
    # both `None`. Values never embed storage paths, credentials,
    # document text, or uncontrolled exception messages.
    failure_stage: str | None = None
    failure_message: str | None = None
    created_at: datetime
