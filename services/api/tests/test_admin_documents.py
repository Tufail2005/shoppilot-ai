import logging
import os
import re

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.repositories import documents as documents_repository
from app.services.embeddings import get_embedding_provider
from app.storage import get_storage
from tests.ingestion_fakes import FakeEmbeddingProvider
from tests.test_documents_units import _build_pdf


# 20 MiB = 20,971,520 bytes
MAX_DOCUMENT_SIZE = 20 * 1024 * 1024

# The upload route now runs the real ingestion pipeline, so success
# paths need an actually parseable, text-bearing PDF (built in-memory
# by the Phase 5.3 fixture builder).
_INGESTABLE_PDF = _build_pdf(["minimal return policy page one"])

_VALID_PDF = b"%PDF-1.4\nminimal test document content"
_STORAGE_PATH_PATTERN = re.compile(
    r"^uploads/"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"\.pdf$"
)


class FakeStorageBucket:
    def __init__(self, state: "FakeStorageState"):
        self._state = state

    def upload(
        self,
        path: str,
        file: bytes,
        file_options: dict,
    ) -> None:
        if self._state.fail_upload:
            raise RuntimeError("simulated storage outage")

        self._state.uploads.append(
            {
                "path": path,
                "data": file,
                "content_type": file_options.get("content-type"),
            }
        )

    def download(self, path: str) -> bytes:
        """Serve the ingestion pipeline's download like the real bucket."""

        self._state.downloads.append(path)

        if self._state.fail_download:
            raise RuntimeError("simulated storage outage")

        for upload in self._state.uploads:
            if upload["path"] == path:
                return upload["data"]

        raise RuntimeError("simulated missing Storage object")

    def remove(self, paths: list[str]) -> None:
        self._state.removes.extend(paths)


class FakeStorageState:
    def __init__(
        self,
        *,
        fail_upload: bool = False,
        fail_download: bool = False,
    ):
        self.fail_upload = fail_upload
        self.fail_download = fail_download
        self.uploads: list[dict] = []
        self.removes: list[str] = []
        self.downloads: list[str] = []


class FakeStorageClient:
    def __init__(self, state: FakeStorageState):
        self._state = state

    def from_(self, bucket: str) -> FakeStorageBucket:
        assert bucket == "documents"
        return FakeStorageBucket(self._state)


def _install_fake_storage(
    state: FakeStorageState,
    *,
    provider: FakeEmbeddingProvider | None = None,
) -> FakeEmbeddingProvider:
    """Override Storage and the embedding provider for the request.

    The upload route resolves the embedding provider BEFORE the upload
    runs, so every request exercise must inject a fake — the real
    dependency would construct a live Gemini client from GEMINI_API_KEY
    when configured, and tests must never reach the network.
    """
    from app.main import app

    fake_provider = provider if provider is not None else FakeEmbeddingProvider()

    app.dependency_overrides[get_storage] = lambda: FakeStorageClient(state)
    app.dependency_overrides[get_embedding_provider] = lambda: fake_provider

    return fake_provider


_DATABASE_URL = os.environ.get("DATABASE_URL", "")


def _fetch_document(doc_id: str) -> dict | None:
    with psycopg.Connection.connect(
        _DATABASE_URL,
        connect_timeout=5,
    ) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT title, visibility, status, storage_path, version,
                       failure_stage, failure_message
                FROM public.documents
                WHERE id = %s
                """,
                (doc_id,),
            )

            row = cursor.fetchone()

    if row is None:
        return None

    return {
        "title": row[0],
        "visibility": row[1],
        "status": row[2],
        "storage_path": row[3],
        "version": row[4],
        "failure_stage": row[5],
        "failure_message": row[6],
    }


def _count_chunks(doc_id: str) -> int:
    with psycopg.Connection.connect(
        _DATABASE_URL,
        connect_timeout=5,
    ) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT count(*)
                FROM public.document_chunks
                WHERE document_id = %s
                """,
                (doc_id,),
            )

            return cursor.fetchone()[0]


