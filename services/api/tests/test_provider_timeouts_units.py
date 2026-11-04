"""Unit tests for the Phase 6.6 provider-timeout helpers.

Fully offline: ``app.core.provider_timeouts`` never touches the network
and never imports the Gemini SDK (``httpx`` is imported lazily inside
:func:`is_timeout_error`). No database, credentials, or live call is
required.
"""

import asyncio

import httpx
import pytest

from app.core.provider_timeouts import (
    coerce_timeout_seconds,
    is_timeout_error,
    timeout_milliseconds,
    validate_timeout_seconds,
)


class TestValidateTimeoutSeconds:
    @pytest.mark.parametrize("value", [1, 1.0, 0.5, 10, 3600.0])
    def test_accepts_positive_numbers(self, value):
        assert validate_timeout_seconds(value, name="t") == float(value)

    @pytest.mark.parametrize(
        "value",
        [
            None,
            True,
            False,
            "10",
            "abc",
            0,
            0.0,
            -1,
            -0.5,
            float("nan"),
            float("inf"),
            float("-inf"),
        ],
    )
    def test_rejects_invalid_values(self, value):
        with pytest.raises(ValueError):
            validate_timeout_seconds(value, name="t")


class TestCoerceTimeoutSeconds:
    def test_numeric_string_is_stripped_and_converted(self):
        assert coerce_timeout_seconds(" 12.5 ", name="t") == 12.5

    def test_plain_float_passes_through(self):
        assert coerce_timeout_seconds(2.0, name="t") == 2.0

    def test_integer_passes_through(self):
        assert coerce_timeout_seconds(7, name="t") == 7.0

    @pytest.mark.parametrize("value", ["abc", "", "   ", "nan", "inf", "-inf"])
    def test_bad_strings_are_rejected(self, value):
        with pytest.raises(ValueError):
            coerce_timeout_seconds(value, name="t")

    @pytest.mark.parametrize(
        "value", [None, True, False, 0, -1, float("inf"), float("nan")]
    )
    def test_non_string_invalid_values_are_rejected(self, value):
        with pytest.raises(ValueError):
            coerce_timeout_seconds(value, name="t")


class TestTimeoutMilliseconds:
    def test_whole_seconds_become_milliseconds(self):
        assert timeout_milliseconds(10.0, name="t") == 10000

    def test_fractional_seconds_round_up(self):
        assert timeout_milliseconds(1.0001, name="t") == 1001

    @pytest.mark.parametrize("seconds", [0.001, 0.0001, 0.0000001])
    def test_sub_millisecond_never_becomes_zero(self, seconds):
        # Zero would read as "unbounded" to the SDK.
        assert timeout_milliseconds(seconds, name="t") == 1

    @pytest.mark.parametrize("seconds", [0, -1, float("inf"), float("nan")])
    def test_invalid_seconds_are_rejected(self, seconds):
        with pytest.raises(ValueError):
            timeout_milliseconds(seconds, name="t")


class _CodedError(Exception):
    """Stand-in for an SDK APIError carrying a numeric status code."""

    def __init__(self, code):
        super().__init__("coded error")
        self.code = code


class TestIsTimeoutError:
    @pytest.mark.parametrize(
        "exc",
        [
            TimeoutError("x"),
            asyncio.TimeoutError("x"),
            httpx.ReadTimeout("x"),
            httpx.ConnectTimeout("x"),
            httpx.WriteTimeout("x"),
            httpx.PoolTimeout("x"),
        ],
    )
    def test_true_for_timeout_exceptions(self, exc):
        assert is_timeout_error(exc) is True

    @pytest.mark.parametrize("code", [408, 504])
    def test_true_for_gateway_timeout_status_codes(self, code):
        assert is_timeout_error(_CodedError(code)) is True

    @pytest.mark.parametrize("exc", [RuntimeError("x"), ValueError("x")])
    def test_false_for_generic_errors(self, exc):
        assert is_timeout_error(exc) is False

    @pytest.mark.parametrize("code", [500, 503, 429, 0, None, True, "504"])
    def test_false_for_other_or_non_integer_codes(self, code):
        assert is_timeout_error(_CodedError(code)) is False

    def test_true_for_the_real_sdk_timeout_wrapper(self):
        """The SDK wraps a stalled call in a non-TimeoutError subclass.

        Regression guard: measured against the installed SDK, a client
        timeout raises ``compat_errors.APITimeoutError`` whose MRO goes
        ``APITimeoutError -> APIConnectionError -> APIError ->
        Exception``. It is therefore NOT an ``isinstance`` of
        ``TimeoutError`` (nor of httpx's classes), so it must be
        recognised by name/chained cause.
        """
        compat_errors = pytest.importorskip(
            "google.genai._gaos.lib.compat_errors"
        )

        request = httpx.Request("POST", "https://example.invalid/v1")
        exc = compat_errors.APITimeoutError(request)

        assert isinstance(exc, TimeoutError) is False
        assert is_timeout_error(exc) is True

    def test_true_for_a_stand_in_named_like_the_sdk_wrapper(self):
        # Keeps the contract covered even if the SDK's private module
        # moves or is renamed.
        class APITimeoutError(Exception):
            pass

        assert is_timeout_error(APITimeoutError("x")) is True

    def test_true_when_the_cause_is_a_timeout(self):
        original = httpx.ReadTimeout("stalled")
        wrapper = RuntimeError("wrapped")

        try:
            try:
                raise original
            except httpx.ReadTimeout as exc:
                raise wrapper from exc
        except RuntimeError as raised:
            assert is_timeout_error(raised) is True

    def test_true_when_the_context_is_a_timeout(self):
        original = TimeoutError("stalled")

        try:
            try:
                raise original
            except TimeoutError:
                raise RuntimeError("wrapped while handling")
        except RuntimeError as raised:
            assert raised.__cause__ is None
            assert is_timeout_error(raised) is True

    @pytest.mark.parametrize(
        "exc",
        [
            httpx.ConnectError("unreachable"),
            httpx.RemoteProtocolError("bad protocol"),
        ],
    )
    def test_false_for_non_timeout_transport_errors(self, exc):
        # A connection/DNS failure is not a timeout and must keep the
        # generic provider-failure stage.
        assert is_timeout_error(exc) is False

    def test_self_referential_chain_terminates(self):
        looped = RuntimeError("looped")
        looped.__cause__ = looped

        assert is_timeout_error(looped) is False

    def test_long_chain_is_bounded(self):
        # Far more links than the walk depth: terminates instead of
        # recursing forever or scanning an unbounded chain.
        head = RuntimeError("head")
        current = head

        for _ in range(50):
            nxt = RuntimeError("next")
            current.__cause__ = nxt
            current = nxt

        assert is_timeout_error(head) is False
