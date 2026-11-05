"""Offline unit tests for the Phase 6.7 bounded provider retry helper.

Fully offline: no database, no network, no Gemini credentials. The real
``google-genai`` error classes are constructed directly (never raised
from a live call) so classification is verified against the SDK shapes
this service actually sees, and the clock/sleep/randomness are injected
so the retry loop is deterministic.
"""

import math
import threading

import httpx
import pytest
from google.genai import errors as legacy_errors
from google.genai._gaos.lib import compat_errors

from app.core.provider_retry import (
    KIND_FATAL,
    KIND_RATE_LIMITED,
    KIND_TIMEOUT,
    KIND_TRANSIENT,
    ProviderCallAborted,
    ProviderCallError,
    RetryPolicy,
    call_with_retries,
    clamp_retry_after,
    classify_provider_error,
    provider_outcome_of,
    retry_after_seconds,
)


@pytest.fixture
def metrics(monkeypatch):
    """A fresh ProviderMetrics registry wired into the retry module.

    The module-level ``provider_metrics`` singleton stays untouched, so
    other tests (and the production call sites) are never affected by
    the assertions here.
    """
    from app.core import provider_retry as retry_module
    from app.core.observability import ProviderMetrics

    registry = ProviderMetrics()
    monkeypatch.setattr(retry_module, "_provider_metrics", registry)
    return registry


def _operation_count(registry, series, **labels):
    for entry in registry.snapshot().counters.get(series, []):
        if entry["labels"] == labels:
            return entry["value"]
    return None


_REQUEST_URL = "https://generativelanguage.example.invalid/v1/models"


def _request() -> httpx.Request:
    return httpx.Request("POST", _REQUEST_URL)


def _response(status_code: int) -> httpx.Response:
    return httpx.Response(status_code, request=_request())


def _legacy(status_code: int) -> Exception:
    return legacy_errors.APIError(status_code, {"error": "simulated"})


def _compat(cls, status_code: int):
    return cls("simulated", response=_response(status_code), body=None)


