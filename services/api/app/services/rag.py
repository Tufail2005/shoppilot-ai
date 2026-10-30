"""Endpoint-facing RAG orchestration for the Phase 6.4 query service.

Phase 6.4 of the roadmap: the service layer behind the authenticated
``POST /rag/query`` endpoint, connecting the existing Phase 6.1
retrieval, Phase 6.2 prompt building, and Phase 6.3 generation and
citation validation to HTTP.

Deliberately out of scope for Phase 6.4 (owned by later phases):

- conversation persistence, message history, and memory (Phases 7/9),
- SSE streaming (Phase 7.5), LangGraph, and agent tools (Phases 9/10),
- prompt-injection filtering and output guardrails (Phase 14),
- similarity thresholds and relevance calibration (Phase 15).

Reuse
-----

The pipeline itself is never duplicated: :func:`answer_query` calls
:func:`app.services.generation.generate_rag_answer` unchanged — the
single seam that already orchestrates retrieval, prompt construction,
generation, and citation validation. This module adds only three
things:

- the two FastAPI provider dependencies (:func:`get_query_embedding_provider`
  and :func:`get_generation_provider`) resolving the existing Phase
  5.5/6.3 factories,
- the lazy generation provider wrapper (:class:`LazyGenerationProvider`),
- the typed-error -> sanitized-HTTP mapping of :func:`answer_query`.

Authorization
-------------

The role is supplied by the router's ``rag_access`` dependency
(``require_role("customer", "support_agent", "admin")``), which resolves
it server-side from ``public.profiles`` — never from the request. The
request schema accepts nothing but ``query`` (``extra="forbid"``), and
the Phase 6.1 fail-closed role map means an unexpected role value still
cannot reach the database.

Generation provider laziness
----------------------------

The generation provider is resolved through
:class:`LazyGenerationProvider`, which defers
``create_gemini_generation_provider()`` to the first ``generate()``
call. A missing Gemini configuration therefore never prevents the
deterministic no-retrieval path — the exact
``INSUFFICIENT_EVIDENCE_MESSAGE`` answer with no citations, decided by
Phase 6.3 without any model call — from responding successfully. A
configuration problem surfaces as a sanitized 503 only when a real
answer must actually be generated.

The embedding provider, by contrast, is validated eagerly by its
dependency (the established Phase 5.7 configuration precedent): the
query cannot even be embedded without a usable configuration, so the
endpoint reports that immediately as a sanitized 503.

Error mapping
-------------

Everything from :func:`generate_rag_answer` is mapped onto sanitized
responses; the original exceptions are chained and never echo their
messages:

- ``EmbeddingError``                                  -> 503 (knowledge base unavailable)
- ``RetrievalError`` (``database_query`` /
  ``embedding_validation`` stages)                    -> 503 (knowledge base unavailable)
- ``RetrievalError`` (``role_validation`` stage)      -> 403 (defense in depth behind
  ``require_role``)
- ``GenerationError`` (``configuration`` stage)       -> 503 (generation not configured)
- ``GenerationError`` (all other stages)              -> 503 (generation temporarily unavailable)
- ``TypeError`` / ``ValueError`` from query or prompt
  validation (unreachable after Pydantic; kept narrow) -> 422 (invalid query)
- unexpected exceptions                               -> 500 (generic detail)

Logging (project convention): exception types and stages only — never
the query text, retrieved content, generated text, provider messages,
or key material.
"""

import logging

from fastapi import HTTPException, status
from psycopg_pool import AsyncConnectionPool

from app.services.embeddings import (
    EmbeddingError,
    EmbeddingProvider,
    create_gemini_provider,
)
from app.services.generation import (
    GenerationError,
    GenerationProvider,
    RagAnswer,
    create_gemini_generation_provider,
    generate_rag_answer,
)
from app.services.retrieval import RetrievalError


logger = logging.getLogger(__name__)


# Sanitized, stable response details. They are asserted verbatim by the
# tests and never carry exception text, configuration details, query
# text, or document content.
DETAIL_FORBIDDEN = "Insufficient permissions"
DETAIL_KB_UNAVAILABLE = "The knowledge base is temporarily unavailable"
DETAIL_KB_NOT_CONFIGURED = "The knowledge base is not configured"
DETAIL_GENERATION_UNAVAILABLE = "The answer could not be generated"
DETAIL_GENERATION_NOT_CONFIGURED = "RAG answer generation is not configured"
DETAIL_INVALID_QUERY = "Invalid query"


_ROLE_VALIDATION_STAGE = "role_validation"
_GENERATION_CONFIGURATION_STAGE = "configuration"


