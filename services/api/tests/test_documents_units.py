"""Unit tests for the Phase 5.2 document upload pure helpers and the
Phase 5.3 PDF text extraction function.

These tests do not require the local Supabase database:
they only exercise app.services.documents helpers directly.
"""

import asyncio
import io
import logging
import re

import pytest
from fastapi import HTTPException
from pypdf import PdfReader, PdfWriter
from starlette.datastructures import Headers, UploadFile

from app.services.documents import (
    MAX_DOCUMENT_SIZE,
    PDFExtractionError,
    _derive_title,
    _generate_storage_path,
    _read_upload_limited,
    _storage_upload_log_fields,
    _upload_to_storage,
    extract_text,
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


# ---------------------------------------------------------------------------
# Phase 5.3: PDF extraction fixture builders
#
# pypdf.PdfWriter cannot draw arbitrary page text, so valid text-bearing
# PDFs are built from raw PDF 1.4 primitives: one Helvetica font, one
# Page per entry, and one content stream using the standard text
# operators (BT/Tf/Td/Tj/ET). The xref table is computed from byte
# offsets, which keeps the fixtures fully deterministic and in-memory.
# ---------------------------------------------------------------------------


def _build_pdf(page_texts: list[str]) -> bytes:
    """Build a valid PDF 1.4 file with one page per entry's text."""
    page_count = len(page_texts)
    page_numbers = [4 + 2 * index for index in range(page_count)]
    kids = " ".join(f"{num} 0 R" for num in page_numbers)

    objects: list[tuple[int, str | bytes]] = [
        (1, "<< /Type /Catalog /Pages 2 0 R >>"),
        (2, f"<< /Type /Pages /Kids [{kids}] /Count {page_count} >>"),
        (3, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"),
    ]

    for index, page_text in enumerate(page_texts):
        page_num = page_numbers[index]
        content_num = page_num + 1
        content = f"BT /F1 12 Tf 72 720 Td ({page_text}) Tj ET"
        objects.append((
            page_num,
            (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                "/Resources << /Font << /F1 3 0 R >> >> "
                f"/Contents {content_num} 0 R >>"
            ),
        ))
        objects.append((
            content_num,
            f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
        ))

    buffer = io.BytesIO()
    buffer.write(b"%PDF-1.4\n")

    offsets: dict[int, int] = {}
    for num, body in objects:
        offsets[num] = buffer.tell()
        payload = body.encode("latin-1") if isinstance(body, str) else body
        buffer.write(f"{num} 0 obj\n".encode("latin-1") + payload + b"\nendobj\n")

    xref_offset = buffer.tell()
    size = len(objects) + 1
    buffer.write(b"xref\n0 " + str(size).encode("latin-1") + b"\n")
    buffer.write(b"0000000000 65535 f \n")
    for num, _ in objects:
        buffer.write(f"{offsets[num]:010d} 00000 n \n".encode("latin-1"))
    buffer.write(
        b"trailer\n<< /Size "
        + str(size).encode("latin-1")
        + b" /Root 1 0 R >>\nstartxref\n"
        + str(xref_offset).encode("latin-1")
        + b"\n%%EOF\n"
    )
    return buffer.getvalue()


def _build_encrypted_pdf() -> bytes:
    """Build a real password-protected PDF in memory."""
    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(_build_pdf(["Secret pages inside"]))))
    writer.encrypt("test-password")
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


class _ReadOnlyStream:
    """Binary input that can be read but not seeked."""

    def read(self, n: int = -1) -> bytes:
        return b""


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


_MARKER_ONE = "Page one marker"
_MARKER_TWO = "Page two marker"
_MARKER_THREE = "Page three marker"


