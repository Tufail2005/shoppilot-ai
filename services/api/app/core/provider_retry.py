"""Retry transient provider failures within a bounded time window.

Rate limits and temporary server or transport errors may be retried.
Timeouts, invalid responses, and other errors are not retried. The retry
window limits when another attempt may start; each running attempt is
bounded by its provider timeout. ``Retry-After`` is advisory.

The clock, sleep, and randomness are injectable for deterministic tests.

Phase 6.8: retries are metrics-instrumented. Every logical invocation
increments ``provider_retry_calls_total``; a failed attempt increments
``provider_retry_attempts_total{kind}`` only when the next attempt
actually begins; a success after at least one retry increments
``provider_retry_success_total`` (total attempts, so ``"1"``, a
first-try success, never appears); a call whose retry budget is
spent — ``max_attempts`` reached, or the retry window preventing
another attempt — increments ``provider_retry_exhausted_total``
(final kind + total attempts). Immediate fatal errors, timeouts,
and cancellations are non-retriable exits, not exhaustion.
Recording is best-effort: it can never change decisions,
results, or exceptions. ``operation`` labels the logical caller path.
Also exposes :func:`provider_outcome_of` for the concurrency worker
layer (retry-layer outcome vocabulary only).
"""

from __future__ import annotations

import logging
import math
import random
import time
from dataclasses import dataclass
from typing import Callable, TypeVar

from app.core.observability import attempts_label
from app.core.observability import provider_metrics as _provider_metrics
from app.core.provider_timeouts import is_timeout_error


logger = logging.getLogger(__name__)

_T = TypeVar("_T")

KIND_TIMEOUT = "timeout"
KIND_RATE_LIMITED = "rate_limited"
KIND_TRANSIENT = "transient"
KIND_FATAL = "fatal"

# Customer-facing Retry-After bounds (seconds).
RETRY_AFTER_MIN_SECONDS = 1
RETRY_AFTER_MAX_SECONDS = 60

_MAX_ATTEMPTS_CEILING = 5

# Gateway timeouts found on the SDK's ``.status_code`` errors.
_TIMEOUT_STATUS_CODES = frozenset({408, 504})

# Server errors that are not temporary.
_NON_RETRYABLE_SERVER_STATUS = frozenset({501, 505})

_RATE_LIMIT_CLASS_NAMES = frozenset({"RateLimitError"})
_SERVER_ERROR_CLASS_NAMES = frozenset({"InternalServerError"})
_CONNECTION_CLASS_NAMES = frozenset({"APIConnectionError"})
_VALIDATION_CLASS_NAMES = frozenset(
    {"APIResponseValidationError", "ResponseValidationError"}
)

# Check for cancellation at this interval during backoff.
_BACKOFF_CANCEL_POLL_SECONDS = 0.05

_UNSET = object()
_HTTPX: object = _UNSET


def _httpx_module():
    """Return the ``httpx`` module, or ``None`` when it is absent."""
    global _HTTPX

    if _HTTPX is _UNSET:
        try:
            import httpx  # noqa: PLC0415 - keep the module import-light
        except ImportError:  # pragma: no cover - httpx is a hard dependency
            _HTTPX = None
        else:
            _HTTPX = httpx

    return _HTTPX


def _httpx_transient_types(httpx):
    """httpx transport failures that are worth one more attempt."""
    names = (
        "ConnectError",
        "ReadError",
        "WriteError",
        "CloseError",
        "NetworkError",
        "RemoteProtocolError",
        "ProxyError",
    )
    types = []
    for name in names:
        candidate = getattr(httpx, name, None)
        if isinstance(candidate, type):
            types.append(candidate)
    return tuple(types)


def _exception_chain(exc: BaseException):
    """Yield an exception and its cause/context chain without cycles."""
    seen: set[int] = set()
    current: BaseException | None = exc

    while current is not None and len(seen) < 8:
        if id(current) in seen:
            return

        seen.add(id(current))
        yield current

        current = (
            current.__cause__
            if current.__cause__ is not None
            else current.__context__
        )


