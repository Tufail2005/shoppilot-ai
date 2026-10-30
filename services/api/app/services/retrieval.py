"""Query-side retrieval for the Phase 6.1 RAG service.

Phase 6.1 of the roadmap: ``retrieve(query, user_role)`` — embed the
query with the existing Phase 5.5 :class:`EmbeddingProvider` and fetch
the permitted ``ready`` chunks through the Phase 6.1 repository SQL.

Deliberately out of scope for Phase 6.1 (owned by later phases):

- ``POST /rag/query`` and any HTTP router (Phase 6.4),
- answer generation, prompt templates, and citations (Phase 6.2/6.3),
- similarity thresholds / relevance calibration (Phase 15),
- persistence of any retrieval outcome.

Authorization
-------------

The role is supplied by the caller from trusted server-side profile
data (Phase 3 ``require_role`` resolves it from ``public.profiles``);
it is never taken from the request. The role is mapped to the
document-visibility set the role may read:

- ``customer``       -> public only
- ``support_agent``  -> public + internal
- ``admin``          -> public + internal

Any missing or unsupported role fails closed — raised before the
embedding call and before any SQL, so no role-less request can ever
reach the database. The mapping is the single authorization decision
of this service; the repository enforces it as SQL filtering because
the application pool uses privileged database access that bypasses
RLS.

Embedding contract
------------------

Queries MUST be embedded through ``embed_query`` — the module
docstring of ``app/services/embeddings.py`` documents that query-side
and document-side formats are asymmetric, and only ``embed_query`` is
compatible with the vectors produced by ``embed_documents`` during
ingestion. The provider is synchronous (like the Phase 5 services) and
is therefore offloaded to a worker thread with ``asyncio.to_thread``,
matching the Phase 5.6 ingestion convention so the event loop is never
blocked by the embedding network call.

The returned vector is re-validated before any SQL runs — exactly one
vector, exactly :data:`EMBEDDING_DIMENSIONS` (768) values, every value
numeric (non-bool) and finite, matching the ``vector(768)`` column and
ingestion contract. Vectors are never truncated, padded, or repaired.

Error mapping
-------------

- Provider failures keep the established types: ``EmbeddingError`` for
  API/configuration problems and ``TypeError``/``ValueError`` for
  invalid query input (e.g. an empty/whitespace query), exactly as the
  provider raises them. Nothing is swallowed and nothing is fabricated;
  the Phase 6.4 route will map these to sanitized responses.
- Database failures are normalized to :class:`RetrievalError` (original
  exception chained) with a ``stage`` attribute, following the
  :class:`IngestionError` pattern. A database failure never results in
  an empty list masquerading as "no relevant documents".

Logging (project convention): never logs the query text, embedding
values, or raw error messages — exception types and counts only.
"""

import asyncio
import logging
import math
from collections.abc import Sequence

from psycopg_pool import AsyncConnectionPool

from app.repositories import retrieval as retrieval_repository
from app.services.embeddings import EMBEDDING_DIMENSIONS, EmbeddingProvider


logger = logging.getLogger(__name__)


# Trusted server-side role -> readable document visibility set.
# Every role below the line (missing profile, unknown future role,
# client-forged strings) fails closed at the lookup.
_CUSTOMER_VISIBILITY = ("public",)
_STAFF_VISIBILITY = ("public", "internal")

_ROLE_VISIBILITY = {
    "customer": _CUSTOMER_VISIBILITY,
    "support_agent": _STAFF_VISIBILITY,
    "admin": _STAFF_VISIBILITY,
}


class RetrievalError(Exception):
    """Raised when retrieval cannot be completed correctly.

    Normalizes database failures (the original exception is chained)
    and input-shape validation on the service side. The message is a
    static string — it never contains query text, role values, or
    vector values — and ``stage`` identifies the failing step, room
    for Phase 6.4's sanitized HTTP mapping.
    """

    def __init__(self, message: str, *, stage: str):
        super().__init__(message)
        self.stage = stage


def _visibility_for_role(user_role: object) -> tuple[str, ...]:
    """Map the trusted role to its readable visibility set, or fail closed."""

    try:
        return _ROLE_VISIBILITY[user_role]
    except (KeyError, TypeError):
        # Static message: the failing value is never echoed back.
        raise RetrievalError(
            "Retrieval is not permitted for this user role",
            stage="role_validation",
        ) from None