class TestExtractText:
    def test_single_page_pdf_returns_extractable_text(self):
        text = "ShopPilot handbook: internal return policy notes"
        pdf = _build_pdf([text])

        result = extract_text(pdf)

        assert isinstance(result, str)
        assert result.strip()
        assert "handbook" in result
        assert "return policy" in result

    def test_multi_page_pdf_extracts_all_pages(self):
        pdf = _build_pdf([_MARKER_ONE, _MARKER_TWO, _MARKER_THREE])

        result = extract_text(io.BytesIO(pdf))

        assert _MARKER_ONE in result
        assert _MARKER_TWO in result
        assert _MARKER_THREE in result

    def test_multi_page_pdf_preserves_page_order(self):
        pdf = _build_pdf([_MARKER_ONE, _MARKER_TWO, _MARKER_THREE])

        result = extract_text(pdf)

        assert result.index(_MARKER_ONE) < result.index(_MARKER_TWO)
        assert result.index(_MARKER_TWO) < result.index(_MARKER_THREE)

    def test_file_like_stream_position_is_reset(self):
        pdf = _build_pdf([_MARKER_ONE])
        stream = io.BytesIO(pdf)
        stream.read(10)
        stream.seek(-1, io.SEEK_END)

        result = extract_text(stream)

        assert _MARKER_ONE in result

    def test_zero_page_pdf_raises(self):
        pdf = _build_pdf([])

        with pytest.raises(PDFExtractionError) as exc_info:
            extract_text(pdf)

        assert "no extractable text" in str(exc_info.value)

    def test_whitespace_only_pdf_raises(self):
        pdf = _build_pdf(["   "])

        with pytest.raises(PDFExtractionError) as exc_info:
            extract_text(pdf)

        assert "no extractable text" in str(exc_info.value)

    def test_empty_bytes_raises_with_chained_cause(self):
        with pytest.raises(PDFExtractionError) as exc_info:
            extract_text(b"")

        assert "could not be parsed" in str(exc_info.value)
        assert exc_info.value.__cause__ is not None

    def test_garbage_bytes_raises_with_chained_cause(self):
        with pytest.raises(PDFExtractionError) as exc_info:
            extract_text(b"this is plain text, definitely not a pdf")

        assert "could not be parsed" in str(exc_info.value)
        assert exc_info.value.__cause__ is not None

    def test_truncated_pdf_raises_with_chained_cause(self):
        with pytest.raises(PDFExtractionError) as exc_info:
            extract_text(b"%PDF-1.4\n1 0 obj")

        assert "could not be parsed" in str(exc_info.value)
        assert exc_info.value.__cause__ is not None

    def test_encrypted_pdf_raises(self):
        pdf = _build_encrypted_pdf()

        with pytest.raises(PDFExtractionError) as exc_info:
            extract_text(pdf)

        assert "encrypted" in str(exc_info.value)

    def test_invalid_input_types_raise(self):
        invalid_inputs = [None, "not pdf", 123, object()]

        for invalid_input in invalid_inputs:
            with pytest.raises(PDFExtractionError) as exc_info:
                extract_text(invalid_input)  # type: ignore[arg-type]

            assert type(invalid_input).__name__ in str(exc_info.value)

    def test_unseekable_file_like_raises(self):
        with pytest.raises(PDFExtractionError) as exc_info:
            extract_text(_ReadOnlyStream())  # type: ignore[arg-type]

        assert "binary file-like" in str(exc_info.value)


class _FakeStorageBucket:
    """``storage.from_(bucket)``-alike whose upload always fails."""

    def __init__(self, exc: Exception):
        self._exc = exc
        self.upload_calls = 0

    def upload(self, *, path, file, file_options):
        self.upload_calls += 1
        raise self._exc


class _FakeStorage:
    def __init__(self, exc: Exception):
        self.bucket = _FakeStorageBucket(exc)

    def from_(self, bucket):
        return self.bucket


