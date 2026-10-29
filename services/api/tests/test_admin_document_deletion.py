"""Phase 5.8 endpoint tests: DELETE /admin/documents/{id}.

Real local Supabase database (the same pool the service uses) with
Storage and the embedding provider faked exactly like the Phase 5.2-5.7
suites, so no Storage network access happens here. The real-Storage
aspects — including the missing-object policy — are verified separately
in tests/test_storage_deletion_policy.py.

Contract (from the approved plan):

- admin-only authorization (destructive, irreversible; the documents
  table has no creator column to scope an agent's deletion right);
- 204 only after the DB commit and a removal without exception;
- 404 for unknown ids — determined by the DELETE ... RETURNING inside
  the transaction, never accompanied by a Storage call;
- 422 for a non-UUID document id (FastAPI path validation);
- storage removal failure → sanitized 503, pending row delete rolled
  back, nothing claimed as deleted (the outcome is potentially
  uncertain on the remote side — the documented row-without-object
  state that a retry heals);
- database failure → sanitized 500; no Storage removal has happened;
- every status (`processing`/`ready`/`failed`) is deletable; chunks
  disappear through the database's ON DELETE CASCADE, never through a
  manual chunk delete.
"""

import logging
import os
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.repositories import documents as documents_repository
from tests.test_admin_documents import (
    FakeStorageState,
    _INGESTABLE_PDF,
    _count_chunks,
    _delete_document,
    _document_id_by_storage_path,
    _fetch_document,
    _install_fake_storage,
)
from tests.test_admin_documents_ingestion import _CORRUPT_PDF
from tests.test_documents_units import _build_pdf


_DATABASE_URL = os.environ.get("DATABASE_URL", "")

_DELETE_URL_TEMPLATE = "/admin/documents/{document_id}"

_DETAIL_404 = {"detail": "Document not found"}
_DETAIL_503 = {"detail": "Document storage is temporarily unavailable"}
_DETAIL_500 = {"detail": "Document could not be deleted"}


def _delete_document_url(client: TestClient, document_id: str):
    return client.delete(_DELETE_URL_TEMPLATE.format(document_id=document_id))


def _upload_document(
    admin_client: TestClient,
    state: FakeStorageState,
    *,
    filename: str = "storage.pdf",
    content: bytes = _INGESTABLE_PDF,
    data: dict | None = None,
) -> str:
    """Create a document through the real upload endpoint and return its id."""
    response = admin_client.post(
        "/admin/documents",
        files={"file": (filename, content, "application/pdf")},
        data=data or {},
    )

    assert response.status_code == 201, response.text

    document = response.json()
    assert document["id"], document

    return document["id"]


def _assert_row_untouched(document_id: str, expected_status: str) -> None:
    row = _fetch_document(document_id)

    assert row is not None
    assert row["status"] == expected_status


def _assert_chunks(document_id: str, expected: int) -> None:
    assert _count_chunks(document_id) == expected


class TestAuthorization:
    def test_no_authorization_header_returns_401(self, client: TestClient):
        response = _delete_document_url(client, str(uuid.uuid4()))

        assert response.status_code == 401

    def test_customer_role_returns_403(self, customer_client: TestClient):
        state = FakeStorageState()
        _install_fake_storage(state)

        response = _delete_document_url(customer_client, str(uuid.uuid4()))

        assert response.status_code == 403
        # Nothing was removed and nothing was created.
        assert state.uploads == []
        assert state.removes == []

    def test_support_agent_role_returns_403(self, agent_client: TestClient):
        state = FakeStorageState()
        _install_fake_storage(state)

        # Agents may upload but must not delete documents.
        response = _delete_document_url(agent_client, str(uuid.uuid4()))

        assert response.status_code == 403
        assert state.removes == []

    def test_unknown_profile_returns_403(self, unknown_user_client: TestClient):
        response = _delete_document_url(unknown_user_client, str(uuid.uuid4()))

        assert response.status_code == 403


