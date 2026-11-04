"""Local Supabase integration tests for POST /rag/query (Phase 6.4).

Run against the local Supabase stack the rest of the suite requires
(``tests/conftest.py``): the real connection pool, the real role
resolution through ``require_role`` (``public.profiles``), and the real
pgvector ``<=>`` query over ``documents``/``document_chunks``. Both
Gemini providers are faked through the endpoint's own FastAPI
dependencies, so no embedding or generation network call is ever made.

Documents/chunks are seeded with crafted 768-dimensional vectors
exactly like ``tests/test_retrieval_integration.py``: the query vector
has distance-0 twins in a public ``ready`` document and in an internal
``ready`` document, so the visibility filter (not relevance) decides
what can reach each role. Rows use unique titles/paths and are deleted
in ``finally``.

The empty-retrieval endpoint test replaces the repository's
``retrieve_chunks`` with an empty result (retrieval has no similarity
threshold, so any visible ready chunk would otherwise always be
returned); the full pipeline above it — embedding, prompt building, and
the no-generation decision — still runs untouched.
"""

import os
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.repositories import retrieval as retrieval_repository
from app.services import rag as rag_service
from app.services.embeddings import EMBEDDING_DIMENSIONS, EmbeddingError
from app.services.generation import GenerationError
from app.services.rag_prompt import INSUFFICIENT_EVIDENCE_MESSAGE


_DATABASE_URL = os.environ["DATABASE_URL"]  # set by tests/conftest.py

_QUERY = "How long do returns take?"

_QUERY_VECTOR = [1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1)

_ALIGNED = list(_QUERY_VECTOR)
_ORTHOGONAL = [0.0, 1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 2)

_CANNED_ANSWER = "Grounded answer. [1]"


def _connect() -> psycopg.Connection:
    return psycopg.Connection.connect(_DATABASE_URL, connect_timeout=5)


def _create_document(*, visibility: str) -> uuid.UUID:
    with _connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO public.documents (title, storage_path, visibility, status)
                VALUES (%s, %s, %s, 'ready')
                RETURNING id
                """,
                (
                    (
                        f"Return Policy probe {uuid.uuid4()}"
                        if visibility == "public"
                        else f"Ops Handbook probe {uuid.uuid4()}"
                    ),
                    f"uploads/{uuid.uuid4()}.pdf",
                    visibility,
                ),
            )
            return cursor.fetchone()[0]


def _insert_chunks(
    document_id: uuid.UUID,
    rows: list[tuple[int, list[float]]],
) -> list[uuid.UUID]:
    """Insert real chunk rows (real embeddings) and return their ids."""

    def _literal(vector: list[float]) -> str:
        return "[" + ",".join(repr(float(value)) for value in vector) + "]"

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
                    (
                        document_id,
                        f"probe content {chunk_index}",
                        _literal(vector),
                        chunk_index,
                    ),
                )
                ids.append(cursor.fetchone()[0])

    return ids


class Seed:
    """Fixture state for the role-visibility matrix."""

    def __init__(self):
        self.public_id = _create_document(visibility="public")
        self.internal_id = _create_document(visibility="internal")

        # Public: 6 chunks (2 distance-0 twins + 4 orthogonal). Internal:
        # 2 chunks, one of them a distance-0 twin the customer must
        # never see.
        self._public_chunks = _insert_chunks(
            self.public_id,
            [
                (0, _ALIGNED),
                (1, _ORTHOGONAL),
                (2, _ALIGNED),
                (3, _ORTHOGONAL),
                (4, _ORTHOGONAL),
                (5, _ORTHOGONAL),
            ],
        )
        self._internal_chunks = _insert_chunks(
            self.internal_id,
            [(0, _ALIGNED), (1, _ORTHOGONAL)],
        )

    @property
    def all_chunk_ids(self) -> list[uuid.UUID]:
        return self._public_chunks + self._internal_chunks

    @property
    def aligned_chunk_ids(self) -> list[uuid.UUID]:
        """The distance-0 chunks: two public, one internal."""

        return [self._public_chunks[0], self._public_chunks[2]] + [
            self._internal_chunks[0]
        ]

    def delete(self) -> None:
        with _connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM public.documents WHERE id = ANY(%s)",
                    ([self.public_id, self.internal_id],),
                )


@pytest.fixture
def kb(client: TestClient) -> Seed:
    """Seeded public/internal ready documents with cleanup."""

    seeded = Seed()
    try:
        yield seeded
    finally:
        seeded.delete()


class StubQueryProvider:
    """Embeds every query as the crafted 768-dim query vector."""

    def __init__(self, *, vector: list[float], error=None):
        self.vector = vector
        self.error = error
        self.calls: list[list[str]] = []

    def embed_query(self, texts, *, cancel_event=None):
        self.calls.append(list(texts))

        if self.error is not None:
            raise self.error

        return [list(self.vector) for _ in texts]

    def embed_documents(self, texts, *, cancel_event=None):
        raise AssertionError("retrieval must only call embed_query")


class StubGenerationProvider:
    """Records generate requests; replays a canned answer or error."""

    def __init__(self, *, answer: str = _CANNED_ANSWER, error=None):
        self.requests: list = []
        self.answer = answer
        self.error = error

    def generate(self, prompt, *, cancel_event=None) -> str:
        self.requests.append(prompt)

        if self.error is not None:
            raise self.error

        return self.answer


class Providers:
    """Harness for the two faked Gemini provider dependencies."""

    def __init__(self):
        self.embedding = StubQueryProvider(vector=_QUERY_VECTOR)
        self.generation = StubGenerationProvider()

    def install(self) -> None:
        app.dependency_overrides[
            rag_service.get_query_embedding_provider
        ] = lambda: self.embedding
        app.dependency_overrides[
            rag_service.get_generation_provider
        ] = lambda: self.generation

    def restore(self) -> None:
        app.dependency_overrides.pop(
            rag_service.get_query_embedding_provider, None
        )
        app.dependency_overrides.pop(
            rag_service.get_generation_provider, None
        )


@pytest.fixture
def providers(client: TestClient) -> Providers:
    """Gemini faked through the endpoint's own provider dependencies."""

    harness = Providers()
    harness.install()

    try:
        yield harness
    finally:
        harness.restore()


