"""Phase 6.8 unit tests: JSON log formatting and configuration modes.

Covers (Plan v3 §4, correction C4/C5; steps 1-2 scope only):

- the JSON formatter's output shape and explicit field allowlist,
- safe interpolation-argument rendering and the no-traceback rule,
- the formatter-failure fallback line,
- idempotent repeated JSON configuration and JSON->text restoration,
  with and without pre-existing ``app`` logger handlers,
- text-mode no-op behavior.

All tests are offline: no database, no network, no real stdout use
(the handler under test is attached to an in-memory ``StringIO``
unless the module-level configuration is exercised).
"""

import io
import json
import logging

import pytest

from app.core.observability import (
    ALLOWED_FIELD_KEYS,
    ObsJSONFormatter,
    configure_logging,
)
from app.core.observability import _installed_handlers


_MARKED = "_shoppilot_observability"


def _record(
    msg: object,
    args: tuple = (),
    level: int = logging.ERROR,
    name: str = "app.services.test",
    exc_info=None,
) -> logging.LogRecord:
    return logging.LogRecord(
        name=name,
        level=level,
        pathname="test.py",
        lineno=1,
        msg=msg,
        args=args,
        exc_info=exc_info,
    )


def _obs_record(obs: dict, msg: object = "message", args: tuple = ()):
    record = _record(msg, args)
    record.obs = obs

    return record


def _json_record_handler():
    """Return (logger, buffer, handler) wired up like the real one."""
    buffer = io.StringIO()
    logger = logging.getLogger("app.observability_units")
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(ObsJSONFormatter())
    logger.addHandler(handler)
    logger.propagate = False

    try:
        yield logger, buffer, handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(logging.NOTSET)
        logger.propagate = True


@pytest.fixture
def json_logger():
    yield from _json_record_handler()


class TestOutputShape:
    def test_basic_record_renders_one_json_object(self, json_logger):
        logger, buffer, _ = json_logger

        logger.error("Provider failed: %s", "ProviderCallError")

        lines = buffer.getvalue().strip().splitlines()
        assert len(lines) == 1

        payload = json.loads(lines[0])

        assert payload["level"] == "ERROR"
        assert payload["logger"] == "app.observability_units"
        assert payload["message"] == "Provider failed: ProviderCallError"
        assert set(payload) == {"time", "level", "logger", "message"}

    def test_safe_interpolation_arguments_are_rendered(self, json_logger):
        logger, buffer, _ = json_logger

        # Project-convention args: type name, retry attempts, count.
        logger.error(
            "Provider attempt %d/%d failed with %s; retrying.",
            2,
            4,
            "rate_limited",
        )

        payload = json.loads(buffer.getvalue().strip())
        assert payload["message"] == (
            "Provider attempt 2/4 failed with rate_limited; retrying."
        )


class TestFieldAllowlist:
    def test_allowed_obs_keys_are_rendered(self, json_logger):
        logger, buffer, _ = json_logger

        logger.info(
            "Provider call finished.",
            extra={"obs": {"outcome": "success", "duration_seconds": 0.1874}},
        )

        payload = json.loads(buffer.getvalue().strip())
        assert payload["event"] == {
            "outcome": "success",
            "duration_seconds": 0.1874,  # rounded to 6 decimal places
        }

    def test_unknown_keys_and_unsafe_types_are_dropped(self, json_logger):
        logger, buffer, _ = json_logger

        logger.info(
            "Message only.",
            extra={
                "obs": {
                    "unknown_key": "dropped",
                    "query_text": "SELECT secret",  # not allowlisted
                    "flag": True,  # bool dropped
                    "nested": {"bad": 1},  # container dropped
                    "list": [1, 2],  # container dropped
                    "object": object(),  # exotic dropped
                    "kind": "kept",
                }
            },
        )

        payload = json.loads(buffer.getvalue().strip())
        assert payload["event"] == {"kind": "kept"}
        assert payload["message"] == "Message only."

    def test_nonfinite_floats_are_dropped(self, json_logger):
        logger, buffer, _ = json_logger

        logger.info(
            "Message only.",
            extra={"obs": {"duration_seconds": float("inf")}},
        )

        payload = json.loads(buffer.getvalue().strip())
        assert "event" not in payload

    def test_allowlist_is_exactly_the_planned_vocabulary(self):
        assert ALLOWED_FIELD_KEYS == frozenset(
            {
                "request_id",
                "operation",
                "resource",
                "outcome",
                "kind",
                "stage",
                "attempts",
                "wait_seconds",
                "duration_seconds",
                "count",
                "error_type",
            }
        )


