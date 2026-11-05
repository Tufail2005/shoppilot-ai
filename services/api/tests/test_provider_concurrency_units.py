"""Offline unit tests for Phase 6.7 provider admission control.

No database, no network, no credentials: the worker functions are plain
Python callables and the "slow" work is a ``threading.Event`` blocked in
an executor thread, which keeps every race deterministic (a blocked
thread cannot be released by a scheduler tick).

Covered here: permit lifetime across cancellation, the pending-waiter
cap (including the concurrent-arrival burst), combined multi-permit
acquisition with one shared admission budget, deadline/margin handling,
shutdown behaviour, and configuration mapping.
"""

import asyncio
import threading
import time

import pytest

from app.core import provider_concurrency
from app.core.provider_concurrency import (
    ProviderBusyError,
    ProviderLimiters,
    ProviderShutdownError,
    run_bounded,
)
from app.core.provider_retry import ProviderCallAborted


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


async def _wait_until(predicate, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)

    raise AssertionError("condition was not reached in time")


class TestPermitLifetime:
    def test_permit_is_held_while_a_cancelled_worker_still_runs(self):
        async def scenario():
            limiters = _limiters(embedding=1, rag_wait=0.05)
            started = threading.Event()
            release = threading.Event()
            completed = []

            def fn(*, cancel_event=None):
                started.set()
                release.wait(5.0)
                completed.append(True)
                return "done"

            caller = asyncio.create_task(run_bounded("embedding", limiters, fn))
            await asyncio.to_thread(started.wait, 5.0)

            caller.cancel()

            with pytest.raises(asyncio.CancelledError):
                await caller

            # The worker keeps running and keeps its permit.
            assert limiters.embedding.locked() is True
            assert completed == []

            with pytest.raises(ProviderBusyError):
                await run_bounded("embedding", limiters, fn, wait_seconds=0.05)

            release.set()
            await _wait_until(lambda: not limiters._inflight)

            assert completed == [True]
            assert limiters.embedding.locked() is False

            assert await run_bounded("embedding", limiters, fn) == "done"

        asyncio.run(scenario())

    def test_cancelled_request_does_not_cancel_the_worker(self):
        async def scenario():
            limiters = _limiters(embedding=1)
            release = threading.Event()
            started = threading.Event()

            def fn(*, cancel_event=None):
                started.set()
                release.wait(5.0)
                return "ok"

            caller = asyncio.create_task(run_bounded("embedding", limiters, fn))
            await asyncio.to_thread(started.wait, 5.0)

            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller

            release.set()
            await _wait_until(lambda: not limiters._inflight)
            assert limiters.embedding.locked() is False

        asyncio.run(scenario())

    def test_abandoned_worker_exception_is_consumed(self):
        async def scenario():
            limiters = _limiters(embedding=1)
            release = threading.Event()
            started = threading.Event()
            seen = []

            def handler(loop, context):  # pragma: no cover - must not run
                seen.append(context)

            asyncio.get_running_loop().set_exception_handler(handler)

            def fn(*, cancel_event=None):
                started.set()
                release.wait(5.0)
                raise RuntimeError("abandoned failure")

            caller = asyncio.create_task(run_bounded("embedding", limiters, fn))
            await asyncio.to_thread(started.wait, 5.0)

            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller

            release.set()
            await _wait_until(lambda: not limiters._inflight)
            await asyncio.sleep(0.05)

            assert seen == []
            assert limiters.embedding.locked() is False

        asyncio.run(scenario())


