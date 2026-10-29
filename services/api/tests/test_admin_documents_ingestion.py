"""Phase 5.7 endpoint tests: the document status lifecycle.

These tests exercise ``POST /admin/documents`` end to end against the
real local Supabase database — the same real connection pool the
running service uses — while Storage and the embedding provider stay
faked (``tests/ingestion_fakes.py`` and the fake bucket in
``tests/test_admin_documents.py``). No Gemini or Storage network
access ever happens.

Contract under test (verified against the codebase before this
phase): no frontend or other backend consumer reads this endpoint's
response or the document ``status`` today (``apps/web/lib/api.ts``
only covers /health and /products), so the decided contract is safe:

- Successful ingestion → 201 with the document in its final state
  (``status == "ready"``).
- Ingestion failure recorded by the service → still 201 (the row was
  created) with ``status == "failed"`` plus sanitized
  ``failure_stage`` / ``failure_message``. A 201 must never be read
  as "successfully indexed": the response body's ``status`` field is
  the only success signal, and these tests pin the distinction.
- A failure to PERSIST an outcome is reported as a sanitized 500;
  the document stays ``processing`` and neither ``ready`` nor
  ``failed`` is ever claimed as persisted.
"""

import logging
import os
import uuid
from types import SimpleNamespace

import psycopg
import pytest
from fastapi.testclient import TestClient
from starlette import status

from app.main import app
from app.repositories import documents as documents_repository
from app.services import embeddings as embeddings_module
from app.services.chunking import chunk_text
from app.services.documents import extract_text
from app.services.embeddings import get_embedding_provider
from app.storage import get_storage
from tests.ingestion_fakes import FakeEmbeddingProvider
from tests.test_admin_documents import (
    FakeStorageClient,
    FakeStorageState,
    _build_pdf,
    _count_chunks,
    _delete_by_state,
    _delete_document,
    _document_id_by_storage_path,
    _fetch_document,
    _install_fake_storage,
)


_INGESTABLE_PDF = _build_pdf(["minimal return policy page one"])
_EXPECTED_CHUNKS = chunk_text(extract_text(_INGESTABLE_PDF))

# Passes the %PDF- signature check but is not a parseable PDF.
_CORRUPT_PDF = b"%PDF-1.4 this signature is valid but the body is not a pdf"

_DOCUMENT_URL = "/admin/documents"


def _document_id_from_state(state: FakeStorageState) -> str | None:
    """The created row's id via its unique Storage object path.

    Sanitized 50x responses intentionally carry no document id, so
    cleanup and DB assertions resolve the row through the path the
    fake Storage recorded.
    """
    if not state.uploads:
        return None

    return _document_id_by_storage_path(state.uploads[0]["path"])


def _install_fakes(
    state: FakeStorageState,
    provider: FakeEmbeddingProvider | None = None,
) -> FakeEmbeddingProvider:
    """Install both request fakes and return the provider in use."""

    return _install_fake_storage(state, provider=provider)


