"""Offline unit tests for the Phase 6.4 POST /rag/query endpoint.

No database, no local Supabase, no Gemini credentials, no network: the
role dependency, the connection pool, and both Gemini providers are
replaced with FastAPI dependency overrides, and the repository is
exercised through a recording async cursor (the established
``test_retrieval_units.py`` pattern). ``TestClient`` is created without
entering its lifespan, so the application pool is never started and
``get_db`` is always overridden.

Covered here: the request contract (only ``query``, everything else
422), the sanitized authentication surface, the successful
answer/citation pipeline, role-driven visibility parameters, the exact
empty-retrieval behavior without any generation call, the full
sanitized error mapping, log sanitization, and provider configuration.
"""

import asyncio
import logging
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import auth as app_auth
from app.db import get_db
from app.main import app
from app.routers.rag import rag_access
from app.services import generation as generation_module
from app.services import rag as rag_service
from app.services.embeddings import EMBEDDING_DIMENSIONS, EmbeddingError
from app.services.generation import GenerationError
from app.services.rag_prompt import (
    INSUFFICIENT_EVIDENCE_MESSAGE,
    build_rag_prompt,
)


_QUERY = "How long do returns take?"

_QUERY_VECTOR = [0.25] + [0.0] * (EMBEDDING_DIMENSIONS - 1)

_COLUMNS = [
    "id",
    "document_id",
    "title",
    "visibility",
    "chunk_index",
    "content",
    "distance",
]


def _row(*, visibility: str = "public", chunk_index: int = 0) -> tuple:
    """One retrieval row shaped like repositories/retrieval.py output."""

    return (
        uuid.uuid4(),
        uuid.uuid4(),
        "Return Policy",
        visibility,
        chunk_index,
        "Returns are accepted within 30 days.",
        0.05,
    )


def _chunk_dict() -> dict:
    return {
        "id": uuid.uuid4(),
        "document_id": uuid.uuid4(),
        "title": "Return Policy",
        "visibility": "public",
        "chunk_index": 0,
        "content": "Returns are accepted within 30 days.",
        "distance": 0.05,
    }


class StubEmbeddingProvider:
    """Configurable ``embed_query`` double; never documents."""

    def __init__(self, *, vector=None, vectors_override=None, error=None):
        self.calls: list[list[str]] = []
        self.vector = vector if vector is not None else list(_QUERY_VECTOR)
        self.vectors_override = vectors_override
        self.error = error

    def embed_query(self, texts) -> list[list[float]]:
        self.calls.append(list(texts))

        if self.error is not None:
            raise self.error

        if self.vectors_override is not None:
            return self.vectors_override

        return [list(self.vector) for _ in texts]

    def embed_documents(self, texts):
        raise AssertionError("retrieval must only call embed_query")


class StubGenerationProvider:
    """Records generate requests; replays a canned answer or error."""

    def __init__(self, *, answer="Grounded answer. [1]", error=None):
        self.requests: list = []
        self.answer = answer
        self.error = error

    def generate(self, prompt) -> str:
        self.requests.append(prompt)

        if self.error is not None:
            raise self.error

        return self.answer


class FakeDescription:
    def __init__(self, name: str):
        self.name = name


class RecordingCursor:
    """Records statements; returns configured rows or raises."""

    def __init__(self, *, rows=None, fail=False):
        self.statements: list[tuple[str, list | None]] = []
        self.rows = rows if rows is not None else []
        self.fail = fail
        self.description = [FakeDescription(name) for name in _COLUMNS]

    async def execute(self, sql: str, params: list | None = None) -> None:
        self.statements.append((sql, params))

        if self.fail:
            raise RuntimeError("simulated database failure")

    async def fetchall(self) -> list[tuple]:
        return list(self.rows)


class _AsyncCM:
    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class FakeConnection:
    def __init__(self, cursor: RecordingCursor):
        self._cursor = cursor

    def cursor(self) -> _AsyncCM:
        return _AsyncCM(self._cursor)


