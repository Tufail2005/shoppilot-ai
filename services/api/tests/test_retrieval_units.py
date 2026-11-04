"""Offline unit tests for the Phase 6.1 retrieval service and repository.

No database and no network: the repository is exercised through a
fake async cursor recording every parameterized statement, and the
embedding provider through a fake implementing the Phase 5.5
``EmbeddingProvider`` protocol. The SQL contract itself — join,
``status = 'ready'`` restriction, bound visibility filter, ``LIMIT 5``
with no client ``top_k``, cosine ordering with deterministic
tie-breakers, and the exact parameter binding of the query vector — is
asserted without a live database.
"""

import asyncio
import inspect

import pytest

from app.repositories import retrieval as retrieval_repository
from app.services import retrieval as retrieval_service
from app.services.embeddings import EMBEDDING_DIMENSIONS, EmbeddingError


_QUERY = "injection' -- DROP TABLE never interesting"
_VECTOR = [0.25] + [0.0] * (EMBEDDING_DIMENSIONS - 1)


def _expected_literal(vector: list[float]) -> str:
    return "[" + ",".join(repr(float(value)) for value in vector) + "]"


class StubEmbeddingProvider:
    """Configurable ``embed_query`` double implementing the protocol."""

    def __init__(
        self,
        *,
        vector: list[float] | None = None,
        vectors_override: object = None,
        fail: bool = False,
    ):
        self.vector = list(vector) if vector is not None else list(_VECTOR)
        self.vectors_override = vectors_override
        self.fail = fail
        self.calls: list[list[str]] = []

    def embed_query(self, texts, *, cancel_event=None) -> list[list[float]]:
        self.calls.append(list(texts))

        if self.fail:
            raise EmbeddingError("simulated embedding provider outage")

        if self.vectors_override is not None:
            return self.vectors_override

        return [list(self.vector) for _ in texts]

    def embed_documents(self, texts, *, cancel_event=None):
        raise AssertionError("retrieval must only call embed_query")


class FakeDescription:
    def __init__(self, name: str):
        self.name = name


_COLUMNS = ["id", "document_id", "title", "visibility", "chunk_index", "content", "distance"]


class RecordingCursor:
    """Records statements; returns configured rows or raises."""

    def __init__(self, *, rows: list[tuple] | None = None, fail: bool = False):
        self.statements: list[tuple[str, list | None]] = []
        self._rows = rows if rows is not None else []
        self._fail = fail
        self.description = [FakeDescription(name) for name in _COLUMNS]

    async def execute(self, sql: str, params: list | None = None) -> None:
        self.statements.append((sql, params))

        if self._fail:
            raise RuntimeError("simulated database failure")

    async def fetchall(self) -> list[tuple]:
        return list(self._rows)


class _AsyncCM:
    """Minimal async context manager wrapping a value."""

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


def _fake_row() -> tuple:
    import uuid

    return (
        uuid.uuid4(),
        uuid.uuid4(),
        "Return Policy",
        "public",
        0,
        "The return window is 30 days.",
        0.05,
    )


def _retrieve(cursor: RecordingCursor, provider, *, role) -> list[dict]:
    return asyncio.run(
        retrieval_service.retrieve(
            FakePool(cursor),
            provider,
            query=_QUERY,
            user_role=role,
        )
    )


class TestVisibilityRoleMapping:
    def test_customer_maps_to_public_only(self):
        provider = StubEmbeddingProvider()
        cursor = RecordingCursor(rows=[_fake_row()])

        result = _retrieve(cursor, provider, role="customer")

        assert result
        sql, params = cursor.statements[0]
        assert params[1] == ["public"]

    @pytest.mark.parametrize("role", ["support_agent", "admin"])
    def test_staff_roles_map_to_public_and_internal(self, role):
        provider = StubEmbeddingProvider()
        cursor = RecordingCursor(rows=[_fake_row()])

        result = _retrieve(cursor, provider, role=role)

        assert result
        sql, params = cursor.statements[0]
        assert set(params[1]) == {"public", "internal"}

    @pytest.mark.parametrize(
        "bad_role", ["", "Customer", "supportagent", "internal", None, 7, " customer"]
    )
    def test_unsupported_roles_fail_closed_before_any_call(self, bad_role):
        provider = StubEmbeddingProvider()
        cursor = RecordingCursor(rows=[_fake_row()])

        with pytest.raises(retrieval_service.RetrievalError) as excinfo:
            _retrieve(cursor, provider, role=bad_role)

        # Fail closed: before the embedding call and before any SQL.
        assert provider.calls == []
        assert cursor.statements == []
        assert excinfo.value.stage == "role_validation"


