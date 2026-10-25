"""Embedded-document ingestion for the knowledge-base pipeline.

Phase 5.6 of the roadmap: turn an uploaded PDF into fully embedded
``document_chunks`` rows. The service orchestrates the existing Phase
5.3-5.5 building blocks instead of reimplementing them:

    download PDF bytes from Storage (Phase 5.2 bucket)
        -> extract_text()          (Phase 5.3)
        -> chunk_text()            (Phase 5.4)
        -> embed_documents()       (Phase 5.5 provider, bounded batches)
        -> insert_document_chunks  (repositories, one transaction)

Explicitly out of scope for Phase 5.6:

- Document status transitions to ``ready``/``failed`` and upload-route
  wiring. This module is a callable service; Phase 5.7 will perform
  the status lifecycle and decide the call site.
- Retry infrastructure, background jobs, queues, or concurrency.
  Everything below is strictly sequential.

Batching
--------

``EMBEDDING_BATCH_SIZE`` is an application-side conservative default
(16), not a verified provider request cap. This codebase has not
verified the synchronous ``embed_content`` request limits against
current official Google documentation, so no such limit is relied on
or stated here. What is documented and relied on is bounded per-input
size: Gemini embedding models accept up to 8,192 tokens per input text
(see ``app/services/embeddings.py``), and a chunk is at most about 800
*estimated* tokens by the approximate 4-characters-per-token heuristic
of ``app/services/chunking.py`` — estimates are not exact token
counts. Cutting the chunk flow into bounded, sequential batches keeps
every request small in both item count and total estimated size; the
batch size may be revisited if providers ever return limit errors,
which surface as ``EmbeddingError`` and fail ingestion atomically.

Validation
----------

Every embedding vector is re-validated by this service, independent of
the provider's own checks, before any SQL runs: exactly one vector per
chunk, exactly 768 values per vector (``EMBEDDING_DIMENSIONS``), every
value finite and numeric (bools rejected), and ordering aligned
chunk-for-chunk. A misbehaving or future provider implementation can
therefore never reach the database through a partial, truncated,
padded, or reordered embedding set.

Transaction and duplicates
--------------------------

Extraction, chunking, embedding, and validation all complete before
the database transaction opens. The repo call then deletes the
document's existing chunks and inserts the new ones inside one
transaction (see ``insert_document_chunks``), so a failure rolls back
completely: no partial chunk set is ever persisted, re-ingestion is
idempotent, and the schema's ``unique (document_id, chunk_index)``
constraint backstops any duplicate rows regardless.

Security
--------

This module never logs the internal storage path, bucket URLs,
PDF/extracted/chunk text, or embedding values. Failures are logged
with the document ID, the failure stage, and the exception type only —
exception *messages* are deliberately excluded because they are
uncontrolled content. ``IngestionError`` messages are static strings
that never restate path-like or vector values.
"""

import asyncio
import logging
import math
import uuid
from collections.abc import Sequence

from psycopg_pool import AsyncConnectionPool
from supabase import Client

from app.repositories import documents as documents_repository
from app.services.chunking import DEFAULT_CHUNK_SIZE, DEFAULT_OVERLAP, chunk_text
from app.services.documents import PDFExtractionError, extract_text
from app.services.embeddings import EMBEDDING_DIMENSIONS, EmbeddingProvider
from app.storage import DOCUMENTS_BUCKET


logger = logging.getLogger(__name__)


EMBEDDING_BATCH_SIZE = 16


class IngestionError(Exception):
    """Raised when a document cannot be fully ingested.

    All pipeline failures are normalized to this type (with the
    original exception chained) so Phase 5.7's status lifecycle can
    handle every failure mode — Storage download, extraction, chunking
    output, embedding generation/validation, and database insertion —
    through a single catch. The message is a static string and never
    contains storage paths, URLs, document text, or vector values.
    """

    def __init__(self, message: str, *, stage: str):
        super().__init__(message)
        self.stage = stage


# One vector item is exactly one (chunk_index, content, embedding)
# tuple, aligned to the chunk's position in document order.
_ChunkWithEmbedding = tuple[int, str, list[float]]


