"""Offline unit tests for the Phase 5.9 visibility-update service.

No local Supabase database, Storage, or Gemini credentials are
required: the pool/cursor layer and the document repository's cursor
are faked, mirroring the conventions of test_document_deletion_units.py
and test_document_status_units.py. Real-database behavior (UPDATE ...
RETURNING, rollback semantics, RLS evaluation of the new visibility
value) is covered by test_admin_document_visibility.py.

Focus here: the SQL contract of
``documents_repository.set_document_visibility`` (single parameterized
statement touching exactly `visibility` and `updated_at`, no status
guard) and the failure sequencing of
``documents_service.update_document_visibility`` — statement failures
and commit failures are tested SEPARATELY, with distinct sanitized
errors, and the uncertain commit outcome never claims the database
state is unchanged.
"""

import asyncio
import logging
import uuid

import pytest
from fastapi import HTTPException

from app.repositories import documents as documents_repository
from app.services import documents as documents_service


_DOCUMENT_ID = uuid.UUID("00000000-0000-0000-0000-0000000059f9")
_VISIBILITY_ROW = (
    _DOCUMENT_ID,
    "Ops Handbook",
    f"uploads/{_DOCUMENT_ID}.pdf",
    "public",
    "ready",
    1,
    None,
    None,
    "2026-10-26T00:00:00+00:00",
)

_DETAIL_404 = {"detail": "Document not found"}
_DETAIL_500_STATEMENT = {"detail": "Document could not be updated"}
_DETAIL_500_COMMIT = {"detail": "Document visibility update could not be confirmed"}


class FakeDescription:
    # Mirrors cursor.description: each entry has a `.name`.
    def __init__(self, name: str):
        self.name = name


class _FakeCursor:
    """Records statements; returns a configurable single RETURNING row."""

    def __init__(self, *, row: tuple | None, fail_statement: bool = False):
        self.statements: list[tuple[str, list | None]] = []
        self._row = row
        self._fail_statement = fail_statement
        self.description = [
            FakeDescription(name)
            for name in [
                "id",
                "title",
                "storage_path",
                "visibility",
                "status",
                "version",
                "failure_stage",
                "failure_message",
                "created_at",
            ]
        ]

    async def execute(self, sql: str, params: list | None = None) -> None:
        self.statements.append((sql, list(params) if params else None))

        if self._fail_statement:
            raise RuntimeError("simulated database failure")

    async def fetchone(self) -> tuple | None:
        return self._row

    async def __aenter__(self) -> "_FakeCursor":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _FakeConnection:
    """Mirrors what the visibility service exercises of a connection.

    The psycopg_pool context manager rolls the transaction back when
    the block exits with an exception and commits on a clean exit —
    the explicit ``await conn.commit()`` of the service makes the
    clean-exit commit a no-op here, which the real behavior matches.
    """

    def __init__(self, cursor: _FakeCursor, *, fail_commit: bool = False):
        self._cursor = cursor
        self._fail_commit = fail_commit
        self.commit_calls = 0
        self.rollback_calls = 0

    def cursor(self) -> _FakeCursor:
        return self._cursor

    async def commit(self) -> None:
        if self._fail_commit:
            raise RuntimeError("simulated commit failure")

        self.commit_calls += 1

    async def rollback(self) -> None:
        self.rollback_calls += 1

    async def __aenter__(self) -> "_FakeConnection":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None:
            # psycopg_pool rolls back on error exits.
            self.rollback_calls += 1

        return False


class _FakePool:
    def __init__(self, connection: _FakeConnection):
        self._connection = connection

    def connection(self) -> _FakeConnection:
        return self._connection


def _make_service(
    *,
    known_row: bool = True,
    fail_statement: bool = False,
    fail_commit: bool = False,
) -> tuple:
    cursor = _FakeCursor(
        row=_VISIBILITY_ROW if known_row else None,
        fail_statement=fail_statement,
    )
    connection = _FakeConnection(cursor, fail_commit=fail_commit)
    pool = _FakePool(connection)

    return pool, connection, cursor


