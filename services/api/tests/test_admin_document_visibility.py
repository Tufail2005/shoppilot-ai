"""Phase 5.9 endpoint tests: PATCH /admin/documents/{document_id}.

Real local Supabase database (the same pool the service uses) with
Storage and the embedding provider faked exactly like the Phase
5.2-5.8 suites, so no Storage network access and no Gemini call
happens here: a visibility change never touches Storage, chunks, or
embeddings.

Contract (from the approved plan):

- admin-only authorization (internal -> public is a content-exposure
  grant; the documents table has no creator column to scope a support
  agent's update right — the same rationale as Phase 5.8 deletion);
- 200 with the persisted DocumentOut only after the commit;
- 404 for unknown ids — the zero-row UPDATE is rolled back, and no
  success is ever claimed;
- 422 for a non-UUID document id, an invalid visibility value, a
  missing field, and unexpected fields (extra="forbid");
- statement failure -> sanitized 500 ("Document could not be
  updated"); the uncertain commit failure and its distinct
  "could not be confirmed" error are covered offline in
  tests/test_document_visibility_units.py, following the Phase 5.8
  convention (commit failures are not inducible through the test
  client);
- every status (`processing`/`ready`/`failed`) is visibility-
  updateable; status, title, version, failure fields, the Storage
  object, the chunks, and their embeddings are unchanged;
- the existing RLS policies evaluate the row's CURRENT visibility at
  query time, verified here with an authenticated customer/support
  agent context against the real database: access changes immediately
  in both directions, for documents AND chunks.
"""

import hashlib
import json
import logging
import os
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.repositories import documents as documents_repository
from tests.conftest import TEST_USERS
from tests.test_admin_documents import (
    FakeStorageState,
    _INGESTABLE_PDF,
    _count_chunks,
    _delete_document,
    _fetch_document,
    _install_fake_storage,
)
from tests.test_admin_documents_ingestion import _CORRUPT_PDF


_DATABASE_URL = os.environ.get("DATABASE_URL", "")

_PATCH_URL_TEMPLATE = "/admin/documents/{document_id}"

_DETAIL_404 = {"detail": "Document not found"}
_DETAIL_500_STATEMENT = {"detail": "Document could not be updated"}

_PUBLIC_REQUEST = {"visibility": "public"}
_INTERNAL_REQUEST = {"visibility": "internal"}


def _patch_document(
    client: TestClient,
    document_id: str,
    payload: dict | None = None,
):
    return client.patch(
        _PATCH_URL_TEMPLATE.format(document_id=document_id),
        json=payload,
    )


def _insert_document(*, visibility: str = "internal") -> str:
    """Insert one `ready` document row directly with SQL (no app code).

    Used where a 403 test needs an existing document while the test
    client is authenticated as a non-admin.
    """
    with psycopg.Connection.connect(
        _DATABASE_URL,
        connect_timeout=5,
    ) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO public.documents
                    (title, storage_path, visibility, status)
                VALUES (%s, %s, %s, 'ready')
                RETURNING id
                """,
                (
                    f"Visibility probe ({visibility})",
                    f"uploads/{uuid.uuid4()}.pdf",
                    visibility,
                ),
            )

            document_id = str(cursor.fetchone()[0])

    return document_id


def _install_document_cleanup(document_id: str):
    """Remove a directly inserted probe row (bypasses the endpoint)."""
    with psycopg.Connection.connect(
        _DATABASE_URL,
        connect_timeout=5,
    ) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "DELETE FROM public.documents WHERE id = %s",
                (document_id,),
            )


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


def _fetch_document_row_full(document_id: str) -> dict | None:
    """Fetch every field a visibility update must leave untouched."""
    with psycopg.Connection.connect(
        _DATABASE_URL,
        connect_timeout=5,
    ) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT title, visibility, status, storage_path, version,
                       failure_stage, failure_message, updated_at, created_at
                FROM public.documents
                WHERE id = %s
                """,
                (document_id,),
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
        # Formatted to stable strings so two snapshots compare by value.
        "updated_at": row[7].isoformat() if row[7] else None,
        "created_at": row[8].isoformat() if row[8] else None,
    }