class TestExceptionRedaction:
    def test_exc_info_is_never_serialized(self, json_logger):
        logger, buffer, _ = json_logger

        record = _record("Provider failed.", exc_info=(ValueError, ValueError("sdk text"), None))
        record.exc_text = "Traceback with provider internals"

        assert not buffer.getvalue()
        logger.handle(record)

        payload = json.loads(buffer.getvalue().strip())

        assert payload["message"] == "Provider failed."
        assert "sdk text" not in json.dumps(payload)
        assert "Traceback" not in json.dumps(payload)
        assert "exc_info" not in payload
        assert "exc_text" not in payload


class TestFormatterFailureFallback:
    def test_broken_interpolation_yields_the_fallback_line(self):
        # Two placeholders, one arg: getMessage() itself raises while
        # formatting, so the formatter must fall back instead of
        # failing logging. (With EMPTY args, %-formatting is skipped
        # entirely and no failure occurs — that is why an argument is
        # required here.)
        #
        # Asserted through the formatter directly. Pytest attaches its
        # own LogCaptureHandlers to any logger used in tests, and those
        # handlers format the record independently and raise — so a
        # real logger/handler round-trip cannot be used for a record
        # that is deliberately malformed. The handler path is covered
        # by format() never raising (exotic-records test below).
        record = logging.LogRecord(
            name="app.services.test",
            level=logging.ERROR,
            pathname="test.py",
            lineno=1,
            msg="Failed: %s %s",
            args=("only-one-arg",),
            exc_info=None,
        )

        line = ObsJSONFormatter().format(record)

        payload = json.loads(line)
        assert payload["message"] == "Log serialization failed"
        assert payload["level"] == "ERROR"
        assert {"time", "level", "logger", "message"} <= set(payload)

    def test_fallback_never_raises_for_exotic_records(self, monkeypatch):
        import app.core.observability as obs

        # Force the JSON engine itself to explode, so the fallback
        # chain (render -> fallback dumps -> static line) is exercised
        # end to end. A pathological msg alone is NOT enough: with
        # empty args, getMessage() just returns it unformatted.
        def boom(*args, **kwargs):
            raise RuntimeError("json engine down")

        monkeypatch.setattr(obs.json, "dumps", boom)

        record = _record("%s and %.2f")
        record.msg = None  # pathological

        line = ObsJSONFormatter().format(record)

        assert json.loads(line)["message"] == "Log serialization failed"


