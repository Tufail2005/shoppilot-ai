"""Offline unit tests for the Phase 5.8 deletion service.

No local Supabase database, Storage, or Gemini credentials are
required: the Storage facade, the pool/cursor layer, and the document
repository's cursor are faked, mirroring the conventions of
test_ingestion_units.py. Real-database behavior (DELETE ... RETURNING,
the ON DELETE CASCADE, rollback semantics) is covered by
test_admin_document_deletion.py; the real-Storage removal policy runs
against the actual local Storage service in
test_storage_deletion_policy.py.

Focus here: the failure sequencing and sanitization decisions of
``documents_service.delete_document`` that are hard to induce through
the test client — a commit failure AFTER a successful Storage removal
(the documented row-without-object residual), statement failures
BEFORE the removal, the 404-before-any-Storage-call rule, and the log
hygiene policy (stage + exception type only, never paths or messages).
"""

import asyncio
import logging
import uuid

import pytest
from fastapi import HTTPException

from app.repositories import documents as documents_repository
from app.services import documents as documents_service
from app.storage import DOCUMENTS_BUCKET


_DOCUMENT_ID = uuid.UUID("00000000-0000-0000-0000-00000000f08e")
_STORAGE_PATH = f"uploads/{_DOCUMENT_ID}.pdf"

_DETAIL_404 = {"detail": "Document not found"}
_DETAIL_503 = {"detail": "Document storage is temporarily unavailable"}
_DETAIL_500 = {"detail": "Document could not be deleted"}