class TestRepositoryContract:
    def test_visibility_update_is_one_parameterized_statement(self):
        cursor = _FakeCursor(row=_VISIBILITY_ROW)

        document = asyncio.run(
            documents_repository.set_document_visibility(
                cursor,  # type: ignore[arg-type]
                document_id=_DOCUMENT_ID,
                visibility="public",
            )
        )

        assert document["visibility"] == "public"

        assert len(cursor.statements) == 1

        sql, params = cursor.statements[0]

        # One guarded-by-RETURNING UPDATE ...
        assert "UPDATE public.documents" in sql
        assert "RETURNING" in sql
        # ... touching exactly visibility and the explicit updated_at
        # (no trigger maintains it).
        assert "visibility = %s" in sql
        assert "updated_at = now()" in sql
        # It must never touch anything else of the row.
        assert "title =" not in sql
        assert "version =" not in sql
        assert "storage_path =" not in sql
        assert "status =" not in sql
        assert "failure_stage =" not in sql
        assert "failure_message =" not in sql
        # A non-transition metadata update: NO status guard exists.
        assert "'processing'" not in sql
        # Fully parameterized; the validated Literal is bound, never
        # interpolated.
        assert params == ["public", _DOCUMENT_ID]

    def test_visibility_update_stays_off_document_chunks(self):
        cursor = _FakeCursor(row=_VISIBILITY_ROW)

        asyncio.run(
            documents_repository.set_document_visibility(
                cursor,  # type: ignore[arg-type]
                document_id=_DOCUMENT_ID,
                visibility="internal",
            )
        )

        # Chunks, chunk content, and embeddings are never touched by a
        # visibility change.
        assert "document_chunks" not in cursor.statements[0][0]

    def test_zero_rows_updated_returns_none(self):
        cursor = _FakeCursor(row=None)

        document = asyncio.run(
            documents_repository.set_document_visibility(
                cursor,  # type: ignore[arg-type]
                document_id=_DOCUMENT_ID,
                visibility="public",
            )
        )

        assert document is None


class TestStatementFailureUnits:
    def test_statement_failure_returns_500_could_not_be_updated(
        self,
        caplog: pytest.LogCaptureFixture,
    ):
        """A failing UPDATE never reaches commit: the statement is
        rolled back on context exit and the database state is
        unchanged."""
        caplog.set_level(logging.DEBUG)

        pool, connection, cursor = _make_service(fail_statement=True)

        with pytest.raises(HTTPException) as error:
            asyncio.run(
                documents_service.update_document_visibility(
                    pool,  # type: ignore[arg-type]
                    document_id=_DOCUMENT_ID,
                    visibility="public",
                )
            )

        assert error.value.status_code == 500
        assert error.value.detail == "Document could not be updated"

        assert connection.commit_calls == 0
        assert connection.rollback_calls >= 1

        messages = "\n".join(record.getMessage() for record in caplog.records)

        assert "database_update" in messages
        assert "RuntimeError" in messages
        # Uncontrolled error text never reaches logs...
        assert "simulated" not in messages
        # ...and storage paths never surface at all.
        assert "uploads/" not in messages

    def test_commit_failure_returns_uncertain_outcome_error(
        self,
        caplog: pytest.LogCaptureFixture,
    ):
        """The UPDATE succeeded but the commit failed: the outcome is
        uncertain — the change may already be applied. The error
        explicitly does NOT claim the database state is unchanged,
        which is why it carries a distinct message."""
        caplog.set_level(logging.DEBUG)

        pool, connection, cursor = _make_service(fail_commit=True)

        with pytest.raises(HTTPException) as error:
            asyncio.run(
                documents_service.update_document_visibility(
                    pool,  # type: ignore[arg-type]
                    document_id=_DOCUMENT_ID,
                    visibility="public",
                )
            )

        assert error.value.status_code == 500

        # Distinct, uncertain-outcome error: never the same message as
        # a statement failure, never a claim of unchange.
        assert error.value.detail == _DETAIL_500_COMMIT["detail"]
        assert error.value.detail != _DETAIL_500_STATEMENT["detail"]

        # The statement ran; the commit was never confirmed.
        assert connection.commit_calls == 0
        assert len(cursor.statements) == 1

        messages = "\n".join(record.getMessage() for record in caplog.records)

        # The commit stage is distinguishable in logs.
        assert "database_commit" in messages
        assert "database_update" not in messages
        assert "RuntimeError" in messages
        assert "simulated" not in messages
        assert "uploads/" not in messages

    def test_success_commits_once_and_returns_updated_row(
        self,
        caplog: pytest.LogCaptureFixture,
    ):
        caplog.set_level(logging.DEBUG)

        pool, connection, cursor = _make_service()

        document = asyncio.run(
            documents_service.update_document_visibility(
                pool,  # type: ignore[arg-type]
                document_id=_DOCUMENT_ID,
                visibility="public",
            )
        )

        assert document["visibility"] == "public"
        assert connection.commit_calls == 1
        assert connection.rollback_calls == 0

        messages = "\n".join(record.getMessage() for record in caplog.records)

        # The success log names the document id (an opaque internal
        # UUID) and the new visibility — never a storage path.
        assert "uploads/" not in messages


class TestUnknownDocumentUnits:
    def test_missing_row_returns_404_and_never_commits(self):
        pool, connection, cursor = _make_service(known_row=False)

        with pytest.raises(HTTPException) as error:
            asyncio.run(
                documents_service.update_document_visibility(
                    pool,  # type: ignore[arg-type]
                    document_id=_DOCUMENT_ID,
                    visibility="public",
                )
            )

        assert error.value.status_code == 404
        assert error.value.detail == "Document not found"

        # No commit; the (no-op) statement is rolled back.
        assert connection.commit_calls == 0
        assert connection.rollback_calls >= 1
