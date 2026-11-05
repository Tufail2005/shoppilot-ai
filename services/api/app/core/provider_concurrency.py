"""Provider concurrency admission and shutdown coordination (Phase 6.7).

Every Gemini network call made by this service is *admitted* through
:func:`run_bounded`. Admission does four things in one place:

1. Bounds how many provider calls run at once per resource
   (``embedding``, ``generation``) with per-process semaphores.
2. Bounds how many callers may *wait* for a resource, so a burst of
   requests fails fast with a sanitized 503 instead of piling up
   unbounded coroutines (a timed wait alone does not bound waiters).
3. Bounds how long a caller may wait, against one absolute deadline
   (optionally shortened by the RAG request deadline).
4. Refuses admission once shutdown has begun, wakes waiters at
   shutdown, and guarantees that an aborted call is reported as
   :class:`ProviderShutdownError` (HTTP 503) rather than an unexpected
   500.

Ingestion needs two permits per batch (the ingestion gate plus the
shared embedding permit). It acquires both in ONE
:func:`run_bounded` call, runs the embedding function exactly once, and
releases both permits from the worker's completion callback. A single
admission deadline is computed at entry and reused for every
acquisition, so sequential waits can never accumulate (20 s total, not
40 s).

Permit lifetime
---------------

A permit is held from acquisition until the worker thread function
returns. Request cancellation does not cancel the worker, does not
release the permit, and does not start a retry: the permit stays held
until the thread finishes, and only the worker's completion callback
releases it. ``asyncio.shield`` is deliberately not used — it re-attaches
its logging callback to a cancelled waiter's task, which would report an
abandoned worker's late exception as an unhandled error; a completion
future consumes that outcome instead. This keeps the thread count
bounded even when clients disconnect.

Shutdown semantics (precise, not stronger than reality)
-------------------------------------------------------

A worker task created immediately before shutdown may begin executing
*after* shutdown has begun — thread scheduling is outside the event
loop's control, and this module does not claim otherwise. What IS
guaranteed: the retry loop checks the cancellation event immediately
before every attempt, including the first, so a worker that observes
the flag issues **no new provider request**. A worker already inside an
SDK call is not interrupted; it is bounded only by its Phase 6.6 SDK
timeout, keeps its permit until it returns, and its outcome is
discarded. Pending acquirers are woken immediately at shutdown instead
of waiting out their budget.

All limits are per process. With one Uvicorn worker the effective
limit equals the configured value; horizontally scaled deployments
would multiply it (cross-process limiting is out of scope).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from asyncio import ensure_future
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal, TypeVar

from fastapi import Request

from app.core.observability import provider_metrics as _provider_metrics
from app.core.provider_retry import ProviderCallAborted
from app.core.provider_retry import provider_outcome_of
from app.core.provider_timeouts import validate_timeout_seconds


logger = logging.getLogger(__name__)

_T = TypeVar("_T")

# Retried-outcome buckets (Phase 6.8): a worker failure classified into
# one of these retry-layer outcomes is a known provider failure for the
# coarse caller buckets; anything else is unexpected.
_RETRIED_OUTCOMES = frozenset(
    {"timeout", "rate_limited", "transient", "fatal"}
)
_UNATTRIBUTED = "unattributed"

# Canonical acquisition order. Every caller is normalised into this
# order, so a multi-permit acquisition can never create a cycle.
_RESOURCE_ORDER = ("ingestion_gate", "embedding", "generation")
_RESOURCE_INDEX = {name: position for position, name in enumerate(_RESOURCE_ORDER)}

# The ingestion gate allows exactly one batch to compete for embedding
# permits at a time, so ingestion can only ever contribute one
# competitor to the shared embedding resource.
INGESTION_ACTIVE_BATCH_LIMIT = 1

# Admission is decided this long before the RAG request deadline so a
# capacity failure (503) can never race the deadline expiry (504).
ADMISSION_DEADLINE_MARGIN_SECONDS = 0.25

# How long shutdown waits for in-flight worker threads.
SHUTDOWN_GRACE_SECONDS = 5.0

# Minimum number of allowed waiters per resource, regardless of limit.
_PENDING_CAP_FLOOR = 2


class ProviderBusyError(Exception):
    """Admission was refused because the resource is at capacity."""

    def __init__(self, resource: str):
        super().__init__("The provider is temporarily at capacity")
        self.resource = resource


class ProviderShutdownError(ProviderBusyError):
    """Admission was refused, or a call aborted, because of shutdown."""


@dataclass
class ProviderCallScope:
    """Per-call state owned by the worker's completion callback."""

    cancel_event: threading.Event
    permits: tuple[asyncio.BoundedSemaphore, ...]

    def cancel(self) -> None:
        self.cancel_event.set()