class TestClassification:
    """One assertion per SDK error shape the adapters can see."""

    @pytest.mark.parametrize("status", [429])
    def test_legacy_rate_limit_is_retryable(self, status):
        assert classify_provider_error(_legacy(status)) == KIND_RATE_LIMITED

    def test_legacy_server_errors_are_transient(self):
        for status in (500, 502, 503):
            assert classify_provider_error(_legacy(status)) == KIND_TRANSIENT

    @pytest.mark.parametrize("status", [501, 505])
    def test_non_temporary_server_errors_are_fatal(self, status):
        assert classify_provider_error(_legacy(status)) == KIND_FATAL

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
    def test_client_errors_are_fatal(self, status):
        assert classify_provider_error(_legacy(status)) == KIND_FATAL

    @pytest.mark.parametrize("status", [408, 504])
    def test_gateway_timeouts_are_timeouts(self, status):
        assert classify_provider_error(_legacy(status)) == KIND_TIMEOUT

    def test_compat_rate_limit_error(self):
        exc = _compat(compat_errors.RateLimitError, 429)
        assert classify_provider_error(exc) == KIND_RATE_LIMITED

    def test_compat_internal_server_error(self):
        exc = _compat(compat_errors.InternalServerError, 500)
        assert classify_provider_error(exc) == KIND_TRANSIENT

    def test_compat_api_status_error_uses_status_code(self):
        exc = _compat(compat_errors.APIStatusError, 503)
        assert classify_provider_error(exc) == KIND_TRANSIENT

    def test_compat_api_status_error_non_temporary_is_fatal(self):
        exc = _compat(compat_errors.APIStatusError, 501)
        assert classify_provider_error(exc) == KIND_FATAL

    def test_compat_timeout_error(self):
        exc = compat_errors.APITimeoutError(request=_request())
        assert classify_provider_error(exc) == KIND_TIMEOUT

    def test_compat_connection_error_without_cause_is_fatal(self):
        exc = compat_errors.APIConnectionError(request=_request())
        assert classify_provider_error(exc) == KIND_FATAL

    @pytest.mark.parametrize(
        "cause_factory",
        [
            lambda: httpx.ConnectError("connect", request=_request()),
            lambda: httpx.ReadError("read", request=_request()),
            lambda: httpx.WriteError("write", request=_request()),
            lambda: httpx.RemoteProtocolError("protocol", request=_request()),
        ],
    )
    def test_compat_connection_error_wrapping_transient_cause_is_transient(
        self, cause_factory
    ):
        exc = compat_errors.APIConnectionError(request=_request())
        exc.__cause__ = cause_factory()
        assert classify_provider_error(exc) == KIND_TRANSIENT

    @pytest.mark.parametrize(
        "cause_factory",
        [
            lambda: httpx.TooManyRedirects("loop", request=_request()),
            lambda: httpx.DecodingError("bad", request=_request()),
            lambda: RuntimeError("unknown transport failure"),
        ],
    )
    def test_compat_connection_error_wrapping_fatal_cause_is_fatal(
        self, cause_factory
    ):
        exc = compat_errors.APIConnectionError(request=_request())
        exc.__cause__ = cause_factory()
        assert classify_provider_error(exc) == KIND_FATAL

    def test_compat_connection_error_wrapping_timeout_is_timeout(self):
        exc = compat_errors.APIConnectionError(request=_request())
        exc.__cause__ = httpx.ReadTimeout("slow", request=_request())
        assert classify_provider_error(exc) == KIND_TIMEOUT

    def test_compat_response_validation_error_is_fatal(self):
        exc = compat_errors.APIResponseValidationError(
            response=_response(200), body=None
        )
        assert classify_provider_error(exc) == KIND_FATAL

    def test_authentication_error_is_fatal(self):
        exc = _compat(compat_errors.AuthenticationError, 401)
        assert classify_provider_error(exc) == KIND_FATAL

    @pytest.mark.parametrize(
        "cls_name",
        [
            "BadRequestError",
            "PermissionDeniedError",
            "NotFoundError",
            "ConflictError",
            "UnprocessableEntityError",
        ],
    )
    def test_other_compat_client_errors_are_fatal(self, cls_name):
        cls = getattr(compat_errors, cls_name)
        status = {
            "BadRequestError": 400,
            "PermissionDeniedError": 403,
            "NotFoundError": 404,
            "ConflictError": 409,
            "UnprocessableEntityError": 422,
        }[cls_name]
        assert classify_provider_error(_compat(cls, status)) == KIND_FATAL

    def test_raw_httpx_timeouts_are_timeouts(self):
        assert classify_provider_error(
            httpx.ReadTimeout("slow", request=_request())
        ) == KIND_TIMEOUT
        assert classify_provider_error(
            httpx.ConnectTimeout("slow", request=_request())
        ) == KIND_TIMEOUT

    @pytest.mark.parametrize(
        "factory",
        [
            lambda: httpx.ConnectError("boom", request=_request()),
            lambda: httpx.ReadError("boom", request=_request()),
            lambda: httpx.RemoteProtocolError("boom", request=_request()),
        ],
    )
    def test_raw_httpx_transport_errors_are_transient(self, factory):
        assert classify_provider_error(factory()) == KIND_TRANSIENT

    def test_redirect_and_decoding_errors_are_fatal(self):
        assert classify_provider_error(
            httpx.TooManyRedirects("loop", request=_request())
        ) == KIND_FATAL
        assert classify_provider_error(
            httpx.DecodingError("bad", request=_request())
        ) == KIND_FATAL

    def test_timeout_anywhere_in_the_cause_chain_wins(self):
        # A wrapper whose own type is unrecognised must still be treated
        # as a timeout when it was caused by one: retrying is forbidden.
        wrapper = RuntimeError("wrapped")
        wrapper.__cause__ = httpx.ReadTimeout("slow", request=_request())
        assert classify_provider_error(wrapper) == KIND_TIMEOUT

    def test_compat_timeout_wrapping_transport_timeout(self):
        exc = compat_errors.APITimeoutError(request=_request())
        exc.__cause__ = httpx.ReadTimeout("slow", request=_request())
        assert classify_provider_error(exc) == KIND_TIMEOUT

    def test_unrecognised_exception_is_fatal(self):
        assert classify_provider_error(RuntimeError("boom")) == KIND_FATAL
        assert classify_provider_error(ValueError("boom")) == KIND_FATAL

    def test_message_text_is_never_used_for_classification(self):
        # "timeout" in the message must not make an unknown error
        # retryable or a timeout; only types, codes, and chains count.
        assert classify_provider_error(
            RuntimeError("upstream timeout while reading")
        ) == KIND_FATAL

    def test_status_code_wins_over_a_conflicting_class_name(self):
        exc = _compat(compat_errors.RateLimitError, 400)
        # The class name says rate limit, the authoritative status code
        # says client error; the code is checked first and is fatal.
        assert classify_provider_error(exc) == KIND_FATAL


class TestRetryAfter:
    def test_numeric_header_is_parsed(self):
        response = _response(429)
        response.headers["Retry-After"] = "7"
        exc = compat_errors.RateLimitError(
            "simulated", response=response, body=None
        )
        assert retry_after_seconds(exc) == 7.0

    @pytest.mark.parametrize(
        "value", ["", "abc", "-1", "0", "nan", "inf", "Wed, 21 Oct 2015 07:28:00 GMT"]
    )
    def test_unusable_header_is_ignored(self, value):
        response = _response(429)
        response.headers["Retry-After"] = value
        exc = compat_errors.RateLimitError(
            "simulated", response=response, body=None
        )
        assert retry_after_seconds(exc) is None

    def test_missing_response_has_no_hint(self):
        assert retry_after_seconds(RuntimeError("boom")) is None

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, 1),
            (0, 1),
            (-5, 1),
            ("abc", 1),
            (float("nan"), 1),
            (float("inf"), 1),
            (0.2, 1),
            (1.0, 1),
            (2.1, 3),
            (60, 60),
            (3600, 60),
        ],
    )
    def test_clamp_bounds_the_customer_facing_value(self, value, expected):
        assert clamp_retry_after(value) == expected


