"""Database integration tests for the Phase 5.6 ingestion service.

These tests run against the local Supabase database — the stack the
rest of the suite already requires (see tests/conftest.py) — and
exercise the real psycopg_pool connection pool and the real
``document_chunks`` table. Storage and the embedding provider remain
faked, so no Storage or Gemini network access is needed.

The real database establishes behavior that offline fake cursors
cannot:

- validated embedding vectors are accepted by the
  ``%s::extensions.vector`` cast with vector(768) dimensionality,
- replace-on-ingestion never leaves duplicate chunks (the
  ``unique (document_id, chunk_index)`` constraint holds),
- a mid-transaction failure rolls back completely on a real pool
  connection — no partial chunk set survives and the document stays
  cleanly re-ingestible.
"""

import asyncio
import os
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.db import create_pool
from app.repositories import documents as documents_repository
from app.services.chunking import chunk_text
from app.services.documents import extract_text
from app.services.ingestion import IngestionError, ingest_document
from tests.ingestion_fakes import (
    FakeEmbeddingProvider,
    FakeStorageClient,
    FakeStorageState,
)
from tests.test_documents_units import _build_pdf


_BATCH_SIZE = 2
_DATABASE_URL = os.environ["DATABASE_URL"]  # set by tests/conftest.py


def _page_text(word_count: int) -> str:
    return " ".join(f"w{number}" for number in range(word_count))


# The expected chunk flow comes from the real Phase 5.3/5.4 building
# blocks, so the ingestion assertions never depend on heuristic tuning.
_PDF_BYTES = _build_pdf([_page_text(4200)])
_EXPECTED_CHUNKS = chunk_text(extract_text(_PDF_BYTES))
assert len(_EXPECTED_CHUNKS) >= 5  # premise: several batches at size 2


def _connect() -> psycopg.Connection:
    return psycopg.Connection.connect(_DATABASE_URL, connect_timeout=5)


def _create_document(storage_path: str) -> uuid.UUID:
    with _connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO public.documents (title, storage_path, visibility)
                VALUES (%s, %s, 'internal')
                RETURNING id
                """,
                ("Ingestion Integration Test", storage_path),
            )
            document_id = cursor.fetchone()[0]

    return document_id


def _delete_document(document_id: uuid.UUID) -> None:
    with _connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM public.documents WHERE id = %s",
                [document_id],
            )


def _count_chunks(document_id: uuid.UUID) -> int:
    with _connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT count(*)
                FROM public.document_chunks
                WHERE document_id = %s
                """,
                [document_id],
            )
            return cursor.fetchone()[0]


def _load_chunks(document_id: uuid.UUID) -> list[dict]:
    """Chunks ordered by chunk_index with embedding diagnostics."""
    with _connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    chunk_index,
                    content,
                    embedding IS NOT NULL,
                    vector_dims(embedding)
                FROM public.document_chunks
                WHERE document_id = %s
                ORDER BY chunk_index
                """,
                [document_id],
            )
            rows = cursor.fetchall()

    return [
        {
            "chunk_index": row[0],
            "content": row[1],
            "has_embedding": row[2],
            "dimensions": row[3],
        }
        for row in rows
    ]


def _load_embedding_texts(document_id: uuid.UUID) -> list[str]:
    with _connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT embedding::text
                FROM public.document_chunks
                WHERE document_id = %s
                ORDER BY chunk_index
                """,
                [document_id],
            )
            rows = cursor.fetchall()

    return [row[0] for row in rows]


def _run_ingest(
    document_id: uuid.UUID,
    storage_path: str,
    provider: FakeEmbeddingProvider,
    **kwargs,
) -> int:
    kwargs.setdefault("batch_size", _BATCH_SIZE)

    state = FakeStorageState()
    state.objects[storage_path] = _PDF_BYTES
    storage = FakeStorageClient(state)

    async def scenario():
        # A real pool per run: connection lifecycle and transaction
        # rollback behave exactly as in the running service.
        pool = create_pool()
        await pool.open()
        await pool.wait()
        try:
            return await ingest_document(
                pool,
                storage,
                provider,
                document_id=document_id,
                storage_path=storage_path,
                **kwargs,
            )
        finally:
            await pool.close()

    return asyncio.run(scenario())


@pytest.fixture
def document_row(client: TestClient) -> tuple[uuid.UUID, str]:
    """A real documents row, cleaned up afterwards.

    Requesting ``client`` pins the database-availability gating that
    the rest of the suite uses.
    """
    storage_path = f"uploads/{uuid.uuid4()}.pdf"
    document_id = _create_document(storage_path)
    try:
        yield document_id, storage_path
    finally:
        _delete_document(document_id)