@dataclass(frozen=True)
class _AcquireResult:
    outcome: Literal["acquired", "timed_out", "failed"]
    error: BaseException | None = None


@dataclass(frozen=True)
class _DrainResult:
    outcome: Literal["acquired", "timed_out", "failed", "deferred"]
    error: BaseException | None = None
    cancellation: BaseException | None = None
    cleanup_transferred: bool = False


class _PendingLease:
    """One occupied pending-waiter slot, released exactly once.

    ``release`` is the caller path and becomes a no-op once ownership
    has been transferred; ``release_orphaned`` is the completion
    callback path. Both run on the event-loop thread.
    """

    __slots__ = ("_limiters", "_resource", "_released", "_transferred")

    def __init__(self, limiters: "ProviderLimiters", resource: str):
        self._limiters = limiters
        self._resource = resource
        self._released = False
        self._transferred = False

    def transfer_to_callback(self) -> None:
        self._transferred = True

    def release(self) -> None:
        if self._transferred or self._released:
            return
        self._release()

    def release_orphaned(self) -> None:
        if self._released:
            return
        self._release()

    def _release(self) -> None:
        self._released = True
        self._limiters._pending[self._resource] -= 1


class ProviderLimiters:
    """Per-process provider admission state, owned by ``app.state``."""

    def __init__(
        self,
        *,
        embedding_limit: int,
        generation_limit: int,
        rag_wait_seconds: float,
        ingestion_wait_seconds: float,
        ingestion_gate_limit: int = INGESTION_ACTIVE_BATCH_LIMIT,
    ):
        self.embedding = asyncio.BoundedSemaphore(embedding_limit)
        self.generation = asyncio.BoundedSemaphore(generation_limit)
        self.ingestion_gate = asyncio.BoundedSemaphore(ingestion_gate_limit)

        self.embedding_limit = embedding_limit
        self.generation_limit = generation_limit

        self.rag_wait_seconds = rag_wait_seconds
        self.ingestion_wait_seconds = ingestion_wait_seconds

        self.pending_cap = {
            "embedding": max(_PENDING_CAP_FLOOR, 2 * embedding_limit),
            "generation": max(_PENDING_CAP_FLOOR, 2 * generation_limit),
            "ingestion_gate": max(_PENDING_CAP_FLOOR, 2 * ingestion_gate_limit),
        }

        # Set on shutdown. ``shutting_down`` is visible from worker
        # threads; ``shutdown_event`` wakes pending acquirers on the loop.
        self.shutting_down = threading.Event()
        self.shutdown_event = asyncio.Event()

        self._pending: dict[str, int] = {name: 0 for name in _RESOURCE_ORDER}
        self._inflight: dict[asyncio.Task, ProviderCallScope] = {}

    # -- construction -----------------------------------------------------

    @classmethod
    def from_settings(cls, settings) -> "ProviderLimiters":
        return cls(
            embedding_limit=settings.gemini_embedding_max_concurrency,
            generation_limit=settings.gemini_generation_max_concurrency,
            rag_wait_seconds=validate_timeout_seconds(
                settings.rag_provider_concurrency_wait_seconds,
                name="rag_provider_concurrency_wait_seconds",
            ),
            ingestion_wait_seconds=validate_timeout_seconds(
                settings.ingestion_provider_concurrency_wait_seconds,
                name="ingestion_provider_concurrency_wait_seconds",
            ),
        )

    # -- lookups ----------------------------------------------------------

    def semaphore_for(self, resource: str) -> asyncio.BoundedSemaphore:
        if resource == "ingestion_gate":
            return self.ingestion_gate
        if resource == "embedding":
            return self.embedding
        if resource == "generation":
            return self.generation

        raise ValueError(f"unknown provider resource: {resource!r}")

    def wait_seconds_for(self, resource: str) -> float:
        if resource == "ingestion_gate":
            return self.ingestion_wait_seconds

        return self.rag_wait_seconds

    def _acquire_pending_lease(self, resource: str) -> _PendingLease | None:
        if self._pending[resource] >= self.pending_cap[resource]:
            return None

        self._pending[resource] += 1

        return _PendingLease(self, resource)

    # -- shutdown ---------------------------------------------------------

    def signal_shutdown(self) -> None:
        """Mark shutdown and wake pending acquirers (loop thread only)."""
        self.shutting_down.set()
        self.shutdown_event.set()

    async def aclose(self, timeout: float = SHUTDOWN_GRACE_SECONDS) -> None:
        """Signal shutdown and give in-flight workers a bounded grace.

        Idempotent: repeated calls re-scan the registry without
        resetting state. Unfinished tasks are NEVER cleared from the
        registry and their permits are NEVER released here — the
        completion callback is the only release path, and it must still
        run if the task finishes later.
        """
        self.signal_shutdown()

        for scope in list(self._inflight.values()):
            scope.cancel()

        # Deliver the waiter wake-ups before waiting on worker threads.
        await asyncio.sleep(0)

        pending = [task for task in list(self._inflight) if not task.done()]

        if pending:
            await asyncio.wait(pending, timeout=timeout)

        unfinished = [task for task in list(self._inflight) if not task.done()]

        if unfinished:
            logger.error(
                "Shutdown: %d provider call(s) still in flight; "
                "permits retained until completion.",
                len(unfinished),
            )


