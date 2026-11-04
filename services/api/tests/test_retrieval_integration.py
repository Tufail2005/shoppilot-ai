"""Database integration tests for Phase 6.1 retrieval.

Run against the local Supabase database the rest of the suite requires
(see ``tests/conftest.py``): the real connection pool, the real
``document_chunks``/``documents`` tables, and the real pgvector
``<=>`` operator over the existing ivfflat cosine index. The embedding
provider stays faked with deterministic 768-dimensional vectors, so no
Gemini network access happens here.

What SQL fixtures establish (mechanics, not semantics — semantic
relevance and threshold calibration are Phase 15 work):

- the visibility/status WHERE clause really decides which chunks can
  be returned (internal documents never leak to customers),
- ``processing``/``failed`` documents are excluded even when their
  chunks would be the closest matches,
- the fixed ``LIMIT 5`` caps the result, and
- pgvector orders by cosine distance at all — with crafted vectors, not
  "meaningful text": a distance-0 twin, an orthogonal vector, and a
  deliberately-worst opposite vector define the expected order.

All rows use unique titles/paths and are deleted in ``finally``.
"""

import asyncio
import os
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.db import create_pool
from app.repositories import retrieval as retrieval_repository
from app.services import retrieval as retrieval_service
from app.services.embeddings import EMBEDDING_DIMENSIONS


_DATABASE_URL = os.environ["DATABASE_URL"]  # set by tests/conftest.py


# The query embedding every scenario uses: a unit vector along axis 0.
_QUERY_VECTOR = [1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1)

# Crafted chunk vectors define the cosine geometry:
#   aligned     -> distance 0 (best possible match)
#   orthogonal  -> distance 1 (irrelevant)
_ALIGNED = list(_QUERY_VECTOR)
_ORTHOGONAL = [0.0, 1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 2)


def _literal(vector: list[float]) -> str:
    return "[" + ",".join(repr(float(value)) for value in vector) + "]"


def _connect() -> psycopg.Connection:
    return psycopg.Connection.connect(_DATABASE_URL, connect_timeout=5)


class SeededDocs:
    """Fixture state: document/chunk ids grouped by role of the row."""

    def __init__(self):
        self.documents: dict[str, dict] = {}
        self.chunks: list[dict] = []

    def add_document(self, name: str, document_id: uuid.UUID, visibility: str, status: str):
        self.documents[name] = {
            "id": document_id,
            "visibility": visibility,
            "status": status,
        }

    def add_chunk(self, document_name: str, chunk_id: uuid.UUID, chunk_index: int, kind: str):
        self.chunks.append(
            {
                "document": document_name,
                "id": chunk_id,
                "index": chunk_index,
                "distance": 0.0 if kind == "aligned" else 1.0,
            }
        )

    def chunks_of(self, document_name: str) -> list[dict]:
        return [c for c in self.chunks if c["document"] == document_name]

    def document_ids(self, *names: str) -> list[uuid.UUID]:
        return [self.documents[name]["id"] for name in names]


def _create_document(*, visibility: str, status: str) -> uuid.UUID:
    with _connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO public.documents (title, storage_path, visibility, status)
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                (
                    f"Retrieval IT {uuid.uuid4()}",
                    f"uploads/{uuid.uuid4()}.pdf",
                    visibility,
                    status,
                ),
            )
            return cursor.fetchone()[0]


def _insert_chunks(document_id: uuid.UUID, rows: list[tuple[int, list[float]]]) -> list[uuid.UUID]:
    """Insert real chunk rows and return their generated ids, in order."""

    with _connect() as connection:
        with connection.cursor() as cursor:
            ids: list[uuid.UUID] = []

            for chunk_index, vector in rows:
                cursor.execute(
                    """
                    INSERT INTO public.document_chunks (
                        document_id, content, embedding, chunk_index
                    )
                    VALUES (%s, %s, %s::extensions.vector, %s)
                    RETURNING id
                    """,
                    (document_id, f"chunk-{chunk_index}", _literal(vector), chunk_index),
                )
                ids.append(cursor.fetchone()[0])

    return ids


def _delete_documents(document_ids: list[uuid.UUID]) -> None:
    with _connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM public.documents WHERE id = ANY(%s)",
                [document_ids],
            )