def _classify_connection_error(exc: BaseException, httpx) -> str:
    """Retry only known transient transport failures."""
    if httpx is None:
        return KIND_FATAL

    chain = tuple(_exception_chain(exc))[1:]
    if not chain:
        return KIND_FATAL

    fatal_types = (httpx.TooManyRedirects, httpx.DecodingError)
    if any(isinstance(candidate, fatal_types) for candidate in chain):
        return KIND_FATAL

    transient_types = _httpx_transient_types(httpx)
    if transient_types and any(
        isinstance(candidate, transient_types) for candidate in chain
    ):
        return KIND_TRANSIENT

    return KIND_FATAL


@dataclass(frozen=True)
class RetryPolicy:
    """Validated retry limits for one provider call."""

    max_attempts: int = 2
    initial_delay_seconds: float = 0.25
    retry_window_seconds: float = 3.0

    def __post_init__(self) -> None:
        attempts = self.max_attempts

        if isinstance(attempts, bool) or not isinstance(attempts, int):
            raise ValueError("max_attempts must be an integer")

        if not 1 <= attempts <= _MAX_ATTEMPTS_CEILING:
            raise ValueError(
                f"max_attempts must be between 1 and {_MAX_ATTEMPTS_CEILING}")
        _validate_positive_seconds(
            self.initial_delay_seconds, name="initial_delay_seconds")
        _validate_positive_seconds(
            self.retry_window_seconds, name="retry_window_seconds")

    @property
    def retries_enabled(self) -> bool:
        return self.max_attempts > 1

    @classmethod
    def from_settings(cls, settings) -> "RetryPolicy":
        """Build the policy from application settings (validated here)."""
        return cls(
            max_attempts=settings.gemini_retry_max_attempts,
            initial_delay_seconds=settings.gemini_retry_initial_delay_seconds,
            retry_window_seconds=settings.gemini_retry_window_seconds,
        )


def _validate_positive_seconds(value: object, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a positive number of seconds")

    numeric = float(value)

    if not math.isfinite(numeric) or numeric <= 0:
        raise ValueError(f"{name} must be a finite positive number of seconds")

    return numeric


class ProviderCallError(Exception):
    """Provider call failure with its kind, attempt count, and retry hint."""

    def __init__(
        self,
        kind: str,
        *,
        attempts: int,
        retry_after_seconds: float | None = None,
    ):
        super().__init__(
            f"Provider call failed with kind {kind!r} "
            f"after {attempts} attempt(s)")
        self.kind = kind
        self.attempts = attempts
        self.retry_after_seconds = retry_after_seconds


class ProviderCallAborted(Exception):
    """The provider call was cancelled before an attempt or during backoff."""


def _status_code(exc: BaseException) -> int | None:
    """Extract a numeric status code from either SDK error hierarchy."""
    code = getattr(exc, "code", None)

    if isinstance(code, int) and not isinstance(code, bool):
        return code

    status_code = getattr(exc, "status_code", None)

    if isinstance(status_code, int) and not isinstance(status_code, bool):
        return status_code

    return None


def classify_provider_error(exc: BaseException) -> str:
    """Classify a provider exception into one of the four retry kinds.

    Rule order is significant; timeouts are always classified first so
    a timeout can never be retried as a transient failure.
    """
    # Check timeouts first, including wrapped timeouts and 408/504 codes.
    if is_timeout_error(exc):
        return KIND_TIMEOUT

    status = _status_code(exc)

    # Compatibility errors carry codes on ``.status_code``.
    if status in _TIMEOUT_STATUS_CODES:
        return KIND_TIMEOUT

    name = type(exc).__name__

    # Handle raw transport errors if they escape the SDK.
    httpx = _httpx_module()

    if httpx is not None:
        if isinstance(exc, httpx.TimeoutException):
            return KIND_TIMEOUT

        transient_types = _httpx_transient_types(httpx)

        if transient_types and isinstance(exc, transient_types):
            return KIND_TRANSIENT

        if isinstance(exc, (httpx.TooManyRedirects, httpx.DecodingError)):
            return KIND_FATAL

    # Prefer the numeric status; use the class name if unavailable.
    if status == 429 or (status is None and name in _RATE_LIMIT_CLASS_NAMES):
        return KIND_RATE_LIMITED

    # Retry temporary upstream failures.
    if (
        status is not None
        and 500 <= status <= 599
        and status not in _NON_RETRYABLE_SERVER_STATUS
    ):
        return KIND_TRANSIENT

    if status in _NON_RETRYABLE_SERVER_STATUS:
        return KIND_FATAL

    if status is None and name in _SERVER_ERROR_CLASS_NAMES:
        return KIND_TRANSIENT

    # Retry wrapped transport errors only for known transient causes.
    if name in _CONNECTION_CLASS_NAMES:
        return _classify_connection_error(exc, httpx)

    # Malformed responses and other unrecognized errors are fatal.
    if name in _VALIDATION_CLASS_NAMES:
        return KIND_FATAL

    return KIND_FATAL


def retry_after_seconds(exc: BaseException) -> float | None:
    """Return the provider's ``Retry-After`` hint, or ``None``.

    Only a finite, strictly positive numeric value is accepted. The
    HTTP-date form is deliberately ignored (unparseable -> no hint).
    """
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)

    if headers is None:
        return None

    try:
        raw = headers.get("retry-after")
    except Exception:  # pragma: no cover - defensive against odd mappings
        return None

    if raw is None:
        return None

    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None

    if not math.isfinite(value) or value <= 0:
        return None

    return value