def _post(client: TestClient, payload: dict, headers: dict | None = None):
    return client.post("/rag/query", json=payload, headers=headers)


# ---------------------------------------------------------------------------
# Role access and visibility
# ---------------------------------------------------------------------------


class TestRoleAccess:
    def test_customer_receives_public_only_citations(
        self, customer_client: TestClient, providers: Providers, kb: Seed
    ):
        response = _post(customer_client, {"query": _QUERY})

        assert response.status_code == 200
        body = response.json()
        assert body["answer"] == _CANNED_ANSWER
        assert body["citations"], "a distance-0 public chunk exists"

        # The internal document holds a distance-0 twin of the query; the
        # SQL visibility filter — not relevance — must hide it entirely.
        assert all(
            citation["document_id"] == str(kb.public_id)
            for citation in body["citations"]
        )

        assert body["citations"][0]["number"] == 1
        assert body["citations"][0]["document_title"].startswith(
            "Return Policy probe"
        )

        # The internal document never reached the prompt either.
        prompt = providers.generation.requests[0]
        assert not any(
            source.document_title.startswith("Ops Handbook probe")
            for source in prompt.sources
        )
        assert "Ops Handbook probe" not in prompt.sources_text

    @pytest.mark.parametrize(
        "role_fixture_name",
        ["agent_client", "admin_client"],
    )
    def test_staff_roles_can_reach_internal_documents(
        self, request, providers: Providers, kb: Seed, role_fixture_name
    ):
        client: TestClient = request.getfixturevalue(role_fixture_name)

        response = _post(client, {"query": _QUERY})

        assert response.status_code == 200
        body = response.json()
        assert body["answer"] == _CANNED_ANSWER
        assert body["citations"], "distance-0 chunks exist for staff"

        # Every citation points at a seeded chunk; citation [1] is one of
        # the distance-0 aligned chunks (ties broken by document id).
        assert all(
            uuid.UUID(citation["chunk_id"]) in kb.all_chunk_ids
            for citation in body["citations"]
        )
        assert body["citations"][0]["number"] == 1
        assert uuid.UUID(body["citations"][0]["chunk_id"]) in kb.aligned_chunk_ids

        # The distance-0 internal chunk reached the prompt for staff.
        prompt = providers.generation.requests[0]
        assert any(
            source.document_title.startswith("Ops Handbook probe")
            for source in prompt.sources
        )


# ---------------------------------------------------------------------------
# Authorization matrix
# ---------------------------------------------------------------------------


class TestAuthorizationMatrix:
    def test_missing_authorization_header_returns_401(
        self, client: TestClient
    ):
        response = _post(client, {"query": _QUERY})

        assert response.status_code == 401
        assert response.json() == {"detail": "Missing Authorization header"}
        assert response.headers["www-authenticate"] == "Bearer"

    def test_invalid_token_returns_401(self, client: TestClient):
        response = _post(
            client,
            {"query": _QUERY},
            headers={"Authorization": "Bearer not-a-real-jwt"},
        )

        assert response.status_code == 401
        # Either the token was rejected as invalid/expired or it could
        # not be verified; both are the existing sanitized auth errors.
        assert response.json()["detail"] in (
            "Invalid or expired token",
            "JWT verification failed",
        )
        assert "not-a-real-jwt" not in response.text

    def test_missing_profile_returns_403(
        self, unknown_user_client: TestClient
    ):
        response = _post(unknown_user_client, {"query": _QUERY})

        assert response.status_code == 403
        assert response.json() == {"detail": "User profile not found"}