class TestPositiveSecondsValidation:
    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"max_attempts": 0}, "max_attempts"),
            ({"max_attempts": 6}, "max_attempts"),
            ({"max_attempts": True}, "max_attempts"),
            ({"max_attempts": 1.5}, "max_attempts"),
            ({"initial_delay_seconds": 0}, "initial_delay_seconds"),
            ({"initial_delay_seconds": -1}, "initial_delay_seconds"),
            ({"initial_delay_seconds": float("inf")}, "initial_delay_seconds"),
            ({"retry_window_seconds": 0}, "retry_window_seconds"),
            ({"retry_window_seconds": float("nan")}, "retry_window_seconds"),
        ],
    )
    def test_invalid_policy_is_rejected(self, kwargs, message):
        with pytest.raises(ValueError, match=message):
            RetryPolicy(**kwargs)

    def test_retries_enabled_reflects_attempt_budget(self):
        assert RetryPolicy(max_attempts=1).retries_enabled is False
        assert RetryPolicy(max_attempts=2).retries_enabled is True


class _Clock:
    def __init__(self, start: float = 0.0):
        self.now = start
        self.reads = 0

    def __call__(self) -> float:
        self.reads += 1
        return self.now


class _Recorder:
    def __init__(self, *, clock: _Clock | None = None, rand_values=None):
        self.clock = clock or _Clock()
        self.sleeps: list[float] = []
        self.events: list[str] = []
        self._rand = list(rand_values or [0.5])

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def rand(self) -> float:
        if len(self._rand) > 1:
            return self._rand.pop(0)
        return self._rand[0]