class TestUnknownDocument:
    def test_missing_document_returns_404_without_storage_calls(
        self,
        admin_client: TestClient,
    ):
        state = FakeStorageState()
        _install_fake_storage(state)

        existing_id = _upload_document(admin_client, state)

        response = _delete_document_url(admin_client, str(uuid.uuid4()))

        assert response.status_code == 404
        assert response.json() == _DETAIL_404

        # No Storage removal was triggered for an unknown document,
        # and the existing document is untouched.
        assert state.removes == []
        _assert_row_untouched(existing_id, "ready")

        _delete_document(existing_id)

    def test_invalid_document_id_returns_422(self, admin_client: TestClient):
        state = FakeStorageState()
        _install_fake_storage(state)

        response = _delete_document_url(admin_client, "not-a-uuid")

        assert response.status_code == 422
        assert state.removes == []


class TestSuccessfulDeletion:
    def test_success_returns_204_and_removes_row_chunks_and_object(
        self,
        admin_client: TestClient,
    ):
        state = FakeStorageState()
        _install_fake_storage(state)

        document_id = _upload_document(admin_client, state)
        internal_path = state.uploads[0]["path"]

        # The pipeline really wrote chunks before deletion.
        assert _count_chunks(document_id) > 0

        response = _delete_document_url(admin_client, document_id)

        assert response.status_code == 204
        assert response.content == b""
        # The internal storage path never appears in the response.
        assert "uploads/" not in response.text
        assert internal_path not in response.text

        # Row and (cascaded) chunk rows are gone ...
        assert _fetch_document(document_id) is None
        assert _count_chunks(document_id) == 0
        # ... and Storage recorded exactly one removal of the object.
        assert state.removes == [internal_path]

    def test_repeated_deletion_returns_404_without_second_removal(
        self,
        admin_client: TestClient,
    ):
        state = FakeStorageState()
        _install_fake_storage(state)

        document_id = _upload_document(admin_client, state)
        internal_path = state.uploads[0]["path"]

        first = _delete_document_url(admin_client, document_id)
        second = _delete_document_url(admin_client, document_id)

        assert first.status_code == 204
        assert second.status_code == 404
        assert second.json() == _DETAIL_404

        # The repeated attempt never touches Storage again.
        assert state.removes == [internal_path]