def clamp_retry_after(seconds: object) -> int:
    """Clamp a provider hint into the customer-facing Retry-After range."""
    if seconds is None:
        return RETRY_AFTER_MIN_SECONDS

    try:
        numeric = float(seconds)
    except (TypeError, ValueError):
        return RETRY_AFTER_MIN_SECONDS

    if not math.isfinite(numeric) or numeric <= 0:
        return RETRY_AFTER_MIN_SECONDS

    return max(
        RETRY_AFTER_MIN_SECONDS,
        min(RETRY_AFTER_MAX_SECONDS, math.ceil(numeric)),
    )


# Retry-layer outcome vocabulary for the worker done-callbacks. The
# concurrency layer resolves its own outcomes (caller cancellation,
# saturation, shutdown, worker abort) BEFORE consulting this function,
# so the concurrency exception types are deliberately never
# referenced here (correction C1), and no adapter exception class is
# ever imported (correction C2) — the adapters reach this function
# through standard exception attributes only.
_KIND_TO_OUTCOME = {
    KIND_TIMEOUT: "timeout",
    KIND_RATE_LIMITED: "rate_limited",
    KIND_TRANSIENT: "transient",
    KIND_FATAL: "fatal",
}

# Explicit stage allowlist (correction C2). The slugs are the ones the
# adapters actually set; they are duplicated here on purpose so the
# retry layer stays free of adapter imports. Unknown families and
# unknown/missing stages map to "unexpected".
_STAGE_TO_OUTCOME = {
    "embedding_timeout": "timeout",
    "generation_timeout": "timeout",
    "rate_limited": "rate_limited",
    "provider_unavailable": "transient",
    "provider_request": "fatal",
    "generation_request": "fatal",
    "configuration": "fatal",
    "empty_response": "fatal",
    "unsuccessful_response": "fatal",
    "empty_output": "fatal",
    "no_sources": "fatal",
}


