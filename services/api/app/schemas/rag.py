import uuid

from pydantic import BaseModel, ConfigDict, field_validator


class RagQueryIn(BaseModel):
    """Phase 6.4 request body for POST /rag/query.

    Only the ``query`` field is accepted: ``extra="forbid"`` rejects any
    unexpected field (``role``, ``user_id``, ``top_k``, ...) with a 422
    instead of silently ignoring it. The endpoint has no client-side
    knobs — the role is resolved server-side by ``require_role`` and the
    retrieval limit is a fixed server-side constant.

    The validator only enforces that the query is not blank; the value
    is forwarded to the pipeline unmodified (never stripped or
    rewritten). Input length limits are Phase 14.1 territory.
    """

    model_config = ConfigDict(extra="forbid")

    query: str

    @field_validator("query")
    @classmethod
    def query_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must not be empty or whitespace-only")

        return value


class RagCitationOut(BaseModel):
    """One validated numbered citation in the response.

    All values are copied from the Phase 6.3 :class:`~app.services.generation.Citation`,
    so a citation always points at the exact database chunk the
    retrieval returned. Chunk content, embeddings, visibility, and
    distance are deliberately NOT exposed here.
    """

    number: int
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    chunk_index: int


class RagQueryOut(BaseModel):
    """Phase 6.4 response body: answer plus validated citation metadata."""

    answer: str
    citations: list[RagCitationOut]