class TestForeignKeyCascade:
    def test_on_delete_cascade_removes_chunks_in_actual_database(self):
        """Prove the chunks are removed by the REAL database cascade.

        Parent row and chunks are inserted directly with SQL (no
        application code), then the parent is deleted with a plain
        DELETE. The `document_chunks.document_id ... on delete cascade`
        foreign key must remove the chunks; this test fails if the
        cascade is ever removed from the schema or a manual chunk
        deletion would be silently required.
        """
        document_id = None

        with psycopg.Connection.connect(_DATABASE_URL, connect_timeout=5) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO public.documents (title, storage_path, visibility)
                    VALUES (%s, %s, 'internal')
                    RETURNING id
                    """,
                    (
                        "Cascade Probe (deletion test)",
                        f"uploads/{uuid.uuid4()}.pdf",
                    ),
                )
                document_id = str(cursor.fetchone()[0])

                zero_vector = "[" + ",".join(["0"] * 768) + "]"

                for chunk_index in range(3):
                    cursor.execute(
                        """
                        INSERT INTO public.document_chunks
                            (document_id, content, embedding, chunk_index)
                        VALUES (%s, %s, %s::extensions.vector, %s)
                        """,
                        (
                            document_id,
                            f"cascade probe chunk {chunk_index}",
                            zero_vector,
                            chunk_index,
                        ),
                    )

        try:
            assert _count_chunks(document_id) == 3

            with psycopg.Connection.connect(_DATABASE_URL, connect_timeout=5) as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "DELETE FROM public.documents WHERE id = %s",
                        (document_id,),
                    )

            assert _fetch_document(document_id) is None
            # Removed by the foreign key cascade, not by any manual
            # chunk-deletion statement (the repository contains none).
            assert _count_chunks(document_id) == 0
        finally:
            if document_id is not None:
                _delete_document(document_id)


class TestDeletionAcrossStatuses:
    def test_failed_document_is_deletable(self, admin_client: TestClient):
        state = FakeStorageState()
        _install_fake_storage(state)

        # A corrupt PDF fails ingestion and stays as a `failed` row.
        document_id = _upload_document(
            admin_client,
            state,
            filename="broken.pdf",
            content=_CORRUPT_PDF,
        )

        _assert_row_untouched(document_id, "failed")

        response = _delete_document_url(admin_client, document_id)

        assert response.status_code == 204
        assert _fetch_document(document_id) is None
        assert state.removes == [state.uploads[0]["path"]]

    def test_processing_document_is_deletable(
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
        _install_fake_storage(state)

        # The ready write fails, so the upload returns a sanitized 500
        # and the document stays `processing` (the documented Phase 5.7
        # behavior — used here as the setup for deletion).
        response = admin_client.post(
            "/admin/documents",
            files={"file": ("stuck.pdf", _INGESTABLE_PDF, "application/pdf")},
        )
        assert response.status_code == 500

        document_id = _document_id_by_storage_path(state.uploads[0]["path"])
        assert document_id is not None

        row = _fetch_document(document_id)
        assert row is not None
        assert row["status"] == "processing"

        response = _delete_document_url(admin_client, document_id)

        assert response.status_code == 204
        assert _fetch_document(document_id) is None
        assert _count_chunks(document_id) == 0


class TestStorageFailurePolicy:
    def test_removal_failure_rolls_back_and_returns_sanitized_503(
        self,
        admin_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ):
        state = FakeStorageState()
        _install_fake_storage(state)

        document_id = _upload_document(admin_client, state)
        internal_path = state.uploads[0]["path"]
        chunks_before = _count_chunks(document_id)

        def failing_remove(self, paths):
            raise RuntimeError("simulated storage outage during deletion")

        monkeypatch.setattr(
            "tests.test_admin_documents.FakeStorageBucket.remove",
            failing_remove,
        )

        response = _delete_document_url(admin_client, document_id)
        try:
            assert response.status_code == 503
            assert response.json() == _DETAIL_503
            # Uncontrolled error text and internal paths never surface.
            assert "simulated" not in response.text
            assert internal_path not in response.text
            assert "uploads/" not in response.text

            # The row delete was rolled back: the pending DELETE is
            # gone, document and chunks are intact, and the object is
            # still in the fake bucket (no removal was recorded).
            _assert_row_untouched(document_id, "ready")
            _assert_chunks(document_id, chunks_before)
            assert state.removes == []
        finally:
            _delete_document(document_id)

    def test_retry_after_uncertain_removal_outcome_heals(
        self,
        admin_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Second attempt returns 204 — the row-without-object state
        heals, exercising the verified real-API behavior that
        removing a nonexistent object is a success."""
        state = FakeStorageState()
        _install_fake_storage(state)

        document_id = _upload_document(admin_client, state)
        internal_path = state.uploads[0]["path"]

        def failing_remove(self, paths):
            raise RuntimeError("simulated storage outage during deletion")

        monkeypatch.setattr(
            "tests.test_admin_documents.FakeStorageBucket.remove",
            failing_remove,
        )
        first = _delete_document_url(admin_client, document_id)

        # Retry only after the intermediate state is pinned: the row
        # delete was rolled back and the document is fully intact.
        assert first.status_code == 503
        assert first.json() == _DETAIL_503
        _assert_row_untouched(document_id, "ready")
        assert _count_chunks(document_id) > 0

        # Recovery: the next removal succeeds. (On the real service
        # this covers both cases: the object still exists, or the
        # object was already remotely removed despite the error.)
        monkeypatch.undo()
        second = _delete_document_url(admin_client, document_id)

        try:
            assert second.status_code == 204
            assert second.content == b""
            assert _fetch_document(document_id) is None
            assert state.removes == [internal_path]
        finally:
            _delete_document(document_id)

    def test_row_with_object_already_gone_is_deletable(
        self,
        admin_client: TestClient,
    ):
        """Row alive while Storage no longer has the object (fresh
        empty bucket after the upload): deletion still succeeds."""
        first_state = FakeStorageState()
        _install_fake_storage(first_state)

        document_id = _upload_document(admin_client, first_state)
        internal_path = first_state.uploads[0]["path"]

        # Storage effectively lost the object: swap to an empty fake
        # bucket for the deletion request.
        empty_state = FakeStorageState()
        _install_fake_storage(empty_state)

        response = _delete_document_url(admin_client, document_id)

        assert response.status_code == 204
        assert empty_state.removes == [internal_path]
        assert _fetch_document(document_id) is None


