"""Offline unit tests for the Phase 5.6 ingestion service.

No local Supabase database, Storage, or Gemini credentials are
required: Storage, the embedding provider, the pool/cursor layer, and
the chunk repository are faked. Real-database behavior (insertion,
atomicity, duplicate ingestion, the vector cast) is covered by
test_ingestion_integration.py.
"""

import asyncio
import logging
import uuid

import pytest

from app.repositories import documents as documents_repository
from app.services import ingestion
from app.services.chunking import chunk_text
from app.services.documents import extract_text
from app.services.embeddings import EmbeddingError
from app.services.ingestion import (
    EMBEDDING_BATCH_SIZE,
    IngestionError,
    ingest_document,
)
from tests.ingestion_fakes import (
    FakeEmbeddingProvider,
    FakeStorageClient,
    FakeStorageState,
)
from tests.test_documents_units import _build_pdf


_STORAGE_PATH = "uploads/unit-test-only-serving-path.pdf"
_DOCUMENT_ID = uuid.UUID("00000000-0000-0000-0000-0000000009c6")
_BATCH_SIZE = 2


def _page_text(word_count: int) -> str:
    return " ".join(f"w{number}" for number in range(word_count))


# A text-heavy PDF guaranteed to produce several chunks. The expected
# chunk flow is recomputed from the real Phase 5.3/5.4 building
# blocks, so chunk-count assertions never depend on heuristic tuning.
_SUCCESS_PDF = _build_pdf([_page_text(4200)])
_SUCCESS_TEXT = extract_text(_SUCCESS_PDF)
_SUCCESS_CHUNKS = chunk_text(_SUCCESS_TEXT)


def _fake_embedding(batch_index: int, position: int) -> list[float]:
    """The FakeEmbeddingProvider vector for one batch slot (n=768)."""
    encoded = float(batch_index * 1000 + position)
    return [encoded] + [0.0] * 767


def _make_storage(pdf_bytes: bytes | None = None) -> tuple[FakeStorageClient, FakeStorageState]:
    state = FakeStorageState()
    state.objects[_STORAGE_PATH] = _SUCCESS_PDF if pdf_bytes is None else pdf_bytes
    return FakeStorageClient(state), state


class FakeCursor:
    """Records parameterized statements like a psycopg AsyncCursor."""

    def __init__(self, *, fail_statement: int | None = None):
        self.statements: list[tuple[str, list | None]] = []
        self._fail_statement = fail_statement
        self._statement_counter = 0
        self._rowcount = 0

    async def execute(self, sql: str, params: list | None = None) -> None:
        self._statement_counter += 1
        self.statements.append(
            (sql, list(params) if params is not None else None)
        )
        self._fail_if_armed()

    async def executemany(self, sql: str, rows: list) -> None:
        self._statement_counter += 1
        self.statements.append((sql, [list(row) for row in rows]))
        self._fail_if_armed()
        self._rowcount = len(rows)

    def _fail_if_armed(self) -> None:
        if self._fail_statement == self._statement_counter:
            raise RuntimeError("simulated database failure")

    @property
    def rowcount(self) -> int:
        return self._rowcount

    async def __aenter__(self) -> "FakeCursor":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class FakeConnection:
    """Mirrors what ingest_document exercises of a pool connection."""

    def __init__(self, *, fail_statement: int | None = None):
        self._cursor = FakeCursor(fail_statement=fail_statement)
        self.commit_calls = 0
        self.rollback_calls = 0
        self.exit_outcome: str | None = None

    def cursor(self) -> FakeCursor:
        return self._cursor

    async def commit(self) -> None:
        self.commit_calls += 1

    async def rollback(self) -> None:
        self.rollback_calls += 1

    async def __aenter__(self) -> "FakeConnection":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self.exit_outcome = "clean" if exc_type is None else "exception"
        return False


class FakePool:
    """Async context-manager like ``psycopg_pool.AsyncConnectionPool``."""

    def __init__(self, connection: FakeConnection):
        self._connection = connection

    def connection(self) -> FakeConnection:
        return self._connection


