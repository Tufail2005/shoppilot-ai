"""Offline unit tests for the Phase 6.7 cancellation marker.

No database and no Storage: ``_ingest_registered_document``'s ingestion
and outcome-persistence seams are replaced with recorders, so these
tests pin exactly what the cancellation path is allowed to do — schedule
a detached marker for the controlled ``request_cancelled`` stage, then
re-raise the cancellation immediately without waiting for it.
"""

import asyncio
import logging
import uuid

import pytest

from app.services import documents as documents_module
from app.services import ingestion as ingestion_module


_DOCUMENT_ID = uuid.UUID("00000000-0000-0000-0000-000000000d0c")


class _Document(dict):
    pass


def _document() -> _Document:
    return _Document(
        id=_DOCUMENT_ID,
        storage_path="uploads/phase-6-7-cancellation.pdf",
        title="Test",
        visibility="internal",
        status="processing",
    )


async def _drain_marker_tasks() -> None:
    """Let every scheduled marker task run to completion."""
    for _ in range(50):
        if not documents_module._CANCELLATION_TASKS:
            return
        await asyncio.sleep(0.01)

    raise AssertionError("cancellation marker task did not finish")


@pytest.fixture(autouse=True)
def _clean_marker_registry():
    documents_module._CANCELLATION_TASKS.clear()

    yield

    documents_module._CANCELLATION_TASKS.clear()


class TestCancelledIngestionMarker:
    def test_cancellation_is_recorded_and_reraised(self, monkeypatch):
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

            # The marker is detached: it has NOT written anything yet at
            # the moment the cancellation is delivered.
            assert recorded == []

            await _drain_marker_tasks()

            return recorded

        recorded = asyncio.run(scenario())

        assert recorded == [
            (
                _DOCUMENT_ID,
                "request_cancelled",
                documents_module._CANCELLATION_MESSAGE,
            )
        ]

    def test_marker_failure_is_logged_but_never_raised(
        self, monkeypatch, caplog
    ):
        async def cancelled_ingestion(*args, **kwargs):
            raise asyncio.CancelledError()

        async def failing_record(pool, document_id, *, stage, message):
            raise RuntimeError("database unavailable")

        monkeypatch.setattr(
            ingestion_module, "ingest_document", cancelled_ingestion
        )
        monkeypatch.setattr(
            documents_module, "_record_ingestion_failure", failing_record
        )

        caplog.set_level(logging.ERROR)

        async def scenario():
            with pytest.raises(asyncio.CancelledError):
                await documents_module._ingest_registered_document(
                    object(),
                    object(),
                    object(),
                    document=_document(),
                )

            await _drain_marker_tasks()

        asyncio.run(scenario())

        messages = "\n".join(
            record.getMessage() for record in caplog.records
        )

        assert "record_cancelled" in messages
        assert "RuntimeError" in messages
        # Exception text is never logged.
        assert "database unavailable" not in messages

    def test_ordinary_failure_is_not_marked_as_cancelled(self, monkeypatch):
        recorded: list[tuple] = []

        async def failing_ingestion(*args, **kwargs):
            from app.services.ingestion import IngestionError

            raise IngestionError(
                "Embedding generation failed for a document",
                stage="embedding_generation",
            )

        async def fake_record(pool, document_id, *, stage, message):
            recorded.append((document_id, stage, message))
            return {"status": "failed"}

        monkeypatch.setattr(
            ingestion_module, "ingest_document", failing_ingestion
        )
        monkeypatch.setattr(
            documents_module, "_record_ingestion_failure", fake_record
        )

        result = asyncio.run(
            documents_module._ingest_registered_document(
                object(),
                object(),
                object(),
                document=_document(),
            )
        )

        assert result == {"status": "failed"}
        assert recorded[0][1] == "embedding_generation"
        assert documents_module._CANCELLATION_TASKS == set()

    def test_record_cancellation_outside_a_loop_logs_instead_of_raising(
        self, caplog
    ):
        caplog.set_level(logging.ERROR)

        # No running event loop: scheduling is impossible, and the
        # helper must degrade to a log rather than break cancellation.
        documents_module._record_cancellation(object(), _DOCUMENT_ID)

        messages = "\n".join(
            record.getMessage() for record in caplog.records
        )

        assert "no event loop" in messages
        assert str(_DOCUMENT_ID) in messages


class TestLimitersPlumbing:
    def test_ingest_registered_document_forwards_admission_arguments(
        self, monkeypatch
    ):
        seen: dict = {}

        async def recording_ingestion(*args, **kwargs):
            seen.update(kwargs)
            return 3

        async def fake_mark_ready(pool, document_id):
            return {"status": "ready"}

        monkeypatch.setattr(
            ingestion_module, "ingest_document", recording_ingestion
        )
        monkeypatch.setattr(
            documents_module, "_mark_document_ready", fake_mark_ready
        )

        sentinel_limiters = object()
        sentinel_deadline = 123.5

        result = asyncio.run(
            documents_module._ingest_registered_document(
                object(),
                object(),
                object(),
                document=_document(),
                limiters=sentinel_limiters,
                deadline_at=sentinel_deadline,
            )
        )

        assert result == {"status": "ready"}
        assert seen["limiters"] is sentinel_limiters
        assert seen["deadline_at"] == sentinel_deadline
        assert seen["document_id"] == _DOCUMENT_ID