def get_provider_limiters(request: Request) -> ProviderLimiters:
    """FastAPI dependency returning the process-wide limiter registry.

    The registry is created by the application lifespan. When the
    lifespan is not running (offline tests construct ``TestClient``
    without entering it) a registry is created lazily and cached on
    ``app.state`` so limits stay shared for the life of the app.
    """
    limiters = getattr(request.app.state, "provider_limiters", None)

    if limiters is None:
        # Deferred import: config imports provider_timeouts only.
        from app.core.config import get_settings

        limiters = ProviderLimiters.from_settings(get_settings())
        request.app.state.provider_limiters = limiters

    return limiters


# ---------------------------------------------------------------------------
# Admission internals
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Phase 6.8 provider-call metrics (best-effort observation only)
# ---------------------------------------------------------------------------


def _now() -> float:
    """The shared instrumentation clock (never used for decisions)."""
    return time.monotonic()


def _record_counter(
    series: str,
    labels: dict[str, str],
) -> None:
    """Record one counter observation; NEVER alter admission behavior.

    ``ProviderMetrics`` recording is best-effort by contract; this
    wrapper adds a boundary against a broken/replaced registry so a
    metrics failure can never surface in admission, cancellation,
    shutdown, or results.
    """
    try:
        _provider_metrics.record_counter(series, labels)
    except Exception:  # pragma: no cover - metrics must be invisible
        pass


def _record_duration(
    series: str,
    labels: dict[str, str],
    seconds: float,
) -> None:
    """Record one duration observation; NEVER alter admission behavior."""
    try:
        _provider_metrics.record_duration(series, labels, seconds)
    except Exception:  # pragma: no cover - metrics must be invisible
        pass


def _record_admission(
    operation: str,
    resource: str,
    admission_outcome: str,
    wait_seconds: float,
) -> None:
    """Record one per-resource admission observation (counter + wait)."""
    labels = {"operation": operation, "resource": resource}

    _record_counter(
        "provider_admissions_total",
        {**labels, "admission_outcome": admission_outcome},
    )
    _record_duration(
        "provider_admission_wait_seconds",
        labels,
        wait_seconds,
    )


def _record_worker(
    operation: str,
    resource: str,
    worker_outcome: str,
    seconds: float,
) -> None:
    """Record one worker observation (counter + duration)."""
    labels = {
        "operation": operation,
        "resource": resource,
        "worker_outcome": worker_outcome,
    }

    _record_counter("provider_worker_outcomes_total", labels)
    _record_duration("provider_worker_seconds", labels, seconds)


def _caller_outcome_of(exc: BaseException) -> str:
    """Map one escaping exception to its coarse caller bucket.

    Admission refusals are precise (shutdown vs saturation); anything a
    worker raised is bucketed through the retry layer's outcome mapping
    (correction C1: coarse buckets at the caller, never adapter
    imports).
    """
    if isinstance(exc, asyncio.CancelledError):
        return "caller_cancelled"

    if isinstance(exc, (ProviderShutdownError, ProviderCallAborted)):
        return "shutdown"

    if isinstance(exc, ProviderBusyError):
        return "saturated"

    if provider_outcome_of(exc) in _RETRIED_OUTCOMES:
        return "provider_failure"

    return "unexpected"