# ---------------------------------------------------------------------------
# Response and pipeline behavior
# ---------------------------------------------------------------------------


class TestResponse:
    def test_body_shape_is_answer_and_citations(
        self, customer_client: TestClient, providers: Providers, kb: Seed
    ):
        response = _post(customer_client, {"query": _QUERY})

        assert response.status_code == 200
        body = response.json()
        assert set(body) == {"answer", "citations"}

        for citation in body["citations"]:
            assert set(citation) == {
                "number",
                "chunk_id",
                "document_id",
                "document_title",
                "chunk_index",
            }

    def test_query_reaches_the_embedding_provider_verbatim(
        self, customer_client: TestClient, providers: Providers, kb: Seed
    ):
        _post(customer_client, {"query": "  spaced question  "})

        assert providers.embedding.calls == [["  spaced question  "]]

    def test_extra_client_fields_are_rejected(
        self, customer_client: TestClient, providers: Providers, kb: Seed
    ):
        response = _post(
            customer_client,
            {"query": _QUERY, "top_k": 20, "role": "admin"},
        )

        assert response.status_code == 422
        assert providers.embedding.calls == []
        assert providers.generation.requests == []


class TestEmptyRetrieval:
    def test_no_chunks_returns_exact_message_without_generation_call(
        self,
        customer_client: TestClient,
        providers: Providers,
        monkeypatch: pytest.MonkeyPatch,
    ):
        async def no_chunks(cursor, *, query_embedding, visibility):
            return []

        monkeypatch.setattr(
            retrieval_repository, "retrieve_chunks", no_chunks
        )

        response = _post(customer_client, {"query": _QUERY})

        assert response.status_code == 200
        assert response.json() == {
            "answer": INSUFFICIENT_EVIDENCE_MESSAGE,
            "citations": [],
        }

        # The query was embedded exactly once; generation never happened.
        assert providers.embedding.calls == [[_QUERY]]
        assert providers.generation.requests == []


# ---------------------------------------------------------------------------
# Failure sanitization
# ---------------------------------------------------------------------------


class TestFailureSanitization:
    def test_embedding_failure_is_sanitized_503(
        self, customer_client: TestClient, providers: Providers, kb: Seed
    ):
        providers.embedding.error = EmbeddingError(
            "simulated embedding outage; internal detail leaked"
        )

        response = _post(customer_client, {"query": _QUERY})

        assert response.status_code == 503
        assert response.json() == {
            "detail": rag_service.DETAIL_KB_UNAVAILABLE,
        }
        assert "simulated embedding outage" not in response.text
        assert providers.generation.requests == []

    def test_generation_failure_is_sanitized_503(
        self, customer_client: TestClient, providers: Providers, kb: Seed
    ):
        providers.generation.error = GenerationError(
            "simulated generation outage internals", stage="generation_request"
        )

        response = _post(customer_client, {"query": _QUERY})

        assert response.status_code == 503
        assert response.json() == {
            "detail": rag_service.DETAIL_GENERATION_UNAVAILABLE,
        }
        assert "simulated generation outage" not in response.text

    def test_unexpected_failure_is_sanitized_500(
        self, customer_client: TestClient, providers: Providers, kb: Seed
    ):
        providers.generation.error = RuntimeError("boom: engine crashed")

        response = _post(customer_client, {"query": _QUERY})

        assert response.status_code == 500
        assert response.json() == {
            "detail": rag_service.DETAIL_GENERATION_UNAVAILABLE,
        }
        assert "boom" not in response.text

    def test_database_failure_is_sanitized_503(
        self,
        customer_client: TestClient,
        providers: Providers,
        monkeypatch: pytest.MonkeyPatch,
    ):
        async def failing_retrieve_chunks(cursor, *, query_embedding, visibility):
            raise RuntimeError("simulated database failure")

        monkeypatch.setattr(
            retrieval_repository,
            "retrieve_chunks",
            failing_retrieve_chunks,
        )

        response = _post(customer_client, {"query": _QUERY})

        assert response.status_code == 503
        assert response.json() == {
            "detail": rag_service.DETAIL_KB_UNAVAILABLE,
        }
        assert "simulated database failure" not in response.text
        assert providers.generation.requests == []