def _log_failure(stage: str, document_id: uuid.UUID, exc: Exception) -> None:
    """Log a pipeline failure without any uncontrolled error content.

    Only the stage, the document ID (an opaque internal UUID), and the
    exception type name are logged. Exception messages can carry
    storage paths, bucket URLs, or provider request details, so they
    are never included, and no traceback is emitted for the same
    reason.
    """

    logger.error(
        "Document ingestion failed at stage %s for document %s: %s",
        stage,
        document_id,
        type(exc).__name__,
    )


def _download_pdf(
    storage: Client,
    storage_path: str,
    document_id: uuid.UUID,
) -> bytes:
    """Download the stored PDF bytes from the private documents bucket.

    Synchronous (the Supabase client is synchronous); the caller runs
    this in a worker thread so the download never blocks the event
    loop. ``storage_path`` is used only to address the object — it is
    never logged. Raises ``IngestionError`` when the object cannot be
    read; the underlying exception is chained but neither its message
    nor the storage path is exposed through logging.
    """

    try:
        return storage.from_(DOCUMENTS_BUCKET).download(storage_path)
    except Exception as exc:
        _log_failure("storage_download", document_id, exc)

        raise IngestionError(
            "The uploaded document could not be downloaded from Storage",
            stage="storage_download",
        ) from exc


def _extracted_text(pdf_bytes: bytes, document_id: uuid.UUID) -> str:
    """Extract document text, mapping extraction failures to 5.6 errors."""

    try:
        return extract_text(pdf_bytes)
    except PDFExtractionError as exc:
        _log_failure("text_extraction", document_id, exc)

        raise IngestionError(
            "Text could not be extracted from the uploaded PDF",
            stage="text_extraction",
        ) from exc


def _chunked(
    text: str,
    document_id: uuid.UUID,
    *,
    chunk_size: int,
    overlap: int,
) -> list[str]:
    """Chunk extracted text and reject an empty chunk set.

    ``extract_text`` already rejects textless PDFs, so an empty chunk
    list can only indicate a pipeline configuration problem; it is
    never silently persisted as "nothing".
    """

    chunks = chunk_text(text, chunk_size=chunk_size, overlap=overlap)

    if not chunks:
        exc = IngestionError(
            "The document produced no chunks; nothing to embed or insert",
            stage="chunking",
        )
        _log_failure("chunking", document_id, exc)

        raise exc

    return chunks


def _validated_vector(vector: Sequence[float]) -> list[float]:
    """Validate one embedding vector before it reaches SQL.

    The vector must be a real sequence of exactly
    ``EMBEDDING_DIMENSIONS`` values, each numeric (``bool`` values are
    rejected even though ``bool`` subclasses ``int``), finite, and
    returned as plain floats. Any deviation fails the entire
    ingestion — vectors are never truncated, padded, or "repaired".
    """

    if isinstance(vector, (str, bytes)) or not isinstance(vector, Sequence):
        raise IngestionError(
            "An embedding vector is not a sequence of numbers",
            stage="embedding_validation",
        )

    try:
        length = len(vector)
    except TypeError as exc:
        raise IngestionError(
            "An embedding vector is not a sequence of numbers",
            stage="embedding_validation",
        ) from exc

    if length != EMBEDDING_DIMENSIONS:
        raise IngestionError(
            "An embedding vector does not match the configured "
            f"dimensionality: expected {EMBEDDING_DIMENSIONS}, got {length}",
            stage="embedding_validation",
        )

    validated: list[float] = []

    for position, value in enumerate(vector):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise IngestionError(
                "An embedding vector contains a non-numeric value",
                stage="embedding_validation",
            )

        numeric = float(value)

        if not math.isfinite(numeric):
            raise IngestionError(
                "An embedding vector contains a non-finite value",
                stage="embedding_validation",
            )

        validated.append(numeric)

    return validated


