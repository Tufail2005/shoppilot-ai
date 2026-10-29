import asyncio
import io
import logging
import uuid
from typing import BinaryIO

import pypdf
from fastapi import HTTPException, UploadFile, status
from psycopg_pool import AsyncConnectionPool
from storage3 import SyncStorageClient

from app.repositories import documents as documents_repository
from app.services.embeddings import EmbeddingProvider
from app.storage import DOCUMENTS_BUCKET


logger = logging.getLogger(__name__)


# Matches the `documents` Storage bucket limit in supabase/config.toml.
MAX_DOCUMENT_SIZE = 20 * 1024 * 1024  # 20 MiB = 20,971,520 bytes

_PDF_SIGNATURE = b"%PDF-"
_READ_CHUNK_SIZE = 64 * 1024
_TITLE_MAX_LENGTH = 200
_DEFAULT_TITLE = "Untitled Document"


class PDFExtractionError(Exception):
    """Raised when text cannot be extracted from a PDF.

    Reported for: input of an unsupported type, PDFs that cannot be
    parsed (corrupt, truncated, empty), password-protected (encrypted)
    PDFs, and valid PDFs that contain no extractable text layer.

    pypdf does not perform OCR. A scanned/image-only PDF is a valid PDF
    without a text layer: it is reported the same as any other textless
    PDF by raising this error, and the absence of extractable text
    cannot in general be attributed to a specific cause.
    """


def extract_text(file: bytes | BinaryIO) -> str:
    """Extract the text of a PDF from all pages, preserving page order.

    Accepts raw PDF bytes or a binary file-like object. Seekable
    streams are reset to the beginning before reading.

    Returns a non-empty extracted text string on success.

    Raises PDFExtractionError when the input type is invalid, the PDF
    cannot be parsed, the PDF is encrypted, or the PDF yields no
    extractable text. pypdf does not perform OCR, so the text layer of
    a scanned/image-only PDF looks exactly like that of any other
    textless PDF and extraction fails instead of returning empty text.

    This function never logs document contents or extracted text.
    """
    if isinstance(file, bytes):
        stream: io.BytesIO | BinaryIO = io.BytesIO(file)
    elif callable(getattr(file, "read", None)) and callable(getattr(file, "seek", None)):
        file.seek(0)
        stream = file
    else:
        raise PDFExtractionError(
            "Expected PDF bytes or a binary file-like object, "
            f"got {type(file).__name__}"
        )

    try:
        reader = pypdf.PdfReader(stream)
    except Exception as exc:
        raise PDFExtractionError("PDF could not be parsed") from exc

    if reader.is_encrypted:
        raise PDFExtractionError(
            "PDF is encrypted and cannot be extracted without a password"
        )

    try:
        page_texts = [page.extract_text() for page in reader.pages]
    except Exception as exc:
        raise PDFExtractionError("PDF text could not be extracted") from exc

    text = "\n".join(page_text for page_text in page_texts if page_text.strip())

    if not text.strip():
        raise PDFExtractionError("PDF contains no extractable text")

    return text


def _derive_title(filename: str | None) -> str:
    """Derive a sanitized document title from the client filename.

    The filename is only ever used as a human-readable title,
    never as the Storage object path.
    """
    if not filename:
        return _DEFAULT_TITLE

    # Keep only the final path component to survive odd client filenames.
    base = filename.replace("\\", "/").split("/")[-1].strip()

    if base.lower().endswith(".pdf"):
        base = base[: -len(".pdf")]

    base = base.strip()

    if not base:
        return _DEFAULT_TITLE

    return base[:_TITLE_MAX_LENGTH]


def _generate_storage_path() -> str:
    # Server-generated, unique, and never derived from client input.
    return f"uploads/{uuid.uuid4()}.pdf"


def _upload_to_storage(
    storage: SyncStorageClient,
    storage_path: str,
    data: bytes,
) -> None:
    try:
        storage.from_(DOCUMENTS_BUCKET).upload(
            path=storage_path,
            file=data,
            file_options={"content-type": "application/pdf"},
        )
    except Exception as exc:
        # Log the stage and exception type only. Storage SDK error
        # messages can contain the internal storage path or bucket URL,
        # which must never appear in logs.
        logger.error(
            "Storage upload failed: %s",
            type(exc).__name__,
        )

        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Document storage is temporarily unavailable",
        ) from exc