def provider_outcome_of(exc: BaseException) -> str:
    """Classify an observed exception into the retry-layer outcome set.

    Returns one of ``timeout``, ``rate_limited``, ``transient``,
    ``fatal``, or ``unexpected``. Rules in strict order:

    1. timeouts first, using the chain-aware :func:`is_timeout_error`
       helper (a wrapped timeout can never be Downgraded to a retryable
       kind),
    2. a known :class:`ProviderCallError` keeps its classification,
    3. the translated exception's SDK cause is classified,
    4. a translated adapter error's ``stage`` slug is mapped through
       the explicit allowlist,
    5. anything else — unknown exception families, unknown stages,
       missing stages, concurrency-layer exception types — is
       ``unexpected``.
    """
    if is_timeout_error(exc):
        return "timeout"

    if isinstance(exc, ProviderCallError):
        return _KIND_TO_OUTCOME.get(exc.kind, "unexpected")

    cause = exc.__cause__

    if cause is not None:
        kind = classify_provider_error(cause)

        if kind in _KIND_TO_OUTCOME:
            return _KIND_TO_OUTCOME[kind]

    if isinstance(getattr(exc, "stage", None), str):
        return _STAGE_TO_OUTCOME.get(exc.stage, "unexpected")

    return "unexpected"


def _backoff_delay(
    policy: RetryPolicy,
    attempt: int,
    rand: Callable[[], float],
) -> float:
    """Full-jitter exponential backoff, capped at the retry window."""
    base = policy.initial_delay_seconds * (2 ** (attempt - 1))
    jittered = rand() * base

    return max(0.0, min(jittered, policy.retry_window_seconds))


def _sleep_with_cancel(
    seconds: float,
    cancel_event,
    sleep: Callable[[float], None],
) -> None:
    if seconds <= 0:
        return

    if cancel_event is None:
        sleep(seconds)
        return

    remaining = seconds

    while remaining > 0:
        if cancel_event.is_set():
            raise ProviderCallAborted(
                "provider call was cancelled during backoff")

        step = min(remaining, _BACKOFF_CANCEL_POLL_SECONDS)
        sleep(step)
        remaining -= step


# ---------------------------------------------------------------------------
# Phase 6.8 retry metrics (best-effort recording only)
# ---------------------------------------------------------------------------

_RETRY_CALLS = "provider_retry_calls_total"
_RETRY_ATTEMPTS = "provider_retry_attempts_total"
_RETRY_SUCCESS = "provider_retry_success_total"
_RETRY_EXHAUSTED = "provider_retry_exhausted_total"


def _record_counter(
    series: str,
    labels: dict[str, str],
    *,
    delta: int = 1,
) -> None:
    """Record one counter observation; NEVER affect retry behavior.

    ``ProviderMetrics`` recording is best-effort by contract; this
    wrapper adds a boundary against a broken/replaced registry so a
    metrics failure can never surface in retry decisions, results, or
    exception chaining.
    """
    try:
        _provider_metrics.record_counter(series, labels, delta=delta)
    except Exception:  # pragma: no cover - metrics must be invisible
        pass


def _record_exhausted(operation: str, kind: str, attempts: int) -> None:
    """Record one call whose retry budget is spent.

    Called only when ``max_attempts`` has been reached or the retry
    window prevents another attempt — never for immediate
    non-retriable exits (fatal errors, timeouts) or cancellations.
    """
    _record_counter(
        _RETRY_EXHAUSTED,
        {
            "operation": operation,
            "kind": kind,
            "attempts": attempts_label(attempts),
        },
    )


