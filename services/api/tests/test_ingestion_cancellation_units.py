"""Offline unit tests for Phase 6.7 ingestion admission and cancellation.

No database, Storage service, or Gemini credentials: Storage, the pool,
and the chunk repository are faked exactly like
``test_ingestion_units.py``, and the provider is a plain callable whose
"slow work" is a ``threading.Event`` blocked inside the worker thread.
Blocking a thread (rather than awaiting) keeps every race deterministic:
an executor thread cannot be released by a scheduler tick.

Covered here: the single combined ``ingestion_gate`` + ``embedding``
admission that spans every batch, one-ingestion-at-a-time behavior,
capacity refusal (``embedding_saturated``), shutdown refusal without any
provider attempt, cancellation that keeps the permit held until the
thread returns, and embedding-failure normalization on the admitted
path.
"""

import asyncio
import threading
import time
import uuid

import pytest

from app.core.provider_concurrency import (
    ProviderBusyError,
    ProviderLimiters,
    ProviderShutdownError,
)
from app.repositories import documents as documents_repository
from app.services import ingestion
from app.services.chunking import chunk_text
from app.services.documents import extract_text
from app.services.embeddings import EmbeddingError
from app.services.ingestion import IngestionError, ingest_document
from tests.ingestion_fakes import FakeStorageClient, FakeStorageState
from tests.test_documents_units import _build_pdf


_STORAGE_PATH = "uploads/phase-6-7-admission.pdf"
_DOCUMENT_ID = uuid.UUID("00000000-0000-0000-0000-000000000c71")
_BATCH_SIZE = 2
_DIMENSIONS = 768


_PDF = _build_pdf([" ".join(f"w{number}" for number in range(4200))])
_CHUNKS = chunk_text(extract_text(_PDF))
assert len(_CHUNKS) >= 5, "premise: several batches per document"


def _limiters(
    *,
    embedding: int = 4,
    generation: int = 2,
    rag_wait: float = 2.0,
    ingestion_wait: float = 20.0,
) -> ProviderLimiters:
    return ProviderLimiters(
        embedding_limit=embedding,
        generation_limit=generation,
        rag_wait_seconds=rag_wait,
        ingestion_wait_seconds=ingestion_wait,
    )


def _make_storage() -> FakeStorageClient:
    state = FakeStorageState()
    state.objects[_STORAGE_PATH] = _PDF
    return FakeStorageClient(state)


class FakeCursor:
    async def execute(self, sql, params=None) -> None:
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class FakeConnection:
    def __init__(self):
        self.commit_calls = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor()

    async def commit(self) -> None:
        self.commit_calls += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class FakePool:
    def __init__(self, connection: FakeConnection):
        self._connection = connection

    def connection(self) -> FakeConnection:
        return self._connection