class TestSuccessfulIngestion:
    def test_inserts_all_chunks_with_contiguous_indices_and_768_embeddings(
        self,
        document_row,
    ):
        document_id, storage_path = document_row

        inserted = _run_ingest(document_id, storage_path, FakeEmbeddingProvider())

        assert inserted == len(_EXPECTED_CHUNKS)

        chunks = _load_chunks(document_id)

        assert [chunk["chunk_index"] for chunk in chunks] == list(
            range(len(_EXPECTED_CHUNKS))
        )
        assert [chunk["content"] for chunk in chunks] == _EXPECTED_CHUNKS
        assert all(chunk["has_embedding"] for chunk in chunks)
        assert all(chunk["dimensions"] == 768 for chunk in chunks)


class TestDuplicateIngestion:
    def test_replacement_semantics_leave_no_duplicates(self, document_row):
        document_id, storage_path = document_row

        first = _run_ingest(document_id, storage_path, FakeEmbeddingProvider(offset=1.0))
        assert first == len(_EXPECTED_CHUNKS)
        assert _count_chunks(document_id) == len(_EXPECTED_CHUNKS)
        embeddings_before = _load_embedding_texts(document_id)

        # A second ingestion with different embeddings must replace the
        # previous chunk set: no unique-violation, no duplicate rows.
        second = _run_ingest(document_id, storage_path, FakeEmbeddingProvider(offset=2.0))

        assert second == len(_EXPECTED_CHUNKS)
        assert _count_chunks(document_id) == len(_EXPECTED_CHUNKS)

        chunks = _load_chunks(document_id)
        assert [chunk["chunk_index"] for chunk in chunks] == list(
            range(len(_EXPECTED_CHUNKS))
        )

        # The stored embeddings really were replaced, not merged.
        embeddings_after = _load_embedding_texts(document_id)
        assert len(embeddings_after) == len(embeddings_before)
        assert embeddings_after != embeddings_before


class TestFailureAtomicity:
    def test_partial_insert_then_failure_leaves_no_chunks(self, document_row):
        document_id, storage_path = document_row

        partial_inserts: list[int] = []

        async def partial_then_fail(cursor, *, document_id, chunks):
            # Perform REAL partial inserts inside the ingestion
            # service's live transaction, then force a failure.
            for position in (0, 1):
                await cursor.execute(
                    """
                    INSERT INTO public.document_chunks (
                        document_id,
                        content,
                        chunk_index
                    )
                    VALUES (%s, %s, %s)
                    """,
                    [document_id, f"partial-{position}", position],
                )
                partial_inserts.append(position)

            raise RuntimeError("simulated insertion failure after partial writes")

        original = documents_repository.insert_document_chunks
        documents_repository.insert_document_chunks = partial_then_fail
        try:
            with pytest.raises(IngestionError):
                _run_ingest(document_id, storage_path, FakeEmbeddingProvider())
        finally:
            documents_repository.insert_document_chunks = original

        # The partial writes really executed inside the transaction.
        assert partial_inserts == [0, 1]

        # The real pool connection rolled the transaction back: zero
        # partial rows survived, even though the DELETE/INSERT flow
        # never completed.
        assert _count_chunks(document_id) == 0

        # A clean retry succeeds: the rollback left no residue that
        # could collide with unique (document_id, chunk_index).
        inserted = _run_ingest(document_id, storage_path, FakeEmbeddingProvider())
        assert inserted == len(_EXPECTED_CHUNKS)

        chunks = _load_chunks(document_id)
        assert [chunk["chunk_index"] for chunk in chunks] == list(
            range(len(_EXPECTED_CHUNKS))
        )
        assert [chunk["content"] for chunk in chunks] == _EXPECTED_CHUNKS


class TestSchemaContract:
    def test_document_chunks_rejects_wrong_dimension_embedding(self, document_row):
        document_id, _ = document_row

        # A 3-value "embedding" must be refused by the vector(768)
        # column's typmod even if unvalidated input reached SQL.
        with pytest.raises(psycopg.Error):
            with _connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO public.document_chunks (
                            document_id,
                            content,
                            embedding,
                            chunk_index
                        )
                        VALUES (%s, %s, %s::extensions.vector, %s)
                        """,
                        [document_id, "mismatched", "[1,2,3]", 0],
                    )

        assert _count_chunks(document_id) == 0