class TestRetryLoop:
    def test_first_attempt_success_returns_the_value(self):
        calls = []

        def attempt():
            calls.append(1)
            return "value"

        recorder = _Recorder()

        assert call_with_retries(
            attempt,
            policy=RetryPolicy(max_attempts=3),
            clock=recorder.clock,
            sleep=recorder.sleep,
            rand=recorder.rand,
        ) == "value"
        assert calls == [1]
        assert recorder.sleeps == []

    @pytest.mark.parametrize(
        "exc",
        [
            _compat(compat_errors.RateLimitError, 429),
            _compat(compat_errors.InternalServerError, 500),
        ],
    )
    def test_retryable_failure_is_retried_then_succeeds(self, exc):
        calls = []

        def attempt():
            calls.append(1)
            if len(calls) == 1:
                raise exc
            return "ok"

        recorder = _Recorder()
        policy = RetryPolicy(max_attempts=3, initial_delay_seconds=0.25)

        assert call_with_retries(
            attempt,
            policy=policy,
            clock=recorder.clock,
            sleep=recorder.sleep,
            rand=recorder.rand,
        ) == "ok"
        assert calls == [1, 1]
        # Full jitter with rand()=0.5 over a 0.25 s base.
        assert recorder.sleeps == [0.125]

    def test_exhausted_budget_raises_with_the_final_classification(self):
        calls = []

        def attempt():
            calls.append(1)
            raise _compat(compat_errors.InternalServerError, 503)

        recorder = _Recorder()

        with pytest.raises(ProviderCallError) as raised:
            call_with_retries(
                attempt,
                policy=RetryPolicy(max_attempts=3, initial_delay_seconds=0.01),
                clock=recorder.clock,
                sleep=recorder.sleep,
                rand=recorder.rand,
            )

        error = raised.value
        assert error.kind == KIND_TRANSIENT
        assert error.attempts == 3
        assert len(calls) == 3
        assert isinstance(error.__cause__, compat_errors.InternalServerError)

    def test_timeout_is_never_retried(self):
        calls = []

        def attempt():
            calls.append(1)
            raise compat_errors.APITimeoutError(request=_request())

        recorder = _Recorder()

        with pytest.raises(ProviderCallError) as raised:
            call_with_retries(
                attempt,
                policy=RetryPolicy(max_attempts=5),
                clock=recorder.clock,
                sleep=recorder.sleep,
                rand=recorder.rand,
            )

        assert raised.value.kind == KIND_TIMEOUT
        assert raised.value.attempts == 1
        assert calls == [1]
        assert recorder.sleeps == []

    def test_fatal_error_is_never_retried(self):
        calls = []

        def attempt():
            calls.append(1)
            raise _legacy(401)

        recorder = _Recorder()

        with pytest.raises(ProviderCallError) as raised:
            call_with_retries(
                attempt,
                policy=RetryPolicy(max_attempts=5),
                clock=recorder.clock,
                sleep=recorder.sleep,
                rand=recorder.rand,
            )

        assert raised.value.kind == KIND_FATAL
        assert raised.value.attempts == 1
        assert calls == [1]

    def test_unexpected_exception_is_fatal(self):
        with pytest.raises(ProviderCallError) as raised:
            call_with_retries(
                lambda: (_ for _ in ()).throw(ValueError("boom")),
                policy=RetryPolicy(max_attempts=3),
                clock=_Recorder().clock,
                sleep=_Recorder().sleep,
                rand=_Recorder().rand,
            )

        assert raised.value.kind == KIND_FATAL
        assert raised.value.attempts == 1

    def test_aborted_call_propagates_unwrapped(self):
        def attempt():
            raise ProviderCallAborted("shutdown")

        with pytest.raises(ProviderCallAborted):
            call_with_retries(
                attempt,
                policy=RetryPolicy(max_attempts=3),
                clock=_Recorder().clock,
                sleep=_Recorder().sleep,
                rand=_Recorder().rand,
            )

    def test_cancellation_before_the_first_attempt_skips_the_call(self):
        import threading

        cancel_event = threading.Event()
        cancel_event.set()
        calls = []

        with pytest.raises(ProviderCallAborted):
            call_with_retries(
                lambda: calls.append(1),
                policy=RetryPolicy(max_attempts=3),
                cancel_event=cancel_event,
                clock=_Recorder().clock,
                sleep=_Recorder().sleep,
                rand=_Recorder().rand,
            )

        assert calls == []

    def test_cancellation_during_backoff_stops_retrying(self):
        import threading

        cancel_event = threading.Event()
        calls = []
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            cancel_event.set()

        def attempt():
            calls.append(1)
            raise _legacy(503)

        with pytest.raises(ProviderCallAborted):
            call_with_retries(
                attempt,
                policy=RetryPolicy(max_attempts=3, initial_delay_seconds=1.0),
                cancel_event=cancel_event,
                clock=_Clock(),
                sleep=sleep,
                rand=lambda: 0.5,
            )

        assert calls == [1]
        # Backoff is delivered in cancellable chunks (<= 0.05 s), so
        # exactly one chunk is observed before cancellation aborts.
        assert len(sleeps) == 1

    def test_cancellable_backoff_is_chunked(self):
        import threading

        cancel_event = threading.Event()
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)

        def attempt():
            raise _legacy(503)

        with pytest.raises(ProviderCallError):
            call_with_retries(
                attempt,
                policy=RetryPolicy(
                    max_attempts=2,
                    initial_delay_seconds=0.3,
                    retry_window_seconds=3.0,
                ),
                cancel_event=cancel_event,
                clock=_Recorder().clock,
                sleep=sleep,
                rand=lambda: 1.0,
            )

        # Full jitter at 1.0 gives 0.3 s, delivered in small chunks so a
        # shutdown is observed promptly instead of after the whole wait.
        assert sleeps
        assert all(step <= 0.05 for step in sleeps)
        assert sum(sleeps) == pytest.approx(0.3)

    def test_window_blocks_a_retry_that_would_start_too_late(self):
        clock = _Clock()

        def attempt():
            # The failing attempt consumed most of the window.
            clock.now = 2.9
            raise _legacy(503)

        recorder = _Recorder(clock=clock, rand_values=[1.0])

        with pytest.raises(ProviderCallError) as raised:
            call_with_retries(
                attempt,
                policy=RetryPolicy(
                    max_attempts=3,
                    initial_delay_seconds=0.25,
                    retry_window_seconds=3.0,
                ),
                clock=recorder.clock,
                sleep=recorder.sleep,
                rand=recorder.rand,
            )

        assert raised.value.kind == KIND_TRANSIENT
        assert raised.value.attempts == 1
        assert recorder.sleeps == []

    def test_window_allows_a_retry_that_fits(self):
        clock = _Clock()
        calls = []
        starts = []
        sleeps = []

        def attempt():
            calls.append(1)
            starts.append(clock.now)
            if len(calls) == 1:
                clock.now = 1.0
                raise _legacy(503)
            return "ok"

        recorder = _Recorder(clock=clock, rand_values=[0.5])

        def sleep(seconds):
            sleeps.append(seconds)
            clock.now += seconds

        assert call_with_retries(
            attempt,
            policy=RetryPolicy(
                max_attempts=2,
                initial_delay_seconds=0.25,
                retry_window_seconds=3.0,
            ),
            clock=recorder.clock,
            sleep=sleep,
            rand=recorder.rand,
        ) == "ok"

        assert calls == [1, 1]
        assert sleeps == [0.125]
        assert starts == [0.0, 1.125]

    @pytest.mark.parametrize(
        "sleep_end",
        [
            pytest.param(3.0, id="at-boundary"),
            pytest.param(3.01, id="past-boundary"),
        ],
    )
    def test_window_blocks_retry_when_sleep_reaches_or_exceeds_boundary(
        self, sleep_end
    ):
        clock = _Clock()
        calls = []
        sleeps = []

        def attempt():
            calls.append(1)
            clock.now = 1.0
            raise _legacy(503)

        def sleep(seconds):
            sleeps.append(seconds)
            clock.now = sleep_end

        with pytest.raises(ProviderCallError) as raised:
            call_with_retries(
                attempt,
                policy=RetryPolicy(
                    max_attempts=3,
                    initial_delay_seconds=0.25,
                    retry_window_seconds=3.0,
                ),
                clock=clock,
                sleep=sleep,
                rand=lambda: 0.5,
            )

        assert raised.value.kind == KIND_TRANSIENT
        assert raised.value.attempts == 1
        assert calls == [1]
        assert sleeps == [0.125]

    def test_retry_after_hint_extends_the_backoff(self):
        response = _response(429)
        response.headers["Retry-After"] = "1"
        calls = []

        def attempt():
            calls.append(1)
            if len(calls) == 1:
                raise compat_errors.RateLimitError(
                    "simulated", response=response, body=None
                )
            return "ok"

        recorder = _Recorder(rand_values=[0.0])

        assert call_with_retries(
            attempt,
            policy=RetryPolicy(
                max_attempts=2,
                initial_delay_seconds=0.1,
                retry_window_seconds=3.0,
            ),
            clock=recorder.clock,
            sleep=recorder.sleep,
            rand=recorder.rand,
        ) == "ok"

        assert recorder.sleeps == [1.0]

    def test_retry_after_hint_never_exceeds_the_window(self):
        response = _response(429)
        response.headers["Retry-After"] = "30"
        calls = []

        def attempt():
            calls.append(1)
            raise compat_errors.RateLimitError(
                "simulated", response=response, body=None
            )

        recorder = _Recorder()

        with pytest.raises(ProviderCallError) as raised:
            call_with_retries(
                attempt,
                policy=RetryPolicy(
                    max_attempts=3,
                    initial_delay_seconds=0.1,
                    retry_window_seconds=3.0,
                ),
                clock=recorder.clock,
                sleep=recorder.sleep,
                rand=recorder.rand,
            )

        assert raised.value.kind == KIND_RATE_LIMITED
        assert raised.value.attempts == 1
        assert raised.value.retry_after_seconds == 30.0
        assert recorder.sleeps == []

    def test_rate_limit_error_exposes_the_provider_hint(self):
        response = _response(429)
        response.headers["Retry-After"] = "4"
        calls = []

        def attempt():
            calls.append(1)
            raise compat_errors.RateLimitError(
                "simulated", response=response, body=None
            )

        recorder = _Recorder()

        with pytest.raises(ProviderCallError) as raised:
            call_with_retries(
                attempt,
                policy=RetryPolicy(max_attempts=1),
                clock=recorder.clock,
                sleep=recorder.sleep,
                rand=recorder.rand,
            )

        assert raised.value.kind == KIND_RATE_LIMITED
        assert raised.value.retry_after_seconds == 4.0

    def test_oversized_backoff_cannot_start_a_retry(self):
        # The window gates attempt starts: a planned backoff that would
        # consume the whole remaining budget ends the call instead of
        # sleeping and then discovering there is no time left.
        calls = []
        sleeps = []

        def attempt():
            calls.append(1)
            raise _legacy(503)

        policy = RetryPolicy(
            max_attempts=3,
            initial_delay_seconds=10.0,
            retry_window_seconds=3.0,
        )

        with pytest.raises(ProviderCallError) as raised:
            call_with_retries(
                attempt,
                policy=policy,
                clock=_Recorder().clock,
                sleep=sleeps.append,
                rand=lambda: 1.0,
            )

        assert raised.value.kind == KIND_TRANSIENT
        assert calls == [1]
        assert sleeps == []

    def test_error_message_never_contains_provider_text(self):
        calls = []

        def attempt():
            calls.append(1)
            raise _legacy(401)

        with pytest.raises(ProviderCallError) as raised:
            call_with_retries(
                attempt,
                policy=RetryPolicy(max_attempts=1),
                clock=_Recorder().clock,
                sleep=_Recorder().sleep,
                rand=_Recorder().rand,
            )

        assert "simulated" not in str(raised.value)
        assert "401" not in str(raised.value)