class _FakeCursor:
    """Records statements; resolves the configured DELETE outcome."""

    def __init__(
        self,
        *,
        rows: dict[str, str] | None = None,
        fail_statement: int | None = None,
    ):
        self.statements: list[tuple[str, list | None]] = []
        self._rows = rows or {}
        self._fail_statement = fail_statement
        self._statement_counter = 0
        self._last_deleted_path: str | None = None

    async def execute(self, sql: str, params: list | None = None) -> None:
        self._statement_counter += 1
        self.statements.append(
            (sql, list(params) if params is not None else None)
        )

        if self._fail_statement == self._statement_counter:
            raise RuntimeError("simulated database failure")

        if params:
            # The only statement is DELETE ... RETURNING storage_path.
            self._last_deleted_path = self._rows.get(str(params[0]))

    async def fetchone(self) -> tuple | None:
        if self._last_deleted_path is None:
            return None

        return (self._last_deleted_path,)

    async def __aenter__(self) -> "_FakeCursor":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _FakeConnection:
    """Mirrors what delete_document exercises of a pool connection.

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


class _FakeStorageBucket:
    def __init__(self, state: "_FakeStorageState"):
        self._state = state

    def remove(self, paths: list[str]) -> list:
        if self._state.fail_removal:
            raise RuntimeError("simulated storage outage during removal")

        self._state.removes.extend(paths)

        # The real Storage API returns metadata for removed objects and
        # [] for missing ones; a fake only needs the recording surface.
        return []


class _FakeStorageState:
    def __init__(self, *, fail_removal: bool = False):
        self.fail_removal = fail_removal
        self.removes: list[str] = []


class _FakeStorage:
    def __init__(self, state: _FakeStorageState):
        self._state = state

    def from_(self, bucket: str) -> _FakeStorageBucket:
        assert bucket == DOCUMENTS_BUCKET
        return _FakeStorageBucket(self._state)


def _make_service(
    *,
    known_row: bool = True,
    fail_statement: int | None = None,
    fail_commit: bool = False,
    fail_removal: bool = False,
) -> tuple:
    cursor = _FakeCursor(
        rows={str(_DOCUMENT_ID): _STORAGE_PATH}
        if known_row else {},
        fail_statement=fail_statement,
    )
    connection = _FakeConnection(cursor, fail_commit=fail_commit)
    pool = _FakePool(connection)
    state = _FakeStorageState(fail_removal=fail_removal)
    storage = _FakeStorage(state)

    return pool, storage, state, connection, cursor


class TestSuccessfulDeletionUnits:
    def test_success_removes_object_once_and_commits_once(
        self,
        caplog: pytest.LogCaptureFixture,
    ):
        caplog.set_level(logging.DEBUG)

        pool, storage, state, connection, cursor = _make_service()

        asyncio.run(
            documents_service.delete_document(
                pool,
                storage,
                document_id=_DOCUMENT_ID,
            )
        )

        # Exactly one Storage removal of exactly the row's path...
        assert state.removes == [_STORAGE_PATH]
        # ... one commit, no rollback (clean exit), ...
        assert connection.commit_calls == 1
        assert connection.rollback_calls == 0
        # ... and a single parameterized DELETE WITHOUT any manual
        # chunk deletion statement from the service or repository.
        assert len(cursor.statements) == 1
        sql, params = cursor.statements[0]
        assert "DELETE FROM public.documents" in sql
        assert "document_chunks" not in sql
        assert params == [_DOCUMENT_ID]

        messages = "\n".join(
            record.getMessage() for record in caplog.records
        )

        # The internal storage path never appears in logs (the log
        # only ever names the document id, an opaque internal UUID).
        assert _STORAGE_PATH not in messages
        assert "uploads/" not in messages


class TestUnknownDocumentUnits:
    def test_missing_row_returns_404_before_any_storage_call(self):
        pool, storage, state, connection, cursor = _make_service(
            known_row=False,
        )

        with pytest.raises(HTTPException) as error:
            asyncio.run(
                documents_service.delete_document(
                    pool,
                    storage,
                    document_id=_DOCUMENT_ID,
                )
            )

        assert error.value.status_code == 404
        assert error.value.detail == "Document not found"

        # No removal, no commit; the (no-op) statement is rolled back.
        assert state.removes == []
        assert connection.commit_calls == 0
        assert connection.rollback_calls >= 1


class TestStorageFailureUnits:
    def test_removal_failure_rolls_back_and_raises_sanitized_503(
        self,
        caplog: pytest.LogCaptureFixture,
    ):
        caplog.set_level(logging.DEBUG)

        pool, storage, state, connection, cursor = _make_service(
            fail_removal=True,
        )

        with pytest.raises(HTTPException) as error:
            asyncio.run(
                documents_service.delete_document(
                    pool,
                    storage,
                    document_id=_DOCUMENT_ID,
                )
            )

        assert error.value.status_code == 503
        assert error.value.detail == "Document storage is temporarily unavailable"

        # Never committed: the row (and cascaded chunks) survive.
        assert connection.commit_calls == 0
        assert connection.rollback_calls >= 1
        # The removal was attempted once; its remote outcome is
        # uncertain by design and the service does not claim either
        # way — only the database side is pinned here (not committed).

        messages = "\n".join(
            record.getMessage() for record in caplog.records
        )

        # Stage + exception type only; no path, no chained message.
        assert "storage_removal" in messages
        assert "RuntimeError" in messages
        assert "simulated" not in messages
        assert _STORAGE_PATH not in messages
        assert "uploads/" not in messages


class TestDatabaseFailureUnits:
    def test_commit_failure_after_removal_returns_sanitized_500(
        self,
        caplog: pytest.LogCaptureFixture,
    ):
        """The committed-object-gone residual: after a successful
        removal the commit fails — a possible row-without-object state
        that a later retry heals (missing-object removal is verified
        idempotent on the real Storage API)."""
        caplog.set_level(logging.DEBUG)

        pool, storage, state, connection, cursor = _make_service(
            fail_commit=True,
        )

        with pytest.raises(HTTPException) as error:
            asyncio.run(
                documents_service.delete_document(
                    pool,
                    storage,
                    document_id=_DOCUMENT_ID,
                )
            )

        assert error.value.status_code == 500
        assert error.value.detail == "Document could not be deleted"

        # The removal was attempted (its remote result may already be
        # applied); the database side never reported success.
        assert state.removes == [_STORAGE_PATH]
        assert connection.commit_calls == 0

        messages = "\n".join(
            record.getMessage() for record in caplog.records
        )

        assert "database_deletion" in messages
        assert "RuntimeError" in messages
        assert "simulated" not in messages
        assert _STORAGE_PATH not in messages

    def test_statement_failure_never_reaches_storage(self):
        pool, storage, state, connection, cursor = _make_service(
            fail_statement=1,
        )

        with pytest.raises(HTTPException) as error:
            asyncio.run(
                documents_service.delete_document(
                    pool,
                    storage,
                    document_id=_DOCUMENT_ID,
                )
            )

        assert error.value.status_code == 500
        assert error.value.detail == "Document could not be deleted"

        # The removal never happened: the statement failed first.
        assert state.removes == []
        assert connection.commit_calls == 0
        assert connection.rollback_calls >= 1


class TestRepositoryDeletionContract:
    def test_delete_is_one_parameterized_statement_without_chunk_deletion(self):
        cursor = _FakeCursor(rows={str(_DOCUMENT_ID): _STORAGE_PATH})

        returned = asyncio.run(
            documents_repository.delete_document(
                cursor,  # type: ignore[arg-type]
                document_id=_DOCUMENT_ID,
            )
        )

        assert returned == _STORAGE_PATH
        assert len(cursor.statements) == 1

        sql, params = cursor.statements[0]
        assert "DELETE FROM public.documents" in sql
        assert "RETURNING storage_path" in sql
        assert "document_chunks" not in sql
        # Fully parameterized: the id arrives as a bound parameter, so
        # the statement never embeds a literal value (the placeholder
        # itself is fine).
        assert params == [_DOCUMENT_ID]

    def test_delete_returns_none_for_unknown_id(self):
        cursor = _FakeCursor(rows={})

        returned = asyncio.run(
            documents_repository.delete_document(
                cursor,  # type: ignore[arg-type]
                document_id=_DOCUMENT_ID,
            )
        )

        assert returned is None
