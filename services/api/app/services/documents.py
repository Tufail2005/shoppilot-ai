import logging
import uuid

from fastapi import HTTPException, UploadFile, status
from psycopg_pool import AsyncConnectionPool
from supabase import Client

from app.repositories import documents as documents_repository


logger = logging.getLogger(__name__)


# Matches the `documents` Storage bucket limit in supabase/config.toml.
MAX_DOCUMENT_SIZE = 20 * 1024 * 1024  # 20 MiB = 20,971,520 bytes

_PDF_SIGNATURE = b"%PDF-"
_STORAGE_BUCKET = "documents"
_READ_CHUNK_SIZE = 64 * 1024
_TITLE_MAX_LENGTH = 200
_DEFAULT_TITLE = "Untitled Document"


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
    storage: Client,
    storage_path: str,
    data: bytes,
) -> None:
    try:
        storage.from_(_STORAGE_BUCKET).upload(
            path=storage_path,
            file=data,
            file_options={"content-type": "application/pdf"},
        )
    except Exception as exc:
        logger.error(
            "Storage upload failed for %s: %s: %s",
            storage_path,
            type(exc).__name__,
            exc,
        )

        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Document storage is temporarily unavailable",
        ) from exc


def _remove_object_safely(storage: Client, storage_path: str) -> None:
    # Cleanup must never hide the original failure.
    try:
        storage.from_(_STORAGE_BUCKET).remove([storage_path])
    except Exception:
        logger.exception(
            "Failed to remove Storage object %s "
            "after failed document registration",
            storage_path,
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
    storage: Client,
    *,
    user_role: str,
    upload: UploadFile,
    visibility: str | None,
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
        logger.exception(
            "Failed to register document %s after Storage upload",
            storage_path,
        )

        _remove_object_safely(storage, storage_path)

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Document could not be registered",
        ) from exc

    return document
