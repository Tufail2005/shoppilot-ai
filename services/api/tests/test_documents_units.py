"""Unit tests for the Phase 5.2 document upload pure helpers.

These tests do not require the local Supabase database:
they only exercise app.services.documents helpers directly.
"""

import asyncio
import io
import re

import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers, UploadFile

from app.services.documents import (
    MAX_DOCUMENT_SIZE,
    _derive_title,
    _generate_storage_path,
    _read_upload_limited,
)


_STORAGE_PATH_PATTERN = re.compile(
    r"^uploads/"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"\.pdf$"
)


def _make_upload(data: bytes, filename: str = "doc.pdf") -> UploadFile:
    return UploadFile(
        file=io.BytesIO(data),
        filename=filename,
        headers=Headers({"content-type": "application/pdf"}),
    )


class TestDeriveTitle:
    def test_strips_pdf_extension(self):
        assert _derive_title("Ops Handbook.pdf") == "Ops Handbook"

    def test_strips_pdf_extension_case_insensitive(self):
        assert _derive_title("RETURN POLICY.PDF") == "RETURN POLICY"

    def test_keeps_filename_without_extension(self):
        assert _derive_title("no-extension-doc") == "no-extension-doc"

    def test_keeps_inner_dots(self):
        assert _derive_title("v1.2 policy draft.pdf") == "v1.2 policy draft"

    def test_uses_only_final_path_component(self):
        assert _derive_title("/etc/confidential/../secret.pdf") == "secret"
        assert _derive_title("C:\\folder\\handbook.pdf") == "handbook"

    def test_strips_surrounding_whitespace(self):
        assert _derive_title("  spaced name.pdf  ") == "spaced name"

    def test_fallback_for_none(self):
        assert _derive_title(None) == "Untitled Document"

    def test_fallback_for_whitespace_only(self):
        assert _derive_title("   .pdf") == "Untitled Document"

    def test_fallback_for_empty(self):
        assert _derive_title("") == "Untitled Document"

    def test_truncates_very_long_filename(self):
        long_name = "x" * 500 + ".pdf"

        title = _derive_title(long_name)

        assert len(title) == 200
        assert _derive_title("x" * 500) == "x" * 200


class TestGenerateStoragePath:
    def test_path_matches_server_generated_pattern(self):
        path = _generate_storage_path()

        assert path.startswith("uploads/")
        assert path.endswith(".pdf")
        assert _STORAGE_PATH_PATTERN.match(path)

    def test_paths_are_unique(self):
        paths = {_generate_storage_path() for _ in range(100)}

        assert len(paths) == 100


class TestReadUploadLimited:
    def test_downloads_valid_pdf(self):
        data = b"%PDF-1.4\ncontent"
        upload = _make_upload(data)

        result = asyncio.run(_read_upload_limited(upload))

        assert result == data

    def test_rejects_non_pdf_content(self):
        upload = _make_upload(b"this is plain text, not a pdf")

        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(_read_upload_limited(upload))

        assert exc_info.value.status_code == 415

    def test_rejects_empty_file(self):
        upload = _make_upload(b"")

        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(_read_upload_limited(upload))

        assert exc_info.value.status_code == 415

    def test_accepts_file_at_exact_size_limit(self):
        data = b"%PDF-" + b"x" * (MAX_DOCUMENT_SIZE - 5)
        assert len(data) == MAX_DOCUMENT_SIZE

        upload = _make_upload(data)

        result = asyncio.run(_read_upload_limited(upload))

        assert result == data

    def test_rejects_file_one_byte_over_limit(self):
        data = b"%PDF-" + b"x" * (MAX_DOCUMENT_SIZE - 5 + 1)
        assert len(data) == MAX_DOCUMENT_SIZE + 1

        upload = _make_upload(data)

        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(_read_upload_limited(upload))

        assert exc_info.value.status_code == 413

    def test_rejects_pdf_signature_after_first_bytes(self):
        # Signature check is on the actual content, not the name.
        upload = _make_upload(b"not-a-pdf %PDF- later", filename="fake.pdf")

        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(_read_upload_limited(upload))

        assert exc_info.value.status_code == 415