def _seed_documents() -> SeededDocs:
    """Create the Phase 6.1 retrieval matrix as real rows.

    - ``public_ready``: 6 chunks (2 aligned, 4 orthogonal) — more than
      the retrieval limit, so the LIMIT is observable for customers.
    - ``internal_ready``: 2 chunks, one of them aligned — the best
      possible match an unauthorized role must NOT see.
    - ``public_processing``: 1 aligned chunk.
    - ``public_failed``: 1 aligned chunk.

    Plans/statuses are set explicitly on insert, so no phase-specific
    pipeline runs; the fixture cleans everything up afterwards.
    """

    seeded = SeededDocs()

    public_id = _create_document(visibility="public", status="ready")
    internal_id = _create_document(visibility="internal", status="ready")
    processing_id = _create_document(visibility="public", status="processing")
    failed_id = _create_document(visibility="public", status="failed")

    public_chunks = _insert_chunks(
        public_id,
        [
            (0, _ALIGNED),
            (1, _ORTHOGONAL),
            (2, _ALIGNED),
            (3, _ORTHOGONAL),
            (4, _ORTHOGONAL),
            (5, _ORTHOGONAL),
        ],
    )
    internal_chunks = _insert_chunks(
        internal_id,
        [(0, _ALIGNED), (1, _ORTHOGONAL)],
    )
    processing_chunks = _insert_chunks(processing_id, [(0, _ALIGNED)])
    failed_chunks = _insert_chunks(failed_id, [(0, _ALIGNED)])

    seeded.add_document("public_ready", public_id, "public", "ready")
    seeded.add_document("internal_ready", internal_id, "internal", "ready")
    seeded.add_document("public_processing", processing_id, "public", "processing")
    seeded.add_document("public_failed", failed_id, "public", "failed")

    for index, chunk_id in enumerate(public_chunks):
        seeded.add_chunk(
            "public_ready", chunk_id, index, "aligned" if index in (0, 2) else "orthogonal"
        )
    seeded.add_chunk("internal_ready", internal_chunks[0], 0, "aligned")
    seeded.add_chunk("internal_ready", internal_chunks[1], 1, "orthogonal")
    seeded.add_chunk("public_processing", processing_chunks[0], 0, "aligned")
    seeded.add_chunk("public_failed", failed_chunks[0], 0, "aligned")

    return seeded


def _delete_seeded(seeded: SeededDocs) -> None:
    _delete_documents(seeded.document_ids("public_ready", "internal_ready", "public_processing", "public_failed"))


class _StubQueryProvider:
    """Embeds every query as the crafted 768-dim query vector."""

    def __init__(self, vector: list[float]):
        self.vector = vector
        self.calls: list[list[str]] = []

    def embed_query(self, texts, *, cancel_event=None):
        self.calls.append(list(texts))
        return [list(self.vector) for _ in texts]

    def embed_documents(self, texts, *, cancel_event=None):
        raise AssertionError("retrieval must only call embed_query")


def _run_retrieve(provider, *, role) -> list[dict]:
    async def scenario():
        pool = create_pool()
        await pool.open()
        await pool.wait()
        try:
            return await retrieval_service.retrieve(
                pool,
                provider,
                query="integration retrieval probe",
                user_role=role,
            )
        finally:
            await pool.close()

    return asyncio.run(scenario())


@pytest.fixture
def retrieval_fixture(client: TestClient) -> SeededDocs:
    """Seeded documents with cleanup, on the gated test database."""

    seeded = _seed_documents()
    try:
        yield seeded
    finally:
        _delete_seeded(seeded)


class TestRoleVisibilityMatrix:
    def test_customer_retrieves_public_ready_chunks_only(self, retrieval_fixture):
        seeded = retrieval_fixture

        result = _run_retrieve(_StubQueryProvider(_QUERY_VECTOR), role="customer")

        assert result, "an aligned public ready chunk exists and must be found"
        returned_document_ids = {chunk["document_id"] for chunk in result}

        # Only the public ready document may appear.
        assert returned_document_ids == {seeded.documents["public_ready"]["id"]}
        for chunk in result:
            assert chunk["visibility"] == "public"
            assert chunk["title"].startswith("Retrieval IT ")

        # The crafted best matches (distance 0) of that document are found.
        aligned_ids = [
            c["id"] for c in seeded.chunks_of("public_ready") if c["distance"] == 0.0
        ]
        assert {chunk["id"] for chunk in result} & set(aligned_ids)

    def test_customer_cannot_retrieve_internal_chunks(self, retrieval_fixture):
        seeded = retrieval_fixture
        internal_chunk_ids = {c["id"] for c in seeded.chunks_of("internal_ready")}

        result = _run_retrieve(_StubQueryProvider(_QUERY_VECTOR), role="customer")

        # The internal document contains a distance-0 twin of the query:
        # a perfect match is still hidden by the SQL visibility filter.
        assert all(chunk["id"] not in internal_chunk_ids for chunk in result)
        assert all(
            chunk["visibility"] == "public" and chunk["document_id"] != seeded.documents["internal_ready"]["id"]
            for chunk in result
        )

    @pytest.mark.parametrize("role", ["support_agent", "admin"])
    def test_staff_roles_retrieve_public_and_internal_chunks(self, role, retrieval_fixture):
        seeded = retrieval_fixture

        result = _run_retrieve(_StubQueryProvider(_QUERY_VECTOR), role=role)

        returned_document_ids = {chunk["document_id"] for chunk in result}
        assert seeded.documents["public_ready"]["id"] in returned_document_ids
        assert seeded.documents["internal_ready"]["id"] in returned_document_ids

        # The internal document's distance-0 twin is retrieved by staff.
        internal_aligned = seeded.chunks_of("internal_ready")[0]["id"]
        assert internal_aligned in {chunk["id"] for chunk in result}