class TestAdmissionFastPath:
    def test_available_permit_creates_no_acquisition_helper_task(self, monkeypatch):
        created = []
        real = provider_concurrency.ensure_future

        def spy(factory, **kwargs):
            created.append(factory)
            return real(factory, **kwargs)

        monkeypatch.setattr(provider_concurrency, "ensure_future", spy)

        async def scenario():
            limiters = _limiters(embedding=4)

            assert await run_bounded(
                "embedding", limiters, lambda **_: "fast"
            ) == "fast"

            assert created == []
            assert limiters._pending["embedding"] == 0

        asyncio.run(scenario())

    def test_concurrent_arrival_burst_cannot_bypass_the_pending_cap(self):
        async def scenario():
            limiters = _limiters(embedding=1, rag_wait=30.0)
            release = threading.Event()
            started = threading.Event()
            calls = []

            def fn(*, cancel_event=None):
                calls.append(1)
                started.set()
                release.wait(5.0)
                return "ok"

            first = asyncio.create_task(run_bounded("embedding", limiters, fn))
            await asyncio.to_thread(started.wait, 5.0)

            others = [
                asyncio.create_task(run_bounded("embedding", limiters, fn))
                for _ in range(9)
            ]
            await asyncio.sleep(0.1)

            busy = [
                task
                for task in others
                if task.done()
                and not task.cancelled()
                and isinstance(task.exception(), ProviderBusyError)
            ]
            waiting = [task for task in others if task not in busy]

            assert len(busy) == 7
            assert len(waiting) == 2
            assert limiters._pending["embedding"] == 2
            assert calls == [1]

            release.set()

            assert await first == "ok"
            assert [await task for task in waiting] == ["ok", "ok"]

            assert limiters._pending["embedding"] == 0
            assert limiters.embedding.locked() is False

        asyncio.run(scenario())

    def test_pending_counter_returns_to_zero_on_timeout(self):
        async def scenario():
            limiters = _limiters(embedding=1, rag_wait=0.05)
            await limiters.embedding.acquire()

            try:
                with pytest.raises(ProviderBusyError):
                    await run_bounded(
                        "embedding", limiters, lambda **_: "no"
                    )

                assert limiters._pending["embedding"] == 0
            finally:
                limiters.embedding.release()

        asyncio.run(scenario())

    def test_deadline_cap_shortens_the_wait(self):
        async def scenario():
            limiters = _limiters(embedding=1, rag_wait=5.0)
            await limiters.embedding.acquire()

            loop = asyncio.get_running_loop()
            started = loop.time()

            try:
                with pytest.raises(ProviderBusyError):
                    await run_bounded(
                        "embedding",
                        limiters,
                        lambda **_: "no",
                        deadline_at=loop.time() + 0.15,
                    )
            finally:
                limiters.embedding.release()

            assert loop.time() - started < 1.5

        asyncio.run(scenario())

    def test_remaining_at_or_below_margin_raises_immediately(self):
        async def scenario():
            limiters = _limiters(embedding=1, rag_wait=5.0)
            loop = asyncio.get_running_loop()
            started = loop.time()

            with pytest.raises(ProviderBusyError):
                await run_bounded(
                    "embedding",
                    limiters,
                    lambda **_: "no",
                    deadline_at=loop.time() + 0.1,
                    margin=0.25,
                )

            assert loop.time() - started < 0.1
            assert limiters._pending["embedding"] == 0

        asyncio.run(scenario())


class TestCombinedAdmission:
    def test_combined_acquisition_runs_the_function_exactly_once(self):
        async def scenario():
            limiters = _limiters(embedding=4)
            calls = []

            def fn(*, cancel_event=None):
                calls.append(1)
                return "vectors"

            result = await run_bounded(
                ("ingestion_gate", "embedding"), limiters, fn
            )

            assert result == "vectors"
            assert calls == [1]
            assert limiters.ingestion_gate.locked() is False
            assert limiters.embedding.locked() is False

        asyncio.run(scenario())

    def test_partial_acquisition_releases_the_gate(self):
        async def scenario():
            limiters = _limiters(embedding=1, ingestion_wait=0.1)
            await limiters.embedding.acquire()
            calls = []

            try:
                with pytest.raises(ProviderBusyError):
                    await run_bounded(
                        ("ingestion_gate", "embedding"),
                        limiters,
                        lambda **_: calls.append(1),
                    )
            finally:
                limiters.embedding.release()

            assert calls == []
            assert limiters.ingestion_gate.locked() is False
            assert limiters._pending["ingestion_gate"] == 0
            assert limiters._pending["embedding"] == 0

        asyncio.run(scenario())

    def test_combined_wait_never_accumulates_two_budgets(self):
        async def scenario():
            limiters = _limiters(embedding=1, ingestion_wait=0.4)
            await limiters.ingestion_gate.acquire()
            await limiters.embedding.acquire()

            async def release_gate_later():
                await asyncio.sleep(0.2)
                limiters.ingestion_gate.release()

            asyncio.create_task(release_gate_later())
            loop = asyncio.get_running_loop()
            started = loop.time()

            try:
                with pytest.raises(ProviderBusyError):
                    await run_bounded(
                        ("ingestion_gate", "embedding"),
                        limiters,
                        lambda **_: "no",
                    )
            finally:
                limiters.embedding.release()

            elapsed = loop.time() - started

            # One 0.4 s budget, not two (0.8 s): the gate wait consumed
            # 0.2 s and the embedding wait is shortened to the remainder.
            assert 0.3 <= elapsed < 0.7
            assert limiters._pending["ingestion_gate"] == 0
            assert limiters._pending["embedding"] == 0

        asyncio.run(scenario())

    def test_only_one_ingestion_batch_runs_at_a_time(self):
        async def scenario():
            limiters = _limiters(embedding=4, ingestion_wait=5.0)
            concurrent = []
            peak = []
            gate = threading.Event()

            def fn(*, cancel_event=None):
                concurrent.append(1)
                peak.append(len(concurrent))
                gate.wait(2.0)
                concurrent.pop()
                return "ok"

            tasks = [
                asyncio.create_task(
                    run_bounded(("ingestion_gate", "embedding"), limiters, fn)
                )
                for _ in range(3)
            ]

            await asyncio.sleep(0.2)
            assert max(peak) == 1

            gate.set()
            await asyncio.gather(*tasks)

            assert limiters._pending["ingestion_gate"] == 0

        asyncio.run(scenario())