def _remove_object_safely(storage: SyncStorageClient, storage_path: str) -> None:
    # Cleanup must never hide the original failure.
    # The storage_path parameter is used only for the remove() call:
    # it is never logged, because internal storage paths must not be
    # exposed through log output. The exception type is logged without
    # its message for the same reason.
    try:
        storage.from_(DOCUMENTS_BUCKET).remove([storage_path])
    except Exception as exc:
        logger.error(
            "Failed to remove Storage object after failed document "
            "registration: %s",
            type(exc).__name__,
        )


async def _read_upload_limited(upload: UploadFile) -> bytes:
    """Read the upload with a bounded strategy and verify PDF content.

    An oversized file is rejected as soon as the limit is exceeded,
    so it is never fully loaded into memory.
    """
    chunks: list[bytes] = []
    size = 0

    while True:
        chunk = await upload.read(_READ_CHUNK_SIZE)

        if not chunk:
            break

        size += len(chunk)

        if size > MAX_DOCUMENT_SIZE:
            await upload.close()

            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="Document exceeds the 20 MiB upload limit",
            )

        chunks.append(chunk)

    data = b"".join(chunks)

    # Verify the actual content, not the declared MIME type.
    if not data.startswith(_PDF_SIGNATURE):
        await upload.close()

        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="File is not a valid PDF document",
        )

    return data


async def upload_document(
    pool: AsyncConnectionPool,
    storage: SyncStorageClient,
    *,
    user_role: str,
    upload: UploadFile,
    visibility: str | None,
    embedding_provider: EmbeddingProvider,
) -> dict:
    # Support agents may only publish internal documents.
    # The safe default is explicit and always overrides the DB default.
    requested_visibility = visibility if visibility is not None else "internal"

    if requested_visibility == "public" and user_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can upload public documents",
        )

    data = await _read_upload_limited(upload)

    storage_path = _generate_storage_path()
    title = _derive_title(upload.filename)

    # Storage and PostgreSQL do not share a transaction.
    # If the insert or commit fails, the uploaded object is removed.
    _upload_to_storage(storage, storage_path, data)

    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cursor:
                document = await documents_repository.create_document(
                    cursor,
                    title=title,
                    storage_path=storage_path,
                    visibility=requested_visibility,
                )

            await conn.commit()
    except Exception as exc:
        # The underlying database error is not logged with its message
        # or traceback: only the stage and exception type. Database
        # error text is uncontrolled and has previously carried query
        # context; document registration failures must not reintroduce
        # internal identifiers into logs.
        logger.error(
            "Failed to register document after Storage upload: %s",
            type(exc).__name__,
        )

        _remove_object_safely(storage, storage_path)

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Document could not be registered",
        ) from exc

    # Phase 5.7: the document exists as `processing`; run the Phase
    # 5.6 ingestion pipeline and persist the terminal status. The
    # registered document (final state) is returned to the caller.
    return await _ingest_registered_document(
        pool,
        storage,
        embedding_provider,
        document=document,
    )


def _remove_object(storage: SyncStorageClient, storage_path: str) -> None:
    """Remove one object from the private documents bucket.

    Synchronous on purpose: ``delete_document`` runs this in a worker
    thread through ``asyncio.to_thread`` so the blocking Storage
    network call never blocks the event loop. The storage_path is used
    only to address the object and is never logged.

    Verified against the real local Supabase Storage service
    (supabase-py 2.31.0): removing an existing object succeeds, and
    removing a nonexistent object also succeeds (empty result) — so a
    repeated removal after an uncertain first attempt is always safe
    to re-attempt. A raised exception does not necessarily mean the
    object is still there: the remote delete may already have been
    applied before the error surfaced (a potentially uncertain
    outcome).
    """
    storage.from_(DOCUMENTS_BUCKET).remove([storage_path])


