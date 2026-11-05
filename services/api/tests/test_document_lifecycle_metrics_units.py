"""Offline unit tests for the Phase 6.8 document lifecycle metrics.

No database and no Storage: ``_ingest_registered_document`` runs with
its ingestion seam replaced (the established Phase 6.7 cancellation-test
pattern) and its status persistence driven by the Phase 5.7 recording
cursor, so the REAL ``_mark_document_ready`` / ``_record_ingestion_failure``
conflict translation is exercised end to end. A fresh
``ProviderMetrics`` registry is monkeypatched onto
``app.services.documents._provider_metrics``.

Covered: exactly one ``document_lifecycle_outcomes_total`` observation
per registered-document ingest — ``ready``, ``failed``, ``conflict``
(ready- and failed-status variants), ``unexpected`` (a status
persistence failure), and ``cancelled`` recorded exactly once, never
duplicated by the detached marker task.
"""

import asyncio
import pytest
from fastapi import HTTPException

from app.core.observability import ProviderMetrics
from app.services import documents as documents_module
from app.services import ingestion as ingestion_module
from app.services.ingestion import IngestionError
from tests.test_document_cancellation_units import _drain_marker_tasks
from tests.test_document_status_units import (
    _FAKE_UPDATED_ROW,
    RecordingCursor,
)


_FAKE_FAILED_ROW = (
    _FAKE_UPDATED_ROW[:4]
    + ("failed", _FAKE_UPDATED_ROW[5])
    + ("embedding_generation", "Embedding generation failed for a document")
    + (_FAKE_UPDATED_ROW[8],)
)


@pytest.fixture(autouse=True)
def _clean_marker_registry():
    documents_module._CANCELLATION_TASKS.clear()

    yield

    documents_module._CANCELLATION_TASKS.clear()


@pytest.fixture
def metrics(monkeypatch):
    """A fresh registry wired into the documents module for this test."""
    registry = ProviderMetrics()
    monkeypatch.setattr(documents_module, "_provider_metrics", registry)
    return registry


# -- status-persistence fakes (pool/connection shape) -------------------


class _AsyncCM:
    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _StatusConnection:
    """What ``_mark_document_ready`` exercises of a pool connection."""

    def __init__(self, cursor: RecordingCursor):
        self._cursor = cursor
        self.commit_calls = 0

    def cursor(self) -> _AsyncCM:
        return _AsyncCM(self._cursor)

    async def commit(self) -> None:
        self.commit_calls += 1


class _StatusPool:
    def __init__(self, cursor: RecordingCursor):
        self._cursor = cursor

    def connection(self) -> _AsyncCM:
        return _AsyncCM(_StatusConnection(self._cursor))


# -- lifecycle metric assertion helpers ---------------------------------


def _lifecycle_rows(registry):
    return registry.snapshot().counters.get(
        "document_lifecycle_outcomes_total", []
    )


def _lifecycle_count(registry, outcome):
    for entry in _lifecycle_rows(registry):
        if entry["labels"] == {
            "document_lifecycle_outcome": outcome
        }:
            return entry["value"]
    return None


def _assert_one_lifecycle_row(registry, outcome):
    """Exactly one counter row exists for the whole ingest."""
    rows = _lifecycle_rows(registry)

    assert len(rows) == 1
    assert rows[0]["labels"] == {"document_lifecycle_outcome": outcome}
    assert rows[0]["value"] == 1


def _document() -> dict:
    return {
        "id": _FAKE_UPDATED_ROW[0],
        "storage_path": "uploads/phase-6-8-lifecycle.pdf",
        "title": "Test",
        "visibility": "internal",
        "status": "processing",
    }


async def _successful_ingestion(*args, **kwargs):
    return 3


def _run_registered(pool, *, ingestion_impl) -> dict:
    """Run ``_ingest_registered_document`` with the ingestion seam set."""

    async def runner():
        return await documents_module._ingest_registered_document(
            pool,
            object(),  # storage
            object(),  # embedding provider
            document=_document(),
        )

    return asyncio.run(runner())


# -- tests ---------------------------------------------------------------