class TestSqlContract:
    def test_single_parameterized_statement_with_fixed_limit(self):
        provider = StubEmbeddingProvider()
        cursor = RecordingCursor(rows=[_fake_row()])

        _retrieve(cursor, provider, role="customer")

        sql, params = cursor.statements[0]
        assert len(cursor.statements) == 1
        assert params is not None and len(params) == 3

        # Fixed server-side limit; no top_k anywhere in the surface.
        assert "LIMIT 5" in sql
        assert "top_k" not in sql
        assert "top_k" not in inspect.signature(
            retrieval_service.retrieve
        ).parameters

    def test_filters_ready_status_and_enforces_visibility_in_sql(self):
        provider = StubEmbeddingProvider()
        cursor = RecordingCursor(rows=[_fake_row()])

        _retrieve(cursor, provider, role="customer")

        sql, params = cursor.statements[0]
        assert "JOIN" in sql and "public.documents" in sql
        assert "d.status = 'ready'" in sql
        assert "d.visibility" in sql and "ANY(" in sql

    def test_cosine_ordering_with_deterministic_tie_breakers(self):
        provider = StubEmbeddingProvider()
        cursor = RecordingCursor(rows=[_fake_row()])

        _retrieve(cursor, provider, role="admin")

        sql, _ = cursor.statements[0]
        assert sql.count("<=>") >= 2
        assert "ORDER BY" in sql
        assert "d.id" in sql and "c.chunk_index" in sql

    def test_query_vector_bound_as_vector_literal_not_interpolated(self):
        provider = StubEmbeddingProvider()
        cursor = RecordingCursor(rows=[_fake_row()])

        _retrieve(cursor, provider, role="customer")

        sql, params = cursor.statements[0]
        expected = _expected_literal(_VECTOR)

        # The embedding literal is bound twice (distance projection and
        # ordering) as the same parameter value.
        assert params[0] == expected
        assert params[2] == expected
        assert params[0].count(",") == EMBEDDING_DIMENSIONS - 1
        assert params[0].startswith("[") and params[0].endswith("]")

        # Nothing from the request is interpolated into the SQL text.
        assert _QUERY not in sql


class TestEmbeddingValidation:
    def test_embeds_query_exactly_once_with_raw_query(self):
        provider = StubEmbeddingProvider()
        cursor = RecordingCursor(rows=[_fake_row()])

        _retrieve(cursor, provider, role="customer")

        assert provider.calls == [[_QUERY]]

    def test_embedding_error_propagates_without_any_sql(self):
        provider = StubEmbeddingProvider(fail=True)
        cursor = RecordingCursor(rows=[])

        with pytest.raises(EmbeddingError):
            _retrieve(cursor, provider, role="admin")

        assert cursor.statements == []

    def test_wrong_vector_count_fails_before_sql(self):
        provider = StubEmbeddingProvider(vectors_override=[_VECTOR, _VECTOR])
        cursor = RecordingCursor(rows=[])

        with pytest.raises(retrieval_service.RetrievalError) as excinfo:
            _retrieve(cursor, provider, role="admin")

        assert excinfo.value.stage == "embedding_validation"
        assert cursor.statements == []

    @pytest.mark.parametrize(
        "bad_vector",
        [
            _VECTOR[:-1],  # 767 values
            _VECTOR + [0.0],  # 769 values
            [True] + [0.0] * (EMBEDDING_DIMENSIONS - 1),
            ["0.5"] + [0.0] * (EMBEDDING_DIMENSIONS - 1),
            [float("inf")] + [0.0] * (EMBEDDING_DIMENSIONS - 1),
        ],
    )
    def test_malformed_query_vectors_fail_before_sql(self, bad_vector):
        provider = StubEmbeddingProvider(vectors_override=[bad_vector])
        cursor = RecordingCursor(rows=[])

        with pytest.raises(retrieval_service.RetrievalError) as excinfo:
            _retrieve(cursor, provider, role="admin")

        assert excinfo.value.stage == "embedding_validation"
        assert cursor.statements == []


class TestResultsAndFailures:
    def test_rows_map_to_expected_columns(self):
        provider = StubEmbeddingProvider()
        row = _fake_row()
        cursor = RecordingCursor(rows=[row])

        result = _retrieve(cursor, provider, role="customer")

        assert list(result[0].keys()) == _COLUMNS
        assert result[0]["title"] == "Return Policy"
        assert result[0]["content"] == "The return window is 30 days."
        assert result[0]["distance"] == 0.05

    def test_empty_result_returns_empty_list(self):
        provider = StubEmbeddingProvider()
        cursor = RecordingCursor(rows=[])

        result = _retrieve(cursor, provider, role="admin")

        assert result == []

    def test_database_failure_raises_retrieval_error_not_empty_list(self):
        provider = StubEmbeddingProvider()
        cursor = RecordingCursor(rows=[], fail=True)

        with pytest.raises(retrieval_service.RetrievalError) as excinfo:
            _retrieve(cursor, provider, role="admin")

        assert excinfo.value.stage == "database_query"
        assert isinstance(excinfo.value.__cause__, RuntimeError)


def test_repository_exposes_the_same_sql_contract():
    """The repository SQL matches the service-level contract too."""

    cursor = RecordingCursor(rows=[])
    asyncio.run(
        retrieval_repository.retrieve_chunks(
            cursor,
            query_embedding=_VECTOR,
            visibility=["public"],
        )
    )

    sql, params = cursor.statements[0]
    assert len(cursor.statements) == 1
    assert params is not None and len(params) == 3
    assert "d.status = 'ready'" in sql
    assert "LIMIT 5" in sql
    assert params[1] == ["public"]