def _document_id_by_storage_path(storage_path: str) -> str | None:
    with psycopg.Connection.connect(
        _DATABASE_URL,
        connect_timeout=5,
    ) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT id
                FROM public.documents
                WHERE storage_path = %s
                """,
                (storage_path,),
            )

            row = cursor.fetchone()

    return str(row[0]) if row is not None else None


def _delete_by_state(state: "FakeStorageState") -> None:
    """Clean up the request's document row via its Storage object path.

    Used by tests whose response does not carry the document id
    (sanitized 50x error responses).
    """
    if not state.uploads:
        return

    document_id = _document_id_by_storage_path(state.uploads[0]["path"])

    if document_id is not None:
        _delete_document(document_id)


def _document_exists(doc_id: str) -> bool:
    return _fetch_document(doc_id) is not None


def _delete_document(doc_id: str) -> None:
    with psycopg.Connection.connect(
        _DATABASE_URL,
        connect_timeout=5,
    ) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "DELETE FROM public.documents WHERE id = %s",
                (doc_id,),
            )


class TestAuthorization:
    def test_no_authorization_header_returns_401(self, client: TestClient):
        response = client.post(
            "/admin/documents",
            files={"file": ("doc.pdf", _VALID_PDF, "application/pdf")},
        )

        assert response.status_code == 401

    def test_customer_role_returns_403(self, customer_client: TestClient):
        state = FakeStorageState()
        _install_fake_storage(state)

        response = customer_client.post(
            "/admin/documents",
            files={"file": ("doc.pdf", _VALID_PDF, "application/pdf")},
        )

        assert response.status_code == 403
        assert state.uploads == []


class TestSuccessfulUpload:
    def test_support_agent_upload_returns_201_internal(self, agent_client: TestClient):
        state = FakeStorageState()
        _install_fake_storage(state)

        response = agent_client.post(
            "/admin/documents",
            files={"file": ("Return Policy.pdf", _INGESTABLE_PDF, "application/pdf")},
        )

        assert response.status_code == 201

        document = response.json()
        assert document["title"] == "Return Policy"
        # The route runs the ingestion pipeline synchronously, so the
        # response reports the document's final, fully ingested state.
        assert document["status"] == "ready"
        assert document["failure_stage"] is None
        assert document["failure_message"] is None
        assert document["visibility"] == "internal"
        assert document["version"] == 1
        assert "storage_path" not in document

        # Storage received the real PDF bytes with PDF content type
        # and the ingestion pipeline downloaded them back.
        assert len(state.uploads) == 1
        upload = state.uploads[0]
        assert upload["data"] == _INGESTABLE_PDF
        assert upload["content_type"] == "application/pdf"
        assert state.downloads == [upload["path"]]

        row = _fetch_document(document["id"])
        assert row is not None
        assert row["status"] == "ready"
        assert row["visibility"] == "internal"
        assert row["version"] == 1
        assert row["storage_path"] == upload["path"]
        assert row["failure_stage"] is None
        assert row["failure_message"] is None

        # Chunks were really inserted by the pipeline.
        assert _count_chunks(document["id"]) > 0

        _delete_document(document["id"])

    def test_admin_upload_returns_201(self, admin_client: TestClient):
        state = FakeStorageState()
        _install_fake_storage(state)

        response = admin_client.post(
            "/admin/documents",
            files={"file": ("Ops Handbook.pdf", _INGESTABLE_PDF, "application/pdf")},
        )

        assert response.status_code == 201

        document = response.json()
        row = _fetch_document(document["id"])
        assert row is not None
        assert row["status"] == "ready"
        assert row["visibility"] == "internal"

        _delete_document(document["id"])

    def test_admin_can_upload_public_document(self, admin_client: TestClient):
        state = FakeStorageState()
        _install_fake_storage(state)

        response = admin_client.post(
            "/admin/documents",
            files={"file": ("doc.pdf", _INGESTABLE_PDF, "application/pdf")},
            data={"visibility": "public"},
        )

        assert response.status_code == 201

        document = response.json()
        row = _fetch_document(document["id"])
        assert row is not None
        assert row["visibility"] == "public"
        assert row["status"] == "ready"

        _delete_document(document["id"])

    def test_support_agent_requesting_public_returns_403(
        self,
        agent_client: TestClient,
    ):
        state = FakeStorageState()
        _install_fake_storage(state)

        response = agent_client.post(
            "/admin/documents",
            files={"file": ("doc.pdf", _VALID_PDF, "application/pdf")},
            data={"visibility": "public"},
        )

        assert response.status_code == 403
        assert response.json() == {
            "detail": "Only admins can upload public documents"
        }
        assert state.uploads == []

    def test_invalid_visibility_value_returns_422(self, admin_client: TestClient):
        state = FakeStorageState()
        _install_fake_storage(state)

        response = admin_client.post(
            "/admin/documents",
            files={"file": ("doc.pdf", _VALID_PDF, "application/pdf")},
            data={"visibility": "customers"},
        )

        assert response.status_code == 422
        assert state.uploads == []


class TestContentValidation:
    def test_invalid_pdf_signature_returns_415(self, admin_client: TestClient):
        state = FakeStorageState()
        _install_fake_storage(state)

        response = admin_client.post(
            "/admin/documents",
            files={"file": ("entity.pdf", b"definitely not a pdf", "application/pdf")},
        )

        assert response.status_code == 415
        assert state.uploads == []
        assert state.removes == []

    def test_empty_file_returns_415(self, admin_client: TestClient):
        state = FakeStorageState()
        _install_fake_storage(state)

        response = admin_client.post(
            "/admin/documents",
            files={"file": ("doc.pdf", b"", "application/pdf")},
        )

        assert response.status_code == 415
        assert state.uploads == []

    def test_oversized_pdf_returns_413(self, admin_client: TestClient):
        state = FakeStorageState()
        _install_fake_storage(state)

        oversized_pdf = _VALID_PDF + b"x" * (MAX_DOCUMENT_SIZE - len(_VALID_PDF) + 1)

        response = admin_client.post(
            "/admin/documents",
            files={"file": ("big.pdf", oversized_pdf, "application/pdf")},
        )

        assert response.status_code == 413
        assert state.uploads == []
        assert state.removes == []

    def test_pdf_at_exact_limit_is_accepted(self, admin_client: TestClient):
        state = FakeStorageState()
        _install_fake_storage(state)

        exact_size_pdf = _VALID_PDF + b"x" * (MAX_DOCUMENT_SIZE - len(_VALID_PDF))

        response = admin_client.post(
            "/admin/documents",
            files={"file": ("exact.pdf", exact_size_pdf, "application/pdf")},
        )

        # The size boundary still accepts the upload and creates the
        # row. This content is not a parseable PDF, so the ingestion
        # outcome is orthogonal here: the final state of the created
        # document is whatever ingestion recorded (contract tested in
        # tests/test_admin_documents_ingestion.py). Only boundary
        # acceptance is asserted.
        assert response.status_code == 201

        document = response.json()

        row = _fetch_document(document["id"])
        assert row is not None

        _delete_document(document["id"])


class TestFailureConsistency:
    def test_storage_upload_failure_creates_no_document_row(
        self,
        admin_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ):
        state = FakeStorageState(fail_upload=True)
        _install_fake_storage(state)

        async def spy_create(*args, **kwargs):
            raise AssertionError("create_document must not be called")

        monkeypatch.setattr(
            documents_repository,
            "create_document",
            spy_create,
        )

        response = admin_client.post(
            "/admin/documents",
            files={"file": ("doc.pdf", _VALID_PDF, "application/pdf")},
        )

        assert response.status_code == 503
        assert response.json() == {
            "detail": "Document storage is temporarily unavailable"
        }
        assert state.uploads == []
        assert state.removes == []

    def test_db_registration_failure_removes_uploaded_object(
        self,
        admin_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ):
        state = FakeStorageState()
        _install_fake_storage(state)

        async def failing_create(*args, **kwargs):
            raise RuntimeError("simulated database failure")

        monkeypatch.setattr(
            documents_repository,
            "create_document",
            failing_create,
        )

        response = admin_client.post(
            "/admin/documents",
            files={"file": ("doc.pdf", _VALID_PDF, "application/pdf")},
        )

        assert response.status_code == 500
        assert response.json() == {
            "detail": "Document could not be registered"
        }

        # The uploaded object must have been cleaned up, exactly once.
        assert len(state.uploads) == 1
        assert state.removes == [state.uploads[0]["path"]]


class TestStoredObjectPath:
    def test_object_path_is_server_generated_uuid(
        self,
        admin_client: TestClient,
    ):
        state = FakeStorageState()
        _install_fake_storage(state)

        # Hostile filename that must never become the object path.
        hostile_filename = "../../evil-« (*.pdf"

        response = admin_client.post(
            "/admin/documents",
            files={"file": (hostile_filename, _INGESTABLE_PDF, "application/pdf")},
        )

        assert response.status_code == 201

        upload = state.uploads[0]
        assert _STORAGE_PATH_PATTERN.match(upload["path"])

        # The client filename must not appear in the object path.
        assert "evil" not in upload["path"]
        assert upload["path"].endswith(".pdf")

        document = response.json()
        row = _fetch_document(document["id"])
        assert row is not None
        assert row["storage_path"] == upload["path"]
        assert row["status"] == "ready"

        _delete_document(document["id"])

    def test_title_is_derived_from_sanitized_filename(
        self,
        admin_client: TestClient,
    ):
        state = FakeStorageState()
        _install_fake_storage(state)

        response = admin_client.post(
            "/admin/documents",
            files={"file": ("Confidential Ops Handbook v2.pdf", _INGESTABLE_PDF, "application/pdf")},
        )

        assert response.status_code == 201
        assert response.json()["title"] == "Confidential Ops Handbook v2"

        _delete_document(response.json()["id"])

    def test_missing_filename_uses_fallback_title(self, admin_client: TestClient):
        state = FakeStorageState()
        _install_fake_storage(state)

        response = admin_client.post(
            "/admin/documents",
            files={"file": ("   .pdf", _INGESTABLE_PDF, "application/pdf")},
        )

        assert response.status_code == 201
        assert response.json()["title"] == "Untitled Document"

        _delete_document(response.json()["id"])


class TestFailureLogging:
    def test_failure_logs_do_not_expose_internal_storage_paths(
        self,
        admin_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ):
        """Internal storage paths/URLs never reach logs or responses.

        Scenario A: upload succeeds, document registration fails, and
        the Storage cleanup also fails — the path would otherwise be
        exposed through the upload, registration, and removal log
        statements. Scenario B: the upload itself fails.

        Response statuses and detail messages must remain exactly as
        before the logging policy, and the storage path must appear in
        neither.
        """
        caplog.set_level(logging.ERROR)

        # Scenario A: registration and cleanup both fail.
        state = FakeStorageState()
        _install_fake_storage(state)

        async def failing_create(*args, **kwargs):
            raise RuntimeError("simulated database failure")

        monkeypatch.setattr(
            documents_repository,
            "create_document",
            failing_create,
        )

        def failing_remove(self, paths: list[str]) -> None:
            raise RuntimeError("simulated cleanup failure")

        monkeypatch.setattr(FakeStorageBucket, "remove", failing_remove)

        response = admin_client.post(
            "/admin/documents",
            files={"file": ("doc.pdf", _VALID_PDF, "application/pdf")},
        )

        assert response.status_code == 500
        assert response.json() == {"detail": "Document could not be registered"}

        internal_path = state.uploads[0]["path"]
        assert internal_path.startswith("uploads/")

        assert internal_path not in response.text

        # Scenario B: the upload itself fails.
        failing_state = FakeStorageState(fail_upload=True)
        _install_fake_storage(failing_state)

        response = admin_client.post(
            "/admin/documents",
            files={"file": ("doc.pdf", _VALID_PDF, "application/pdf")},
        )

        assert response.status_code == 503
        assert response.json() == {
            "detail": "Document storage is temporarily unavailable"
        }

        messages = "\n".join(
            record.getMessage() for record in caplog.records
        )

        # The internal storage path (and any URL) appears in neither
        # the responses nor the captured log records.
        assert internal_path not in messages
        assert "uploads/" not in messages
        assert "http" not in messages.lower()
        assert internal_path not in response.text

        # The log records identify the failing stage and exception
        # type instead of uncontrolled error text.
        assert "Storage upload failed: RuntimeError" in messages
        assert (
            "Failed to register document after Storage upload: RuntimeError"
            in messages
        )
        assert (
            "Failed to remove Storage object after failed document "
            "registration: RuntimeError" in messages
        )