async def delete_document(
    pool: AsyncConnectionPool,
    storage: SyncStorageClient,
    *,
    document_id: uuid.UUID,
) -> None:
    """Delete one document: its row, its chunks (by cascade), and its Storage object.

    Phase 5.8. Route policy: admin-only (set in the router); the router
    stays thin and this service owns the consistency and error-mapping
    decisions.

    Flow and consistency guarantees
    -------------------------------

    Storage and PostgreSQL share no transaction, so the deletion is
    NOT atomic across the two systems. The observable contract is at
    least as strong as this:

    1. The document row is deleted with ``DELETE ... RETURNING
       storage_path`` on an open database transaction. Zero returning
       rows raise the sanitized 404 *before any Storage call* — an
       unknown document id never touches Storage, because its object
       path is only known from the row. The pending (empty) statement
       is rolled back by the pool connection context manager.
    2. The Storage object is removed (worker thread) while the
       database transaction is still open. If the removal raises, the
       pending row deletion is rolled back and a sanitized 503 is
       raised: the document row, its cascaded chunks, and any
       not-yet-removed object are untouched — *as far as the local
       transaction observes*. The outcome is nevertheless potentially
       uncertain: the remote delete may already have been applied
       before the error surfaced, so a 503 can leave a row whose
       object is already gone (row-without-object). This state is
       documented, never claimed impossible, and self-healing (step
       4).
    3. Only after a removal without exception does the transaction
       commit. A commit failure yields a sanitized 500 while the
       object is already gone — again a possible row-without-object
       state, documented rather than hidden. The database side had
       never reported success.
    4. A repeated deletion of the same document is always safe: a
       row already gone produces the same 404, and removing a
       nonexistent object succeeds on the real Storage API, so any
       stale row (row present, object gone) heals on the next
       deletion request, which then reports 204 with both sides
       agreeing.

    Success signal: a 204 is emitted only after the database
    transaction committed AND the Storage removal did not raise; any
    error response leaves the database side untouched from this
    request's perspective.

    Statuses: ``processing``, ``ready``, and ``failed`` documents are
    equally deletable — no new lifecycle state is introduced. When
    ingestion and deletion overlap on a ``processing`` document, the
    database's foreign-key locking serializes them: either the
    deleting transaction commits first (the ingestion's pending chunk
    insert then fails its FK check and rolls back, and its status
    write becomes the Phase 5.7 conflict path — a sanitized 500 to the
    concurrent uploader), or it commits after ingestion (cascading the
    freshly written chunks away). The database never ends up
    inconsistent.

    Logging: failures log the deletion stage, the document id (an
    opaque internal UUID), and the exception type only — never
    exception messages, storage paths, or internal identifiers. No
    response ever carries a storage path or an internal error message.

    Raises ``HTTPException`` 404/503/500 as documented above; the
    original exception is chained onto each error for debugging.
    """
    document_id = uuid.UUID(str(document_id))

    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cursor:
                storage_path = await documents_repository.delete_document(
                    cursor,
                    document_id=document_id,
                )

            if storage_path is None:
                # Zero rows: the document does not exist. Raising here
                # exits the pool connection with an exception, which
                # rolls back the (no-op) statement — no Storage call
                # happens for a missing document.
                logger.info(
                    "Document %s does not exist; deletion is a no-op.",
                    document_id,
                )

                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Document not found",
                )

            # Removal runs while the deletion transaction is still
            # open (see docstring steps 2-3). asyncio.to_thread keeps
            # the synchronous Storage network call off the event loop.
            try:
                await asyncio.to_thread(
                    _remove_object,
                    storage,
                    storage_path,
                )
            except Exception as exc:
                # The remote removal outcome is uncertain (it may have
                # been applied before the error). Log stage + type
                # only; storage_path is a local parameter that is
                # never logged.
                logger.error(
                    "Document deletion failed at stage storage_removal "
                    "for document %s: %s",
                    document_id,
                    type(exc).__name__,
                )

                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Document storage is temporarily unavailable",
                ) from exc

            await conn.commit()
    except HTTPException:
        raise
    except Exception as exc:
        # Covers database-side failures: statement errors (rolled
        # back on context exit) and commit failures (object possibly
        # already removed — see docstring step 3). Stage + type only.
        logger.error(
            "Document deletion failed at stage database_deletion for "
            "document %s: %s",
            document_id,
            type(exc).__name__,
        )

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Document could not be deleted",
        ) from exc

    logger.info(
        "Document %s deleted with its chunks and Storage object.",
        document_id,
    )