@pytest.fixture
def insertion_calls(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    async def fake_insert(cursor, *, document_id, chunks):
        calls.append({"document_id": document_id, "chunks": list(chunks)})
        return len(chunks)

    monkeypatch.setattr(
        documents_repository, "insert_document_chunks", fake_insert
    )
    return calls


class BlockingEmbeddingProvider:
    """A provider whose first call blocks inside the worker thread."""

    def __init__(self, *, dimensions: int = _DIMENSIONS):
        self.dimensions = dimensions
        self.calls: list[list[str]] = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.cancel_events: list[object] = []

    def embed_documents(self, texts, *, cancel_event=None):
        self.calls.append(list(texts))
        self.cancel_events.append(cancel_event)
        self.started.set()
        self.release.wait(5.0)

        return [[0.5] * self.dimensions for _ in texts]

    def embed_query(self, texts, *, cancel_event=None):  # pragma: no cover
        raise AssertionError("ingestion must only call embed_documents")


class FailingEmbeddingProvider:
    def __init__(self, *, fail_on_call: int = 1):
        self.fail_on_call = fail_on_call
        self.calls = 0

    def embed_documents(self, texts, *, cancel_event=None):
        self.calls += 1

        if self.calls == self.fail_on_call:
            raise EmbeddingError("simulated embedding outage")

        return [[0.5] * _DIMENSIONS for _ in texts]

    def embed_query(self, texts, *, cancel_event=None):  # pragma: no cover
        raise AssertionError("ingestion must only call embed_documents")


def _run_ingest(provider, *, limiters=None, connection=None, **kwargs):
    return ingest_document(
        FakePool(connection or FakeConnection()),
        _make_storage(),
        provider,
        document_id=_DOCUMENT_ID,
        storage_path=_STORAGE_PATH,
        batch_size=_BATCH_SIZE,
        limiters=limiters,
        **kwargs,
    )


async def _wait_until(predicate, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)

    raise AssertionError("condition was not reached in time")


def _no_permits_held(limiters: ProviderLimiters) -> bool:
    return (
        not limiters.embedding.locked()
        and not limiters.ingestion_gate.locked()
        and all(value == 0 for value in limiters._pending.values())
        and limiters._inflight == {}
    )


class TestCombinedAdmission:
    def test_one_admission_spans_every_batch(
        self, insertion_calls, monkeypatch
    ):
        """The gate and embedding permit are taken ONCE per document."""
        admissions: list[object] = []
        real_run_bounded = ingestion.run_bounded

        async def spy(resources, limiters, fn, /, *args, **kwargs):
            admissions.append(resources)
            return await real_run_bounded(
                resources, limiters, fn, *args, **kwargs
            )

        monkeypatch.setattr(ingestion, "run_bounded", spy)

        async def scenario():
            limiters = _limiters(embedding=1)
            provider = BlockingEmbeddingProvider()

            task = asyncio.create_task(
                _run_ingest(provider, limiters=limiters)
            )
            await asyncio.to_thread(provider.started.wait, 5.0)

            # While the worker is blocked inside its first batch, both
            # permits are held and no waiter slot is consumed.
            assert limiters.embedding.locked() is True
            assert limiters.ingestion_gate.locked() is True
            assert limiters._pending == {
                "ingestion_gate": 0,
                "embedding": 0,
                "generation": 0,
            }

            provider.release.set()
            inserted = await task

            return inserted, limiters, provider

        inserted, limiters, provider = asyncio.run(scenario())

        assert admissions == [("ingestion_gate", "embedding")]
        # Several batches ran inside that single admission.
        assert len(provider.calls) >= 2
        assert inserted == len(_CHUNKS)
        assert _no_permits_held(limiters)

    def test_second_ingestion_waits_for_the_first_to_finish_embedding(
        self, insertion_calls
    ):
        async def scenario():
            limiters = _limiters(embedding=4, ingestion_wait=5.0)
            first = BlockingEmbeddingProvider()
            second = BlockingEmbeddingProvider()
            second.started = threading.Event()

            first_task = asyncio.create_task(
                _run_ingest(first, limiters=limiters)
            )
            await asyncio.to_thread(first.started.wait, 5.0)

            second_task = asyncio.create_task(
                _run_ingest(second, limiters=limiters)
            )
            await asyncio.sleep(0.15)

            # The ingestion gate admits only one embedding stage at a
            # time, so the second document has not reached the provider.
            assert second_task.done() is False
            assert second.calls == []

            first.release.set()
            second.started.clear()
            second.release.set()

            await first_task
            await second_task

            assert second.calls

        asyncio.run(scenario())

    def test_capacity_refusal_is_saturated_and_releases_the_gate(self):
        async def scenario():
            limiters = _limiters(embedding=1, ingestion_wait=0.05)
            await limiters.embedding.acquire()
            provider = BlockingEmbeddingProvider()

            try:
                with pytest.raises(IngestionError) as raised:
                    await _run_ingest(provider, limiters=limiters)
            finally:
                limiters.embedding.release()

            assert raised.value.stage == "embedding_saturated"
            assert isinstance(raised.value.__cause__, ProviderBusyError)
            assert provider.calls == []
            assert _no_permits_held(limiters)

        asyncio.run(scenario())

    def test_shutdown_refusal_never_attempts_the_provider(self):
        async def scenario():
            limiters = _limiters()
            limiters.signal_shutdown()
            provider = BlockingEmbeddingProvider()

            with pytest.raises(IngestionError) as raised:
                await _run_ingest(provider, limiters=limiters)

            assert raised.value.stage == "embedding_saturated"
            assert provider.calls == []
            assert isinstance(
                raised.value.__cause__, ProviderShutdownError
            )

        asyncio.run(scenario())

    def test_embedding_failure_on_the_admitted_path_is_normalized(self):
        async def scenario():
            limiters = _limiters()
            provider = FailingEmbeddingProvider()

            with pytest.raises(IngestionError) as raised:
                await _run_ingest(provider, limiters=limiters)

            assert raised.value.stage == "embedding_generation"
            assert isinstance(raised.value.__cause__, EmbeddingError)
            assert _no_permits_held(limiters)

        asyncio.run(scenario())

    def test_without_limiters_the_direct_provider_path_is_unchanged(
        self, insertion_calls
    ):
        async def scenario():
            provider = BlockingEmbeddingProvider()
            provider.release.set()

            inserted = await _run_ingest(provider, limiters=None)

            return inserted, provider

        inserted, provider = asyncio.run(scenario())

        assert inserted == len(_CHUNKS)
        assert len(provider.calls) == len(range(0, len(_CHUNKS), _BATCH_SIZE))
        # No admission layer was involved, so no cancel_event was passed.
        assert provider.cancel_events == [None] * len(provider.calls)


class TestCancellation:
    def test_cancelled_ingestion_keeps_the_permit_until_the_thread_returns(
        self, insertion_calls
    ):
        async def scenario():
            limiters = _limiters(embedding=1)
            provider = BlockingEmbeddingProvider()

            task = asyncio.create_task(
                _run_ingest(provider, limiters=limiters)
            )
            await asyncio.to_thread(provider.started.wait, 5.0)

            task.cancel()

            with pytest.raises(asyncio.CancelledError):
                await task

            # The worker thread is still running: its permits are held
            # and its outcome is discarded, and no chunks were written.
            assert limiters.embedding.locked() is True
            assert limiters.ingestion_gate.locked() is True
            assert insertion_calls == []

            provider.release.set()
            await _wait_until(lambda: not limiters._inflight)

            assert _no_permits_held(limiters)

        asyncio.run(scenario())

    def test_cancellation_does_not_start_a_second_admission(self):
        async def scenario():
            limiters = _limiters(embedding=1, ingestion_wait=0.05)
            provider = BlockingEmbeddingProvider()

            task = asyncio.create_task(
                _run_ingest(provider, limiters=limiters)
            )
            await asyncio.to_thread(provider.started.wait, 5.0)

            task.cancel()

            with pytest.raises(asyncio.CancelledError):
                await task

            # While the abandoned worker still holds the only permit,
            # a new admission is refused rather than queued forever.
            with pytest.raises(IngestionError) as raised:
                await _run_ingest(
                    BlockingEmbeddingProvider(), limiters=limiters
                )

            assert raised.value.stage == "embedding_saturated"

            provider.release.set()
            await _wait_until(lambda: not limiters._inflight)

        asyncio.run(scenario())

    def test_batches_never_issued_concurrently(self, insertion_calls):
        peak = []

        class ConcurrencyProbe(BlockingEmbeddingProvider):
            def __init__(self):
                super().__init__()
                self.live = 0

            def embed_documents(self, texts, *, cancel_event=None):
                self.live += 1
                peak.append(self.live)

                try:
                    return super().embed_documents(
                        texts, cancel_event=cancel_event
                    )
                finally:
                    self.live -= 1

        async def scenario():
            limiters = _limiters()
            provider = ConcurrencyProbe()
            provider.release.set()

            await _run_ingest(provider, limiters=limiters)

            return provider

        provider = asyncio.run(scenario())

        assert max(peak) == 1
        assert len(provider.calls) >= 2
        assert insertion_calls
