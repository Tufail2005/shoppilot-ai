"""Phase 6.4 HTTP surface: authenticated POST /rag/query.

The router is thin on purpose: the request schema rejects everything
except ``query``, the role is resolved server-side by ``require_role``,
the pipeline runs through ``app.services.rag.answer_query`` (which
reuses the Phase 6.1-6.3 components unchanged), and the response is the
answer plus validated citation metadata. There is no conversation
persistence, message history, LangGraph, or tool logging here.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from psycopg_pool import AsyncConnectionPool

from app.auth import require_role
from app.db import get_db
from app.schemas.rag import RagCitationOut, RagQueryIn, RagQueryOut
from app.services import rag as rag_service
from app.services.embeddings import EmbeddingProvider
from app.services.generation import GenerationProvider


router = APIRouter(
    prefix="/rag",
    tags=["rag"],
)

# Module-level so tests can override the resolved user through
# FastAPI's dependency_overrides without depending on the database.
rag_access = require_role("customer", "support_agent", "admin")


@router.post("/query", response_model=RagQueryOut)
async def query_knowledge_base(
    payload: RagQueryIn,
    current_user: Annotated[
        dict,
        Depends(rag_access),
    ],
    pool: Annotated[AsyncConnectionPool, Depends(get_db)],
    embedding_provider: Annotated[
        EmbeddingProvider,
        Depends(rag_service.get_query_embedding_provider),
    ],
    generation_provider: Annotated[
        GenerationProvider,
        Depends(rag_service.get_generation_provider),
    ],
):
    # The role comes exclusively from the server-side profiles lookup;
    # the request body has no influence on it (extra="forbid").
    result = await rag_service.answer_query(
        pool,
        embedding_provider,
        generation_provider,
        query=payload.query,
        user_role=current_user["role"],
    )

    return RagQueryOut(
        answer=result.answer,
        citations=[
            RagCitationOut(
                number=citation.number,
                chunk_id=citation.chunk_id,
                document_id=citation.document_id,
                document_title=citation.document_title,
                chunk_index=citation.chunk_index,
            )
            for citation in result.citations
        ],
    )