class TestSuccessfulIngestionReady:
    def test_success_returns_ready_with_chunks_and_no_failure_fields(
        self,
        admin_client: TestClient,
    ):
        state = FakeStorageState()
        _install_fakes(state)

        response = admin_client.post(
            _DOCUMENT_URL,
            files={"file": ("doc.pdf", _INGESTABLE_PDF, "application/pdf")},
        )

        assert response.status_code == status.HTTP_201_CREATED

        document = response.json()
        assert document["status"] == "ready"
        assert document["failure_stage"] is None
        assert document["failure_message"] is None

        row = _fetch_document(document["id"])
        assert row["status"] == "ready"
        assert row["failure_stage"] is None
        assert row["failure_message"] is None

        # The chunk transaction really committed: the expected chunks
        # (from the real 5.3/5.4 building blocks) exist in the table.
        with psycopg.Connection.connect(
            os.environ["DATABASE_URL"],
            connect_timeout=5,
        ) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT chunk_index, content
                    FROM public.document_chunks
                    WHERE document_id = %s
                    ORDER BY chunk_index
                    """,
                    (document["id"],),
                )
                rows = cursor.fetchall()

        assert [row[0] for row in rows] == list(range(len(_EXPECTED_CHUNKS)))
        assert [row[1] for row in rows] == _EXPECTED_CHUNKS

        _delete_document(document["id"])


class TestIngestionFailureStages:
    """Each major pipeline stage records a sanitized failed status.

    No matter where the pipeline fails — including the database
    insertion stage — the final status is `failed` with the controlled
    stage slug, the failure fields are persisted on the row and match
    the response, and the chunk table holds zero rows for the
    document (the Phase 5.6 transaction rolled back).
    """

    @pytest.mark.parametrize(
        ("failure_setup", "expected_stage"),
        [
            ("storage_download", "storage_download"),
            ("text_extraction", "text_extraction"),
            ("embedding_generation", "embedding_generation"),
            ("embedding_validation", "embedding_validation"),
            ("database_insertion", "database_insertion"),
        ],
    )
    def test_failure_records_failed_status_with_sanitized_fields(
        self,
        admin_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        failure_setup: str,
        expected_stage: str,
    ):
        pdf = _INGESTABLE_PDF

        if failure_setup == "text_extraction":
            # Same %PDF- acceptance as the upload validator, but no
            # parseable text layer → extraction failure.
            pdf = _CORRUPT_PDF

        if failure_setup == "database_insertion":
            async def failing_insert(cursor, *, document_id, chunks):
                raise RuntimeError("simulated chunk insertion failure")

            monkeypatch.setattr(
                documents_repository,
                "insert_document_chunks",
                failing_insert,
            )

        state = FakeStorageState(
            fail_download=failure_setup == "storage_download"
        )
        provider = FakeEmbeddingProvider(
            fail_batches={0} if failure_setup == "embedding_generation" else (),
            wrong_dim_batch=0 if failure_setup == "embedding_validation" else None,
        )
        _install_fakes(state, provider)

        response = admin_client.post(
            _DOCUMENT_URL,
            files={"file": ("doc.pdf", pdf, "application/pdf")},
        )

        document = response.json()
        try:
            assert response.status_code == status.HTTP_201_CREATED

            assert document["status"] == "failed"
            assert document["failure_stage"] == expected_stage
            # Non-empty and sanitized: never empty, and never the
            # chained original exception text ("simulated ...").
            assert document["failure_message"]
            assert "simulated" not in document["failure_message"].lower()

            row = _fetch_document(document["id"])
            assert row is not None
            assert row["status"] == "failed"
            assert row["failure_stage"] == expected_stage
            assert row["failure_message"] == document["failure_message"]

            assert _count_chunks(document["id"]) == 0
        finally:
            if document.get("id"):
                _delete_document(document["id"])

    def test_corrupt_pdf_records_text_extraction_failure(
        self,
        admin_client: TestClient,
    ):
        state = FakeStorageState()
        _install_fakes(state)

        response = admin_client.post(
            _DOCUMENT_URL,
            files={"file": ("doc.pdf", _CORRUPT_PDF, "application/pdf")},
        )

        document = response.json()
        try:
            assert response.status_code == status.HTTP_201_CREATED
            assert document["status"] == "failed"
            assert document["failure_stage"] == "text_extraction"
            assert _count_chunks(document["id"]) == 0
        finally:
            _delete_document(document["id"])


class TestRecordedFailureResponseContract:
    def test_failed_response_has_document_out_shape_and_no_storage_path(
        self,
        admin_client: TestClient,
    ):
        state = FakeStorageState(fail_download=True)
        _install_fakes(state)

        response = admin_client.post(
            _DOCUMENT_URL,
            files={"file": ("doc.pdf", _INGESTABLE_PDF, "application/pdf")},
        )

        document = response.json()
        try:
            assert response.status_code == status.HTTP_201_CREATED

            # Exactly the DocumentOut fields, in the documented shape.
            assert set(document) == {
                "id",
                "title",
                "status",
                "visibility",
                "version",
                "failure_stage",
                "failure_message",
                "created_at",
            }

            uuid.UUID(document["id"])  # parseable
            assert document["status"] == "failed"

            # The internal storage path never appears in the body.
            assert "uploads/" not in response.text
            assert state.uploads[0]["path"] not in response.text
        finally:
            _delete_document(document["id"])


class TestStatusPersistenceFailures:
    def test_ready_write_failure_returns_500_and_keeps_processing(
        self,
        admin_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ):
        async def failing_mark_ready(cursor, *, document_id):
            raise RuntimeError("simulated status write failure")

        monkeypatch.setattr(
            documents_repository,
            "mark_document_ready",
            failing_mark_ready,
        )

        state = FakeStorageState()
        _install_fakes(state)

        response = admin_client.post(
            _DOCUMENT_URL,
            files={"file": ("doc.pdf", _INGESTABLE_PDF, "application/pdf")},
        )

        document_id = None
        try:
            # The chunk transaction had already committed, so chunks
            # exist on a still-`processing` document — `ready` was
            # never claimed as persisted.
            assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
            assert response.json() == {
                "detail": "Document status could not be finalized"
            }

            document_id = _document_id_from_state(state)
            assert document_id is not None

            row = _fetch_document(document_id)
            assert row["status"] == "processing"
            assert row["failure_stage"] is None
            assert row["failure_message"] is None
            assert _count_chunks(document_id) == len(_EXPECTED_CHUNKS)
            assert "simulated" not in response.text
        finally:
            if document_id:
                _delete_document(document_id)

    def test_ready_conflict_returns_500_and_keeps_processing(
        self,
        admin_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ):
        async def conflicting_mark_ready(cursor, *, document_id):
            return None  # zero rows transitioned from processing

        monkeypatch.setattr(
            documents_repository,
            "mark_document_ready",
            conflicting_mark_ready,
        )

        state = FakeStorageState()
        _install_fakes(state)

        response = admin_client.post(
            _DOCUMENT_URL,
            files={"file": ("doc.pdf", _INGESTABLE_PDF, "application/pdf")},
        )

        document_id = None
        try:
            assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
            assert response.json() == {
                "detail": "Document status could not be finalized"
            }

            document_id = _document_id_from_state(state)
            assert document_id is not None

            row = _fetch_document(document_id)
            assert row["status"] == "processing"
        finally:
            if document_id:
                _delete_document(document_id)

    def test_failed_write_failure_returns_500_and_keeps_processing(
        self,
        admin_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ):
        async def failing_insert(cursor, *, document_id, chunks):
            raise RuntimeError("simulated chunk insertion failure")

        async def failing_mark_failed(cursor, *, document_id, stage, message):
            raise RuntimeError("simulated status write failure")

        monkeypatch.setattr(
            documents_repository,
            "insert_document_chunks",
            failing_insert,
        )
        monkeypatch.setattr(
            documents_repository,
            "mark_document_failed",
            failing_mark_failed,
        )

        state = FakeStorageState()
        _install_fakes(state)

        response = admin_client.post(
            _DOCUMENT_URL,
            files={"file": ("doc.pdf", _INGESTABLE_PDF, "application/pdf")},
        )

        document_id = None
        try:
            assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
            assert response.json() == {
                "detail": "Document ingestion outcome could not be recorded"
            }

            document_id = _document_id_from_state(state)
            assert document_id is not None

            row = _fetch_document(document_id)
            assert row["status"] == "processing"
            assert row["failure_stage"] is None
            assert row["failure_message"] is None
            assert _count_chunks(document_id) == 0
            assert "simulated" not in response.text
        finally:
            if document_id:
                _delete_document(document_id)

    def test_failed_conflict_returns_500_and_keeps_processing(
        self,
        admin_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ):
        async def failing_insert(cursor, *, document_id, chunks):
            raise RuntimeError("simulated chunk insertion failure")

        async def conflicting_mark_failed(cursor, *, document_id, stage, message):
            return None

        monkeypatch.setattr(
            documents_repository,
            "insert_document_chunks",
            failing_insert,
        )
        monkeypatch.setattr(
            documents_repository,
            "mark_document_failed",
            conflicting_mark_failed,
        )

        state = FakeStorageState()
        _install_fakes(state)

        response = admin_client.post(
            _DOCUMENT_URL,
            files={"file": ("doc.pdf", _INGESTABLE_PDF, "application/pdf")},
        )

        document_id = None
        try:
            assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
            assert response.json() == {
                "detail": "Document ingestion outcome could not be recorded"
            }

            document_id = _document_id_from_state(state)
            assert document_id is not None

            row = _fetch_document(document_id)
            assert row["status"] == "processing"
        finally:
            if document_id:
                _delete_document(document_id)


class TestEmbeddingProviderConfiguration:
    def test_missing_gemini_api_key_fails_fast_with_no_document(
        self,
        admin_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """The provider is resolved BEFORE any upload or row creation.

        With no ``GEMINI_API_KEY``, the real dependency reports a
        sanitized 503, so the request can never orphan a Storage
        upload or a doomed ``processing`` row.
        """
        state = FakeStorageState()
        app.dependency_overrides[get_storage] = lambda: FakeStorageClient(state)

        # Use the REAL provider dependency for this request: point the
        # settings loader the factory reads at a configuration with no
        # key, then let the actual dependency resolve the provider.
        monkeypatch.setattr(
            embeddings_module,
            "get_settings",
            lambda: SimpleNamespace(gemini_api_key=None),
        )
        app.dependency_overrides.pop(get_embedding_provider, None)

        try:
            response = admin_client.post(
                _DOCUMENT_URL,
                files={"file": ("doc.pdf", _INGESTABLE_PDF, "application/pdf")},
            )
        finally:
            app.dependency_overrides.pop(get_embedding_provider, None)

        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert response.json() == {
            "detail": "Document ingestion is not configured",
        }

        # No orphaned upload and therefore no document row at all.
        assert state.uploads == []


class TestSanitizedLogsAndResponses:
    def _assert_hygiene(
        self,
        caplog: pytest.LogCaptureFixture,
        state: FakeStorageState,
        response,
    ):
        internal_path = state.uploads[0]["path"]

        # Only application log records are asserted: the test client
        # (httpx) logs its own request line, which is not part of the
        # service's logging policy.
        messages = "\n".join(
            record.getMessage()
            for record in caplog.records
            if record.name.startswith("app.")
        )

        # Neither the storage path / URL, nor chained fake error
        # text, nor PDF/chunk content leaks into logs or responses.
        assert internal_path not in response.text
        assert internal_path not in messages
        assert "uploads/" not in messages
        assert "http" not in messages.lower()
        assert "simulated" not in response.text
        assert "simulated" not in messages
        assert "minimal" not in response.text
        assert "return policy" not in response.text
        # Logs identify stages and exception types, not messages
        # (exception type names are allowed and expected).
        assert "RuntimeError" in messages

    def test_ingestion_failure_logs_and_responses_are_sanitized(
        self,
        admin_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ):
        async def failing_insert(cursor, *, document_id, chunks):
            raise RuntimeError("simulated chunk insertion failure")

        monkeypatch.setattr(
            documents_repository,
            "insert_document_chunks",
            failing_insert,
        )

        caplog.set_level(logging.DEBUG)

        state = FakeStorageState()
        _install_fakes(state)

        response = admin_client.post(
            _DOCUMENT_URL,
            files={"file": ("doc.pdf", _INGESTABLE_PDF, "application/pdf")},
        )

        document = response.json()
        try:
            assert response.status_code == status.HTTP_201_CREATED
            assert document["status"] == "failed"

            self._assert_hygiene(caplog, state, response)
        finally:
            _delete_document(document["id"])

    def test_status_write_failure_logs_and_responses_are_sanitized(
        self,
        admin_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ):
        async def failing_mark_ready(cursor, *, document_id):
            raise RuntimeError("simulated status write failure")

        monkeypatch.setattr(
            documents_repository,
            "mark_document_ready",
            failing_mark_ready,
        )

        caplog.set_level(logging.DEBUG)

        state = FakeStorageState()
        _install_fakes(state)

        response = admin_client.post(
            _DOCUMENT_URL,
            files={"file": ("doc.pdf", _INGESTABLE_PDF, "application/pdf")},
        )

        try:
            assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR

            self._assert_hygiene(caplog, state, response)
        finally:
            _delete_by_state(state)
