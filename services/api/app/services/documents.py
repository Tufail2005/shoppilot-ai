import io
import logging
import uuid
from typing import BinaryIO

import pypdf
from fastapi import HTTPException, UploadFile, status
from psycopg_pool import AsyncConnectionPool
from supabase import Client

from app.repositories import documents as documents_repository
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
    storage: Client,
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


def _remove_object_safely(storage: Client, storage_path: str) -> None:
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

    return document