class FakePool:
    def __init__(self, cursor: RecordingCursor):
        self._cursor = cursor

    def connection(self) -> _AsyncCM:
        return _AsyncCM(FakeConnection(self._cursor))


class RagHarness:
    """Shared stubs plus FastAPI overrides for one offline endpoint."""

    def __init__(self):
        self.cursor = RecordingCursor()
        self.embedding = StubEmbeddingProvider()
        self.generation = StubGenerationProvider()
        self.role = "customer"
        self.user_id = str(uuid.uuid4())
        self.client = TestClient(app)

    def install(self, *, override_role: bool = True):
        if override_role:
            app.dependency_overrides[rag_access] = self._access

        app.dependency_overrides[get_db] = lambda: FakePool(self.cursor)
        app.dependency_overrides[
            rag_service.get_query_embedding_provider
        ] = lambda: self.embedding
        app.dependency_overrides[
            rag_service.get_generation_provider
        ] = lambda: self.generation

    async def _access(self):
        return {
            "id": self.user_id,
            "email": None,
            "claims": {"sub": self.user_id},
            "role": self.role,
        }

    def post(self, payload: dict, headers: dict | None = None):
        return self.client.post("/rag/query", json=payload, headers=headers)


_OVERRIDDEN_KEYS = (
    rag_access,
    get_db,
    rag_service.get_query_embedding_provider,
    rag_service.get_generation_provider,
)


@pytest.fixture
def harness():
    """Full offline harness: role, pool, and both providers faked."""

    h = RagHarness()
    h.install()

    try:
        yield h
    finally:
        for key in _OVERRIDDEN_KEYS:
            app.dependency_overrides.pop(key, None)


@pytest.fixture
def auth_harness():
    """Real ``get_current_user`` runs; only the external edges are faked."""

    h = RagHarness()
    h.install(override_role=False)

    try:
        yield h
    finally:
        for key in _OVERRIDDEN_KEYS:
            app.dependency_overrides.pop(key, None)


# ---------------------------------------------------------------------------
# Request contract
# ---------------------------------------------------------------------------


class TestRequestContract:
    def test_missing_query_returns_422_without_any_calls(self, harness):
        response = harness.post({})

        assert response.status_code == 422
        assert harness.embedding.calls == []
        assert harness.generation.requests == []
        assert harness.cursor.statements == []

    @pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
    def test_blank_query_returns_422(self, harness, blank):
        response = harness.post({"query": blank})

        assert response.status_code == 422
        assert harness.embedding.calls == []
        assert harness.generation.requests == []

    def test_non_string_query_returns_422(self, harness):
        response = harness.post({"query": 42})

        assert response.status_code == 422

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("role", "admin"),
            ("user_id", str(uuid.uuid4())),
            ("top_k", 7),
            ("visibility", "internal"),
        ],
    )
    def test_extra_fields_are_rejected_as_422(self, harness, field, value):
        response = harness.post({"query": _QUERY, field: value})

        assert response.status_code == 422
        assert harness.embedding.calls == []
        assert harness.generation.requests == []
        assert harness.cursor.statements == []

    def test_query_is_forwarded_unmodified(self, harness):
        harness.cursor.rows = []

        response = harness.post({"query": "  padded question  "})

        assert response.status_code == 200
        assert harness.embedding.calls == [["  padded question  "]]

    def test_response_body_has_exactly_answer_and_citations(self, harness):
        harness.cursor.rows = [_row(), _row(visibility="internal", chunk_index=3)]

        response = harness.post({"query": _QUERY})

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


# ---------------------------------------------------------------------------
# Authentication surface (real get_current_user, stubbed Supabase client)
# ---------------------------------------------------------------------------