class TestStorageUploadLogFields:
    """The ``status``/``code`` log fields are hostile untrusted input.

    They come from third-party SDK exception objects, so only a
    numeric HTTP status and a strictly-shaped short token are allowed
    through; everything else becomes a static placeholder, and message
    text, paths, URLs, and reprs never reach the log stream.
    """

    @pytest.mark.parametrize(
        ("status_value", "expected"),
        [
            (None, None),  # attribute missing
            (200, 200),  # a fine numeric status
            (599, 599),  # furthest still-in-range status
            (True, "unsafe"),  # bool is not a status
            ("500", "unsafe"),  # numeric string is not a status
            (50.5, "unsafe"),  # float is not a status
            (99, "unsafe"),  # below the HTTP range
            (600, "unsafe"),  # above the HTTP range
            (-1, "unsafe"),  # negative
        ],
    )
    def test_status_bounds(self, status_value, expected):
        class _Exc(Exception):
            status = status_value

        assert _storage_upload_log_fields(_Exc())[0] == expected

    @pytest.mark.parametrize(
        ("code_value", "expected"),
        [
            (None, None),  # attribute missing
            ("25002", "25002"),  # a plain promoted code
            ("PGRST-da.tch-01", "PGRST-da.tch-01"),  # allowed punctuation
            ("a" * 32, "a" * 32),  # exactly at the length limit
            ("a" * 33, "unsafe"),  # over the length limit
            ("", "unsafe"),  # empty (no leading safe character)
            (" has space", "unsafe"),  # leading space
            ("evil;drop", "unsafe"),  # semicolon
            ("../etc/passwd", "unsafe"),  # path traversal
            ("newline\nkey", "unsafe"),  # log-injection newline
            ("日本語", "unsafe"),  # non-ASCII
            (25002, "unsafe"),  # non-string
            (b"25002", "unsafe"),  # bytes
        ],
    )
    def test_code_bounds(self, code_value, expected):
        class _Exc(Exception):
            code = code_value

        assert _storage_upload_log_fields(_Exc())[1] == expected

    def test_bare_exception_logs_missing_fields_as_none(self):
        assert _storage_upload_log_fields(RuntimeError("boom")) == (None, None)

    def test_hostile_attributes_never_reach_the_log_stream(self, caplog):
        # Everything hostile at once: message text carrying a storage
        # path and a URL, a status that is actually a path, and a code
        # that is a log-injection attempt repr'ing an arbitrary object.
        hostile_message = (
            "upload failed for uploads/secret-path.pdf "
            "at https://internal.example.com bucket"
        )

        class _HostileError(Exception):
            def __init__(self):
                super().__init__(hostile_message)
                self.status = "uploads/secret-path.pdf"
                self.code = dict({"secret": "key"})  # arbitrary object

        storage = _FakeStorage(_HostileError())

        with caplog.at_level(logging.ERROR, logger="app.services.documents"):
            with pytest.raises(HTTPException) as exc_info:
                _upload_to_storage(
                    storage, "uploads/hostile.pdf", b"%PDF-1.4 fake"
                )

        # The sanitized HTTP response is preserved exactly.
        assert exc_info.value.status_code == 503
        assert exc_info.value.detail == (
            "Document storage is temporarily unavailable"
        )

        record = next(
            record
            for record in caplog.records
            if "Storage upload failed" in record.getMessage()
        )
        rendered = record.getMessage()

        assert record.levelno == logging.ERROR
        assert "type=_HostileError" in rendered
        # Placeholders replaced every malformed attribute; nothing
        # hostile ever reached the log stream.
        assert "status=unsafe" in rendered
        assert "code=unsafe" in rendered
        assert hostile_message not in rendered
        assert "uploads/secret-path.pdf" not in rendered
        assert "internal.example.com" not in rendered
        assert "secret" not in rendered
        assert "key" not in rendered
        assert repr({"secret": "key"}) not in rendered

    def test_well_formed_sdk_fields_are_logged_verbatim(self, caplog):
        class _SDKLikeError(Exception):
            status = 413
            code = "payload_too_large"

        storage = _FakeStorage(_SDKLikeError())

        with caplog.at_level(logging.ERROR, logger="app.services.documents"):
            with pytest.raises(HTTPException):
                _upload_to_storage(
                    storage, "uploads/sdk-like.pdf", b"%PDF-1.4 fake"
                )

        record = next(
            record
            for record in caplog.records
            if "Storage upload failed" in record.getMessage()
        )
        # Committed behavioral contract: type first, then the validated
        # structured fields with the upload's own log format.
        assert (
            record.getMessage()
            == "Storage upload failed: type=_SDKLikeError "
            "status=413 code=payload_too_large"
        )