class _CallerObservation:
    """Records exactly one coarse caller outcome and call duration.

    ``finish`` is idempotent by construction (exactly-once guard), so
    even a defensive duplicate site can never produce two rows.
    """

    __slots__ = ("_operation", "_started", "_finished")

    def __init__(self, operation: str, started: float):
        self._operation = operation
        self._started = started
        self._finished = False

    def finish(self, caller_outcome: str) -> None:
        if self._finished:
            return

        self._finished = True

        labels = {
            "operation": self._operation,
            "caller_outcome": caller_outcome,
        }

        _record_counter("provider_caller_outcomes_total", labels)
        _record_duration(
            "provider_call_seconds", labels, _now() - self._started
        )


def _on_worker_metrics(
    task: asyncio.Task,
    operation: str,
    resource: str,
    cancel_event,
    limiters: "ProviderLimiters",
    started_at: float,
) -> None:
    """Record one worker outcome and duration; never raises.

    Registered AFTER ``_on_worker_done`` and BEFORE the completion
    future's ``_settle`` callback, so permits are already released when
    this runs (callback order follows registration order) and the late
    outcome path is untouched. ``task.cancelled()`` is checked BEFORE
    ``task.exception()``: retrieving an exception from a cancelled task
    re-raises instead of returning ``None``.

    The outcome is observed even when the caller detached (request
    cancellation never stops the worker), so caller cancellation and
    detached worker completion are separate observations.

    Worker outcome mapping:

    - the task was cancelled outright      -> ``worker_aborted``,
    - the cancel event is set DURING a
      process shutdown                     -> ``shutdown`` (the outcome
      is discarded exactly as before),
    - a caller cancellation WITHOUT a
      process shutdown never overrides
      what the worker actually did         -> ``succeeded`` for a clean
      return; an exception is classified
      through the retry layer's outcome
      (``timeout``/``rate_limited``/``transient``/``fatal``, else
      ``unexpected``),
    - the worker raised ``ProviderCallAborted`` (retry loop observed
      cancellation)                        -> ``shutdown`` under a
      process shutdown, else ``worker_aborted``,
    - a clean return                       -> ``succeeded``.
    """
    try:
        shutting_down = limiters.shutting_down.is_set()

        if task.cancelled():
            outcome = "worker_aborted"
        elif cancel_event.is_set() and shutting_down:
            outcome = "shutdown"
        else:
            exc = task.exception()

            if exc is None:
                outcome = "succeeded"
            elif isinstance(exc, ProviderCallAborted):
                outcome = (
                    "shutdown" if shutting_down else "worker_aborted"
                )
            else:
                outcome = provider_outcome_of(exc)

        _record_worker(operation, resource, outcome, _now() - started_at)
    except Exception:  # pragma: no cover - metrics must be invisible
        pass


def _canonical_resources(resources: str | Sequence[str]) -> list[str]:
    names = [resources] if isinstance(resources, str) else list(resources)

    if not names:
        raise ValueError("run_bounded requires at least one provider resource")

    unique: list[str] = []

    for name in names:
        if name not in _RESOURCE_INDEX:
            raise ValueError(f"unknown provider resource: {name!r}")

        if name not in unique:
            unique.append(name)

    unique.sort(key=_RESOURCE_INDEX.__getitem__)

    return unique


def _release_all(permits: Sequence[asyncio.BoundedSemaphore]) -> None:
    for semaphore in reversed(list(permits)):
        try:
            semaphore.release()
        except ValueError:  # pragma: no cover - defensive double release
            logger.error("Provider permit double release detected.")


def _completion_future(
    task: asyncio.Task,
    loop,
    *,
    label: str = "call",
) -> asyncio.Future:
    """Return a future that settles like ``task``, without ``shield``.

    ``asyncio.shield`` re-attaches its ``_log_on_exception`` callback to
    the inner task when the waiter is cancelled, so an abandoned worker
    that fails later is reported through the loop exception handler as
    ``"RuntimeError exception in shielded future"``. Here the outcome is
    delivered through a callback instead: when nobody is waiting any
    more the late outcome is consumed once, so the loop never reports it
    as unhandled.
    """
    waiter = loop.create_future()

    def _settle(done: asyncio.Task) -> None:
        if waiter.done():
            # The caller already went away: consume the outcome.
            if not done.cancelled():
                exc = done.exception()

                if exc is not None:
                    logger.error(
                        "Abandoned provider %s ended with %s.",
                        label,
                        type(exc).__name__,
                    )
            return

        if done.cancelled():
            waiter.cancel()
            return

        exc = done.exception()

        if exc is not None:
            waiter.set_exception(exc)
        else:
            waiter.set_result(done.result())

    task.add_done_callback(_settle)

    return waiter