async def update_document_visibility(
    pool: AsyncConnectionPool,
    *,
    document_id: uuid.UUID,
    visibility: str,
) -> dict:
    """Update one document's visibility and return the updated document.

    Phase 5.9. Route policy: admin-only (enforced in the router).
    Flipping `internal -> public` is a content-exposure grant, and the
    documents table has no creator column to scope a support agent's
    update right to their own uploads — mirroring the Phase 5.8
    deletion rationale.

    The service owns the consistency and error-mapping decisions; the
    repository issues one parameterized UPDATE that touches exactly
    `visibility` and `updated_at` (see
    ``documents_repository.set_document_visibility``). Status, failure
    fields, title, version, the storage path, the Storage object, the
    document's chunks, and their embeddings are never touched, and no
    request to this endpoint requires Storage or the embedding
    provider at all.

    Failure sequencing and uncertainty handling:

    1. Statement failure (the UPDATE itself raises) — the pool
       connection context manager rolls the statement back, the
       database state is unchanged, and a sanitized 500 ("Document
       could not be updated") is raised.
    2. Zero returning rows — the document id does not exist; a
       sanitized 404 is raised before any commit, and the pending
       (no-op) statement is rolled back on context exit.
    3. Commit failure — the UPDATE statement succeeded, so the commit
       outcome is UNCERTAIN: the visibility change may already have
       been applied remotely, or not. The failure is reported with a
       sanitized 500 ("Document visibility update could not be
       confirmed") that explicitly does NOT claim the database state
       is unchanged; nothing here ever asserts persistence either way.
       A caller that cannot obtain a confirmed outcome must re-read
       the document's state before retrying.

    Success signal: the returned document is only ever the row whose
    UPDATE committed.

    Logging: failures log the stage (`database_update` for statement
    failures and `database_commit` for commit failures), the document
    id (an opaque internal UUID), and the exception type only — never
    exception messages, storage paths, or internal identifiers. No
    response ever carries a storage path or an internal error message.

    Raises ``HTTPException`` 404/500 as documented above; the original
    exception is chained onto each error for debugging.
    """
    document_id = uuid.UUID(str(document_id))

    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cursor:
                document = await documents_repository.set_document_visibility(
                    cursor,
                    document_id=document_id,
                    visibility=visibility,
                )

            if document is None:
                # Zero rows: the document does not exist. Raising here
                # exits the pool connection with an exception, which
                # rolls back the (no-op) statement.
                logger.info(
                    "Document %s does not exist; visibility update is a no-op.",
                    document_id,
                )

                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Document not found",
                )

            try:
                await conn.commit()
            except Exception as exc:
                # Commit failure: the UPDATE statement succeeded, so
                # the commit outcome is UNCERTAIN — the visibility
                # change may already have been applied. The sanitized
                # error explicitly does NOT claim the database state is
                # unchanged; nothing here asserts persistence either
                # way.
                logger.error(
                    "Document visibility update failed at stage "
                    "database_commit for document %s: %s",
                    document_id,
                    type(exc).__name__,
                )

                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Document visibility update could not be confirmed",
                ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        # Statement-stage database failures: the UPDATE itself raised
        # and was rolled back on context exit, so the database state is
        # unchanged. Stage + exception type only — never messages,
        # storage paths, or internal identifiers.
        logger.error(
            "Document visibility update failed at stage database_update "
            "for document %s: %s",
            document_id,
            type(exc).__name__,
        )

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Document could not be updated",
        ) from exc

    logger.info(
        "Document %s visibility set to %s.",
        document_id,
        document["visibility"],
    )

    return document


def _finalized_failure_category(exc: Exception) -> tuple[str, str]:
    """Map an ingestion failure to sanitized, persistable fields.

    Returns the ``(failure_stage, failure_message)`` pair. For
    ``IngestionError`` (the pipeline's normalized failure type) the
    controlled stage slug and its static message — which never embed
    storage paths, credentials, document text, vector values, or
    uncontrolled exception text — are safe to store verbatim. Any
    other exception is reduced to the literal `unexpected` category
    and a fixed message; original exception text is discarded.
    """

    # Deferred import: the ingestion service imports extract_text
    # from this module, so a module-level import here would be
    # circular. At call time both modules are fully loaded.
    from app.services.ingestion import IngestionError

    if isinstance(exc, IngestionError):
        return exc.stage, str(exc)

    return "unexpected", "Document ingestion failed"


