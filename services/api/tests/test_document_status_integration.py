"""Phase 5.7 database integration tests for the status lifecycle.

These tests run against the real local Supabase database and verify
behavior purely in-memory fakes cannot establish:

- The guarded `processing → ready` / `processing → failed` transitions
  really fire on the live table and really refuse to overwrite a newer
  terminal status (zero rows updated → `None`).
- The `ready` transition really clears the failure fields, and the
  Phase 5.7 check constraint
  (`documents_failure_fields_only_when_failed`) really rejects a
  non-`failed` document that carries failure information.
- The persisted failure fields round-trip exactly what the service
  wrote (response ↔ database consistency is covered by the endpoint
  tests); here the transitions are exercised directly on the repo.

Storage, the embedding provider, and any network stay out of scope of
this file entirely.
"""

import asyncio
import os
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.db import create_pool
from app.repositories import documents as documents_repository


_DATABASE_URL = os.environ["DATABASE_URL"]  # set by tests/conftest.py


def _connect() -> psycopg.Connection:
    return psycopg.Connection.connect(_DATABASE_URL, connect_timeout=5)


def _create_document() -> tuple[str, str]:
    """Insert one `processing` document; returns (id-as-str, path)."""
    storage_path = f"uploads/{uuid.uuid4()}.pdf"

    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO public.documents (title, storage_path, visibility)
                VALUES (%s, %s, 'internal')
                RETURNING id
                """,
                ("Status Integration Test", storage_path),
            )
            document_id = str(cursor.fetchone()[0])

    return document_id, storage_path


def _fetch_status(document_id: str) -> dict:
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT status, failure_stage, failure_message
                FROM public.documents
                WHERE id = %s
                """,
                (document_id,),
            )
            status_, stage, message = cursor.fetchone()

    return {
        "status": status_,
        "failure_stage": stage,
        "failure_message": message,
    }


def _delete_document(document_id: str) -> None:
    with _connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "DELETE FROM public.documents WHERE id = %s",
                (document_id,),
            )


def _run_repo_transition(cursor_call, document_id: str, **kwargs):
    """Run one guarded transition through a real pool connection."""

    from psycopg_pool import AsyncConnectionPool

    async def scenario():
        pool = create_pool()
        await pool.open()
        await pool.wait()
        try:
            async with pool.connection() as conn:
                async with conn.cursor() as cursor:
                    return await cursor_call(
                        cursor,
                        document_id=document_id,
                        **kwargs,
                    )
        finally:
            await pool.close()

    return asyncio.run(scenario())


@pytest.fixture
def document_row(client: TestClient) -> str:
    """A real `processing` documents row, cleaned up afterwards.

    Requesting ``client`` pins the same database-availability gating
    the rest of the suite uses.
    """
    document_id, _storage_path = _create_document()
    try:
        yield document_id
    finally:
        _delete_document(document_id)


class TestGuardedTransitionsOnRealDatabase:
    def test_processing_to_failed_persists_stage_and_message(self, document_row):
        updated = _run_repo_transition(
            documents_repository.mark_document_failed,
            document_row,
            stage="embedding_generation",
            message="Embedding generation failed for a document",
        )

        assert updated["status"] == "failed"
        assert updated["failure_stage"] == "embedding_generation"

        row = _fetch_status(document_row)
        assert row["status"] == "failed"
        assert row["failure_stage"] == "embedding_generation"
        assert row["failure_message"] == (
            "Embedding generation failed for a document"
        )

    def test_ready_after_failed_is_a_conflict(self, document_row):
        _run_repo_transition(
            documents_repository.mark_document_failed,
            document_row,
            stage="database_insertion",
            message="The document chunks could not be stored",
        )

        later = _run_repo_transition(
            documents_repository.mark_document_ready,
            document_row,
        )

        # Zero rows transitioned: the newer terminal status wins, no
        # overwrite happened.
        assert later is None

        row = _fetch_status(document_row)
        assert row["status"] == "failed"

    def test_failed_after_ready_is_a_conflict(self, document_row):
        _run_repo_transition(
            documents_repository.mark_document_ready,
            document_row,
        )

        later = _run_repo_transition(
            documents_repository.mark_document_failed,
            document_row,
            stage="storage_download",
            message="The uploaded document could not be downloaded from Storage",
        )

        assert later is None

        row = _fetch_status(document_row)
        assert row["status"] == "ready"
        # The check constraint keeps a `ready` document free of any
        # residual failure information.
        assert row["failure_stage"] is None
        assert row["failure_message"] is None

    def test_transitions_bump_updated_at(self, document_row):
        with _connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT updated_at FROM public.documents WHERE id = %s",
                    (document_row,),
                )
                created_at_updated = cursor.fetchone()[0]

        _run_repo_transition(
            documents_repository.mark_document_failed,
            document_row,
            stage="chunking",
            message="The document produced no chunks; nothing to embed or insert",
        )

        with _connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT updated_at FROM public.documents WHERE id = %s",
                    (document_row,),
                )
                after_updated = cursor.fetchone()[0]

        assert after_updated > created_at_updated


class TestFailureFieldsConstraint:
    def test_failure_fields_on_ready_are_rejected_by_the_database(
        self,
        document_row,
    ):
        # Force a status/field combination the service can never
        # produce; the Phase 5.7 check constraint must refuse it.
        with pytest.raises(psycopg.errors.CheckViolation):
            with _connect() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE public.documents
                        SET status = 'ready',
                            failure_stage = 'storage_download',
                            failure_message = 'should never persist'
                        WHERE id = %s
                        """,
                        (document_row,),
                    )

        # The rejected statement left the row untouched.
        row = _fetch_status(document_row)
        assert row["status"] == "processing"
        assert row["failure_stage"] is None