class TestShutdown:
    def test_waiting_request_does_not_start_worker_after_shutdown_begins(self):
        async def scenario():
            limiters = _limiters(embedding=1, rag_wait=30.0)
            await limiters.embedding.acquire()
            calls = []

            def fn(*, cancel_event=None):  # pragma: no cover - must not run
                calls.append(1)
                return "no"

            waiter = asyncio.create_task(run_bounded("embedding", limiters, fn))
            await asyncio.sleep(0.05)

            loop = asyncio.get_running_loop()
            started = loop.time()

            await limiters.aclose()

            with pytest.raises(ProviderShutdownError):
                await waiter

            assert loop.time() - started < 1.0
            assert calls == []
            assert limiters._inflight == {}
            assert limiters._pending["embedding"] == 0

            limiters.embedding.release()

        asyncio.run(scenario())

    def test_aclose_is_idempotent(self):
        async def scenario():
            limiters = _limiters()

            await limiters.aclose()
            await limiters.aclose()

            assert limiters.shutting_down.is_set()

        asyncio.run(scenario())

    def test_aclose_retains_unfinished_tasks_and_does_not_release_permits(self):
        async def scenario():
            limiters = _limiters(embedding=1)
            release = threading.Event()
            started = threading.Event()

            def fn(*, cancel_event=None):
                started.set()
                release.wait(5.0)
                return "late"

            caller = asyncio.create_task(run_bounded("embedding", limiters, fn))
            await asyncio.to_thread(started.wait, 5.0)

            await limiters.aclose(timeout=0.05)

            assert len(limiters._inflight) == 1
            assert limiters.embedding.locked() is True

            release.set()
            await _wait_until(lambda: not limiters._inflight)
            assert limiters.embedding.locked() is False

            with pytest.raises(ProviderShutdownError):
                await caller

        asyncio.run(scenario())

    def test_new_calls_after_shutdown_raise_shutdown_error(self):
        async def scenario():
            limiters = _limiters()
            limiters.signal_shutdown()

            with pytest.raises(ProviderShutdownError):
                await run_bounded("embedding", limiters, lambda **_: "no")

        asyncio.run(scenario())

    def test_shutdown_abort_maps_to_shutdown_error_not_500(self):
        async def scenario():
            limiters = _limiters(embedding=1)
            started = threading.Event()

            def fn(*, cancel_event=None):
                started.set()
                cancel_event.wait(5.0)
                raise ProviderCallAborted("shutdown")

            caller = asyncio.create_task(run_bounded("embedding", limiters, fn))
            await asyncio.to_thread(started.wait, 5.0)

            await limiters.aclose(timeout=2.0)

            with pytest.raises(ProviderShutdownError):
                await caller

            assert limiters.embedding.locked() is False

        asyncio.run(scenario())

    def test_rag_wait_expires_while_an_ingestion_batch_runs(self):
        async def scenario():
            limiters = _limiters(embedding=1, rag_wait=0.1, ingestion_wait=5.0)
            release = threading.Event()
            started = threading.Event()

            def ingestion_fn(*, cancel_event=None):
                started.set()
                release.wait(5.0)
                return "batch"

            batch = asyncio.create_task(
                run_bounded(
                    ("ingestion_gate", "embedding"),
                    limiters,
                    ingestion_fn,
                )
            )
            await asyncio.to_thread(started.wait, 5.0)

            try:
                with pytest.raises(ProviderBusyError):
                    await run_bounded("embedding", limiters, lambda **_: "rag")
            finally:
                release.set()
                await batch

        asyncio.run(scenario())


class TestFromSettings:
    def test_defaults_map_to_the_provisional_limits(self):
        from types import SimpleNamespace

        settings = SimpleNamespace(
            gemini_embedding_max_concurrency=4,
            gemini_generation_max_concurrency=2,
            rag_provider_concurrency_wait_seconds=2.0,
            ingestion_provider_concurrency_wait_seconds=20.0,
        )

        limiters = ProviderLimiters.from_settings(settings)

        assert limiters.embedding_limit == 4
        assert limiters.generation_limit == 2
        assert limiters.rag_wait_seconds == 2.0
        assert limiters.ingestion_wait_seconds == 20.0
        assert limiters.pending_cap == {
            "ingestion_gate": 2,
            "embedding": 8,
            "generation": 4,
        }