def call_with_retries(
    attempt: Callable[[], _T],
    *,
    policy: RetryPolicy,
    cancel_event=None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    rand: Callable[[], float] = random.random,
    operation: str = "unattributed",
) -> _T:
    """Run one logical provider call under the bounded retry policy.

    Returns the attempt's value. Raises :class:`ProviderCallError` with
    the classification of the final failure, or
    :class:`ProviderCallAborted` when cancellation is observed before an
    attempt or during backoff.

    ``operation`` (keyword-only, Phase 6.8) labels the logical caller
    path for the retry metrics and defaults to ``unattributed`` so
    pre-6.8 call sites keep working unchanged.

    Metrics semantics:

    - ``provider_retry_calls_total{operation}``: every logical call,
    - ``provider_retry_attempts_total{operation, kind}``: every failed
      attempt whose retry actually began (a cancellation-interrupted
      backoff is therefore recorded by the concurrency layer, not here),
    - ``provider_retry_success_total{operation, attempts}``: successes
      recovered after at least one retry, labelled by TOTAL attempts
      (initial included, so ``"1"`` never appears),
    - ``provider_retry_exhausted_total{operation, kind, attempts}``:
      calls whose retry budget is spent — ``max_attempts`` reached,
      or the retry window preventing another attempt (both the
      pre-backoff check and the post-backoff check). Immediate fatal
      errors, timeouts, and cancellations are NOT exhaustion: the
      retry budget never came into play.

    Recording is best-effort and never alters decisions, chaining, or
    results.
    """
    started_at = clock()
    attempts = 0

    _record_counter(_RETRY_CALLS, {"operation": operation})

    while True:
        if cancel_event is not None and cancel_event.is_set():
            raise ProviderCallAborted(
                "provider call was cancelled before an attempt")

        attempts += 1

        try:
            value = attempt()
        except ProviderCallAborted:
            raise
        except Exception as exc:
            kind = classify_provider_error(exc)
            hint = retry_after_seconds(exc)

            if kind == KIND_TIMEOUT:
                # Provider timeouts are never retried. This is a
                # non-retriable failure, not retry-budget exhaustion:
                # the exhausted counter stays untouched.
                raise ProviderCallError(
                    KIND_TIMEOUT,
                    attempts=attempts,
                    retry_after_seconds=hint,
                ) from exc

            if kind == KIND_FATAL:
                # Fatal errors are never retried. Like a timeout, this
                # immediate non-retriable exit is not exhaustion.
                raise ProviderCallError(
                    kind,
                    attempts=attempts,
                    retry_after_seconds=hint,
                ) from exc

            if attempts >= policy.max_attempts:
                # The attempt budget is spent: no further attempt can
                # follow.
                _record_exhausted(operation, kind, attempts)
                raise ProviderCallError(
                    kind,
                    attempts=attempts,
                    retry_after_seconds=hint,
                ) from exc

            delay = _backoff_delay(policy, attempts, rand)

            if hint is not None:
                delay = max(delay, hint)

            elapsed = clock() - started_at

            if elapsed + delay >= policy.retry_window_seconds:
                _record_exhausted(operation, kind, attempts)
                raise ProviderCallError(
                    kind,
                    attempts=attempts,
                    retry_after_seconds=hint,
                ) from exc

            logger.warning(
                "Provider attempt %d/%d failed with %s; retrying.",
                attempts,
                policy.max_attempts,
                kind,
            )

            _sleep_with_cancel(delay, cancel_event, sleep)

            # Do not retry if sleeping exceeded the retry window.
            if cancel_event is not None and cancel_event.is_set():
                raise ProviderCallAborted(
                    "provider call was cancelled during backoff")

            if clock() - started_at >= policy.retry_window_seconds:
                _record_exhausted(operation, kind, attempts)
                raise ProviderCallError(
                    kind,
                    attempts=attempts,
                    retry_after_seconds=hint,
                ) from exc

            # The next attempt actually begins: only now is this failed
            # attempt attributed to its classification. A backoff that
            # was interrupted by cancellation instead ends this call as
            # aborted, and the concurrency layer records
            # caller_cancelled for it.
            _record_counter(
                _RETRY_ATTEMPTS,
                {"operation": operation, "kind": kind},
            )

            continue

        # Only a completed attempt reaches this point.
        if attempts >= 2:
            # Recovered after at least one retry, labelled by the TOTAL
            # attempt count (the initial attempt included, so "1" never
            # appears here by construction).
            _record_counter(
                _RETRY_SUCCESS,
                {
                    "operation": operation,
                    "attempts": attempts_label(attempts),
                },
            )

        return value