import asyncio
import io
import logging
import re
import uuid
from typing import BinaryIO

import pypdf
from fastapi import HTTPException, UploadFile, status
from psycopg_pool import AsyncConnectionPool
from storage3 import SyncStorageClient

from app.core.observability import provider_metrics as _provider_metrics
from app.core.provider_concurrency import ProviderLimiters
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


# Bounds of the storage upload failure log's structured attributes.
# Both are derived from untrusted SDK exception objects, so they are
# validated before logging: only a numeric HTTP status and a short,
# strictly-shaped token are allowed through. Anything else — including
# message text, paths, and arbitrary object reprs — is replaced with a
# static placeholder. Values that were never set are logged as None.
_UNSAFE_LOG_PLACEHOLDER = "unsafe"
_LOGGED_STATUS_CODES = range(100, 600)
_LOGGED_CODE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")


def _storage_upload_log_fields(exc: Exception) -> tuple[int | str | None, str | None]:
    """Sanitized ``(status, code)`` log fields for a Storage error.

    The Storage SDK raises heterogeneous exception objects whose
    attributes are never trusted input to the log stream: a numeric
    HTTP status or a short promoted-code token is fine, but anything
    else (message text, URLs, paths, reprs of arbitrary objects) is
    reduced to a static placeholder.
    """

    raw_status = getattr(exc, "status", None)
    logged_status: int | str | None

    # A bare ``None`` (attribute missing, or the SDK's own "not set")
    # is logged as None. ``bool`` is a subclass of ``int``: True/False
    # are not statuses. Strings ("500"), floats, and out-of-range
    # numbers are never statuses either.
    if raw_status is None:
        logged_status = None
    elif isinstance(raw_status, bool) or not isinstance(raw_status, int):
        logged_status = _UNSAFE_LOG_PLACEHOLDER
    elif raw_status in _LOGGED_STATUS_CODES:
        logged_status = raw_status
    else:
        logged_status = _UNSAFE_LOG_PLACEHOLDER

    raw_code = getattr(exc, "code", None)
    if isinstance(raw_code, str) and _LOGGED_CODE_PATTERN.match(raw_code):
        logged_code: str | None = raw_code
    elif raw_code is None:
        logged_code = None
    else:
        logged_code = _UNSAFE_LOG_PLACEHOLDER

    return logged_status, logged_code


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
        # which must never appear in logs. The SDK's structured fields
        # are logged only after strict validation (_storage_upload_log_
        # fields): malformed values become a safe placeholder, and the
        # exception's message text is never logged.
        status_field, code_field = _storage_upload_log_fields(exc)
        logger.error(
            "Storage upload failed: type=%s status=%s code=%s",
            type(exc).__name__,
            status_field,
            code_field,
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

_CLEANUP_TASKS: set[asyncio.Task] = set()


async def _storage_object_is_registered(
    pool: AsyncConnectionPool,
    storage_path: str,
) -> bool:
    """Check whether the Storage object has a committed document record."""
    async with pool.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM public.documents
                    WHERE storage_path = %s
                )
                """,
                (storage_path,),
            )
            row = await cursor.fetchone()

    return bool(row[0])


async def _cleanup_cancelled_upload(
    pool: AsyncConnectionPool,
    storage: SyncStorageClient,
    storage_path: str,
) -> None:
    """Remove an uploaded object only if registration did not commit."""
    try:
        registered = await _storage_object_is_registered(
            pool,
            storage_path,
        )
    except Exception as exc:
        # Commit outcome could be uncertain. Preserve the object rather
        # than risk deleting one referenced by a committed database row.
        logger.error(
            "Cancelled upload cleanup could not verify registration: %s",
            type(exc).__name__,
        )
        return

    if registered:
        logger.info(
            "Storage cleanup skipped: document registration already committed."
        )
        return

    await asyncio.to_thread(
        _remove_object_safely,
        storage,
        storage_path,
    )


def _schedule_cancelled_upload_cleanup(
    pool: AsyncConnectionPool,
    storage: SyncStorageClient,
    storage_path: str,
) -> None:
    """Schedule cleanup without swallowing the original cancellation."""
    task = asyncio.create_task(
        _cleanup_cancelled_upload(
            pool,
            storage,
            storage_path,
        )
    )

    _CLEANUP_TASKS.add(task)
    task.add_done_callback(_CLEANUP_TASKS.discard)


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
    limiters: ProviderLimiters | None = None,
    deadline_at: float | None = None,
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
    # Run the synchronous Storage upload in a worker thread so it
    # does not block the event loop.
    await asyncio.to_thread(
        _upload_to_storage,
        storage,
        storage_path,
        data,
    )

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

    except asyncio.CancelledError:
        logger.warning(
            "Document registration cancelled; scheduling Storage cleanup."
        )

        _schedule_cancelled_upload_cleanup(
            pool,
            storage,
            storage_path,
        )

        raise

    except Exception as exc:
        logger.error(
            "Failed to register document after Storage upload: %s",
            type(exc).__name__,
        )

        await asyncio.to_thread(
            _remove_object_safely,
            storage,
            storage_path,
        )

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
        limiters=limiters,
        deadline_at=deadline_at,
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
    """Delete a document, its chunks, and its storage object.

    Storage and DB writes are separate, so the method keeps a safe
    failure path and returns sanitized 404/503/500 responses.
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
    """Update a document's visibility and return the stored record.

    Route policy is admin-only; the repo updates only visibility and
    the service maps DB failures to sanitized HTTP responses.
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
    """Map ingestion errors to safe persisted stage/message pairs.

    Unknown errors are reduced to a fixed ``unexpected`` outcome so raw
    exception text is never stored.
    """

    # Deferred import: the ingestion service imports extract_text
    # from this module, so a module-level import here would be
    # circular. At call time both modules are fully loaded.
    from app.services.ingestion import IngestionError

    if isinstance(exc, IngestionError):
        return exc.stage, str(exc)

    return "unexpected", "Document ingestion failed"


class _StatusConflictError(Exception):
    """A guarded terminal status write found no ``processing`` row.

    Raised by :func:`_mark_document_ready` and
    :func:`_record_ingestion_failure` when a newer terminal status won.
    The service layer translates it into the SAME sanitized
    ``HTTPException`` those helpers used to raise directly, so the HTTP
    contract is unchanged while the lifecycle metric can distinguish a
    status conflict from a persistence failure.
    """

    def __init__(self, message: str, *, detail: str):
        super().__init__(message)
        self.detail = detail


_STATUS_CONFLICT_DETAIL_READY = "Document status could not be finalized"
_STATUS_CONFLICT_DETAIL_FAILED = (
    "Document ingestion outcome could not be recorded"
)


def _record_document_lifecycle(outcome: str) -> None:
    """Record one document lifecycle outcome; NEVER raise.

    Exactly one terminal outcome per registered-document ingest:
    ``ready``, ``failed``, ``conflict`` (a newer terminal status won),
    ``cancelled`` (recorded exactly once, where the cancellation is
    handled — never by the detached marker), or ``unexpected`` (a
    status-persistence failure). Best-effort: a broken registry can
    never change persistence, cancellation, or HTTP behavior.
    """
    try:
        _provider_metrics.record_counter(
            "document_lifecycle_outcomes_total",
            {"document_lifecycle_outcome": outcome},
        )
    except Exception:  # pragma: no cover - metrics must be invisible
        pass


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

        # Phase 6.8: the conflict raises the internal typed signal; the
        # service layer keeps raising the identical sanitized 500.
        raise _StatusConflictError(
            "no processing row remaining",
            detail=_STATUS_CONFLICT_DETAIL_READY,
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
    # Ingestion or cancellation failed; the outcome is recorded on
    # the existing document row.
    #
    # If this persistence operation fails, the failure must not be
    # misreported as success. A sanitized 500 is raised, and the
    # document may remain `processing`.
    #
    # Cancellation markers are best-effort; there is no guaranteed
    # background recovery mechanism for failed status persistence.
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

        # Phase 6.8: conflict raised as the typed internal signal (same
        # sanitized HTTP response via the service layer).
        raise _StatusConflictError(
            "no processing row remaining",
            detail=_STATUS_CONFLICT_DETAIL_FAILED,
        )

    logger.info(
        "Document %s recorded as failed at stage %s.",
        document_id,
        stage,
    )

    return document


# Controlled, persistable category for an ingestion that was cancelled
# before it could finish (client disconnect or shutdown).
_CANCELLATION_STAGE = "request_cancelled"
_CANCELLATION_MESSAGE = "Document ingestion was interrupted before it finished"

# Strong references to the background cancellation-marker tasks: the
# event loop only keeps weak references to tasks, so an unreferenced
# task could otherwise be garbage collected mid-flight.
_CANCELLATION_TASKS: set[asyncio.Task] = set()


async def _mark_cancelled_document(
    pool: AsyncConnectionPool,
    document_id: uuid.UUID,
) -> None:
    """Best-effort persistence of an interrupted ingestion's outcome.

    Never raises: this runs detached from the cancelled request, and a
    failure to record the marker must not surface anywhere except the
    log (stage + exception type only).
    """
    try:
        await _record_ingestion_failure(
            pool,
            document_id,
            stage=_CANCELLATION_STAGE,
            message=_CANCELLATION_MESSAGE,
        )
    except Exception as exc:
        _log_persistence_failure("record_cancelled", document_id, exc)


def _record_cancellation(
    pool: AsyncConnectionPool,
    document_id: uuid.UUID,
) -> None:
    """Schedule a detached marker for a cancelled ingestion.

    Called from the ``asyncio.CancelledError`` handler, so the
    interruption is recorded without delaying the cancellation itself.
    The task is referenced in :data:`_CANCELLATION_TASKS` until it
    completes.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # The coroutine is never constructed here, so no "coroutine was
        # never awaited" warning can be produced by this path.
        logger.error(
            "Document %s cancellation could not be recorded: no event loop",
            document_id,
        )
        return

    task = loop.create_task(_mark_cancelled_document(pool, document_id))
    _CANCELLATION_TASKS.add(task)
    task.add_done_callback(_CANCELLATION_TASKS.discard)