class TestConfiguration:
    def _assert_no_installed_handlers(self):
        logger = logging.getLogger("app")
        assert _installed_handlers(logger) == []
        assert not any(getattr(h, _MARKED, False) for h in logger.handlers)

    def test_repeated_json_configuration_is_idempotent(self):
        from app.core.observability import _install_json

        logging.getLogger("app").propagate = True  # pretend a baseline

        try:
            _install_json()
            _install_json()
            _install_json()

            marked = _installed_handlers(logging.getLogger("app"))
            assert len(marked) == 1
            assert isinstance(
                marked[0].formatter, ObsJSONFormatter
            )
            assert logging.getLogger("app").propagate is False
        finally:
            configure_logging("text")

        self._assert_no_installed_handlers()

    def test_json_to_text_restores_true_baseline(self):
        logger = logging.getLogger("app")
        propagated_before = logger.propagate

        try:
            configure_logging("json")

            assert _installed_handlers(logger)
            assert logger.propagate is False

            configure_logging("text")

            self._assert_no_installed_handlers()
            assert logger.propagate is propagated_before
            assert logger.level == logging.NOTSET
        finally:
            configure_logging("text")

    def test_json_to_text_keeps_pre_existing_handlers(self):
        logger = logging.getLogger("app")

        pre_existing = logging.StreamHandler(io.StringIO())
        logger.addHandler(pre_existing)
        logger.setLevel(10)
        logger.propagate = True  # non-default, must be restored

        try:
            configure_logging("json")

            handlers = _installed_handlers(logger)
            assert len(handlers) == 1  # exactly ours; the other stays
            assert pre_existing in logger.handlers
            assert logger.propagate is False
            # The foreign handler is muted, never removed, while JSON
            # mode is active (no duplicate or mixed-format output).
            assert pre_existing.level == logging.CRITICAL + 1

            configure_logging("text")

            # Only our handler is removed; baseline state is restored.
            assert logger.handlers == [pre_existing]
            assert logger.level == 10  # captured, not assumed
            assert logger.propagate is True  # captured, not assumed
            assert pre_existing.level == logging.NOTSET  # captured level restored
        finally:
            logger.removeHandler(pre_existing)
            logger.setLevel(logging.NOTSET)
            logger.propagate = True
            configure_logging("text")

    def test_json_mode_produces_no_duplicate_or_mixed_output(self):
        """With a pre-existing handler, JSON mode emits through ours only.

        The pre-existing handler's buffer must stay empty while JSON
        mode is active (no plain-text duplication), and it must resume
        rendering after the switch back to text mode.
        """
        logger = logging.getLogger("app")
        buffer = io.StringIO()
        pre_existing = logging.StreamHandler(buffer)
        logger.addHandler(pre_existing)
        logger.setLevel(logging.INFO)

        try:
            configure_logging("json")

            record = logging.LogRecord(
                name="app",
                level=logging.ERROR,
                pathname="test.py",
                lineno=1,
                msg="json-only probe",
                args=(),
                exc_info=None,
            )
            logger.handle(record)

            assert buffer.getvalue() == ""  # muted: no duplicate line

            configure_logging("text")

            assert logger.handlers == [pre_existing]
            assert pre_existing.level == logging.NOTSET

            resumed = logging.LogRecord(
                name="app",
                level=logging.ERROR,
                pathname="test.py",
                lineno=1,
                msg="text-only probe",
                args=(),
                exc_info=None,
            )
            logger.handle(resumed)

            assert buffer.getvalue().count("text-only probe") == 1
            assert "json-only probe" not in buffer.getvalue()
        finally:
            logger.removeHandler(pre_existing)
            logger.setLevel(logging.NOTSET)
            logger.propagate = True
            configure_logging("text")

    def test_uninstalled_text_mode_is_a_complete_no_op(self):
        logger = logging.getLogger("app")

        configure_logging("text")
        assert logger.handlers == []
        assert logger.level == logging.NOTSET
        assert logger.propagate is True

        # Still no markers, no baseline consumed.
        assert _installed_handlers(logger) == []

    def test_unknown_mode_is_rejected(self):
        with pytest.raises(ValueError):
            configure_logging("structured")


class TestTextModePreservation:
    def test_text_mode_records_render_with_the_default_formatter(
        self, caplog
    ):
        configure_logging("text")

        logger = logging.getLogger("app.services.text_test")
        # caplog defaults to WARNING; the record under test is INFO.
        caplog.set_level(logging.INFO, logger="app.services.text_test")

        logger.info("Text mode says %s with %d.", "value", 3)

        assert caplog.records[-1].getMessage() == (
            "Text mode says value with 3."
        )
        # No JSON structure and no correlation stamp anywhere.
        assert not hasattr(caplog.records[-1], "request_id")
        assert not hasattr(caplog.records[-1], "obs")
