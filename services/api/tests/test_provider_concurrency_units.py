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
