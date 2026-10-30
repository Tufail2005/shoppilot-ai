"""pgvector chunk retrieval for the Phase 6.1 RAG service.

Retrieves the chunks most similar to a pre-validated query embedding,
enforcing the knowledge-base visibility matrix in plain SQL. The
application pool connects with privileged database access, so RLS on
``documents``/``document_chunks`` (Phase 3.10) cannot be relied on
here — the WHERE clause is the authorization boundary, always bound
from server-side role data (never anything from the request).

SQL contract
------------

- Only ``ready`` documents are retrievable (``processing``/``failed``
  documents carry no trustworthy chunk set).
- ``visibility`` is filtered through ``ANY(%s)`` over a list passed by
  the service. The service derives that list exclusively from the
  trusted profile role; the SQL itself never sees a raw role name.
- Similarity ordering is cosine distance (``<=>``), matching the
  existing ``ivfflat (embedding extensions.vector_cosine_ops)`` index
  convention from the Phase 2 migrations.
- ``LIMIT 5`` is a fixed server-side constant: no ``top_k`` (or any
  other client-supplied limit) exists anywhere in this module.
- Ties are broken deterministically by
  ``(distance, document_id, chunk_index)`` so repeated runs return
  chunks in a stable order.

The query embedding reaches this module already validated by the
service (768 finite numbers), and is rendered through the shared
``_vector_literal`` helper from ``repositories/documents.py`` — the
same `%s::extensions.vector` cast used by ingestion, so no new vector
serialization format is introduced.
"""

from collections.abc import Sequence

from psycopg import AsyncCursor

# Reuse of the ingestion vector literal: validated floats only, bound
# with the same %s::extensions.vector cast as insert_document_chunks.
from app.repositories.documents import _vector_literal  # noqa: PLC2701


# The fixed server-side limit for Phase 6.1 retrieval. Breadth tuning
# (and any similarity threshold) belongs to the Phase 15 evaluation
# work and must never become a request parameter.
_RETRIEVAL_LIMIT = 5

_RETRIEVE_CHUNKS_SQL = f"""
    SELECT
        c.id,
        c.document_id,
        d.title,
        d.visibility,
        c.chunk_index,
        c.content,
        c.embedding <=> %s::extensions.vector AS distance
    FROM public.document_chunks AS c
    JOIN public.documents AS d ON d.id = c.document_id
    WHERE d.status = 'ready'
      AND d.visibility::text = ANY(%s)
    ORDER BY
        c.embedding <=> %s::extensions.vector ASC,
        d.id ASC,
        c.chunk_index ASC
    LIMIT {_RETRIEVAL_LIMIT}
"""


async def retrieve_chunks(
    cursor: AsyncCursor,
    *,
    query_embedding: Sequence[float],
    visibility: Sequence[str],
) -> list[dict]:
    """Run one parameterized similarity query and map the rows.

    ``query_embedding`` is the caller-validated query vector (768
    finite floats); it is bound twice — once for the projected
    ``distance`` and once for the ORDER BY — as a pgvector literal.
    ``visibility`` comes from the service's trusted role map and is
    bound as a text array, never interpolated into the SQL.

    Returns one dict per retrieved chunk: ``id`` (chunk id),
    ``document_id``, ``title`` (document title), ``visibility``,
    ``chunk_index``, ``content``, and ``distance`` (cosine distance —
    kept for the future Phase 15 threshold calibration, not used as a
    filter). An empty result set is returned as ``[]``.
    """

    literal = _vector_literal(list(query_embedding))
    await cursor.execute(
        _RETRIEVE_CHUNKS_SQL,
        [literal, list(visibility), literal],
    )
    rows = await cursor.fetchall()

    column_names = [column.name for column in cursor.description]

    return [dict(zip(column_names, row, strict=True)) for row in rows]


# The repository module deliberately exposes no helper beyond
# retrieve_chunks: chunk creation/deletion stays with
# repositories/documents.py (ingestion), and results carry no
# embedding values.