def _aligned_chunks(
    chunks: list[str],
    vectors: list[list[float]],
) -> list[_ChunkWithEmbedding]:
    """Validate the embedding set against the chunk set, fully.

    Checks one embedding per chunk (aggregate count), the 768-value
    shape and content of every vector, and strict pairwise alignment
    so chunk *i* is embedded with chunk *i*'s vector. Returns the
    ``(chunk_index, content, embedding)`` rows ready for insertion,
    in document order.
    """

    if len(vectors) != len(chunks):
        raise IngestionError(
            "The embedding count does not match the chunk count: "
            f"expected {len(chunks)} chunk(s), got {len(vectors)}",
            stage="embedding_validation",
        )

    rows: list[_ChunkWithEmbedding] = []

    for index, (content, vector) in enumerate(zip(chunks, vectors, strict=True)):
        validated = _validated_vector(vector)
        rows.append((index, content, validated))

    return rows


async def _embedded(
    embedding_provider: EmbeddingProvider,
    chunks: list[str],
    document_id: uuid.UUID,
    *,
    batch_size: int,
) -> list[list[float]]:
    """Embed chunks in bounded, strictly sequential batches.

    Each batch is handled by one ``embed_documents`` call executed in
    a worker thread through ``asyncio.to_thread`` — the provider is a
    synchronous, potentially slow client, and each call is awaited to
    completion before the next batch starts. Batches are never issued
    concurrently and no retries or backoff are applied.

    Raises ``IngestionError`` when any batch fails, with the original
    exception chained.
    """

    vectors: list[list[float]] = []

    for start in range(0, len(chunks), batch_size):
        batch = chunks[start:start + batch_size]

        try:
            batch_vectors = await asyncio.to_thread(
                embedding_provider.embed_documents,
                batch,
            )
        except Exception as exc:
            _log_failure("embedding_generation", document_id, exc)
            raise IngestionError(
                "Embedding generation failed for a document",
                stage="embedding_generation",
            ) from exc

        vectors.extend(batch_vectors)

    return vectors


async def ingest_document(
    pool: AsyncConnectionPool,
    storage: Client,
    embedding_provider: EmbeddingProvider,
    *,
    document_id: uuid.UUID,
    storage_path: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
    batch_size: int = EMBEDDING_BATCH_SIZE,
) -> int:
    """Ingest one uploaded document into ``document_chunks``.

    Downloads the PDF the caller uploaded for `document_id` from the
    private ``documents`` Storage bucket at its internal
    `storage_path` (a parameter only — never logged), extracts and
    chunks its text, embeds every chunk through the supplied
    ``EmbeddingProvider`` in bounded sequential batches, validates the
    complete embedding set (one 768-value vector per chunk, aligned
    and finite), and replaces the document's chunk rows inside one
    database transaction.

    All dependencies are explicit parameters, mirroring the existing
    service conventions, so Storage, the provider, and the pool can be
    faked in tests. The document must already exist (the upload route
    creates it with ``status = 'processing'``); this phase does not
    change that status — Phase 5.7 owns status transitions and the
    route integration.

    Returns the number of chunk rows inserted.

    Raises ``IngestionError`` when any stage fails; the original
    exception is always chained for debugging. Raises ``ValueError``
    for an invalid `batch_size`. If the database insert fails, the
    transaction rolls back and no partial chunk set is left behind.
    """

    document_id = uuid.UUID(str(document_id))

    if batch_size < 1:
        raise ValueError(f"batch_size must be at least 1, got {batch_size}")

    # Storage download and extraction/chunking happen before any
    # embedding call: nothing is sent to the provider unless the
    # document is verifiably usable.
    pdf_bytes = await asyncio.to_thread(_download_pdf, storage, storage_path, document_id)
    text = _extracted_text(pdf_bytes, document_id)
    chunks = _chunked(text, document_id, chunk_size=chunk_size, overlap=overlap)
    vectors = await _embedded(
        embedding_provider,
        chunks,
        document_id,
        batch_size=batch_size,
    )
    chunk_rows = _aligned_chunks(chunks, vectors)

    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cursor:
                inserted = await documents_repository.insert_document_chunks(
                    cursor,
                    document_id=document_id,
                    chunks=chunk_rows,
                )

            await conn.commit()
    except Exception as exc:
        _log_failure("database_insertion", document_id, exc)
        raise IngestionError(
            "The document chunks could not be stored",
            stage="database_insertion",
        ) from exc

    logger.info(
        "Document %s ingested: %d chunk(s) stored.",
        document_id,
        inserted,
    )

    return inserted