async def _ingest_registered_document(
    pool: AsyncConnectionPool,
    storage: SyncStorageClient,
    embedding_provider: EmbeddingProvider,
    *,
    document: dict,
    limiters: ProviderLimiters | None = None,
    deadline_at: float | None = None,
) -> dict:
    """Ingest one registered document and persist its terminal status.

    Runs the existing Phase 5.6 ingestion pipeline. The document
    lifecycle is:

        registered (processing)
            -> ingest_document()
                -> extract PDF text
                -> chunk text
                -> embed all batches
                -> persist document chunks transactionally
            -> ready (only after successful chunk persistence)

    Failure handling:
    - An ingestion failure is recorded as `failed` with a sanitized
      stage and message. The document row is returned under the
      existing HTTP 201 contract.
    - If failure-status persistence itself fails, a sanitized HTTP 500
      is raised. The document may remain `processing`; no automatic
      recovery is guaranteed for persistence failures.

    Cancellation handling:
    - asyncio.CancelledError is never converted into a successful
      response or swallowed.
    - Cancellation during ingestion or final status finalization
      schedules a detached, strongly-referenced task that attempts
      to transition the document from `processing` to `failed` with
      the controlled `request_cancelled` stage.
    - The marker is best-effort. Its failure is logged using only
      the document ID, stage, and exception type.
    - Cancellation is re-raised immediately; the request does not
      wait for the detached marker.
    - The guarded status transition prevents the cancellation marker
      from overwriting a document that has already reached a terminal
      state.

    Success is reported only after the chunk transaction and the
    document's `ready` status transition have both completed.
    """

    document_id = _document_id(document)

    try:
        try:
            # Deferred import avoids circular dependency.
            from app.services.ingestion import ingest_document

            await ingest_document(
                pool,
                storage,
                embedding_provider,
                document_id=document_id,
                storage_path=document["storage_path"],
                limiters=limiters,
                deadline_at=deadline_at,
            )

        except asyncio.CancelledError:
            # Cancellation is handled exactly once, in the outer
            # handler below.
            raise

        except Exception as exc:
            stage, message = _finalized_failure_category(exc)

            try:
                recorded = await _record_ingestion_failure(
                    pool,
                    document_id,
                    stage=stage,
                    message=message,
                )
            except _StatusConflictError as conflict:
                # A newer terminal status won: the lifecycle outcome is
                # `conflict`, and the identical sanitized 500 the
                # helper used to raise propagates unchanged.
                _record_document_lifecycle("conflict")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=conflict.detail,
                ) from conflict
            except asyncio.CancelledError:
                raise
            except Exception:
                # The failure could not be persisted: not `failed`,
                # not a conflict — the outcome itself is unknown.
                _record_document_lifecycle("unexpected")
                raise

            _record_document_lifecycle("failed")
            return recorded

        # Finalization is now inside the outer cancellation boundary.
        try:
            finalized = await _mark_document_ready(pool, document_id)
        except _StatusConflictError as conflict:
            _record_document_lifecycle("conflict")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=conflict.detail,
            ) from conflict
        except asyncio.CancelledError:
            # Cancellation during finalization is handled exactly once
            # in the outer handler below.
            raise
        except Exception:
            # Status persistence failed; the document's terminal
            # outcome is neither ready nor visibly failed.
            _record_document_lifecycle("unexpected")
            raise

        _record_document_lifecycle("ready")
        return finalized

    except asyncio.CancelledError:
        _record_cancellation(pool, document_id)
        # Exactly one lifecycle observation for this cancellation; the
        # detached marker task must never record a second one.
        _record_document_lifecycle("cancelled")
        raise