def _abandon_waiter(waiter: asyncio.Future) -> None:
    """Release a completion future whose caller is no longer waiting."""
    if waiter.done():
        # Consume a late exception so it is not reported as unretrieved.
        if not waiter.cancelled():
            waiter.exception()
        return

    waiter.cancel()


async def _try_acquire(
    semaphore: asyncio.BoundedSemaphore,
    remaining: float,
) -> _AcquireResult:
    """Acquire within ``remaining`` seconds and report an explicit outcome.

    Never converts an unexpected error into a capacity outcome, and
    never raises on timeout; only this task's own cancellation escapes.
    """
    try:
        async with asyncio.timeout(remaining):
            await semaphore.acquire()

        return _AcquireResult("acquired")
    except TimeoutError:
        return _AcquireResult("timed_out")
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        return _AcquireResult("failed", error=exc)


def _settled_result(task: asyncio.Task) -> _AcquireResult:
    if task.cancelled():
        return _AcquireResult("timed_out")

    exc = task.exception()

    if exc is not None:  # pragma: no cover - _try_acquire is exhaustive
        return _AcquireResult("failed", error=exc)

    return task.result()


async def _drain(
    task: asyncio.Task,
    semaphore: asyncio.BoundedSemaphore,
    lease: _PendingLease,
    resource: str,
) -> _DrainResult:
    """Finish an acquisition task without ever raising.

    Caller cancellation is detected by comparing the caller's
    cancellation counter before and after the await: ``task.done()``
    cannot distinguish "the acquisition task failed" from "this caller
    was cancelled while the task completed successfully".
    """
    if not task.done():
        task.cancel()

    current = asyncio.current_task()
    cancels_before = current.cancelling() if current is not None else 0
    caller_cancelled = False
    cancellation = None
    waiter = _completion_future(
        task, asyncio.get_running_loop(), label="admission"
    )

    try:
        await waiter
    except asyncio.CancelledError as exc:
        _abandon_waiter(waiter)

        if current is not None and current.cancelling() > cancels_before:
            caller_cancelled = True
            cancellation = exc
        # Otherwise the acquisition task itself was cancelled.
    except Exception:
        pass

    if not task.done():
        # The acquisition could not be finished here (the caller was
        # cancelled again). Ownership of the permit decision and of the
        # pending lease moves to the completion callback.
        lease.transfer_to_callback()
        task.add_done_callback(
            lambda done: _finish_orphaned(done, semaphore, lease, resource)
        )

        return _DrainResult(
            "deferred",
            cancellation=cancellation,
            cleanup_transferred=True,
        )

    settled = _settled_result(task)

    if caller_cancelled:
        if settled.outcome == "failed" and settled.error is not None:
            logger.error(
                "Provider admission failed while the request was "
                "cancelled: %s",
                type(settled.error).__name__,
            )

        return _DrainResult(
            settled.outcome,
            error=settled.error if settled.outcome == "failed" else None,
            cancellation=cancellation,
        )

    return _DrainResult(
        settled.outcome,
        error=settled.error if settled.outcome == "failed" else None,
    )


def _finish_orphaned(
    task: asyncio.Task,
    semaphore: asyncio.BoundedSemaphore,
    lease: _PendingLease,
    resource: str,
) -> None:
    """Settle an orphaned acquisition: permit first, then the lease."""
    try:
        if task.cancelled():
            return

        exc = task.exception()

        if exc is not None:
            logger.error(
                "Abandoned provider admission failed with %s.",
                type(exc).__name__,
            )
            return

        result = task.result()

        if result.outcome == "acquired":
            try:
                semaphore.release()
            except ValueError:  # pragma: no cover - defensive
                logger.error(
                    "Provider permit double release for %s.", resource)
    finally:
        lease.release_orphaned()