class TestLifecycleOutcomes:
    def test_ready_is_recorded_exactly_once(self, metrics, monkeypatch):
        monkeypatch.setattr(
            ingestion_module, "ingest_document", _successful_ingestion
        )

        result = _run_registered(
            _StatusPool(RecordingCursor(row=_FAKE_UPDATED_ROW)),
            ingestion_impl=_successful_ingestion,
        )

        assert result["status"] == "ready"
        _assert_one_lifecycle_row(metrics, "ready")

    def test_failed_is_recorded_exactly_once(self, metrics, monkeypatch):
        async def failing_ingestion(*args, **kwargs):
            raise IngestionError(
                "Embedding generation failed for a document",
                stage="embedding_generation",
            )

        monkeypatch.setattr(
            ingestion_module, "ingest_document", failing_ingestion
        )

        result = _run_registered(
            _StatusPool(RecordingCursor(row=_FAKE_FAILED_ROW)),
            ingestion_impl=failing_ingestion,
        )

        assert result["status"] == "failed"
        assert result["failure_stage"] == "embedding_generation"
        assert documents_module._CANCELLATION_TASKS == set()

        rows = _lifecycle_rows(metrics)
        assert len(rows) == 1
        assert rows[0]["labels"] == {"document_lifecycle_outcome": "failed"}
        assert rows[0]["value"] == 1

    def test_ready_status_conflict_records_conflict_exactly_once(
        self, metrics, monkeypatch
    ):
        monkeypatch.setattr(
            ingestion_module, "ingest_document", _successful_ingestion
        )

        # Real `_mark_document_ready` with a zero-row guarded update:
        # a newer terminal status already won.
        try:
            _run_registered(
                _StatusPool(RecordingCursor(row=None)),
                ingestion_impl=_successful_ingestion,
            )
        except HTTPException as exc:
            assert exc.status_code == 500
            assert exc.detail == (
                documents_module._STATUS_CONFLICT_DETAIL_READY
            )
        else:
            raise AssertionError("expected the sanitized 500 conflict")

        _assert_one_lifecycle_row(metrics, "conflict")

    def test_failed_status_conflict_records_conflict_exactly_once(
        self, metrics, monkeypatch
    ):
        async def failing_ingestion(*args, **kwargs):
            raise IngestionError(
                "Embedding generation failed for a document",
                stage="embedding_generation",
            )

        monkeypatch.setattr(
            ingestion_module, "ingest_document", failing_ingestion
        )

        # Real `_record_ingestion_failure` with a zero-row guarded
        # update: a newer terminal status already won.
        try:
            _run_registered(
                _StatusPool(RecordingCursor(row=None)),
                ingestion_impl=failing_ingestion,
            )
        except HTTPException as exc:
            assert exc.status_code == 500
            assert exc.detail == (
                documents_module._STATUS_CONFLICT_DETAIL_FAILED
            )
        else:
            raise AssertionError("expected the sanitized 500 conflict")

        _assert_one_lifecycle_row(metrics, "conflict")

    def test_unexpected_persistence_failure_records_unexpected_exactly_once(
        self, metrics, monkeypatch
    ):
        monkeypatch.setattr(
            ingestion_module, "ingest_document", _successful_ingestion
        )

        # The cursor explodes: `_mark_document_ready` maps the
        # persistence failure to the same sanitized 500 as before, and
        # the lifecycle layer sees an `unexpected` outcome.
        try:
            _run_registered(
                _StatusPool(RecordingCursor(row=_FAKE_UPDATED_ROW, fail=True)),
                ingestion_impl=_successful_ingestion,
            )
        except HTTPException as exc:
            assert exc.status_code == 500
            assert exc.detail == (
                documents_module._STATUS_CONFLICT_DETAIL_READY
            )
        else:
            raise AssertionError("expected the sanitized persistence 500")

        _assert_one_lifecycle_row(metrics, "unexpected")

    def test_cancellation_records_cancelled_exactly_once_and_once_only(
        self, metrics, monkeypatch
    ):
        recorded: list[tuple] = []

        async def cancelled_ingestion(*args, **kwargs):
            raise asyncio.CancelledError()

        async def fake_record(pool, document_id, *, stage, message):
            recorded.append((document_id, stage, message))
            return {"status": "failed"}

        monkeypatch.setattr(
            ingestion_module, "ingest_document", cancelled_ingestion
        )
        monkeypatch.setattr(
            documents_module, "_record_ingestion_failure", fake_record
        )

        async def scenario():
            with pytest.raises(asyncio.CancelledError):
                await documents_module._ingest_registered_document(
                    object(),
                    object(),
                    object(),
                    document=_document(),
                )

            # The detached marker has not written anything yet; the
            # lifecycle cancellation row is already there, exactly once.
            assert recorded == []
            assert _lifecycle_count(metrics, "cancelled") == 1

            await _drain_marker_tasks()

            return _lifecycle_rows(metrics)

        rows = asyncio.run(scenario())

        # The detachment recorded the controlled marker, but it must
        # never record a second lifecycle observation.
        assert recorded == [
            (
                _FAKE_UPDATED_ROW[0],
                "request_cancelled",
                documents_module._CANCELLATION_MESSAGE,
            )
        ]
        assert len(rows) == 1
        assert rows[0]["labels"] == {"document_lifecycle_outcome": "cancelled"}
        assert rows[0]["value"] == 1