class TestDatabaseFailurePolicy:
    def test_db_deletion_failure_returns_sanitized_500_touching_nothing(
        self,
        admin_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ):
        state = FakeStorageState()
        _install_fake_storage(state)

        document_id = _upload_document(admin_client, state)
        internal_path = state.uploads[0]["path"]
        chunks_before = _count_chunks(document_id)

        async def failing_delete_document(cursor, *, document_id):
            raise RuntimeError("simulated database failure during deletion")

        monkeypatch.setattr(
            documents_repository,
            "delete_document",
            failing_delete_document,
        )

        response = _delete_document_url(admin_client, document_id)
        try:
            assert response.status_code == 500
            assert response.json() == _DETAIL_500
            assert "simulated" not in response.text
            assert internal_path not in response.text

            # Nothing changed anywhere: row, chunks, and object untouched.
            _assert_row_untouched(document_id, "ready")
            _assert_chunks(document_id, chunks_before)
            assert state.removes == []
        finally:
            _delete_document(document_id)


class TestSanitizedLogsAndResponses:
    def _app_log_messages(self, caplog: pytest.LogCaptureFixture) -> str:
        return "\n".join(
            record.getMessage()
            for record in caplog.records
            if record.name.startswith("app.")
        )

    def test_removal_failure_logs_are_sanitized(
        self,
        admin_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ):
        caplog.set_level(logging.DEBUG)

        state = FakeStorageState()
        _install_fake_storage(state)

        document_id = _upload_document(admin_client, state)
        internal_path = state.uploads[0]["path"]

        def failing_remove(self, paths):
            raise RuntimeError("simulated storage outage during deletion")

        monkeypatch.setattr(
            "tests.test_admin_documents.FakeStorageBucket.remove",
            failing_remove,
        )

        response = _delete_document_url(admin_client, document_id)

        try:
            assert response.status_code == 503

            messages = self._app_log_messages(caplog)

            # No storage path, no URL, no uncontrolled error text —
            # neither in logs nor in the response.
            assert internal_path not in response.text
            assert internal_path not in messages
            assert "uploads/" not in messages
            assert "http" not in messages.lower()
            assert "simulated" not in response.text
            assert "simulated" not in messages

            # Stage and exception type are logged instead.
            assert "storage_removal" in messages
            assert "RuntimeError" in messages
        finally:
            _delete_document(document_id)

    def test_database_failure_logs_are_sanitized(
        self,
        admin_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ):
        caplog.set_level(logging.DEBUG)

        state = FakeStorageState()
        _install_fake_storage(state)

        document_id = _upload_document(admin_client, state)
        internal_path = state.uploads[0]["path"]

        async def failing_delete_document(cursor, *, document_id):
            raise RuntimeError("simulated database failure during deletion")

        monkeypatch.setattr(
            documents_repository,
            "delete_document",
            failing_delete_document,
        )

        response = _delete_document_url(admin_client, document_id)

        try:
            assert response.status_code == 500

            messages = self._app_log_messages(caplog)

            assert internal_path not in response.text
            assert internal_path not in messages
            assert "uploads/" not in messages
            assert "http" not in messages.lower()
            assert "simulated" not in response.text
            assert "simulated" not in messages

            assert "database_deletion" in messages
            assert "RuntimeError" in messages
        finally:
            _delete_document(document_id)