async def _discard(wake: asyncio.Task) -> None:
    with contextlib.suppress(asyncio.CancelledError, Exception):
        wake.cancel()
        await wake


async def _wait_for_permit(
    semaphore: asyncio.BoundedSemaphore,
    resource: str,
    limiters: ProviderLimiters,
    remaining: float,
    lease: _PendingLease,
) -> None:
    acquire = ensure_future(_try_acquire(semaphore, remaining))
    wake = ensure_future(limiters.shutdown_event.wait())

    try:
        done, _ = await asyncio.wait(
            {acquire, wake},
            return_when=asyncio.FIRST_COMPLETED,
        )
    except asyncio.CancelledError:
        result = await _drain(acquire, semaphore, lease, resource)

        if result.outcome == "acquired" and not result.cleanup_transferred:
            semaphore.release()

        await _discard(wake)

        raise

    shutdown = wake in done or limiters.shutting_down.is_set()

    await _discard(wake)

    result = await _drain(acquire, semaphore, lease, resource)

    # Precedence: caller cancellation > shutdown > acquired > timed out
    # > unexpected failure. Cleanup above is always complete first.
    if result.cancellation is not None:
        if result.outcome == "acquired" and not result.cleanup_transferred:
            semaphore.release()

        raise result.cancellation

    if result.cleanup_transferred:  # pragma: no cover - implies cancellation
        raise asyncio.CancelledError

    if shutdown:
        if result.outcome == "acquired":
            semaphore.release()

        raise ProviderShutdownError(resource)

    if result.outcome == "acquired":
        return

    if result.outcome == "timed_out":
        raise ProviderBusyError(resource)

    if result.error is not None:
        raise result.error

    raise ProviderBusyError(resource)  # pragma: no cover - defensive


async def _acquire_one(
    semaphore: asyncio.BoundedSemaphore,
    resource: str,
    limiters: ProviderLimiters,
    admission_deadline: float,
    *,
    operation: str = _UNATTRIBUTED,
) -> None:
    """Acquire one permit; observe its admission and wait.

    Phase 6.8: every acquisition is observed exactly once with a
    single ``admission_outcome`` (per the METRIC vocabulary: both
    timed-out and pending-cap refusals are ``saturated``) and the
    wall-clock wait it actually spent, including instant refusals
    (wait ≈ 0). Recording is best-effort and never raises.
    """
    started = _now()
    outcome = "unexpected"

    try:
        if limiters.shutting_down.is_set():
            outcome = "shutdown"
            raise ProviderShutdownError(resource)

        remaining = admission_deadline - asyncio.get_running_loop().time()

        if remaining <= 0:
            outcome = "saturated"
            raise ProviderBusyError(resource)

        # Fast path: available now. The permit is taken in this coroutine
        # with no suspension point, so no helper task and no waiter slot
        # are needed and no concurrent arrival can slip in between.
        if not semaphore.locked():
            await semaphore.acquire()
            outcome = "acquired"
            return

        # Slow path: guaranteed to wait, therefore it must be counted.
        lease = limiters._acquire_pending_lease(resource)

        if lease is None:
            outcome = "saturated"
            raise ProviderBusyError(resource)

        try:
            await _wait_for_permit(
                semaphore, resource, limiters, remaining, lease
            )
        finally:
            # No-op once the callback owns the lease.
            lease.release()

        outcome = "acquired"
    except asyncio.CancelledError:
        # A caller cancellation delivers CancelledError here (the
        # acquisition task's own outcome is drained below it); an
        # interrupted wait is caller cancellation, never a refusal.
        outcome = "caller_cancelled"
        raise
    except ProviderShutdownError:
        outcome = "shutdown"
        raise
    except ProviderBusyError:
        outcome = "saturated"
        raise
    except Exception:
        outcome = "unexpected"
        raise
    finally:
        _record_admission(operation, resource, outcome, _now() - started)


def _on_worker_done(task: asyncio.Task, limiters: ProviderLimiters) -> None:
    """Release permits for a finished worker (the only release path).

    The worker's outcome is delivered to (or consumed by) its completion
    future, so this callback only owns the permits. ``aclose`` never
    releases them, which guarantees a permit is held until the thread
    actually returns.
    """
    scope = limiters._inflight.pop(task, None)

    if scope is not None:
        _release_all(scope.permits)


