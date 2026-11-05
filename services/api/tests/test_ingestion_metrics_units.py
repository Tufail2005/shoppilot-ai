"""Offline unit tests for the Phase 6.8 ingestion pipeline metrics.

No database, no local Supabase, no Gemini credentials, no network: the
Phase 5.6 fakes (Storage, embedding provider, pool/connection/cursor,
chunk repository) are reused from ``test_ingestion_units``, and the
Phase 6.8 ``ingest_document`` wrapper is exercised directly. A fresh
``ProviderMetrics`` registry is monkeypatched onto
``app.services.ingestion._provider_metrics``.

Covered: exactly one terminal ``ingestion_pipelines_total`` and
``ingestion_pipeline_seconds`` observation per document (succeeded,
failed, caller_cancelled), the ``ingestion_failure_stages_total`` row of
a failed pipeline (and its absence when the exception has no stage),
and broken-registry invariance.
"""

import asyncio

import pytest

from app.core.observability import ProviderMetrics
from app.core.provider_concurrency import ProviderLimiters
from app.repositories import documents as documents_repository
from app.services import ingestion
from app.services.ingestion import IngestionError, ingest_document
from tests.ingestion_fakes import FakeEmbeddingProvider
from tests.test_ingestion_units import (
    _BATCH_SIZE,
    _DOCUMENT_ID,
    _STORAGE_PATH,
    FakeConnection,
    FakePool,
    _make_storage,
    _run_ingest,
)


@pytest.fixture
def metrics(monkeypatch):
    """A fresh registry wired into the ingestion module for this test."""
    registry = ProviderMetrics()
    monkeypatch.setattr(ingestion, "_provider_metrics", registry)
    return registry


@pytest.fixture
def insertion_calls(monkeypatch):
    """The chunk repository recorder (same pattern as the Phase 5.6 suite)."""
    calls = []

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
        documents_repository, "insert_document_chunks", fake_insert
    )
    return calls


def _count(registry, series, **labels):
    for entry in registry.snapshot().counters.get(series, []):
        if entry["labels"] == dict(sorted(labels.items())):
            return entry["value"]
    return None


def _duration_count(registry, series, **labels):
    for stats in registry.snapshot().durations.get(series, []):
        if stats.labels == dict(sorted(labels.items())):
            return stats.count
    return None


def _stage_count(registry, stage):
    return _count(registry, "ingestion_failure_stages_total", stage=stage)


def _assert_one_pipeline_row(registry, outcome):
    """Exactly one counter row and one matching duration row."""
    assert _count(
        registry, "ingestion_pipelines_total", ingestion_outcome=outcome
    ) == 1
    assert _duration_count(
        registry, "ingestion_pipeline_seconds", ingestion_outcome=outcome
    ) == 1

    # The other terminal outcomes were never recorded for this pipeline.
    for other in ("succeeded", "failed", "caller_cancelled"):
        if other != outcome:
            assert _count(
                registry,
                "ingestion_pipelines_total",
                ingestion_outcome=other,
            ) is None