# ---------------------------------------------------------------------------
# Phase 6.8 provider-call metrics
# ---------------------------------------------------------------------------


@pytest.fixture
def metrics(monkeypatch):
    """A fresh ProviderMetrics registry wired into the module.

    The module-level singleton stays untouched, so production call
    sites and other tests are unaffected.
    """
    from app.core.observability import ProviderMetrics

    registry = ProviderMetrics()
    monkeypatch.setattr(provider_concurrency, "_provider_metrics", registry)
    return registry


def _count(registry, series, **labels):
    for entry in registry.snapshot().counters.get(series, []):
        if entry["labels"] == labels:
            return entry["value"]
    return None


def _duration_count(registry, series, **labels):
    for stats in registry.snapshot().durations.get(series, []):
        if stats.labels == dict(sorted(labels.items())):
            return stats.count
    return None


class TestAdmissionMetrics:
    def test_fast_path_records_one_acquired_admission(self, metrics):
        async def scenario():
            limiters = _limiters(embedding=4)

            assert await run_bounded(
                "embedding", limiters, lambda **_: "fast"
            ) == "fast"

        asyncio.run(scenario())

        assert _count(
            metrics,
            "provider_admissions_total",
            operation="unattributed",
            resource="embedding",
            admission_outcome="acquired",
        ) == 1
        assert _duration_count(
            metrics,
            "provider_admission_wait_seconds",
            operation="unattributed",
            resource="embedding",
        ) == 1

    def test_combined_admission_observes_each_resource(self, metrics):
        async def scenario():
            limiters = _limiters(embedding=4)

            assert await run_bounded(
                ("ingestion_gate", "embedding"),
                limiters,
                lambda **_: "batch",
                operation="ingestion_embedding",
            ) == "batch"

        asyncio.run(scenario())

        assert _count(
            metrics,
            "provider_admissions_total",
            operation="ingestion_embedding",
            resource="ingestion_gate",
            admission_outcome="acquired",
        ) == 1
        assert _count(
            metrics,
            "provider_admissions_total",
            operation="ingestion_embedding",
            resource="embedding",
            admission_outcome="acquired",
        ) == 1

    def test_timeout_refusal_is_saturated(self, metrics):
        async def scenario():
            limiters = _limiters(embedding=1, rag_wait=0.05)
            await limiters.embedding.acquire()

            try:
                with pytest.raises(ProviderBusyError):
                    await run_bounded(
                        "embedding", limiters, lambda **_: "no"
                    )
            finally:
                limiters.embedding.release()

        asyncio.run(scenario())

        assert _count(
            metrics,
            "provider_admissions_total",
            operation="unattributed",
            resource="embedding",
            admission_outcome="saturated",
        ) == 1
        assert _count(
            metrics,
            "provider_admissions_total",
            operation="unattributed",
            resource="embedding",
            admission_outcome="acquired",
        ) is None

    def test_pending_cap_refusal_is_saturated_without_waiting(self, metrics):
        async def scenario():
            limiters = _limiters(embedding=1)
            # The single permit is held (slow path) and the waiter
            # slots are already exhausted, so the next acquisition is
            # refused instantly (pending cap) without ever waiting.
            await limiters.embedding.acquire()
            limiters._pending["embedding"] = limiters.pending_cap[
                "embedding"
            ]
            started = provider_concurrency._now()

            try:
                with pytest.raises(ProviderBusyError):
                    await run_bounded("embedding", limiters, lambda **_: "no")
            finally:
                limiters.embedding.release()

            assert provider_concurrency._now() - started < 0.05

        asyncio.run(scenario())

        assert _count(
            metrics,
            "provider_admissions_total",
            operation="unattributed",
            resource="embedding",
            admission_outcome="saturated",
        ) == 1
        assert _duration_count(
            metrics,
            "provider_admission_wait_seconds",
            operation="unattributed",
            resource="embedding",
        ) == 1

    def test_shutdown_is_distinguished_from_saturation(self, metrics):
        async def scenario():
            limiters = _limiters()
            limiters.signal_shutdown()

            loop = asyncio.get_running_loop()

            # Admission level: an acquisition under shutdown refuses
            # with shutdown (recorded per resource by _acquire_one).
            with pytest.raises(ProviderShutdownError):
                await provider_concurrency._acquire_one(
                    limiters.semaphore_for("embedding"),
                    "embedding",
                    limiters,
                    loop.time() + 1.0,
                    operation="query_embedding",
                )

            # Caller level: run_bounded refuses with shutdown before
            # any acquisition is attempted, so only the caller row
            # exists for this call — never a saturation row.
            with pytest.raises(ProviderShutdownError):
                await run_bounded("embedding", limiters, lambda **_: "no")

        asyncio.run(scenario())

        assert _count(
            metrics,
            "provider_admissions_total",
            operation="query_embedding",
            resource="embedding",
            admission_outcome="shutdown",
        ) == 1
        assert _count(
            metrics,
            "provider_admissions_total",
            operation="query_embedding",
            resource="embedding",
            admission_outcome="saturated",
        ) is None

        assert _count(
            metrics,
            "provider_caller_outcomes_total",
            operation="unattributed",
            caller_outcome="shutdown",
        ) == 1

    def test_caller_cancelled_during_wait_records_caller_cancelled(
        self, metrics
    ):
        async def scenario():
            limiters = _limiters(embedding=1, rag_wait=5.0)
            await limiters.embedding.acquire()

            try:
                caller = asyncio.create_task(
                    run_bounded("embedding", limiters, lambda **_: "no")
                )
                await asyncio.sleep(0.05)
                caller.cancel()

                with pytest.raises(asyncio.CancelledError):
                    await caller
            finally:
                limiters.embedding.release()

        asyncio.run(scenario())

        assert _count(
            metrics,
            "provider_admissions_total",
            operation="unattributed",
            resource="embedding",
            admission_outcome="caller_cancelled",
        ) == 1
        assert _count(
            metrics,
            "provider_caller_outcomes_total",
            operation="unattributed",
            caller_outcome="caller_cancelled",
        ) == 1

    def test_unexpected_acquisition_failure_is_never_a_refusal(self, metrics):
        async def scenario():
            limiters = _limiters(embedding=4)

            class _FailingSemaphore:
                def locked(self):
                    return False

                async def acquire(self):
                    raise RuntimeError("simulated internal failure")

            loop = asyncio.get_running_loop()
            semaphore = _FailingSemaphore()

            with pytest.raises(RuntimeError):
                await provider_concurrency._acquire_one(
                    semaphore,
                    "embedding",
                    limiters,
                    loop.time() + 1.0,
                    operation="query_embedding",
                )

        asyncio.run(scenario())

        assert _count(
            metrics,
            "provider_admissions_total",
            operation="query_embedding",
            resource="embedding",
            admission_outcome="unexpected",
        ) == 1