class TestAuthenticationSurface:
    def test_missing_authorization_header_returns_401(self, auth_harness):
        response = auth_harness.post({"query": _QUERY})

        assert response.status_code == 401
        assert response.json() == {"detail": "Missing Authorization header"}
        assert response.headers["www-authenticate"] == "Bearer"
        assert auth_harness.embedding.calls == []

    def test_unverifiable_token_returns_sanitized_401(
        self, auth_harness, monkeypatch
    ):
        def get_claims(token):
            raise RuntimeError("expired token internals: sub=abc key=xyz")

        def fake_client():
            return SimpleNamespace(
                auth=SimpleNamespace(get_claims=get_claims),
            )

        monkeypatch.setattr(app_auth, "get_supabase_client", fake_client)

        response = auth_harness.post(
            {"query": _QUERY},
            headers={"Authorization": "Bearer some-token"},
        )

        assert response.status_code == 401
        assert response.json() == {"detail": "JWT verification failed"}
        assert "expired token internals" not in response.text

    def test_token_without_claims_returns_401(self, auth_harness, monkeypatch):
        def fake_client():
            return SimpleNamespace(
                auth=SimpleNamespace(get_claims=lambda token: None),
            )

        monkeypatch.setattr(app_auth, "get_supabase_client", fake_client)

        response = auth_harness.post(
            {"query": _QUERY},
            headers={"Authorization": "Bearer some-token"},
        )

        assert response.status_code == 401
        assert response.json() == {"detail": "Invalid or expired token"}


# ---------------------------------------------------------------------------
# Successful pipeline
# ---------------------------------------------------------------------------


class TestSuccessfulPipeline:
    def test_returns_answer_and_validated_citations(self, harness):
        first = _row()
        second = _row(visibility="internal", chunk_index=3)
        harness.cursor.rows = [first, second]
        harness.generation = StubGenerationProvider(
            answer=(
                "The return window is 30 days [1]; shipping takes "
                "3-5 business days [2] [9]."
            )
        )

        response = harness.post({"query": _QUERY})

        assert response.status_code == 200
        body = response.json()
        assert body["answer"] == (
            "The return window is 30 days [1]; shipping takes "
            "3-5 business days [2] [9]."
        )

        # [9] is out of range: excluded, never fabricated.
        assert [citation["number"] for citation in body["citations"]] == [1, 2]

        by_number = {citation["number"]: citation for citation in body["citations"]}
        assert by_number[1]["chunk_id"] == str(first[0])
        assert by_number[1]["document_id"] == str(first[1])
        assert by_number[1]["document_title"] == first[2]
        assert by_number[1]["chunk_index"] == first[4]
        assert by_number[2]["chunk_id"] == str(second[0])
        assert by_number[2]["document_id"] == str(second[1])
        assert by_number[2]["chunk_index"] == second[4]

    def test_generation_receives_the_built_prompt(self, harness):
        harness.cursor.rows = [_row()]

        response = harness.post({"query": _QUERY})

        assert response.status_code == 200
        assert len(harness.generation.requests) == 1

        prompt = harness.generation.requests[0]
        assert prompt.question == _QUERY
        assert len(prompt.sources) == 1
        assert prompt.sources[0].content == "Returns are accepted within 30 days."
        assert prompt.sources[0].document_title == "Return Policy"

    def test_embedding_receives_exactly_one_query(self, harness):
        harness.cursor.rows = []

        harness.post({"query": _QUERY})

        assert harness.embedding.calls == [[_QUERY]]

    @pytest.mark.parametrize(
        ("role", "expected_visibility"),
        [
            ("customer", ["public"]),
            ("support_agent", ["public", "internal"]),
            ("admin", ["public", "internal"]),
        ],
    )
    def test_role_drives_visibility_parameters(
        self, harness, role, expected_visibility
    ):
        harness.role = role
        harness.cursor.rows = []

        response = harness.post({"query": _QUERY})

        assert response.status_code == 200

        _, params = harness.cursor.statements[0]
        assert params[1] == expected_visibility

    def test_unsupported_role_fails_closed_with_403(self, harness):
        # The dependency override simulates a stale/unknown profile role
        # reaching retrieval: the Phase 6.1 role map fails closed before
        # the embedding call and before any SQL.
        harness.role = "contractor"

        response = harness.post({"query": _QUERY})

        assert response.status_code == 403
        assert response.json() == {
            "detail": rag_service.DETAIL_FORBIDDEN,
        }
        assert harness.embedding.calls == []
        assert harness.cursor.statements == []