class TestIngestionPipelineMetrics:
    def test_successful_pipeline_records_succeeded_exactly_once(
        self, metrics, insertion_calls
    ):
        storage, _ = _make_storage()
        provider = FakeEmbeddingProvider()
        connection = FakeConnection()

        inserted = _run_ingest(storage, provider, connection)

        assert inserted > 0

        _assert_one_pipeline_row(metrics, "succeeded")
        assert metrics.snapshot().counters.get(
            "ingestion_failure_stages_total"
        ) is None

    def test_text_extraction_failure_records_failed_with_stage(self, metrics):
        storage, _ = _make_storage(pdf_bytes=b"definitely not a pdf")
        provider = FakeEmbeddingProvider()
        connection = FakeConnection()

        with pytest.raises(IngestionError) as exc_info:
            _run_ingest(storage, provider, connection)

        assert exc_info.value.stage == "text_extraction"

        _assert_one_pipeline_row(metrics, "failed")
        assert _stage_count(metrics, "text_extraction") == 1

    def test_database_insertion_failure_records_failed_with_stage(
        self, metrics, monkeypatch
    ):
        storage, _ = _make_storage()
        provider = FakeEmbeddingProvider()

        async def failing_insert(cursor, *, document_id, chunks):
            raise RuntimeError("simulated insertion failure")

        monkeypatch.setattr(
            documents_repository, "insert_document_chunks", failing_insert
        )

        connection = FakeConnection()

        with pytest.raises(IngestionError) as exc_info:
            _run_ingest(storage, provider, connection)

        assert exc_info.value.stage == "database_insertion"

        _assert_one_pipeline_row(metrics, "failed")
        assert _stage_count(metrics, "database_insertion") == 1

    def test_embedding_saturated_records_failed_with_stage(self, metrics):
        storage, _ = _make_storage()
        provider = FakeEmbeddingProvider()
        connection = FakeConnection()

        # One embedding permit, fully held: the shared admission
        # deadline (the tiny ingestion wait) expires while the worker
        # holds the ingestion-gate permit and waits for embedding.
        limiters = ProviderLimiters(
            embedding_limit=1,
            generation_limit=1,
            rag_wait_seconds=2.0,
            ingestion_wait_seconds=0.05,
        )

        async def scenario():
            await limiters.embedding.acquire()

            try:
                with pytest.raises(IngestionError) as exc_info:
                    await ingest_document(
                        FakePool(connection),
                        storage,
                        provider,
                        document_id=_DOCUMENT_ID,
                        storage_path=_STORAGE_PATH,
                        batch_size=_BATCH_SIZE,
                        limiters=limiters,
                    )
            finally:
                limiters.embedding.release()

            return exc_info.value.stage

        assert asyncio.run(scenario()) == "embedding_saturated"

        _assert_one_pipeline_row(metrics, "failed")
        assert _stage_count(metrics, "embedding_saturated") == 1

    def test_invalid_batch_size_records_failed_without_a_stage_row(
        self, metrics
    ):
        storage, _ = _make_storage()
        provider = FakeEmbeddingProvider()
        connection = FakeConnection()

        # A ValueError carries no `stage` attribute: the pipeline still
        # failed, but there is no failure-stage vocabulary value for it,
        # so only the pipeline row is recorded.
        with pytest.raises(ValueError, match="batch_size must be at least 1"):
            _run_ingest(storage, provider, connection, batch_size=0)

        _assert_one_pipeline_row(metrics, "failed")

        snapshot = metrics.snapshot()
        assert snapshot.counters.get("ingestion_failure_stages_total") is None

    def test_caller_cancellation_records_cancelled_exactly_once(self, metrics):
        class _CancelledEmbeddingProvider:
            """``embed_documents`` raises simulated cancellation."""

            def embed_documents(self, texts, *, cancel_event=None):
                raise asyncio.CancelledError()

            def embed_query(self, texts, *, cancel_event=None):
                raise AssertionError(
                    "ingestion must only call embed_documents"
                )

        storage, _ = _make_storage()
        connection = FakeConnection()

        with pytest.raises(asyncio.CancelledError):
            _run_ingest(
                storage, _CancelledEmbeddingProvider(), connection
            )

        _assert_one_pipeline_row(metrics, "caller_cancelled")

    def test_two_pipelines_share_one_label_combination(
        self, metrics, insertion_calls
    ):
        _run_ingest(_make_storage()[0], FakeEmbeddingProvider(),
                    FakeConnection())
        _run_ingest(_make_storage()[0], FakeEmbeddingProvider(),
                    FakeConnection())

        rows = metrics.snapshot().counters["ingestion_pipelines_total"]

        assert len(rows) == 1  # one label combination, not two rows
        assert rows[0]["labels"] == {"ingestion_outcome": "succeeded"}
        assert rows[0]["value"] == 2

        duration_rows = (
            metrics.snapshot().durations["ingestion_pipeline_seconds"]
        )
        assert len(duration_rows) == 1
        assert duration_rows[0].count == 2


class TestBrokenRegistryInvariance:
    def test_successful_pipeline_result_is_unchanged(
        self, monkeypatch, insertion_calls
    ):
        class _BrokenRegistry:
            def record_counter(self, *args, **kwargs):
                raise RuntimeError("registry exploded")

            def record_duration(self, *args, **kwargs):
                raise RuntimeError("registry exploded")

        monkeypatch.setattr(
            ingestion, "_provider_metrics", _BrokenRegistry()
        )

        inserted = _run_ingest(
            _make_storage()[0], FakeEmbeddingProvider(), FakeConnection()
        )

        assert inserted > 0

    def test_failed_pipeline_exception_is_unchanged(self, monkeypatch):
        class _BrokenRegistry:
            def record_counter(self, *args, **kwargs):
                raise RuntimeError("registry exploded")

            def record_duration(self, *args, **kwargs):
                raise RuntimeError("registry exploded")

        monkeypatch.setattr(
            ingestion, "_provider_metrics", _BrokenRegistry()
        )

        with pytest.raises(IngestionError) as exc_info:
            _run_ingest(
                _make_storage(pdf_bytes=b"definitely not a pdf")[0],
                FakeEmbeddingProvider(),
                FakeConnection(),
            )

        assert exc_info.value.stage == "text_extraction"