class TestCallerOutcomeMetrics:
    def test_success_records_one_coarse_caller_row(self, metrics):
        async def scenario():
            limiters = _limiters(embedding=4)

            assert await run_bounded(
                "generation", limiters, lambda **_: "answer"
            ) == "answer"

        asyncio.run(scenario())

        assert _count(
            metrics,
            "provider_caller_outcomes_total",
            operation="unattributed",
            caller_outcome="succeeded",
        ) == 1
        assert _duration_count(
            metrics,
            "provider_call_seconds",
            operation="unattributed",
            caller_outcome="succeeded",
        ) == 1
        assert _duration_count(
            metrics,
            "provider_call_seconds",
            operation="unattributed",
            caller_outcome="saturated",
        ) is None

    def test_saturation_refusal_records_saturated_caller(self, metrics):
        async def scenario():
            limiters = _limiters(embedding=1, rag_wait=0.05)
            await limiters.embedding.acquire()

            try:
                with pytest.raises(ProviderBusyError):
                    await run_bounded("embedding", limiters, lambda **_: "no")
            finally:
                limiters.embedding.release()

        asyncio.run(scenario())

        assert _count(
            metrics,
            "provider_caller_outcomes_total",
            operation="unattributed",
            caller_outcome="saturated",
        ) == 1

    def test_worker_failure_is_bucketed_provider_failure_not_unexpected(
        self, metrics, monkeypatch
    ):
        from app.services.embeddings import EmbeddingError

        async def scenario():
            limiters = _limiters(embedding=4)

            def failing_fn(*, cancel_event=None):
                raise EmbeddingError("simulated", stage="provider_request")

            with pytest.raises(EmbeddingError):
                await run_bounded(
                    "embedding",
                    limiters,
                    failing_fn,
                    operation="query_embedding",
                )

        asyncio.run(scenario())

        assert _count(
            metrics,
            "provider_caller_outcomes_total",
            operation="query_embedding",
            caller_outcome="provider_failure",
        ) == 1
        assert _duration_count(
            metrics,
            "provider_call_seconds",
            operation="query_embedding",
            caller_outcome="provider_failure",
        ) == 1