class LazyGenerationProvider:
    """A :class:`GenerationProvider` that defers configuration to first use.

    The Gemini factory (:func:`create_gemini_generation_provider`) runs
    only when a real answer must be generated — never on the
    no-retrieval path, so a missing or unusable generation configuration
    cannot block the deterministic insufficient-evidence response. The
    provider is created once per wrapper and reused for subsequent
    generations; a configuration failure propagates as
    :class:`GenerationError` sanely mapped by :func:`answer_query`.
    """

    def __init__(self) -> None:
        self._provider: GenerationProvider | None = None

    def generate(self, prompt) -> str:
        if self._provider is None:
            self._provider = create_gemini_generation_provider()

        return self._provider.generate(prompt)


def get_query_embedding_provider() -> EmbeddingProvider:
    """FastAPI dependency resolving the embedding provider per request.

    Configuration is resolved and validated here — before any retrieval
    runs — following the Phase 5.7 precedent of failing fast on an
    unusable configuration. The endpoint answers nothing without query
    embeddings, so a missing ``GEMINI_API_KEY`` surfaces immediately as
    a sanitized 503 instead of a cryptic failure later in the pipeline.

    The factory is the existing Phase 5.5 one; the RAG-specific detail
    wording differs from the ingestion dependency on purpose.
    """
    try:
        return create_gemini_provider()
    except EmbeddingError as exc:
        logger.error(
            "Query embedding provider configuration is unusable: %s",
            type(exc).__name__,
        )

        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=DETAIL_KB_NOT_CONFIGURED,
        ) from exc


def get_generation_provider() -> GenerationProvider:
    """FastAPI dependency for the lazy generation provider.

    Deliberately resolves nothing: see :class:`LazyGenerationProvider`.
    Configuration problems surface as sanitized 503s only when the
    model is actually needed.
    """
    return LazyGenerationProvider()


async def answer_query(
    pool: AsyncConnectionPool,
    embedding_provider: EmbeddingProvider,
    generation_provider: GenerationProvider,
    *,
    query: str,
    user_role: object,
) -> RagAnswer:
    """Answer one knowledge-base question with validated citations.

    A thin wrapper around :func:`generate_rag_answer` (retrieval, prompt
    building, generation, and citation validation all happen there):
    `user_role` is the server-resolved profile role from the
    ``require_role`` dependency, never a request value; `query` is the
    validated request field, forwarded unchanged.

    Returns the complete :class:`~app.services.generation.RagAnswer`.
    Empty retrieval already carries the exact
    ``INSUFFICIENT_EVIDENCE_MESSAGE`` with no citations and no model
    call; failures are mapped to sanitized :class:`HTTPException`s as
    documented in the module docstring.
    """
    try:
        return await generate_rag_answer(
            pool,
            embedding_provider,
            generation_provider,
            query=query,
            user_role=user_role,
        )
    except HTTPException:
        raise
    except EmbeddingError as exc:
        logger.error(
            "RAG query embedding failed: %s",
            type(exc).__name__,
        )

        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=DETAIL_KB_UNAVAILABLE,
        ) from exc
    except RetrievalError as exc:
        if exc.stage == _ROLE_VALIDATION_STAGE:
            logger.warning(
                "RAG retrieval rejected the role: %s",
                exc.stage,
            )

            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=DETAIL_FORBIDDEN,
            ) from exc

        logger.error(
            "RAG retrieval failed: %s (%s)",
            exc.stage,
            type(exc).__name__,
        )

        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=DETAIL_KB_UNAVAILABLE,
        ) from exc
    except GenerationError as exc:
        if exc.stage == _GENERATION_CONFIGURATION_STAGE:
            logger.error(
                "RAG generation configuration is unusable: %s",
                type(exc).__name__,
            )

            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=DETAIL_GENERATION_NOT_CONFIGURED,
            ) from exc

        logger.error(
            "RAG generation failed: %s (%s)",
            exc.stage,
            type(exc).__name__,
        )

        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=DETAIL_GENERATION_UNAVAILABLE,
        ) from exc
    except (TypeError, ValueError) as exc:
        # Query or prompt validation leaked past the request schema:
        # a client input problem, not an outage. Kept deliberately
        # narrow so provider-internal errors still fail as 5xx.
        logger.error(
            "RAG query or prompt validation failed: %s",
            type(exc).__name__,
        )

        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=DETAIL_INVALID_QUERY,
        ) from exc
    except Exception as exc:
        logger.error(
            "RAG query failed unexpectedly: %s",
            type(exc).__name__,
        )

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=DETAIL_GENERATION_UNAVAILABLE,
        ) from exc
