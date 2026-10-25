import uuid
from collections.abc import Sequence

from psycopg import AsyncCursor


_DOCUMENT_COLUMNS = """
    id,
    title,
    storage_path,
    visibility,
    status,
    version,
    created_at
"""

_INSERT_CHUNK_SQL = """
    INSERT INTO public.document_chunks (
        document_id,
        content,
        embedding,
        chunk_index
    )
    VALUES (%s, %s, %s::extensions.vector, %s)
"""


async def create_document(
    cursor: AsyncCursor,
    *,
    title: str,
    storage_path: str,
    visibility: str,
) -> dict:
    # Visibility is always explicit here.
    # The database's `public` default is never relied upon.
    await cursor.execute(
        f"""
        INSERT INTO public.documents (
            title,
            storage_path,
            visibility
        )
        VALUES (%s, %s, %s)
        RETURNING {_DOCUMENT_COLUMNS}
        """,
        [title, storage_path, visibility],
    )

    row = await cursor.fetchone()

    if row is None:
        raise RuntimeError("Document creation returned no row")

    column_names = [column.name for column in cursor.description]

    document = dict(zip(column_names, row, strict=True))

    return document


def _vector_literal(values: list[float]) -> str:
    """Render one validated embedding vector as a pgvector text literal.

    The values must already have been validated as finite numbers by
    the ingestion service (768 values, numeric but not bool, finite).
    Every element is rendered through ``repr(float(...))``, so the
    string contains only digits, signs, decimal points, exponents, and
    separators — no quoting or interpolation of external input is
    involved. The literal is then passed to psycopg as a bound text
    parameter cast with ``::extensions.vector`` by the caller.

    Returns e.g. ``"[0.0123,-0.987]"``.
    """

    return "[" + ",".join(repr(float(value)) for value in values) + "]"


async def insert_document_chunks(
    cursor: AsyncCursor,
    *,
    document_id: uuid.UUID,
    chunks: Sequence[tuple[int, str, list[float]]],
) -> int:
    """Replace all chunks of one document, preserving chunk order.

    Each `chunks` item is ``(chunk_index, content, embedding)``, where
    ``chunk_index`` is the chunk's zero-based position in the document
    and ``embedding`` is a validated 768-value vector. The current
    chunk set of the document is deleted first and the new rows are
    inserted afterwards, all as parameterized SQL on the caller's
    cursor/transaction:

    - The ``unique (document_id, chunk_index)`` constraint from the
      initial schema guards against duplicate chunks ever persisting;
      replace-on-ingest keeps re-ingestion idempotent instead of
      failing on duplicate ingestion.
    - Because the DELETE and the INSERTs share the caller's
      transaction, a failure partway through is rolled back together
      with the deletion — the previous chunk set (if any) survives
      untouched and no partial set is left behind.
    - The `embedding` column is kept out of user-facing responses
      upstream; it is transmitted only as a database parameter.

    Returns the number of rows inserted (the caller's transaction is
    not committed here; that is the service's responsibility).

    Raises ``ValueError`` when `chunks` is empty: wiping a document's
    chunks without inserting anything is never an ingestion outcome
    (extraction rejects textless PDFs before chunking) and must not
    happen silently.
    """

    if not chunks:
        raise ValueError(
            "insert_document_chunks requires at least one chunk; "
            "an empty chunk set would delete existing chunks without "
            "replacing them"
        )

    # The schema-sequence-preserving flow relied on by the caller:
    # the DELETE runs on the same cursor/transaction as the INSERTs.
    await cursor.execute(
        """
        DELETE FROM public.document_chunks
        WHERE document_id = %s
        """,
        [document_id],
    )

    # executemany batches the INSERTs onto one prepared statement.
    rows = [
        (
            document_id,
            content,
            _vector_literal(embedding),
            chunk_index,
        )
        for chunk_index, content, embedding in chunks
    ]

    await cursor.executemany(_INSERT_CHUNK_SQL, rows)

    return cursor.rowcount