class TestStatusRestrictions:
    def test_processing_documents_are_excluded(self, retrieval_fixture):
        seeded = retrieval_fixture
        processing_id = seeded.documents["public_processing"]["id"]

        result = _run_retrieve(_StubQueryProvider(_QUERY_VECTOR), role="admin")

        assert all(chunk["document_id"] != processing_id for chunk in result)

    def test_failed_documents_are_excluded(self, retrieval_fixture):
        seeded = retrieval_fixture
        failed_id = seeded.documents["public_failed"]["id"]

        result = _run_retrieve(_StubQueryProvider(_QUERY_VECTOR), role="admin")

        assert all(chunk["document_id"] != failed_id for chunk in result)


class TestLimitAndOrdering:
    def test_no_more_than_five_chunks_are_returned(self, retrieval_fixture):
        # The public ready document alone has 6 chunks: the fixed
        # server-side limit must cap the result at 5.
        result = _run_retrieve(_StubQueryProvider(_QUERY_VECTOR), role="customer")

        assert 1 <= len(result) <= 5
        assert len(result) == 5

    def test_ordering_follows_distance_with_deterministic_tie_breaks(
        self, retrieval_fixture
    ):
        seeded = retrieval_fixture
        ready_chunks = [c for c in seeded.chunks if c["document"] in ("public_ready", "internal_ready")]

        result = _run_retrieve(_StubQueryProvider(_QUERY_VECTOR), role="admin")

        # Actual results are non-increasing in similarity (cosine
        # distance ascending).
        distances = [chunk["distance"] for chunk in result]
        assert distances == sorted(distances)

        # And the exact expected order including deterministic
        # tie-breakers (distance, document_id, chunk_index).
        expected_order = sorted(
            ready_chunks,
            key=lambda c: (c["distance"], seeded.documents[c["document"]]["id"], c["index"]),
        )[:5]
        assert [chunk["id"] for chunk in result] == [c["id"] for c in expected_order]


class TestContractsAndEdgeCases:
    def test_retrieval_returns_citation_ready_fields(self, retrieval_fixture):
        result = _run_retrieve(_StubQueryProvider(_QUERY_VECTOR), role="customer")

        expected_keys = {
            "id", "document_id", "title", "visibility", "chunk_index",
            "content", "distance",
        }
        assert all(set(chunk) == expected_keys for chunk in result)

    def test_supported_roles_fail_closed_for_unsupported_role(self, retrieval_fixture):
        with pytest.raises(retrieval_service.RetrievalError) as excinfo:
            _run_retrieve(_StubQueryProvider(_QUERY_VECTOR), role="anonymous")

        assert excinfo.value.stage == "role_validation"

    def test_repository_returns_empty_list_when_nothing_matches(self):
        # A visibility set matching no documents returns an empty
        # collection, not an error — with the real SQL, pool, and
        # pgvector operator in the loop.
        async def scenario():
            pool = create_pool()
            await pool.open()
            await pool.wait()
            try:
                async with pool.connection() as conn:
                    async with conn.cursor() as cursor:
                        return await retrieval_repository.retrieve_chunks(
                            cursor,
                            query_embedding=list(_QUERY_VECTOR),
                            # A visibility value that never exists in
                            # the schema enum: nothing can match.
                            visibility=["unknown"],
                        )
            finally:
                await pool.close()

        result = asyncio.run(scenario())

        assert result == []

    def test_vector_column_enforces_the_768_dimension_contract(self, client):
        # A 767-dimension query embedding violates the vector(768)
        # contract and must be rejected by pgvector, never answered
        # with a made-up similarity order.
        short_vector = [1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 2)

        async def scenario():
            pool = create_pool()
            await pool.open()
            await pool.wait()
            try:
                async with pool.connection() as conn:
                    async with conn.cursor() as cursor:
                        return await retrieval_repository.retrieve_chunks(
                            cursor,
                            query_embedding=short_vector,
                            visibility=["public"],
                        )
            finally:
                await pool.close()

        with pytest.raises(psycopg.Error):
            asyncio.run(scenario())