# ---------------------------------------------------------------------------
# Empty retrieval
# ---------------------------------------------------------------------------


class TestEmptyRetrieval:
    def test_no_chunks_returns_exact_message_without_generation_call(
        self, harness
    ):
        harness.cursor.rows = []
        harness.generation = StubGenerationProvider(
            answer=AssertionError("generation must not be called")
        )

        response = harness.post({"query": _QUERY})

        assert response.status_code == 200
        assert response.json() == {
            "answer": INSUFFICIENT_EVIDENCE_MESSAGE,
            "citations": [],
        }
        assert harness.embedding.calls == [[_QUERY]]
        assert harness.generation.requests == []

    def test_generation_provider_is_never_constructed_on_empty_retrieval(
        self, harness, monkeypatch
    ):
        harness.cursor.rows = []
        app.dependency_overrides[rag_service.get_generation_provider] = (
            lambda: rag_service.LazyGenerationProvider()
        )

        def fail_if_constructed():
            raise AssertionError(
                "the generation factory must not run without sources"
            )

        monkeypatch.setattr(
            rag_service,
            "create_gemini_generation_provider",
            fail_if_constructed,
        )

        response = harness.post({"query": _QUERY})

        assert response.status_code == 200
        assert response.json()["answer"] == INSUFFICIENT_EVIDENCE_MESSAGE


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


class TestErrorMapping:
    def test_embedding_failure_is_sanitized_503(self, harness):
        harness.embedding = StubEmbeddingProvider(
            error=EmbeddingError("simulated outage; leaked value sk-secret-123")
        )

        response = harness.post({"query": _QUERY})

        assert response.status_code == 503
        assert response.json() == {
            "detail": rag_service.DETAIL_KB_UNAVAILABLE,
        }
        assert "sk-secret-123" not in response.text
        assert harness.generation.requests == []

    def test_database_failure_is_sanitized_503(self, harness):
        harness.cursor.fail = True

        response = harness.post({"query": _QUERY})

        assert response.status_code == 503
        assert response.json() == {
            "detail": rag_service.DETAIL_KB_UNAVAILABLE,
        }
        assert "simulated" not in response.text
        assert harness.generation.requests == []

    def test_wrong_embedding_shape_is_sanitized_503(self, harness):
        # Retrieval re-validates the vector: a wrong-shape response must
        # never reach SQL and never become an empty answer.
        harness.embedding = StubEmbeddingProvider(
            vectors_override=[[0.1]] * 1
        )

        response = harness.post({"query": _QUERY})

        assert response.status_code == 503
        assert response.json() == {
            "detail": rag_service.DETAIL_KB_UNAVAILABLE,
        }

    def test_generation_request_failure_is_sanitized_503(self, harness):
        harness.cursor.rows = [_row()]
        harness.generation.error = GenerationError(
            "internal model quota exhausted", stage="generation_request"
        )

        response = harness.post({"query": _QUERY})

        assert response.status_code == 503
        assert response.json() == {
            "detail": rag_service.DETAIL_GENERATION_UNAVAILABLE,
        }
        assert "quota" not in response.text

    def test_generation_configuration_failure_is_sanitized_503(
        self, harness, monkeypatch
    ):
        harness.cursor.rows = [_row()]
        app.dependency_overrides[rag_service.get_generation_provider] = (
            lambda: rag_service.LazyGenerationProvider()
        )
        monkeypatch.setattr(
            generation_module,
            "get_settings",
            lambda: SimpleNamespace(
                gemini_api_key=None,
                gemini_generation_model="gemini-test",
            ),
        )

        response = harness.post({"query": _QUERY})

        assert response.status_code == 503
        assert response.json() == {
            "detail": rag_service.DETAIL_GENERATION_NOT_CONFIGURED,
        }

    def test_unexpected_failure_is_sanitized_500(self, harness):
        harness.cursor.rows = [_row()]
        harness.generation.error = RuntimeError("boom: connection string")

        response = harness.post({"query": _QUERY})

        assert response.status_code == 500
        assert response.json() == {
            "detail": rag_service.DETAIL_GENERATION_UNAVAILABLE,
        }
        assert "boom" not in response.text
        assert "connection string" not in response.text

    def test_validation_failure_maps_to_422(self, harness):
        harness.cursor.rows = [_row()]
        harness.generation.error = ValueError(
            "simulated prompt validation failure"
        )

        response = harness.post({"query": _QUERY})

        assert response.status_code == 422
        assert response.json() == {
            "detail": rag_service.DETAIL_INVALID_QUERY,
        }
        assert "simulated prompt validation failure" not in response.text