def test_classification_is_total_for_random_exceptions():
    for exc in (
        BaseException("base"),
        KeyError("missing"),
        OSError("io"),
        MemoryError(),
    ):
        assert classify_provider_error(exc) in {
            KIND_TIMEOUT,
            KIND_RATE_LIMITED,
            KIND_TRANSIENT,
            KIND_FATAL,
        }


def test_policy_defaults_match_the_documented_bounds():
    policy = RetryPolicy()

    assert policy.max_attempts == 2
    assert policy.initial_delay_seconds == 0.25
    assert policy.retry_window_seconds == 3.0
    assert math.isfinite(policy.retry_window_seconds)


def _metrics_policy(**overrides) -> RetryPolicy:
    """A deterministic, fast policy for the metrics tests.

    ``rand=lambda: 0.0`` zeroes the backoff so no sleep happens unless a
    test installs its own sleep fake.
    """
    values = {
        "max_attempts": 3,
        "initial_delay_seconds": 0.001,
        "retry_window_seconds": 3.0,
    }
    values.update(overrides)

    return RetryPolicy(**values)


def _run(attempt, policy, **overrides):
    options = {
        "clock": _Recorder().clock,
        "sleep": lambda _seconds: None,
        "rand": lambda: 0.0,
    }
    options.update(overrides)

    return call_with_retries(attempt, policy=policy,
                             **options)