def _validated_query_vector(vector: object) -> list[float]:
    """Validate the query vector against the shared embedding contract.

    Mirrors the ingestion-side validation (Phase 5.6): a real sequence
    of exactly ``EMBEDDING_DIMENSIONS`` numeric (non-``bool``), finite
    values. Deviations raise :class:`RetrievalError` instead of reaching
    SQL with a shape the ``vector(768)`` column/`<=>` operator cannot
    handle correctly.
    """

    if isinstance(vector, (str, bytes)) or not isinstance(vector, Sequence):
        raise RetrievalError(
            "The retrieval query embedding is not a sequence of numbers",
            stage="embedding_validation",
        )

    try:
        length = len(vector)
    except TypeError as exc:
        raise RetrievalError(
            "The retrieval query embedding is not a sequence of numbers",
            stage="embedding_validation",
        ) from exc

    if length != EMBEDDING_DIMENSIONS:
        raise RetrievalError(
            "The retrieval query embedding does not match the configured "
            f"dimensionality: expected {EMBEDDING_DIMENSIONS}, got {length}",
            stage="embedding_validation",
        )

    validated: list[float] = []

    for position, value in enumerate(vector):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RetrievalError(
                "The retrieval query embedding contains a non-numeric value",
                stage="embedding_validation",
            )

        numeric = float(value)

        if not math.isfinite(numeric):
            raise RetrievalError(
                "The retrieval query embedding contains a non-finite value",
                stage="embedding_validation",
            )

        validated.append(numeric)

    return validated


async def retrieve(
    pool: AsyncConnectionPool,
    embedding_provider: EmbeddingProvider,
    *,
    query: str,
    user_role: object,
) -> list[dict]:
    """Retrieve the permitted ``ready`` chunks most similar to `query`.

    Parameters mirror the existing service conventions (the pool and
    provider as explicit injectables, everything else keyword-only) so
    both work in tests and in the future Phase 6.4 route:

        retrieve(pool, provider, query=?, user_role=?)

    `user_role` MUST come from trusted server-side profile data (the
    Phase 3 ``require_role`` dependency), never from the request.

    Returns at most five chunks (fixed server-side limit) ordered by
    cosine similarity, ties broken deterministically. Each dict carries
    the citation-ready fields: chunk ``id``, ``document_id``,
    document ``title``, ``visibility``, ``chunk_index``, ``content``,
    and the governing ``distance``. An empty result set is ``[]`` —
    not an answer and not a relevance statement (Phase 15 owns
    threshold calibration; Phase 6.3 owns answer generation).

    Provider failures (``EmbeddingError``, ``TypeError``/``ValueError``
    for an invalid query) propagate unchanged; database failures raise
    :class:`RetrievalError` with the original cause chained.
    """

    # Fail closed before anything else: no embedding call, no SQL.
    visibility = _visibility_for_role(user_role)

    # The provider is synchronous and makes a network call: offload to
    # a worker thread exactly like the Phase 5.6 ingestion does.
    vectors = await asyncio.to_thread(
        embedding_provider.embed_query,
        [query],
    )

    if not isinstance(vectors, Sequence) or isinstance(vectors, (str, bytes)):
        raise RetrievalError(
            "The embedding response is not a list of vectors",
            stage="embedding_validation",
        )

    if len(vectors) != 1:
        raise RetrievalError(
            "The embedding response count does not match the single "
            f"query input: expected 1, got {len(vectors)}",
            stage="embedding_validation",
        )

    query_vector = _validated_query_vector(vectors[0])

    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cursor:
                chunks = await retrieval_repository.retrieve_chunks(
                    cursor,
                    query_embedding=query_vector,
                    visibility=list(visibility),
                )
    except Exception as exc:
        # Static message: exception text can carry database internals.
        logger.error(
            "Chunk retrieval database query failed: %s",
            type(exc).__name__,
        )

        raise RetrievalError(
            "The permitted chunks could not be retrieved",
            stage="database_query",
        ) from exc

    logger.info(
        "Chunk retrieval returned %d chunk(s).",
        len(chunks),
    )

    return chunks
