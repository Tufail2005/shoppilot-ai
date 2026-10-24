import os
import re

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.repositories import documents as documents_repository
from app.storage import get_storage


# 20 MiB = 20,971,520 bytes
MAX_DOCUMENT_SIZE = 20 * 1024 * 1024

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

    def remove(self, paths: list[str]) -> None:
        self._state.removes.extend(paths)


class FakeStorageState:
    def __init__(
        self,
        *,
        fail_upload: bool = False,
    ):
        self.fail_upload = fail_upload
        self.uploads: list[dict] = []
        self.removes: list[str] = []


class FakeStorageClient:
    def __init__(self, state: FakeStorageState):
        self._state = state

    def from_(self, bucket: str) -> FakeStorageBucket:
        assert bucket == "documents"
        return FakeStorageBucket(self._state)


def _install_fake_storage(
    state: FakeStorageState,
) -> None:
    from app.main import app

    app.dependency_overrides[get_storage] = lambda: FakeStorageClient(state)


_DATABASE_URL = os.environ.get("DATABASE_URL", "")


def _fetch_document(doc_id: str) -> dict | None:
    with psycopg.Connection.connect(
        _DATABASE_URL,
        connect_timeout=5,
    ) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT title, visibility, status, storage_path, version
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
    }


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
            files={"file": ("Return Policy.pdf", _VALID_PDF, "application/pdf")},
        )

        assert response.status_code == 201

        document = response.json()
        assert document["title"] == "Return Policy"
        assert document["status"] == "processing"
        assert document["visibility"] == "internal"
        assert document["version"] == 1
        assert "storage_path" not in document

        # Storage received the real PDF bytes with PDF content type.
        assert len(state.uploads) == 1
        upload = state.uploads[0]
        assert upload["data"] == _VALID_PDF
        assert upload["content_type"] == "application/pdf"

        row = _fetch_document(document["id"])
        assert row is not None
        assert row["status"] == "processing"
        assert row["visibility"] == "internal"
        assert row["version"] == 1
        assert row["storage_path"] == upload["path"]

        _delete_document(document["id"])

    def test_admin_upload_returns_201(self, admin_client: TestClient):
        state = FakeStorageState()
        _install_fake_storage(state)

        response = admin_client.post(
            "/admin/documents",
            files={"file": ("Ops Handbook.pdf", _VALID_PDF, "application/pdf")},
        )

        assert response.status_code == 201

        document = response.json()
        row = _fetch_document(document["id"])
        assert row is not None
        assert row["status"] == "processing"
        assert row["visibility"] == "internal"

        _delete_document(document["id"])

    def test_admin_can_upload_public_document(self, admin_client: TestClient):
        state = FakeStorageState()
        _install_fake_storage(state)

        response = admin_client.post(
            "/admin/documents",
            files={"file": ("doc.pdf", _VALID_PDF, "application/pdf")},
            data={"visibility": "public"},
        )

        assert response.status_code == 201

        document = response.json()
        row = _fetch_document(document["id"])
        assert row is not None
        assert row["visibility"] == "public"
        assert row["status"] == "processing"

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

        assert response.status_code == 201

        document = response.json()
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
            files={"file": (hostile_filename, _VALID_PDF, "application/pdf")},
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

        _delete_document(document["id"])

    def test_title_is_derived_from_sanitized_filename(
        self,
        admin_client: TestClient,
    ):
        state = FakeStorageState()
        _install_fake_storage(state)

        response = admin_client.post(
            "/admin/documents",
            files={"file": ("Confidential Ops Handbook v2.pdf", _VALID_PDF, "application/pdf")},
        )

        assert response.status_code == 201
        assert response.json()["title"] == "Confidential Ops Handbook v2"

        _delete_document(response.json()["id"])

    def test_missing_filename_uses_fallback_title(self, admin_client: TestClient):
        state = FakeStorageState()
        _install_fake_storage(state)

        response = admin_client.post(
            "/admin/documents",
            files={"file": ("   .pdf", _VALID_PDF, "application/pdf")},
        )

        assert response.status_code == 201
        assert response.json()["title"] == "Untitled Document"

        _delete_document(response.json()["id"])