class TestServiceErrorMapping:
    """Direct :func:`answer_query` mapping for the reachable retrieval stages."""

    def _run(self, *, cursor_fail=False, vectors_override=None):
        cursor = RecordingCursor(fail=cursor_fail)
        provider = StubEmbeddingProvider(vectors_override=vectors_override)

        return rag_service.answer_query(
            FakePool(cursor),
            provider,
            StubGenerationProvider(),
            query=_QUERY,
            user_role="customer",
        )

    def test_database_failure_stage_maps_to_503(self):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(self._run(cursor_fail=True))

        assert exc.value.status_code == 503
        assert exc.value.detail == rag_service.DETAIL_KB_UNAVAILABLE

    def test_embedding_validation_stage_maps_to_503(self):
        # A malformed embedding response shape never reaches SQL.
        with pytest.raises(HTTPException) as exc:
            asyncio.run(self._run(vectors_override=[[0.1]] * 1))

        assert exc.value.status_code == 503
        assert exc.value.detail == rag_service.DETAIL_KB_UNAVAILABLE


# ---------------------------------------------------------------------------
# Log sanitization
# ---------------------------------------------------------------------------


class TestLogSanitization:
    def test_failure_logs_never_contain_query_or_internal_text(
        self, harness, caplog: pytest.LogCaptureFixture
    ):
        unique_query = "How do I refund order ORDER-99?"
        harness.embedding = StubEmbeddingProvider(
            error=EmbeddingError("simulated provider outage")
        )

        caplog.set_level(logging.ERROR)

        response = harness.post({"query": unique_query})

        assert response.status_code == 503

        messages = "\n".join(
            record.getMessage() for record in caplog.records
        )

        assert unique_query not in messages
        assert "simulated provider outage" not in messages
        assert "EmbeddingError" in messages


# ---------------------------------------------------------------------------
# Provider configuration
# ---------------------------------------------------------------------------


class TestProviderConfiguration:
    def test_missing_embedding_configuration_is_sanitized_503(
        self, monkeypatch
    ):
        def failing_factory():
            raise EmbeddingError(
                "GEMINI_API_KEY is not configured; set it in the environment"
            )

        monkeypatch.setattr(rag_service, "create_gemini_provider", failing_factory)

        with pytest.raises(HTTPException) as exc:
            rag_service.get_query_embedding_provider()

        assert exc.value.status_code == 503
        assert exc.value.detail == rag_service.DETAIL_KB_NOT_CONFIGURED

    def test_generation_dependency_is_lazy_and_defers_configuration(
        self, monkeypatch
    ):
        provider = rag_service.get_generation_provider()

        assert isinstance(provider, rag_service.LazyGenerationProvider)

        monkeypatch.setattr(
            generation_module,
            "get_settings",
            lambda: SimpleNamespace(
                gemini_api_key=None,
                gemini_generation_model="gemini-test",
            ),
        )

        with pytest.raises(GenerationError) as exc:
            provider.generate(build_rag_prompt(_QUERY, [_chunk_dict()]))

        assert exc.value.stage == "configuration"