def _fetch_chunks_snapshot(document_id: str) -> str:
    """Return a stable fingerprint of all chunks and their embeddings."""
    with psycopg.Connection.connect(
        _DATABASE_URL,
        connect_timeout=5,
    ) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT chunk_index, content, embedding::text
                FROM public.document_chunks
                WHERE document_id = %s
                ORDER BY chunk_index
                """,
                (document_id,),
            )

            rows = cursor.fetchall()

    lines = [
        f"{chunk_index}|{content}|"
        f"{hashlib.sha256(embedding_text.encode('utf-8')).hexdigest()}"
        for chunk_index, content, embedding_text in rows
    ]

    return "\n".join(lines)


def _rls_visible_counts(document_id: str, user_id: str) -> tuple[int, int]:
    """Count documents and chunks visible under REAL RLS for one user.

    Opens a superuser connection, then switches to the real
    `authenticated` role with the user's JWT claims simulated via
    `request.jwt.claims` (the same technique used for the Phase 3.10
    runtime checks), so the actual RLS policies decide what is
    returned. The role/claims only apply for this transaction: `set
    local` is reset when the transaction ends.
    """
    with psycopg.Connection.connect(
        _DATABASE_URL,
        connect_timeout=5,
    ) as conn:
        with conn.transaction():
            cursor = conn.cursor()
            cursor.execute("set local role authenticated")
            cursor.execute(
                "select set_config('request.jwt.claims', %s, true)",
                (json.dumps({"sub": user_id, "role": "authenticated"}),),
            )
            cursor.execute(
                "SELECT count(*) FROM public.documents WHERE id = %s",
                (document_id,),
            )
            documents_visible = cursor.fetchone()[0]
            cursor.execute(
                "SELECT count(*) FROM public.document_chunks "
                "WHERE document_id = %s",
                (document_id,),
            )
            chunks_visible = cursor.fetchone()[0]

    return documents_visible, chunks_visible


class TestAuthorization:
    def test_no_authorization_header_returns_401(self, client: TestClient):
        response = _patch_document(client, str(uuid.uuid4()), _PUBLIC_REQUEST)

        assert response.status_code == 401

    def test_customer_role_returns_403_and_document_unchanged(
        self,
        customer_client: TestClient,
    ):
        # A probe row exists while the client is a customer: persons
        # without the admin role must not be able to change anything.
        document_id = _insert_document(visibility="internal")

        try:
            response = _patch_document(
                customer_client,
                document_id,
                _PUBLIC_REQUEST,
            )

            assert response.status_code == 403

            row = _fetch_document_row_full(document_id)
            assert row is not None
            assert row["visibility"] == "internal"
            assert row["status"] == "ready"
        finally:
            _install_document_cleanup(document_id)

    def test_support_agent_role_returns_403_and_document_unchanged(
        self,
        agent_client: TestClient,
    ):
        document_id = _insert_document(visibility="internal")

        try:
            response = _patch_document(
                agent_client,
                document_id,
                _PUBLIC_REQUEST,
            )

            assert response.status_code == 403

            row = _fetch_document_row_full(document_id)
            assert row is not None
            assert row["visibility"] == "internal"
        finally:
            _install_document_cleanup(document_id)

    def test_unknown_profile_returns_403(self, unknown_user_client: TestClient):
        response = _patch_document(
            unknown_user_client,
            str(uuid.uuid4()),
            _PUBLIC_REQUEST,
        )

        assert response.status_code == 403


class TestRequestValidation:
    def test_invalid_visibility_value_returns_422(
        self,
        admin_client: TestClient,
    ):
        document_id = _insert_document(visibility="internal")

        try:
            response = _patch_document(
                admin_client,
                document_id,
                {"visibility": "customers"},
            )

            assert response.status_code == 422

            row = _fetch_document_row_full(document_id)
            assert row is not None
            assert row["visibility"] == "internal"
        finally:
            _install_document_cleanup(document_id)

    def test_unexpected_extra_field_returns_422(
        self,
        admin_client: TestClient,
    ):
        document_id = _insert_document(visibility="internal")

        try:
            response = _patch_document(
                admin_client,
                document_id,
                {"visibility": "public", "status": "ready"},
            )

            # extra="forbid": an unexpected field must be rejected, not
            # silently ignored.
            assert response.status_code == 422

            row = _fetch_document_row_full(document_id)
            assert row is not None
            assert row["visibility"] == "internal"
            assert row["status"] == "ready"
        finally:
            _install_document_cleanup(document_id)

    def test_missing_visibility_field_returns_422(
        self,
        admin_client: TestClient,
    ):
        response = _patch_document(admin_client, str(uuid.uuid4()), {})

        assert response.status_code == 422

    def test_invalid_document_id_returns_422(self, admin_client: TestClient):
        response = _patch_document(admin_client, "not-a-uuid", _PUBLIC_REQUEST)

        assert response.status_code == 422


class TestUnknownDocument:
    def test_missing_document_returns_404_and_touches_nothing(
        self,
        admin_client: TestClient,
    ):
        state = FakeStorageState()
        _install_fake_storage(state)

        existing_id = _upload_document(admin_client, state)
        row_before = _fetch_document_row_full(existing_id)

        response = _patch_document(admin_client, str(uuid.uuid4()), _PUBLIC_REQUEST)

        try:
            assert response.status_code == 404
            assert response.json() == _DETAIL_404

            # The existing document is untouched.
            row_after = _fetch_document_row_full(existing_id)
            assert row_after == row_before
        finally:
            _delete_document(existing_id)


class TestSuccessfulVisibilityChanges:
    def test_internal_to_public_returns_200_and_takes_effect_immediately(
        self,
        admin_client: TestClient,
    ):
        state = FakeStorageState()
        _install_fake_storage(state)

        document_id = _upload_document(admin_client, state)
        assert _count_chunks(document_id) > 0

        try:
            response = _patch_document(admin_client, document_id, _PUBLIC_REQUEST)

            assert response.status_code == 200

            document = response.json()
            assert document["id"] == document_id
            assert document["visibility"] == "public"
            assert document["status"] == "ready"
            assert document["version"] == 1
            assert document["failure_stage"] is None
            assert document["failure_message"] is None
            # The response never carries the internal storage path.
            assert "storage_path" not in document
            assert "uploads/" not in response.text

            row = _fetch_document(document_id)
            assert row is not None
            assert row["visibility"] == "public"

            # Storage was never touched by the visibility change: no
            # additional download and no removal happened.
            assert len(state.downloads) == 1
            assert state.removes == []
        finally:
            _delete_document(document_id)

    def test_public_to_internal_returns_200(
        self,
        admin_client: TestClient,
    ):
        state = FakeStorageState()
        _install_fake_storage(state)

        document_id = _upload_document(
            admin_client,
            state,
            data={"visibility": "public"},
        )

        row = _fetch_document(document_id)
        assert row is not None
        assert row["visibility"] == "public"

        try:
            response = _patch_document(admin_client, document_id, _INTERNAL_REQUEST)

            assert response.status_code == 200

            document = response.json()
            assert document["visibility"] == "internal"

            row = _fetch_document(document_id)
            assert row is not None
            assert row["visibility"] == "internal"
            assert row["status"] == "ready"
        finally:
            _delete_document(document_id)

    def test_repeated_same_visibility_value_returns_200(
        self,
        admin_client: TestClient,
    ):
        """Setting the already-current value is a defined, harmless
        success: the value is unchanged and no error is raised."""
        state = FakeStorageState()
        _install_fake_storage(state)

        document_id = _upload_document(admin_client, state)

        try:
            first = _patch_document(admin_client, document_id, _INTERNAL_REQUEST)
            second = _patch_document(admin_client, document_id, _INTERNAL_REQUEST)

            assert first.status_code == 200
            assert second.status_code == 200
            assert second.json()["visibility"] == "internal"

            row = _fetch_document(document_id)
            assert row is not None
            assert row["visibility"] == "internal"
            assert row["status"] == "ready"
        finally:
            _delete_document(document_id)


class TestUntouchedStateAcrossStatuses:
    def test_ready_document_everything_but_visibility_is_unchanged(
        self,
        admin_client: TestClient,
    ):
        state = FakeStorageState()
        _install_fake_storage(state)

        document_id = _upload_document(admin_client, state)
        chunks_before = _count_chunks(document_id)
        row_before = _fetch_document_row_full(document_id)
        chunks_snapshot_before = _fetch_chunks_snapshot(document_id)
        assert chunks_before > 0

        try:
            response = _patch_document(admin_client, document_id, _PUBLIC_REQUEST)

            assert response.status_code == 200

            row_after = _fetch_document_row_full(document_id)
            chunks_snapshot_after = _fetch_chunks_snapshot(document_id)

            # Only `visibility` changed; everything else — status,
            # title, version, failure fields, and the immutable
            # `created_at` — is identical.
            assert row_after["visibility"] != row_before["visibility"]
            for field in (
                "title",
                "status",
                "storage_path",
                "version",
                "failure_stage",
                "failure_message",
                "created_at",
            ):
                assert row_after[field] == row_before[field]
            # The explicit `updated_at` bump is the only other write.
            assert row_after["updated_at"] >= row_before["updated_at"]

            # Chunks, chunk content, and embeddings are byte-identical.
            assert chunks_snapshot_after == chunks_snapshot_before
        finally:
            _delete_document(document_id)

    def test_failed_document_is_visibility_updateable(
        self,
        admin_client: TestClient,
    ):
        state = FakeStorageState()
        _install_fake_storage(state)

        document_id = _upload_document(
            admin_client,
            state,
            filename="broken.pdf",
            content=_CORRUPT_PDF,
        )

        row = _fetch_document_row_full(document_id)
        assert row is not None
        assert row["status"] == "failed"
        assert row["failure_stage"] is not None

        try:
            response = _patch_document(admin_client, document_id, _PUBLIC_REQUEST)

            assert response.status_code == 200
            assert response.json()["visibility"] == "public"

            row_after = _fetch_document_row_full(document_id)
            assert row_after["visibility"] == "public"
            # Failure information of the failed document is untouched.
            assert row_after["status"] == "failed"
            assert row_after["failure_stage"] == row["failure_stage"]
            assert row_after["failure_message"] == row["failure_message"]
            assert row_after["storage_path"] == row["storage_path"]
        finally:
            _delete_document(document_id)

    def test_processing_document_is_visibility_updateable(
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
        # behavior — used here as the setup).
        response = admin_client.post(
            "/admin/documents",
            files={"file": ("stuck.pdf", _INGESTABLE_PDF, "application/pdf")},
        )
        assert response.status_code == 500

        document_id = None

        with psycopg.Connection.connect(
            _DATABASE_URL,
            connect_timeout=5,
        ) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT id FROM public.documents "
                    "WHERE storage_path = %s",
                    (state.uploads[0]["path"],),
                )
                row = cursor.fetchone()
                document_id = str(row[0]) if row is not None else None

        assert document_id is not None

        row = _fetch_document_row_full(document_id)
        assert row is not None
        assert row["status"] == "processing"

        chunks_before = _count_chunks(document_id)
        assert chunks_before > 0

        try:
            response = _patch_document(admin_client, document_id, _PUBLIC_REQUEST)

            assert response.status_code == 200
            assert response.json()["visibility"] == "public"

            row_after = _fetch_document_row_full(document_id)
            assert row_after["status"] == "processing"
            assert row_after["failure_stage"] is None
            assert row_after["failure_message"] is None
            assert _count_chunks(document_id) == chunks_before
        finally:
            _delete_document(document_id)


class TestDatabaseFailurePolicy:
    def test_statement_failure_returns_sanitized_500_and_nothing_changed(
        self,
        admin_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ):
        state = FakeStorageState()
        _install_fake_storage(state)

        document_id = _upload_document(admin_client, state)
        row_before = _fetch_document_row_full(document_id)
        chunks_before = _count_chunks(document_id)

        async def failing_set_visibility(cursor, *, document_id, visibility):
            raise RuntimeError("simulated database failure during update")

        monkeypatch.setattr(
            documents_repository,
            "set_document_visibility",
            failing_set_visibility,
        )

        response = _patch_document(admin_client, document_id, _PUBLIC_REQUEST)

        try:
            assert response.status_code == 500
            assert response.json() == _DETAIL_500_STATEMENT

            # Uncontrolled error text and internal paths never surface.
            assert "simulated" not in response.text
            assert "uploads/" not in response.text

            # The failing statement was rolled back on context exit:
            # the database state is unchanged and Storage was never
            # touched.
            row_after = _fetch_document_row_full(document_id)
            assert row_after == row_before
            assert _count_chunks(document_id) == chunks_before
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

    def test_statement_failure_logs_are_sanitized(
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

        async def failing_set_visibility(cursor, *, document_id, visibility):
            raise RuntimeError("simulated database failure during update")

        monkeypatch.setattr(
            documents_repository,
            "set_document_visibility",
            failing_set_visibility,
        )

        response = _patch_document(admin_client, document_id, _PUBLIC_REQUEST)

        try:
            assert response.status_code == 500

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
            assert "database_update" in messages
            assert "database_commit" not in messages
            assert "RuntimeError" in messages
        finally:
            _delete_document(document_id)


class TestRlsImmediateEffect:
    """The REAL RLS policies re-evaluate the updated visibility value.

    The backend pool uses the service role and bypasses RLS on
    purpose; these tests switch to the real `authenticated` role with
    simulated JWT claims so the actual migration-defined policies
    decide visibility — verifying that no policy change or migration
    was needed and that taking effect is immediate.
    """

    def test_internal_to_public_grants_customer_access_immediately(
        self,
        admin_client: TestClient,
    ):
        state = FakeStorageState()
        _install_fake_storage(state)

        document_id = _upload_document(admin_client, state)  # internal default
        chunk_count = _count_chunks(document_id)
        assert chunk_count > 0

        try:
            # Before: internal — invisible to a customer under RLS,
            # visible to a support agent (staff).
            assert _rls_visible_counts(
                document_id, TEST_USERS["customer"]
            ) == (0, 0)
            assert _rls_visible_counts(
                document_id, TEST_USERS["support_agent"]
            ) == (1, chunk_count)

            response = _patch_document(admin_client, document_id, _PUBLIC_REQUEST)
            assert response.status_code == 200

            # After: public — both the document AND its chunks are
            # visible to the same customer immediately.
            assert _rls_visible_counts(
                document_id, TEST_USERS["customer"]
            ) == (1, chunk_count)
            assert _rls_visible_counts(
                document_id, TEST_USERS["support_agent"]
            ) == (1, chunk_count)
        finally:
            _delete_document(document_id)

    def test_public_to_internal_revokes_customer_access_immediately(
        self,
        admin_client: TestClient,
    ):
        state = FakeStorageState()
        _install_fake_storage(state)

        document_id = _upload_document(
            admin_client,
            state,
            data={"visibility": "public"},
        )
        chunk_count = _count_chunks(document_id)
        assert chunk_count > 0

        try:
            # Before: public — visible to a customer under RLS.
            assert _rls_visible_counts(
                document_id, TEST_USERS["customer"]
            ) == (1, chunk_count)

            response = _patch_document(admin_client, document_id, _INTERNAL_REQUEST)
            assert response.status_code == 200

            # After: internal — the document AND its chunks are
            # immediately hidden from the same customer, while staff
            # visibility is preserved.
            assert _rls_visible_counts(
                document_id, TEST_USERS["customer"]
            ) == (0, 0)
            assert _rls_visible_counts(
                document_id, TEST_USERS["support_agent"]
            ) == (1, chunk_count)
        finally:
            _delete_document(document_id)
