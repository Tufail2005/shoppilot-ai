"""Offline unit tests for the Phase 6.8 RAG request metrics.

No database, no local Supabase, no Gemini credentials, no network: the
reusable Phase 6.4 fakes (recording cursor, pool, provider stubs) are
imported from ``test_rag_query_units`` and ``rag_service.answer_query``
is invoked directly. A fresh ``ProviderMetrics`` registry is
monkeypatched onto ``app.services.rag._provider_metrics``, so the
production singleton is never touched.

Covered: exactly one terminal ``rag_requests_total`` and
``rag_request_seconds`` observation per request (succeeded, failed for
every non-cancellation exception, caller_cancelled), and broken-registry
invariance: a metrics failure can never change a returned answer or a
raised exception.
"""

import asyncio
import threading

import pytest
from fastapi import HTTPException

from app.core.observability import ProviderMetrics
from app.services import rag as rag_service
from app.services.embeddings import EmbeddingError
from app.services.generation import GenerationError
from app.services.rag_prompt import INSUFFICIENT_EVIDENCE_MESSAGE
from tests.test_rag_query_units import (
    FakePool,
    RecordingCursor,
    StubEmbeddingProvider,
    StubGenerationProvider,
    _QUERY,
    _row,
)


@pytest.fixture
def metrics(monkeypatch):
    """A fresh registry wired into the rag module for this test."""
    registry = ProviderMetrics()
    monkeypatch.setattr(rag_service, "_provider_metrics", registry)
    return registry


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


def _assert_one_request_row(registry, outcome):
    """Exactly one counter and one matching duration row."""
    assert _count(registry, "rag_requests_total", outcome=outcome) == 1
    assert _count(
        registry,
        "rag_requests_total",
        outcome={
            "succeeded": "failed",
            "failed": "succeeded",
            "caller_cancelled": "succeeded",
        }[outcome],
    ) is None
    assert _duration_count(
        registry, "rag_request_seconds", outcome=outcome
    ) == 1


def _pool(rows=None) -> FakePool:
    return FakePool(RecordingCursor(rows=rows if rows is not None else []))


def _answer_ok(*, answer="Grounded answer. [1]") -> StubGenerationProvider:
    return StubGenerationProvider(answer=answer)


def _run_query(pool, embedding=None, generation=None):
    return asyncio.run(
        rag_service.answer_query(
            pool,
            embedding if embedding is not None else StubEmbeddingProvider(),
            generation if generation is not None else _answer_ok(),
            query=_QUERY,
            user_role="customer",
        )
    )


class TestRagRequestMetrics:
    def test_successful_request_records_succeeded_exactly_once(
        self, metrics
    ):
        # Empty retrieval: the deterministic no-generation answer path.
        answer = _run_query(_pool())

        assert answer.answer == INSUFFICIENT_EVIDENCE_MESSAGE
        assert list(answer.citations) == []

        _assert_one_request_row(metrics, "succeeded")

    def test_sanitized_provider_failure_records_failed_exactly_once(
        self, metrics
    ):
        embedding = StubEmbeddingProvider(
            error=EmbeddingError(
                "simulated provider request failure", stage="provider_request"
            )
        )

        with pytest.raises(HTTPException) as exc_info:
            _run_query(_pool(), embedding)

        # The pre-existing sanitized HTTP mapping is unchanged.
        assert exc_info.value.status_code == 503
        assert exc_info.value.detail == rag_service.DETAIL_KB_UNAVAILABLE

        _assert_one_request_row(metrics, "failed")

    def test_unexpected_pipeline_failure_still_records_failed_exactly_once(
        self, metrics
    ):
        generation = StubGenerationProvider(
            error=RuntimeError("simulated internal failure")
        )

        # Non-empty retrieval: the generation stage runs and explodes.
        with pytest.raises(HTTPException) as exc_info:
            _run_query(_pool([_row()]), StubEmbeddingProvider(), generation)

        assert exc_info.value.status_code == 500

        _assert_one_request_row(metrics, "failed")
        assert _count(
            metrics, "rag_requests_total", outcome="caller_cancelled"
        ) is None

    def test_generation_configuration_failure_records_failed(self, metrics):
        generation = StubGenerationProvider(
            error=GenerationError(
                "no api key", stage="configuration"
            )
        )

        with pytest.raises(HTTPException) as exc_info:
            _run_query(_pool([_row()]), StubEmbeddingProvider(), generation)

        assert exc_info.value.detail == (
            rag_service.DETAIL_GENERATION_NOT_CONFIGURED
        )

        _assert_one_request_row(metrics, "failed")

    def test_caller_cancellation_records_cancelled_exactly_once(
        self, metrics
    ):
        release = threading.Event()
        started = threading.Event()

        class _BlockingGeneration:
            def generate(self, prompt, *, cancel_event=None):
                started.set()
                release.wait(5.0)
                return "late answer"

        async def scenario():
            blocking = _BlockingGeneration()

            caller = asyncio.create_task(
                rag_service.answer_query(
                    _pool([_row()]),
                    StubEmbeddingProvider(),
                    blocking,
                    query=_QUERY,
                    user_role="customer",
                )
            )
            await asyncio.to_thread(started.wait, 5.0)

            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller

            # The detached worker thread must not be left blocked.
            release.set()

        asyncio.run(scenario())

        _assert_one_request_row(metrics, "caller_cancelled")

    def test_two_requests_share_one_label_combination(self, metrics):
        _run_query(_pool())
        _run_query(_pool())

        rows = metrics.snapshot().counters["rag_requests_total"]

        assert len(rows) == 1  # one label combination, not two rows
        assert rows[0]["labels"] == {"outcome": "succeeded"}
        assert rows[0]["value"] == 2

        duration_rows = metrics.snapshot().durations["rag_request_seconds"]
        assert len(duration_rows) == 1
        assert duration_rows[0].count == 2


class TestBrokenRegistryInvariance:
    def test_successful_request_result_is_unchanged(self, metrics, monkeypatch):
        class _BrokenRegistry:
            def record_counter(self, *args, **kwargs):
                raise RuntimeError("registry exploded")

            def record_duration(self, *args, **kwargs):
                raise RuntimeError("registry exploded")

        monkeypatch.setattr(
            rag_service, "_provider_metrics", _BrokenRegistry()
        )

        answer = _run_query(_pool())

        assert answer.answer == INSUFFICIENT_EVIDENCE_MESSAGE
        assert list(answer.citations) == []

    def test_failed_request_exception_is_unchanged(self, monkeypatch):
        from fastapi import HTTPException as _HTTPException

        class _BrokenRegistry:
            def record_counter(self, *args, **kwargs):
                raise RuntimeError("registry exploded")

            def record_duration(self, *args, **kwargs):
                raise RuntimeError("registry exploded")

        monkeypatch.setattr(
            rag_service, "_provider_metrics", _BrokenRegistry()
        )

        embedding = StubEmbeddingProvider(
            error=EmbeddingError(
                "simulated provider request failure", stage="provider_request"
            )
        )

        with pytest.raises(_HTTPException) as exc_info:
            _run_query(_pool(), embedding)

        assert exc_info.value.status_code == 503
        assert exc_info.value.detail == rag_service.DETAIL_KB_UNAVAILABLE