class TestRetryMetrics:
    def test_first_attempt_success_counts_only_the_call(self, metrics):
        def attempt():
            return 42

        result = _run(
            attempt,
            _metrics_policy(max_attempts=2),
            operation="query_embedding",
        )

        assert result == 42
        assert _operation_count(
            metrics, "provider_retry_calls_total", operation="query_embedding"
        ) == 1
        assert _operation_count(
            metrics,
            "provider_retry_attempts_total",
            operation="query_embedding",
            kind="transient",
        ) is None
        assert _operation_count(
            metrics,
            "provider_retry_success_total",
            operation="query_embedding",
            attempts="1",
        ) is None
        assert metrics.snapshot().counters.get(
            "provider_retry_success_total", []
        ) == []
        assert _operation_count(
            metrics,
            "provider_retry_exhausted_total",
            operation="query_embedding",
            kind="transient",
            attempts="1",
        ) is None

    def test_success_after_one_retry_is_counted_by_total_attempts(
        self, metrics
    ):
        outcomes = [_legacy(429), "ok"]

        def attempt():
            outcome = outcomes.pop(0)

            if outcome != "ok":
                raise outcome

            return outcome

        result = _run(
            attempt, _metrics_policy(), operation="query_embedding"
        )

        assert result == "ok"
        # calls: one logical invocation.
        assert _operation_count(
            metrics, "provider_retry_calls_total", operation="query_embedding"
        ) == 1
        # attempts: the single failed attempt that triggered the retry.
        assert _operation_count(
            metrics,
            "provider_retry_attempts_total",
            operation="query_embedding",
            kind="rate_limited",
        ) == 1
        # success: recovered after one retry, labelled by TOTAL attempts.
        assert _operation_count(
            metrics,
            "provider_retry_success_total",
            operation="query_embedding",
            attempts="2",
        ) == 1
        assert _operation_count(
            metrics,
            "provider_retry_success_total",
            operation="query_embedding",
            attempts="1",
        ) is None

    def test_success_after_multiple_retries_counts_each_failed_attempt(
        self, metrics
    ):
        outcomes = [_legacy(503), _legacy(500), "ok"]

        def attempt():
            outcome = outcomes.pop(0)

            if outcome != "ok":
                raise outcome

            return outcome

        result = _run(
            attempt, _metrics_policy(), operation="query_embedding"
        )

        assert result == "ok"
        assert _operation_count(
            metrics,
            "provider_retry_attempts_total",
            operation="query_embedding",
            kind="rate_limited",
        ) is None
        assert _operation_count(
            metrics,
            "provider_retry_attempts_total",
            operation="query_embedding",
            kind="transient",
        ) == 2
        assert _operation_count(
            metrics,
            "provider_retry_success_total",
            operation="query_embedding",
            attempts="3",
        ) == 1

    def test_exhaustion_by_max_attempts_records_exhausted(
        self, metrics
    ):
        def attempt():
            raise _legacy(503)

        with pytest.raises(ProviderCallError) as raised:
            _run(
                attempt,
                _metrics_policy(max_attempts=2),
                operation="query_embedding",
            )

        assert raised.value.kind == KIND_TRANSIENT
        # The final failure did not trigger another retry, so only the
        # first (retry-triggering) failure is attributed here.
        assert _operation_count(
            metrics,
            "provider_retry_attempts_total",
            operation="query_embedding",
            kind="transient",
        ) == 1
        assert _operation_count(
            metrics,
            "provider_retry_exhausted_total",
            operation="query_embedding",
            kind="transient",
            attempts="2",
        ) == 1
        assert metrics.snapshot().counters.get(
            "provider_retry_success_total", []
        ) == []

    def test_window_expiry_before_backoff_records_exhausted(
        self, metrics
    ):
        def attempt():
            raise _legacy(503)

        with pytest.raises(ProviderCallError):
            _run(
                attempt,
                _metrics_policy(initial_delay_seconds=10.0),
                rand=lambda: 1.0,
                operation="ingestion_embedding",
            )

        # The call ended before any retry attempt began: no attempt
        # counter, and the exhausted series carries the single attempt.
        assert _operation_count(
            metrics,
            "provider_retry_attempts_total",
            operation="ingestion_embedding",
            kind="transient",
        ) is None
        assert _operation_count(
            metrics,
            "provider_retry_exhausted_total",
            operation="ingestion_embedding",
            kind="transient",
            attempts="1",
        ) == 1

    def test_window_expiry_during_backoff_records_exhausted(
        self, metrics
    ):
        clock = _Clock(0.0)
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            # The backoff is "real": the clock advances three times the
            # nominal delay, pushing the window past its expiry.
            clock.now += seconds * 3

        def attempt():
            raise _legacy(503)

        with pytest.raises(ProviderCallError):
            _run(
                attempt,
                _metrics_policy(
                    initial_delay_seconds=0.05,
                    retry_window_seconds=0.06,
                ),
                clock=clock,
                sleep=sleep,
                rand=lambda: 1.0,
                operation="rag_generation",
            )

        assert sleeps == [0.05]
        assert _operation_count(
            metrics,
            "provider_retry_attempts_total",
            operation="rag_generation",
            kind="transient",
        ) is None
        assert _operation_count(
            metrics,
            "provider_retry_exhausted_total",
            operation="rag_generation",
            kind="transient",
            attempts="1",
        ) == 1

    def test_timeout_exit_is_non_retriable_but_not_exhaustion(self, metrics):
        def attempt():
            raise httpx.ReadTimeout("slow", request=_request())

        with pytest.raises(ProviderCallError) as raised:
            _run(attempt, _metrics_policy(), operation="query_embedding")

        assert raised.value.kind == KIND_TIMEOUT
        # The logical call is counted once, but a timeout that is never
        # retried is NOT retry-budget exhaustion: the counter that
        # distinguishes exhausted retries stays at zero.
        assert _operation_count(
            metrics, "provider_retry_calls_total", operation="query_embedding"
        ) == 1
        assert _operation_count(
            metrics,
            "provider_retry_attempts_total",
            operation="query_embedding",
            kind="timeout",
        ) is None
        assert _operation_count(
            metrics,
            "provider_retry_exhausted_total",
            operation="query_embedding",
            kind="timeout",
            attempts="1",
        ) is None
        assert metrics.snapshot().counters.get(
            "provider_retry_exhausted_total", []
        ) == []

    def test_rate_limited_exhaustion_records_the_kind(self, metrics):
        def attempt():
            raise _legacy(429)

        with pytest.raises(ProviderCallError) as raised:
            _run(
                attempt,
                _metrics_policy(max_attempts=2),
                operation="ingestion_embedding",
            )

        assert raised.value.kind == KIND_RATE_LIMITED
        assert _operation_count(
            metrics,
            "provider_retry_exhausted_total",
            operation="ingestion_embedding",
            kind="rate_limited",
            attempts="2",
        ) == 1

    def test_fatal_exit_is_non_retriable_but_not_exhaustion(self, metrics):
        def attempt():
            raise _legacy(400)

        with pytest.raises(ProviderCallError) as raised:
            _run(attempt, _metrics_policy(), operation="rag_generation")

        assert raised.value.kind == KIND_FATAL
        # An immediate fatal failure never consults the retry budget,
        # so it must not be counted as exhaustion — the distinguishing
        # property a max-attempts exit does have.
        assert _operation_count(
            metrics, "provider_retry_calls_total", operation="rag_generation"
        ) == 1
        assert _operation_count(
            metrics,
            "provider_retry_exhausted_total",
            operation="rag_generation",
            kind="fatal",
            attempts="1",
        ) is None
        assert metrics.snapshot().counters.get(
            "provider_retry_exhausted_total", []
        ) == []
        assert metrics.snapshot().counters.get(
            "provider_retry_attempts_total",
            [],
        ) == []

    def test_retry_budget_reached_on_the_only_attempt_is_exhaustion(
        self, metrics
    ):
        # max_attempts=1: a retriable failure spends the whole budget on
        # the first attempt. This exit IS exhaustion (unlike the fatal
        # and timeout exits above, which fail before the budget does).
        def attempt():
            raise _legacy(503)

        with pytest.raises(ProviderCallError) as raised:
            _run(
                attempt,
                _metrics_policy(max_attempts=1),
                operation="query_embedding",
            )

        assert raised.value.kind == KIND_TRANSIENT
        assert _operation_count(
            metrics,
            "provider_retry_exhausted_total",
            operation="query_embedding",
            kind="transient",
            attempts="1",
        ) == 1
        assert metrics.snapshot().counters.get(
            "provider_retry_attempts_total", []
        ) == []

    def test_cancel_before_an_attempt_keeps_the_call_counter_default(
        self, metrics
    ):
        cancel_event = threading.Event()
        cancel_event.set()

        with pytest.raises(ProviderCallAborted):
            _run(
                lambda: 1,
                _metrics_policy(),
                cancel_event=cancel_event,
            )

        # Called without ``operation``: the backward-compatible default
        # labels the logical call, but failed-attempt/success/exhausted
        # counters are never touched.
        assert _operation_count(
            metrics, "provider_retry_calls_total", operation="unattributed"
        ) == 1
        assert metrics.snapshot().counters.get(
            "provider_retry_attempts_total", []
        ) == []
        assert metrics.snapshot().counters.get(
            "provider_retry_success_total", []
        ) == []
        assert metrics.snapshot().counters.get(
            "provider_retry_exhausted_total", []
        ) == []

    def test_cancelled_backoff_records_no_attempt_or_exhaustion(
        self, metrics
    ):
        cancel_event = threading.Event()

        def sleep(_seconds):
            # Cancellation arrives while the backoff is awaited.
            cancel_event.set()

        def attempt():
            raise _legacy(503)

        with pytest.raises(ProviderCallAborted):
            _run(
                attempt,
                _metrics_policy(),
                cancel_event=cancel_event,
                sleep=sleep,
                rand=lambda: 0.5,
                operation="query_embedding",
            )

        # The cancellation replaces the retry decision: the concurrency
        # layer records caller_cancelled, so nothing is counted here
        # beyond the logical call itself.
        assert _operation_count(
            metrics, "provider_retry_calls_total", operation="query_embedding"
        ) == 1
        assert metrics.snapshot().counters.get(
            "provider_retry_attempts_total", []
        ) == []
        assert metrics.snapshot().counters.get(
            "provider_retry_success_total", []
        ) == []
        assert metrics.snapshot().counters.get(
            "provider_retry_exhausted_total", []
        ) == []

    def test_broken_registry_never_affects_retry_behavior(self, monkeypatch):
        from app.core import provider_retry as retry_module

        class _BrokenRegistry:
            def record_counter(self, *args, **kwargs):
                raise RuntimeError("registry exploded")

        monkeypatch.setattr(
            retry_module, "_provider_metrics", _BrokenRegistry()
        )

        # A retried call still succeeds...
        outcomes = [_legacy(503), "ok"]

        def attempt():
            outcome = outcomes.pop(0)

            if outcome != "ok":
                raise outcome

            return outcome

        assert _run(attempt, _metrics_policy()) == "ok"

        # ...and an exhausting call still raises its classification.
        def failing_attempt():
            raise _legacy(400)

        with pytest.raises(ProviderCallError) as raised:
            _run(
                failing_attempt,
                _metrics_policy(max_attempts=1),
            )

        assert raised.value.kind == KIND_FATAL

    def test_backward_compatible_default_operation(self, metrics):
        def attempt():
            return 7

        # Pre-6.8 call sites never pass ``operation``.
        assert call_with_retries(
            attempt,
            policy=_metrics_policy(),
            clock=_Recorder().clock,
            sleep=lambda _seconds: None,
            rand=lambda: 0.0,
        ) == 7

        assert _operation_count(
            metrics, "provider_retry_calls_total", operation="unattributed"
        ) == 1


