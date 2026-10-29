"""Offline unit tests for the Phase 5.7 guarded status transitions.

These tests run entirely in memory (no database): the repository
functions are exercised through a fake async cursor that records the
parameterized statements they issue, so the SQL contract itself —
the `status = 'processing'` guard, the explicit `updated_at` bump,
the cleared failure fields on `ready`, the parameter binding of the
sanitized failure fields, and the zero-row → `None` result — is
verified without any database.
"""

import asyncio
import uuid

from app.repositories import documents as documents_repository


_DOCUMENT_ID = uuid.uuid4()

_FAKE_UPDATED_ROW = (
    _DOCUMENT_ID,
    "Return Policy",
    "uploads/00000000-0000-0000-0000-000000000001.pdf",
    "internal",
    "ready",
    1,
    None,
    None,
    "2026-10-26T00:00:00+00:00",
)


class FakeDescription:
    # Mirrors cursor.description: each entry has a `.name`.
    def __init__(self, name: str):
        self.name = name


class RecordingCursor:
    """Records statements, returns a configurable single row."""

    def __init__(self, *, row: tuple | None, fail: bool = False):
        self.statements: list[tuple[str, list | None]] = []
        self._row = row
        self._fail = fail
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
        self.statements.append((sql, params))

        if self._fail:
            raise RuntimeError("simulated database failure")

    async def fetchone(self):
        return self._row


def _assert_single_parameterized_update(cursor: RecordingCursor):
    assert len(cursor.statements) == 1

    sql, params = cursor.statements[0]
    # No client input is ever interpolated into the SQL text: when
    # parameters exist they are all bound through placeholder lists.
    assert params is not None

    # The guarded transition policy: only a `processing` row can move.
    assert "status = 'processing'" in sql
    # No trigger maintains it; the update bumps it explicitly.
    assert "updated_at" in sql

    return sql, params


class TestMarkDocumentReady:
    def test_issues_guarded_update_clearing_failure_fields(self):
        cursor = RecordingCursor(row=_FAKE_UPDATED_ROW)

        document = asyncio.run(
            documents_repository.mark_document_ready(
                cursor,
                document_id=_DOCUMENT_ID,
            )
        )

        sql, params = _assert_single_parameterized_update(cursor)

        assert document["id"] == _DOCUMENT_ID
        assert document["status"] == "ready"
        assert document["failure_stage"] is None
        assert document["failure_message"] is None

        # failure fields are cleared inside the statement itself.
        assert "failure_stage = NULL" in sql
        assert "failure_message = NULL" in sql
        # Ready is written as the enum literal.
        assert "status = 'ready'" in sql
        # Exactly one parameter: the document id.
        assert params == [_DOCUMENT_ID]

    def test_zero_rows_transitioned_returns_none(self):
        cursor = RecordingCursor(row=None)

        document = asyncio.run(
            documents_repository.mark_document_ready(
                cursor,
                document_id=_DOCUMENT_ID,
            )
        )

        assert document is None


class TestMarkDocumentFailed:
    def test_binds_sanitized_fields_as_parameters(self):
        row = (
            _FAKE_UPDATED_ROW[:4]
            + ("failed", _FAKE_UPDATED_ROW[5])
            + ("database_insertion", "The document chunks could not be stored")
            + (_FAKE_UPDATED_ROW[8],)
        )
        cursor = RecordingCursor(row=row)

        document = asyncio.run(
            documents_repository.mark_document_failed(
                cursor,
                document_id=_DOCUMENT_ID,
                stage="database_insertion",
                message="The document chunks could not be stored",
            )
        )

        sql, params = _assert_single_parameterized_update(cursor)

        assert document["id"] == _DOCUMENT_ID
        assert document["status"] == "failed"
        assert document["failure_stage"] == "database_insertion"
        assert document["failure_message"] == (
            "The document chunks could not be stored"
        )

        assert "status = 'failed'" in sql
        # Parameters: stage, message, document_id — all bound, never
        # interpolated.
        assert list(params) == [
            "database_insertion",
            "The document chunks could not be stored",
            _DOCUMENT_ID,
        ]

    def test_zero_rows_transitioned_returns_none(self):
        cursor = RecordingCursor(row=None)

        document = asyncio.run(
            documents_repository.mark_document_failed(
                cursor,
                document_id=_DOCUMENT_ID,
                stage="database_insertion",
                message="The document chunks could not be stored",
            )
        )

        assert document is None


class TestRowMapping:
    def test_returned_document_has_schema_column_names(self):
        cursor = RecordingCursor(row=_FAKE_UPDATED_ROW)

        document = asyncio.run(
            documents_repository.mark_document_ready(
                cursor,
                document_id=_DOCUMENT_ID,
            )
        )

        assert set(document) == {
            "id",
            "title",
            "storage_path",
            "visibility",
            "status",
            "version",
            "failure_stage",
            "failure_message",
            "created_at",
        }