async def run_bounded(
    resources: str | Sequence[str],
    limiters: ProviderLimiters,
    fn: Callable[..., _T],
    /,
    *args,
    wait_seconds: float | None = None,
    deadline_at: float | None = None,
    margin: float = 0.0,
    operation: str = _UNATTRIBUTED,
) -> _T:
    """Run one provider call under admission control.

    ``fn`` is invoked exactly once, in a worker thread, with
    ``cancel_event=``. ``resources`` lists every permit the call needs;
    they are acquired in canonical order under ONE admission deadline
    computed here (never two sequential budgets).

    Phase 6.8: the call is observed — one coarse caller outcome and
    duration (``provider_caller_outcomes_total`` /
    ``provider_call_seconds``), one per-resource admission observation
    per permit (``provider_admissions_total`` /
    ``provider_admission_wait_seconds``), and one worker outcome and
    duration delivered by the worker's done callback
    (``provider_worker_outcomes_total`` / ``provider_worker_seconds``),
    attributed to the admission's primary resource. ``operation``
    labels the logical caller path for all of these and defaults to
    ``unattributed``. Recording is best-effort and never changes
    admission, cancellation, shutdown, or permit-release behavior.
    """
    names = _canonical_resources(resources)
    primary = names[0]

    observation = _CallerObservation(operation, _now())

    loop = asyncio.get_running_loop()
    now = loop.time()

    if limiters.shutting_down.is_set():
        observation.finish("shutdown")
        raise ProviderShutdownError(primary)

    budget = (
        wait_seconds
        if wait_seconds is not None
        else limiters.wait_seconds_for(primary)
    )

    admission_deadline = now + budget

    if deadline_at is not None:
        admission_deadline = min(admission_deadline, deadline_at - margin)

    if admission_deadline <= now:
        observation.finish("saturated")
        raise ProviderBusyError(primary)

    acquired: list[asyncio.BoundedSemaphore] = []

    try:
        for name in names:
            semaphore = limiters.semaphore_for(name)
            await _acquire_one(
                semaphore,
                name,
                limiters,
                admission_deadline,
                operation=operation,
            )
            acquired.append(semaphore)
    except BaseException as exc:
        _release_all(acquired)
        observation.finish(_caller_outcome_of(exc))
        raise

    # --- await-free critical section ------------------------------------
    # A shutdown observed here releases everything already acquired.
    # Nothing below suspends until the worker is registered, so aclose()
    # (which only runs at await points) cannot interleave.
    if limiters.shutting_down.is_set():
        _release_all(acquired)
        observation.finish("shutdown")
        raise ProviderShutdownError(primary)

    scope = ProviderCallScope(
        cancel_event=threading.Event(),
        permits=tuple(acquired),
    )

    worker_started_at = _now()

    try:
        task = loop.create_task(
            asyncio.to_thread(fn, *args, cancel_event=scope.cancel_event)
        )
    except BaseException:
        _release_all(acquired)
        observation.finish("unexpected")
        raise

    limiters._inflight[task] = scope
    # Permit release must stay first: done callbacks run in
    # registration order, so permits are released before the worker
    # outcome is observed, and the completion future's late-outcome
    # handling is untouched (exactly-once permit release preserved).
    task.add_done_callback(lambda done: _on_worker_done(done, limiters))
    task.add_done_callback(
        lambda done: _on_worker_metrics(
            done,
            operation,
            primary,
            scope.cancel_event,
            limiters,
            worker_started_at,
        )
    )

    waiter = _completion_future(task, loop)

    try:
        result = await waiter
    except asyncio.CancelledError:
        # Stop further retries; the worker keeps running and its permit
        # is released by the callback when the thread returns.
        observation.finish("caller_cancelled")
        scope.cancel()
        _abandon_waiter(waiter)
        raise
    except ProviderCallAborted as exc:
        # A live request must never surface this as an unknown 500.
        observation.finish("shutdown")
        raise ProviderShutdownError(primary) from exc
    except Exception as exc:
        # A worker failure that reached the caller: bucketed coarsely
        # through the retry layer's outcome mapping (correction C1).
        observation.finish(_caller_outcome_of(exc))
        raise

    if scope.cancel_event.is_set():
        # Shutdown cancelled this call while it was in flight. A worker
        # that returns anyway (it may be inside an uninterruptible SDK
        # call) has an outcome that must not be served to a request
        # whose process is going down.
        observation.finish("shutdown")
        raise ProviderShutdownError(primary)

    observation.finish("succeeded")
    return result