class TestWorkerMetrics:
    def test_detached_worker_success_after_caller_cancellation(
        self, metrics
    ):
        """Caller cancellation never overrides the worker's actual outcome.

        Caller cancellation aborts the call (``scope.cancel``) but
        never stops the worker; when the thread returns successfully
        anyway, its completion is observed exactly once as
        ``succeeded`` — never ``worker_aborted`` merely because the
        cancel event was set — and the permit is released by the
        callbacks, never leaked.
        """
        async def scenario():
            limiters = _limiters(embedding=1)
            release = threading.Event()
            started = threading.Event()

            def fn(*, cancel_event=None):
                started.set()
                release.wait(5.0)
                return "late success"

            caller = asyncio.create_task(
                run_bounded("embedding", limiters, fn)
            )
            await asyncio.to_thread(started.wait, 5.0)

            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller

            release.set()
            await _wait_until(lambda: not limiters._inflight)
            await asyncio.sleep(0.05)

        asyncio.run(scenario())

        # Caller outcome: cancelled, exactly once.
        assert _count(
            metrics,
            "provider_caller_outcomes_total",
            operation="unattributed",
            caller_outcome="caller_cancelled",
        ) == 1
        # Worker outcome: the detached worker genuinely succeeded —
        # caller cancellation does not rewrite the worker's result.
        assert _count(
            metrics,
            "provider_worker_outcomes_total",
            operation="unattributed",
            resource="embedding",
            worker_outcome="succeeded",
        ) == 1
        assert _count(
            metrics,
            "provider_worker_outcomes_total",
            operation="unattributed",
            resource="embedding",
            worker_outcome="worker_aborted",
        ) is None
        assert _count(
            metrics,
            "provider_worker_outcomes_total",
            operation="unattributed",
            resource="embedding",
            worker_outcome="shutdown",
        ) is None
        assert _duration_count(
            metrics,
            "provider_worker_seconds",
            operation="unattributed",
            resource="embedding",
            worker_outcome="succeeded",
        ) == 1

    def test_detached_worker_failure_after_caller_cancellation(
        self, metrics
    ):
        """A detached worker's real provider error is classified, not
        forced into ``worker_aborted`` by the caller's cancellation."""
        from app.services.embeddings import EmbeddingError

        async def scenario():
            limiters = _limiters(embedding=1)
            release = threading.Event()
            started = threading.Event()

            def late_failure(*, cancel_event=None):
                started.set()
                release.wait(5.0)
                raise EmbeddingError(
                    "simulated timeout", stage="embedding_timeout"
                )

            caller = asyncio.create_task(
                run_bounded("embedding", limiters, late_failure)
            )
            await asyncio.to_thread(started.wait, 5.0)

            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller

            release.set()
            await _wait_until(lambda: not limiters._inflight)
            await asyncio.sleep(0.05)

        asyncio.run(scenario())

        assert _count(
            metrics,
            "provider_caller_outcomes_total",
            operation="unattributed",
            caller_outcome="caller_cancelled",
        ) == 1
        # The worker's actual provider failure is classified through
        # the retry layer's outcome vocabulary.
        assert _count(
            metrics,
            "provider_worker_outcomes_total",
            operation="unattributed",
            resource="embedding",
            worker_outcome="timeout",
        ) == 1
        assert _count(
            metrics,
            "provider_worker_outcomes_total",
            operation="unattributed",
            resource="embedding",
            worker_outcome="worker_aborted",
        ) is None

    def test_worker_provider_call_aborted_is_worker_aborted_or_shutdown(
        self, metrics
    ):
        """``ProviderCallAborted`` reaches the caller as the sanitized
        shutdown error while the worker observation stays precise."""
        from app.core.provider_retry import ProviderCallAborted

        async def scenario():
            limiters = _limiters(embedding=4)

            def aborted_fn(*, cancel_event=None):
                raise ProviderCallAborted("simulated retry-loop abort")

            with pytest.raises(ProviderShutdownError):
                await run_bounded(
                    "embedding", limiters, aborted_fn,
                    operation="query_embedding",
                )

        asyncio.run(scenario())

        # Worker: a retry-loop abort without a process shutdown is
        # abort, not shutdown.
        assert _count(
            metrics,
            "provider_worker_outcomes_total",
            operation="query_embedding",
            resource="embedding",
            worker_outcome="worker_aborted",
        ) == 1
        assert _count(
            metrics,
            "provider_worker_outcomes_total",
            operation="query_embedding",
            resource="embedding",
            worker_outcome="shutdown",
        ) is None

    def test_worker_exception_records_the_retry_outcome(self, metrics):
        from app.services.embeddings import EmbeddingError

        async def scenario():
            limiters = _limiters(embedding=4)

            def failing_fn(*, cancel_event=None):
                raise EmbeddingError("boom", stage="embedding_timeout")

            with pytest.raises(EmbeddingError):
                await run_bounded(
                    "embedding", limiters, failing_fn,
                    operation="query_embedding",
                )

        asyncio.run(scenario())

        assert _count(
            metrics,
            "provider_worker_outcomes_total",
            operation="query_embedding",
            resource="embedding",
            worker_outcome="timeout",
        ) == 1

    def test_worker_outcome_is_recorded_exactly_once(self, metrics):
        async def scenario():
            limiters = _limiters(embedding=4)

            def fn(*, cancel_event=None):
                return "done"

            for _ in range(3):
                assert await run_bounded(
                    "generation", limiters, fn
                ) == "done"

        asyncio.run(scenario())

        series = metrics.snapshot().counters.get(
            "provider_worker_outcomes_total", []
        )
        assert len(series) == 1  # one label combination, not three rows
        assert series[0]["value"] == 3

    def test_metrics_callback_runs_after_the_permit_release(
        self, metrics, monkeypatch
    ):
        """The permit is already released when the outcome is observed."""
        holder = {}
        sampled_locked_states = []

        real_record = metrics.record_counter

        def recording(series, labels, **kwargs):
            if series == "provider_worker_outcomes_total":
                # Observed from inside the metrics callback: whether
                # _on_worker_done had already released the permit.
                sampled_locked_states.append(
                    holder["limiters"].embedding.locked()
                )
            return real_record(series, labels, **kwargs)

        monkeypatch.setattr(metrics, "record_counter", recording)

        async def scenario():
            limiters = _limiters(embedding=2)
            holder["limiters"] = limiters

            def fn(*, cancel_event=None):
                return "done"

            assert await run_bounded("embedding", limiters, fn) == "done"

            # The permit is back after the call.
            assert limiters.embedding.locked() is False

        asyncio.run(scenario())

        # Done callbacks run in registration order: _on_worker_done
        # (release) before _on_worker_metrics, so the permit was free.
        assert sampled_locked_states == [False]

    def test_worker_metrics_never_run_before_the_permit_release(
        self, metrics, monkeypatch
    ):
        """Definitive ordering check via the in-flight registry."""
        observed = []
        holder = {}

        real_record = metrics.record_counter

        def recording(series, labels, **kwargs):
            if series == "provider_worker_outcomes_total":
                inflight = holder["limiters"]._inflight
                observed.append([task for task in inflight if task.done()])
            return real_record(series, labels, **kwargs)

        monkeypatch.setattr(metrics, "record_counter", recording)

        async def scenario():
            limiters = _limiters(embedding=2)
            holder["limiters"] = limiters

            def fn(*, cancel_event=None):
                return "done"

            assert await run_bounded("embedding", limiters, fn) == "done"

            # The completed worker was removed from the registry.
            assert limiters._inflight == {}

        asyncio.run(scenario())

        # The release callback ran before the metrics callback (done
        # callbacks run in registration order), so the finished worker
        # task was no longer registered when its outcome was observed.
        assert observed == [[]]

    def test_shutdown_discarded_worker_records_shutdown(self, metrics):
        async def scenario():
            limiters = _limiters(embedding=1)
            started = threading.Event()
            release = threading.Event()

            def fn(*, cancel_event=None):
                started.set()
                release.wait(5.0)
                return "late"

            caller = asyncio.create_task(run_bounded("embedding", limiters, fn))
            await asyncio.to_thread(started.wait, 5.0)

            await limiters.aclose(timeout=0.05)
            release.set()

            await _wait_until(lambda: not limiters._inflight)
            await asyncio.sleep(0.05)

        asyncio.run(scenario())

        assert _count(
            metrics,
            "provider_worker_outcomes_total",
            operation="unattributed",
            resource="embedding",
            worker_outcome="shutdown",
        ) == 1
        assert _count(
            metrics,
            "provider_worker_outcomes_total",
            operation="unattributed",
            resource="embedding",
            worker_outcome="succeeded",
        ) is None

    def test_worker_aborted_and_shutdown_observed_by_the_helper(
        self, metrics
    ):
        """Direct helper tests for the task-level outcome mapping."""

        class _FakeTask:
            def __init__(self, *, cancelled=False, exception=None):
                self._cancelled = cancelled
                self._exception = exception

            def cancelled(self):
                return self._cancelled

            def exception(self):
                if self._cancelled:
                    raise asyncio.CancelledError()

                return self._exception

        provider_concurrency._on_worker_metrics(
            _FakeTask(cancelled=True),
            "query_embedding",
            "embedding",
            threading.Event(),
            _limiters(),
            provider_concurrency._now(),
        )
        assert _count(
            metrics,
            "provider_worker_outcomes_total",
            operation="query_embedding",
            resource="embedding",
            worker_outcome="worker_aborted",
        ) == 1

        # Caller-aborted call (the cancel event was set by the caller's
        # cancellation, NOT by shutdown): the worker's ACTUAL outcome
        # is recorded — a clean return is succeeded, a real provider
        # failure keeps its retry classification, and a caller abort
        # never rewires either into worker_aborted.
        caller_abort_event = threading.Event()
        caller_abort_event.set()

        provider_concurrency._on_worker_metrics(
            _FakeTask(exception=None),
            "query_embedding",
            "embedding",
            caller_abort_event,
            _limiters(),
            provider_concurrency._now(),
        )
        assert _count(
            metrics,
            "provider_worker_outcomes_total",
            operation="query_embedding",
            resource="embedding",
            worker_outcome="succeeded",
        ) == 1

        from app.services.embeddings import EmbeddingError

        provider_concurrency._on_worker_metrics(
            _FakeTask(
                exception=EmbeddingError(
                    "simulated", stage="embedding_timeout"
                )
            ),
            "query_embedding",
            "embedding",
            caller_abort_event,
            _limiters(),
            provider_concurrency._now(),
        )
        assert _count(
            metrics,
            "provider_worker_outcomes_total",
            operation="query_embedding",
            resource="embedding",
            worker_outcome="timeout",
        ) == 1

        # A worker exception that is not a provider error at all stays
        # unexpected even under a caller abort.
        provider_concurrency._on_worker_metrics(
            _FakeTask(exception=RuntimeError("survived")),
            "query_embedding",
            "embedding",
            caller_abort_event,
            _limiters(),
            provider_concurrency._now(),
        )
        assert _count(
            metrics,
            "provider_worker_outcomes_total",
            operation="query_embedding",
            resource="embedding",
            worker_outcome="unexpected",
        ) == 1

        # ProviderCallAborted without shutdown: worker_aborted.
        from app.core.provider_retry import ProviderCallAborted

        provider_concurrency._on_worker_metrics(
            _FakeTask(exception=ProviderCallAborted("retry abort")),
            "query_embedding",
            "embedding",
            threading.Event(),
            _limiters(),
            provider_concurrency._now(),
        )
        assert _count(
            metrics,
            "provider_worker_outcomes_total",
            operation="query_embedding",
            resource="embedding",
            worker_outcome="worker_aborted",
        ) == 2  # cancelled task + retry-loop abort, same combo

        # Process shutdown: the discarded worker outcome is shutdown.
        shutdown_event = threading.Event()
        shutdown_event.set()
        shutting_limiters = _limiters()
        shutting_limiters.signal_shutdown()

        provider_concurrency._on_worker_metrics(
            _FakeTask(exception=RuntimeError("survived")),
            "rag_generation",
            "generation",
            shutdown_event,
            shutting_limiters,
            provider_concurrency._now(),
        )
        assert _count(
            metrics,
            "provider_worker_outcomes_total",
            operation="rag_generation",
            resource="generation",
            worker_outcome="shutdown",
        ) == 1

    def test_broken_registry_never_changes_concurrency_behavior(
        self, monkeypatch
    ):
        class _BrokenRegistry:
            def record_counter(self, *args, **kwargs):
                raise RuntimeError("registry exploded")

            def record_duration(self, *args, **kwargs):
                raise RuntimeError("registry exploded")

        monkeypatch.setattr(
            provider_concurrency, "_provider_metrics", _BrokenRegistry()
        )

        async def scenario():
            limiters = _limiters(embedding=1, rag_wait=0.05)

            # Waiters still succeed...
            assert await run_bounded(
                "embedding", limiters, lambda **_: "ok"
            ) == "ok"

            # ...and refusal still refuses with its exact type.
            await limiters.embedding.acquire()
            try:
                with pytest.raises(ProviderBusyError):
                    await run_bounded(
                        "embedding", limiters, lambda **_: "no",
                    )
            finally:
                limiters.embedding.release()

            limiters.signal_shutdown()

            with pytest.raises(ProviderShutdownError):
                await run_bounded("embedding", limiters, lambda **_: "no")

            # Permit accounting is untouched by the broken registry.
            assert limiters.embedding.locked() is False
            assert limiters._inflight == {}

        asyncio.run(scenario())