class TestProviderOutcomeOf:
    def test_timeout_wins_over_the_provider_call_error_kind(self):
        # A ProviderCallError whose wrapper chain carries a real
        # timeout: the chain-aware timeout check must classify first.
        exc = ProviderCallError(KIND_RATE_LIMITED, attempts=2)
        exc.__cause__ = httpx.ReadTimeout("slow", request=_request())

        assert provider_outcome_of(exc) == "timeout"

    @pytest.mark.parametrize(
        ("kind", "expected"),
        [
            (KIND_TIMEOUT, "timeout"),
            (KIND_RATE_LIMITED, "rate_limited"),
            (KIND_TRANSIENT, "transient"),
            (KIND_FATAL, "fatal"),
        ],
    )
    def test_known_provider_call_error_kinds_are_preserved(
        self, kind, expected
    ):
        assert provider_outcome_of(ProviderCallError(kind, attempts=1)) == (
            expected
        )

    def test_translated_sdk_cause_chain_is_classified(self):
        # The adapters chain the original SDK cause behind the
        # translated error; the cause is classified (not the wrapper).
        from app.services.embeddings import EmbeddingError

        exc = EmbeddingError("simulated")
        exc.__cause__ = httpx.ConnectError(
            "boom", request=_request()
        )

        assert provider_outcome_of(exc) == "transient"

    def test_stage_allowlist_for_embedding_errors(self):
        from app.services.embeddings import EmbeddingError

        assert (
            provider_outcome_of(EmbeddingError("x", stage="provider_request"))
            == "fatal"
        )
        assert (
            provider_outcome_of(
                EmbeddingError("x", stage="embedding_timeout")
            )
            == "timeout"
        )
        assert (
            provider_outcome_of(EmbeddingError("x", stage="rate_limited"))
            == "rate_limited"
        )
        assert (
            provider_outcome_of(
                EmbeddingError("x", stage="provider_unavailable")
            )
            == "transient"
        )

    def test_stage_allowlist_for_generation_errors(self):
        from app.services.generation import GenerationError

        assert (
            provider_outcome_of(
                GenerationError("x", stage="generation_timeout")
            )
            == "timeout"
        )
        assert (
            provider_outcome_of(GenerationError("x", stage="rate_limited"))
            == "rate_limited"
        )
        assert (
            provider_outcome_of(
                GenerationError("x", stage="generation_request")
            )
            == "fatal"
        )
        assert (
            provider_outcome_of(GenerationError("x", stage="configuration"))
            == "fatal"
        )
        assert (
            provider_outcome_of(GenerationError("x", stage="empty_response"))
            == "fatal"
        )
        assert (
            provider_outcome_of(GenerationError("x", stage="no_sources"))
            == "fatal"
        )

    def test_unknown_stage_maps_to_unexpected(self):
        from app.services.embeddings import EmbeddingError

        assert (
            provider_outcome_of(
                EmbeddingError("x", stage="totally_new_stage")
            )
            == "unexpected"
        )

        class _NoneStage(Exception):
            """An exception family carrying a non-string stage."""

        exc = _NoneStage("x")
        exc.stage = None

        # A stage of the wrong type is treated as missing.
        assert provider_outcome_of(exc) == "unexpected"

    def test_unknown_exception_families_map_to_unexpected(self):
        assert provider_outcome_of(KeyError("missing")) == "unexpected"
        assert provider_outcome_of(OSError("io")) == "unexpected"

    def test_aborted_calls_are_outside_the_retry_outcome_set(self):
        assert provider_outcome_of(ProviderCallAborted("cancelled")) == (
            "unexpected"
        )

    def test_module_never_imports_concurrency_or_adapter_layers(self):
        import inspect

        from app.core import provider_retry
        from app.services import embeddings, generation  # noqa: F401

        source = inspect.getsource(provider_retry)

        assert "provider_concurrency" not in source
        assert "from app.services" not in source
        assert "import embeddings" not in source
        assert "import generation" not in source