@pytest.fixture
def insertion_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Replace the chunk repository and record every insertion call.

    The service references ``documents_repository.insert_document_chunks``
    through the module at call time, so patching the module attribute
    redirects the service without faking the pool.
    """
    calls: list[dict] = []

    async def fake_insert(cursor, *, document_id, chunks):
        calls.append(
            {
                "cursor": cursor,
                "document_id": document_id,
                "chunks": list(chunks),
            }
        )
        return len(chunks)

    monkeypatch.setattr(
        documents_repository,
        "insert_document_chunks",
        fake_insert,
    )
    return calls


_BATCH = object()  # sentinel: use the helper's small default batch size


def _run_ingest(
    storage: FakeStorageClient,
    provider: FakeEmbeddingProvider,
    connection: FakeConnection,
    *,
    batch_size=_BATCH,
    **kwargs,
) -> int:
    """Run ingest_document.

    ``batch_size = _BATCH`` (sentinel) injects the small helper batch
    size; ``batch_size = None`` omits the argument entirely so the
    function's own default applies; any other value is passed through.
    """

    if batch_size is _BATCH:
        kwargs["batch_size"] = _BATCH_SIZE
    elif batch_size is not None:
        kwargs["batch_size"] = batch_size

    return asyncio.run(
        ingest_document(
            FakePool(connection),
            storage,
            provider,
            document_id=_DOCUMENT_ID,
            storage_path=_STORAGE_PATH,
            **kwargs,
        )
    )


class TestSuccessfulIngestion:
    def test_stores_one_embedding_per_chunk_in_document_order(
        self,
        insertion_calls,
    ):
        storage, state = _make_storage()
        provider = FakeEmbeddingProvider()
        connection = FakeConnection()

        inserted = _run_ingest(storage, provider, connection)

        assert inserted == len(_SUCCESS_CHUNKS)
        assert len(_SUCCESS_CHUNKS) >= 5  # premise of the batch tests

        # The PDF was downloaded exactly once at the internal path.
        assert state.downloads == [_STORAGE_PATH]
        # The repository was faked, so the service issued no SQL itself.
        assert connection.cursor().statements == []

        [call] = insertion_calls
        assert call["document_id"] == _DOCUMENT_ID
        assert call["chunks"] == [
            (index, content, _fake_embedding(index // 2, index % 2))
            for index, content in enumerate(_SUCCESS_CHUNKS)
        ]

        # The transaction committed exactly once and exited cleanly.
        assert connection.commit_calls == 1
        assert connection.exit_outcome == "clean"

    def test_default_batch_size_constant_and_single_batch(self, insertion_calls):
        assert EMBEDDING_BATCH_SIZE == 16

        storage, _ = _make_storage()
        provider = FakeEmbeddingProvider()
        connection = FakeConnection()

        inserted = _run_ingest(storage, provider, connection, batch_size=None)

        assert inserted == len(_SUCCESS_CHUNKS)

        # _SUCCESS_CHUNKS is smaller than the default batch: the whole
        # document embeds in one provider call, in document order.
        assert provider.calls == [list(_SUCCESS_CHUNKS)]

        [call] = insertion_calls

        for index, (_, _, embedding) in enumerate(call["chunks"]):
            assert embedding[0] == float(index)  # batch 0, position index
            assert embedding[1:] == [0.0] * 767

    def test_pipeline_stages_run_in_order(self, insertion_calls):
        storage, state = _make_storage()
        provider = FakeEmbeddingProvider()
        connection = FakeConnection()

        inserted = _run_ingest(storage, provider, connection)

        # Download recorded before the provider ever ran: an embedding
        # failure still leaves the download recorded, and conversely a
        # storage failure would leave zero provider calls (tested in
        # TestStageFailures).
        assert state.downloads == [_STORAGE_PATH]

        [call] = insertion_calls

        # Repository rows equal the chunker output verbatim; chunk
        # indices are contiguous from zero. Validated embeddings can
        # only exist after the provider ran, so their presence in the
        # insertion call also proves the stage order.
        assert [chunk_index for chunk_index, _, _ in call["chunks"]] == list(
            range(len(_SUCCESS_CHUNKS))
        )
        assert [content for _, content, _ in call["chunks"]] == _SUCCESS_CHUNKS
        assert inserted == len(_SUCCESS_CHUNKS)
        assert connection.commit_calls == 1


class TestBatching:
    def test_batches_are_sequential_bounded_and_order_aligned(
        self,
        insertion_calls,
    ):
        storage, _ = _make_storage()
        provider = FakeEmbeddingProvider()
        connection = FakeConnection()

        inserted = _run_ingest(storage, provider, connection)

        expected_batches = [
            _SUCCESS_CHUNKS[start:start + _BATCH_SIZE]
            for start in range(0, len(_SUCCESS_CHUNKS), _BATCH_SIZE)
        ]
        assert provider.calls == expected_batches

        # Vector n belongs to chunk n: value 0 encodes the batch and
        # position of the provider call that produced it.
        for index, (_, _, embedding) in enumerate(insertion_calls[0]["chunks"]):
            expected_encoding = float(
                (index // _BATCH_SIZE) * 1000 + (index % _BATCH_SIZE)
            )
            assert embedding[0] == expected_encoding
            assert embedding[1:] == [0.0] * 767

        assert inserted == len(_SUCCESS_CHUNKS)

    def test_batch_size_below_one_is_rejected(self, insertion_calls):
        storage, _ = _make_storage()
        provider = FakeEmbeddingProvider()
        connection = FakeConnection()

        with pytest.raises(ValueError, match="batch_size must be at least 1"):
            _run_ingest(storage, provider, connection, batch_size=0)

        assert provider.calls == []
        assert insertion_calls == []
        assert connection.commit_calls == 0


class TestEmbeddingValidation:
    """A fake provider misbehaves where the real one is prevented by
    its own validation: the ingestion layer must enforce the whole
    contract independently before SQL runs."""

    @pytest.mark.parametrize(
        ("provider", "expected_stage"),
        [
            (
                FakeEmbeddingProvider(wrong_dim_batch=0),
                "embedding_validation",
            ),
            (
                FakeEmbeddingProvider(wrong_count_batch=0),
                "embedding_validation",
            ),
            (
                FakeEmbeddingProvider(bad_value_batch=(0, "not-a-number")),
                "embedding_validation",
            ),
            (
                FakeEmbeddingProvider(bad_value_batch=(0, True)),
                "embedding_validation",
            ),
            (
                FakeEmbeddingProvider(bad_value_batch=(0, float("inf"))),
                "embedding_validation",
            ),
            (
                FakeEmbeddingProvider(
                    raw_override_batch=(0, {"not": "a vector"}),
                ),
                "embedding_validation",
            ),
        ],
    )
    def test_invalid_embedding_sets_fail_before_sql(
        self,
        insertion_calls,
        provider,
        expected_stage,
    ):
        storage, _ = _make_storage()
        connection = FakeConnection()

        with pytest.raises(IngestionError) as exc_info:
            _run_ingest(storage, provider, connection)

        assert exc_info.value.stage == expected_stage
        assert insertion_calls == []
        assert connection.commit_calls == 0
        # The pool connection is never opened for failures that happen
        # before the database stage.
        assert connection.exit_outcome is None


class TestStageFailures:
    def test_storage_download_failure_reaches_no_embedding_call(
        self,
        insertion_calls,
    ):
        storage, state = _make_storage()
        state.fail_download = True
        provider = FakeEmbeddingProvider()
        connection = FakeConnection()

        with pytest.raises(IngestionError) as exc_info:
            _run_ingest(storage, provider, connection)

        assert exc_info.value.stage == "storage_download"
        assert state.downloads == [_STORAGE_PATH]
        assert provider.calls == []
        assert insertion_calls == []
        assert connection.commit_calls == 0
        assert connection.exit_outcome is None  # DB stage never reached

    def test_pdf_extraction_failure_stops_before_chunking(
        self,
        insertion_calls,
    ):
        storage, _ = _make_storage(pdf_bytes=b"definitely not a pdf")
        provider = FakeEmbeddingProvider()
        connection = FakeConnection()

        with pytest.raises(IngestionError) as exc_info:
            _run_ingest(storage, provider, connection)

        assert exc_info.value.stage == "text_extraction"
        assert provider.calls == []
        assert insertion_calls == []
        assert connection.commit_calls == 0

    def test_empty_chunk_output_is_rejected(
        self,
        insertion_calls,
        monkeypatch: pytest.MonkeyPatch,
    ):
        storage, _ = _make_storage()
        provider = FakeEmbeddingProvider()
        connection = FakeConnection()

        monkeypatch.setattr(ingestion, "chunk_text", lambda *args, **kwargs: [])

        with pytest.raises(IngestionError) as exc_info:
            _run_ingest(storage, provider, connection)

        assert exc_info.value.stage == "chunking"
        assert provider.calls == []
        assert insertion_calls == []
        assert connection.commit_calls == 0

    def test_embedding_provider_failure_is_wrapped_and_chained(
        self,
        insertion_calls,
    ):
        storage, _ = _make_storage()
        provider = FakeEmbeddingProvider(fail_batches={0})
        connection = FakeConnection()

        with pytest.raises(IngestionError) as exc_info:
            _run_ingest(storage, provider, connection)

        assert exc_info.value.stage == "embedding_generation"
        assert isinstance(exc_info.value.__cause__, EmbeddingError)
        assert provider.calls == [_SUCCESS_CHUNKS[:_BATCH_SIZE]]
        assert insertion_calls == []
        assert connection.commit_calls == 0
        assert connection.exit_outcome is None  # DB stage never reached


class TestDatabaseFailure:
    def test_repository_failure_is_wrapped_without_commit(
        self,
        insertion_calls,
        monkeypatch: pytest.MonkeyPatch,
    ):
        storage, _ = _make_storage()
        provider = FakeEmbeddingProvider()

        async def failing_insert(cursor, *, document_id, chunks):
            raise RuntimeError("simulated insertion failure")

        monkeypatch.setattr(
            documents_repository,
            "insert_document_chunks",
            failing_insert,
        )

        connection = FakeConnection()

        with pytest.raises(IngestionError) as exc_info:
            _run_ingest(storage, provider, connection)

        assert exc_info.value.stage == "database_insertion"
        assert isinstance(exc_info.value.__cause__, RuntimeError)
        assert connection.commit_calls == 0
        assert connection.exit_outcome == "exception"


class TestRepositoryInsertChunks:
    """The real repository function driven against the fake cursor."""

    def _chunks(self) -> list[tuple[int, str, list[float]]]:
        return [
            (0, "first chunk", [0.1] * 768),
            (1, "second chunk", [-2.5] + [0.0] * 767),
        ]

    def test_delete_precedes_inserts_on_the_same_cursor(self):
        connection = FakeConnection()

        inserted = asyncio.run(
            documents_repository.insert_document_chunks(
                connection.cursor(),
                document_id=_DOCUMENT_ID,
                chunks=self._chunks(),
            )
        )

        assert inserted == 2

        delete_sql, delete_params = connection.cursor().statements[0]
        assert "DELETE FROM public.document_chunks" in delete_sql
        assert delete_params == [_DOCUMENT_ID]

        insert_sql, insert_rows = connection.cursor().statements[1]
        assert "INSERT INTO public.document_chunks" in insert_sql
        assert len(insert_rows) == 2

        # Parameter tuples follow the SQL column order:
        # (document_id, content, vector literal, chunk_index). The fake
        # cursor records each row as a list.
        assert insert_rows[0] == list(
            (
                _DOCUMENT_ID,
                "first chunk",
                "[" + ",".join(repr(0.1) for _ in range(768)) + "]",
                0,
            )
        )
        assert insert_rows[1] == list(
            (
                _DOCUMENT_ID,
                "second chunk",
                "[" + repr(-2.5) + "," + ",".join(repr(0.0) for _ in range(767)) + "]",
                1,
            )
        )

        assert connection.cursor().rowcount == 2

    def test_empty_chunk_set_is_rejected(self):
        connection = FakeConnection()

        with pytest.raises(ValueError, match="at least one chunk"):
            asyncio.run(
                documents_repository.insert_document_chunks(
                    connection.cursor(),
                    document_id=_DOCUMENT_ID,
                    chunks=[],
                )
            )

        assert connection.cursor().statements == []

    def test_insert_failure_raises_through_the_cursor(self):
        connection = FakeConnection(fail_statement=2)

        with pytest.raises(RuntimeError, match="simulated database failure"):
            asyncio.run(
                documents_repository.insert_document_chunks(
                    connection.cursor(),
                    document_id=_DOCUMENT_ID,
                    chunks=self._chunks(),
                )
            )


class TestLoggingHygiene:
    def test_failures_log_stage_and_type_without_paths_or_urls(self, caplog):
        caplog.set_level(logging.ERROR)

        # 1. Storage download failure with a hostile-looking path.
        hostile_path = "uploads/hostile-«-path.pdf"
        state = FakeStorageState(fail_download=True)
        storage = FakeStorageClient(state)
        provider = FakeEmbeddingProvider()
        connection = FakeConnection()

        with pytest.raises(IngestionError):
            asyncio.run(
                ingest_document(
                    FakePool(connection),
                    storage,
                    provider,
                    document_id=_DOCUMENT_ID,
                    storage_path=hostile_path,
                )
            )

        # 2. Embedding failure of the same document — vectors are never
        # logged, only the stage and exception type.
        state = FakeStorageState()
        state.objects[hostile_path] = _SUCCESS_PDF
        storage = FakeStorageClient(state)
        provider = FakeEmbeddingProvider(fail_batches={0})

        with pytest.raises(IngestionError):
            asyncio.run(
                ingest_document(
                    FakePool(FakeConnection()),
                    storage,
                    provider,
                    document_id=_DOCUMENT_ID,
                    storage_path=hostile_path,
                )
            )

        messages = [record.getMessage() for record in caplog.records]

        assert f"stage storage_download for document {_DOCUMENT_ID}" in "\n".join(
            messages
        )
        assert f"stage embedding_generation for document {_DOCUMENT_ID}" in "\n".join(
            messages
        )

        for message in messages:
            # Only stage/type-style log records are emitted.
            assert message.startswith("Document ingestion failed at stage ")

        joined = "\n".join(messages)

        # Internal storage paths and URLs never surface in logs, and
        # neither does any embedding content.
        assert hostile_path not in joined
        assert "uploads/" not in joined
        assert "http" not in joined.lower()
        assert "0.0" not in joined
