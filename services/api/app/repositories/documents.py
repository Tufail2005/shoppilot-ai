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
    failure_stage,
    failure_message,
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


async def set_document_visibility(
    cursor: AsyncCursor,
    *,
    document_id: uuid.UUID,
    visibility: str,
) -> dict | None:
    """Set one document's visibility and return the updated row.

    Phase 5.9: a non-transition metadata update. Unlike the guarded
    status transitions above, there is deliberately NO `status` guard
    on this statement: visibility is settable for every document
    status (`processing`/`ready`/`failed`, matching the deletion
    policy), and a single-statement UPDATE serializes safely with a
    concurrent status write through Postgres row locking. Zero updated
    rows therefore means only one thing — the document id does not
    exist — and the caller reports 404.

    The statement touches exactly `visibility` and `updated_at`
    (explicit, because no trigger maintains it). It never alters
    `status`, the failure fields, `title`, `version`, or the row's
    `storage_path`, and it never touches document_chunks: chunks,
    embeddings, and the Storage object are untouched by a visibility
    change. `visibility` is a Pydantic-validated Literal bound as a
    parameter.

    The existing SELECT RLS policies evaluate the row's *current*
    visibility value at query time, so the change takes effect
    immediately for readers; no policy change is required.

    Returns the updated document row (exactly one row updated), or
    ``None`` when zero rows matched — the caller must report
    not-found and never claim an update was persisted.
    """

    await cursor.execute(
        f"""
        UPDATE public.documents
        SET
            visibility = %s,
            updated_at = now()
        WHERE id = %s
        RETURNING {_DOCUMENT_COLUMNS}
        """,
        [visibility, document_id],
    )

    return await _fetch_updated_row(cursor)


async def mark_document_ready(
    cursor: AsyncCursor,
    *,
    document_id: uuid.UUID,
) -> dict | None:
    """Transition one document from `processing` to `ready`.

    The guard `status = 'processing'` makes the transition explicit
    and one-way per row: a document that already reached `ready` or
    `failed` can never be overwritten by a late status update (an
    unexpected conflict is reported as no transition instead of being
    silently "fixed" by a second transition). The failure fields are
    cleared on `ready`, which the Phase 5.7 schema check constraint
    (`documents_failure_fields_only_when_failed`) requires — a `ready`
    document must never carry residual failure information.

    `updated_at` is set explicitly because no trigger maintains it.

    Returns the updated document row (exactly one row transitioned
    from `processing`), or `None` when zero rows transitioned — the
    caller must then treat the outcome as conflict/failure and never
    claim `ready` was persisted.
    """

    await cursor.execute(
        f"""
        UPDATE public.documents
        SET
            status = 'ready',
            failure_stage = NULL,
            failure_message = NULL,
            updated_at = now()
        WHERE id = %s AND status = 'processing'
        RETURNING {_DOCUMENT_COLUMNS}
        """,
        [document_id],
    )

    return await _fetch_updated_row(cursor)


async def mark_document_failed(
    cursor: AsyncCursor,
    *,
    document_id: uuid.UUID,
    stage: str,
    message: str,
) -> dict | None:
    """Transition one document from `processing` to `failed`.

    `stage` and `message` are sanitized, application-controlled
    values (a controlled failure-stage slug and a static message —
    never uncontrolled exception text, storage paths, credentials,
    or document contents). They are bound as parameters; the Phase
    5.7 check constraint requires exactly this pair on a `failed`
    document that records failure information.

    The same `status = 'processing'` guard applies: zero updated rows
    means the document is no longer `processing` (conflict), and the
    caller must treat that as a persistence failure rather than
    claiming `failed` was recorded.
    """

    await cursor.execute(
        f"""
        UPDATE public.documents
        SET
            status = 'failed',
            failure_stage = %s,
            failure_message = %s,
            updated_at = now()
        WHERE id = %s AND status = 'processing'
        RETURNING {_DOCUMENT_COLUMNS}
        """,
        [stage, message, document_id],
    )

    return await _fetch_updated_row(cursor)


async def delete_document(
    cursor: AsyncCursor,
    *,
    document_id: uuid.UUID,
) -> str | None:
    """Delete one document row and return its internal storage_path.

    The ``RETURNING`` clause yields the row's storage_path in the same
    statement that deletes it. Zero fetched rows means the document id
    does not exist: the caller must report not-found and must not
    attempt any Storage removal, since the object path is only ever
    known from the row.

    The document's chunk rows are removed by the schema's existing
    foreign key (``document_chunks.document_id references
    documents(id) on delete cascade`` from the initial schema) as part
    of this same statement. No separate chunk deletion is performed,
    and this file therefore contains no chunk-deletion statement for
    deletion purposes.

    The storage_path is an internal value: it is used only to address
    the Storage object, never logged, and never returned to callers
    above the service layer or placed in responses.

    Parameterized SQL. Commit semantics follow the repository
    convention in this module: the statement runs on the caller's
    cursor/transaction, and the caller (service) commits.

    Returns the deleted row's storage_path, or ``None`` when no row
    matched ``document_id``.
    """
    await cursor.execute(
        """
        DELETE FROM public.documents
        WHERE id = %s
        RETURNING storage_path
        """,
        [document_id],
    )

    row = await cursor.fetchone()

    if row is None:
        return None

    return row[0]


async def _fetch_updated_row(cursor: AsyncCursor) -> dict | None:
    """Return the single RETURNING row, or None for a zero-row update."""

    row = await cursor.fetchone()

    if row is None:
        return None

    column_names = [column.name for column in cursor.description]

    return dict(zip(column_names, row, strict=True))