def _document_id(document: dict) -> uuid.UUID:
    # create_document returns a raw uuid.UUID from psycopg.
    identifier = document["id"]
    return identifier if isinstance(identifier, uuid.UUID) else uuid.UUID(str(identifier))


def _log_persistence_failure(
    stage: str,
    document_id: uuid.UUID,
    exc: Exception,
) -> None:
    # Same logging policy as the ingestion service: stage and
    # exception type only — never exception messages or tracebacks,
    # which can carry database error text or internal identifiers.
    logger.error(
        "Document status persistence failed at stage %s for document %s: %s",
        stage,
        document_id,
        type(exc).__name__,
    )


async def _mark_document_ready(
    pool: AsyncConnectionPool,
    document_id: uuid.UUID,
) -> dict:
    # The chunk transaction committed before this point; `ready` is
    # only ever written for a fully ingested document. Guarded repo
    # update: `None` means zero rows transitioned from `processing`
    # (conflict) — reported as a persistence failure, never claimed
    # as persisted.
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cursor:
                document = await documents_repository.mark_document_ready(
                    cursor,
                    document_id=document_id,
                )

            await conn.commit()
    except Exception as exc:
        _log_persistence_failure("mark_ready", document_id, exc)

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Document status could not be finalized",
        ) from exc

    if document is None:
        # The document is no longer `processing`; a newer terminal
        # status won. The current state is unknown from here, so no
        # success can be claimed and no state is overwritten.
        logger.error(
            "Document status update conflict for document %s: "
            "no processing row remaining.",
            document_id,
        )

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Document status could not be finalized",
        )

    logger.info("Document %s is ready.", document_id)

    return document


async def _record_ingestion_failure(
    pool: AsyncConnectionPool,
    document_id: uuid.UUID,
    *,
    stage: str,
    message: str,
) -> dict:
    # Ingestion failed; the failure is recorded on the existing
    # document row. If this persistence fails, the original ingestion
    # failure must not be misreported as success — a sanitized 500 is
    # raised and the document stays `processing` (a documented
    # Phase 5.7 limitation: no background recovery).
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cursor:
                document = await documents_repository.mark_document_failed(
                    cursor,
                    document_id=document_id,
                    stage=stage,
                    message=message,
                )

            await conn.commit()
    except Exception as exc:
        _log_persistence_failure("record_failed", document_id, exc)

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Document ingestion outcome could not be recorded",
        ) from exc

    if document is None:
        logger.error(
            "Document status update conflict for document %s: "
            "no processing row remaining.",
            document_id,
        )

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Document ingestion outcome could not be recorded",
        )

    logger.info(
        "Document %s recorded as failed at stage %s.",
        document_id,
        stage,
    )

    return document


async def _ingest_registered_document(
    pool: AsyncConnectionPool,
    storage: Client,
    embedding_provider: EmbeddingProvider,
    *,
    document: dict,
) -> dict:
    """Ingest one registered document and persist its terminal status.

    Runs the existing Phase 5.6 pipeline unchanged. On success the
    document is finalized as `ready` (only after the chunk
    transaction committed). On any pipeline failure the document is
    recorded as `failed` with sanitized fields; the caller always
    receives the document's actual final state rather than an
    exception, so the API response reports the created-but-failed
    document under the same 201 contract as a successful upload.

    `asyncio.CancelledError` is deliberately not caught: the open
    pool connection rolls the chunk transaction back through its
    context manager, the status update never runs, and the document
    remains `processing` (abrupt-interruption limitation — see the
    Phase 5.7 notes). Swallowing cancellation to attempt a database
    write would risk hanging the shutdown instead.
    """

    document_id = _document_id(document)

    try:
        # Deferred import (see _finalized_failure_category): avoids
        # the circular import with the ingestion service.
        from app.services.ingestion import ingest_document

        await ingest_document(
            pool,
            storage,
            embedding_provider,
            document_id=document_id,
            storage_path=document["storage_path"],
        )
    except Exception as exc:
        stage, message = _finalized_failure_category(exc)

        return await _record_ingestion_failure(
            pool,
            document_id,
            stage=stage,
            message=message,
        )

    return await _mark_document_ready(pool, document_id)
