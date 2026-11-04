"""Unit tests for provider configuration validation and defaults."""

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def _settings(**overrides) -> Settings:
    base = {
        "database_url": "postgresql://postgres:postgres@127.0.0.1:54322/postgres",
        "supabase_url": "http://127.0.0.1:54321",
        "supabase_service_role_key": "test-key",
    }
    base.update(overrides)
    return Settings(**base)


_TIMEOUT_FIELDS = (
    "gemini_embedding_timeout_seconds",
    "gemini_generation_timeout_seconds",
    "rag_request_deadline_seconds",
)


class TestTimeoutFieldCoercion:
    @pytest.mark.parametrize("field", _TIMEOUT_FIELDS)
    def test_numeric_environment_string_is_coerced(self, field):
        assert getattr(_settings(**{field: "12.5"}), field) == 12.5

    @pytest.mark.parametrize("field", _TIMEOUT_FIELDS)
    def test_padded_numeric_string_is_coerced(self, field):
        assert getattr(_settings(**{field: " 8 "}), field) == 8.0

    @pytest.mark.parametrize("field", _TIMEOUT_FIELDS)
    def test_plain_float_is_accepted(self, field):
        assert getattr(_settings(**{field: 3.5}), field) == 3.5

    @pytest.mark.parametrize("field", _TIMEOUT_FIELDS)
    @pytest.mark.parametrize(
        "bad",
        [
            "abc",
            "",
            "   ",
            "nan",
            "inf",
            "-inf",
            True,
            False,
            None,
            0,
            -1,
            float("nan"),
            float("inf"),
        ],
    )
    def test_invalid_values_are_rejected(self, field, bad):
        with pytest.raises(ValidationError):
            _settings(**{field: bad})


class TestTimeoutDefaults:
    def test_defaults_match_the_documented_values(self):
        settings = _settings()

        assert settings.gemini_embedding_timeout_seconds == 10.0
        assert settings.gemini_generation_timeout_seconds == 30.0
        assert settings.rag_request_deadline_seconds == 60.0

    def test_deadline_exceeds_provider_timeout_sum(self):
        settings = _settings()

        assert settings.rag_request_deadline_seconds > (
            settings.gemini_embedding_timeout_seconds
            + settings.gemini_generation_timeout_seconds
        )


_INTEGER_FIELDS = (
    "gemini_embedding_max_concurrency",
    "gemini_generation_max_concurrency",
    "gemini_retry_max_attempts",
)

_SECONDS_FIELDS = (
    "rag_provider_concurrency_wait_seconds",
    "ingestion_provider_concurrency_wait_seconds",
    "gemini_retry_initial_delay_seconds",
    "gemini_retry_window_seconds",
)


class TestConcurrencyFieldCoercion:
    @pytest.mark.parametrize(
        "field", ["gemini_embedding_max_concurrency", "gemini_generation_max_concurrency"]
    )
    @pytest.mark.parametrize(("raw", "expected"), [
        ("1", 1),
        ("4", 4),
        (" 2 ", 2),
        ("64", 64),
        (1, 1),
        (3, 3),
        (5.0, 5),
        (64.0, 64),
    ],)
    def test_valid_values_are_accepted(self, field, raw, expected):
        assert getattr(_settings(**{field: raw}), field) == expected

    @pytest.mark.parametrize(
        "field", ["gemini_embedding_max_concurrency", "gemini_generation_max_concurrency"]
    )
    @pytest.mark.parametrize(
        "bad",
       [
        0,
        -1,
        "0",
        "-2",
        "",
        "abc",
        True,
        False,
        None,
        1.5,
        "1.5",
        float("inf"),
        float("nan"),
        65,
        66,
        100,
        1000,
        "65",
        "100",
        "1000",
    ],
    )
    def test_invalid_limits_are_rejected(self, field, bad):
        with pytest.raises(ValidationError):
            _settings(**{field: bad})


class TestRetryFieldCoercion:
    @pytest.mark.parametrize(("raw", "expected"), [("1", 1), ("5", 5), (2, 2), (3.0, 3)])
    def test_valid_attempts_are_accepted(self, raw, expected):
        assert _settings(gemini_retry_max_attempts=raw).gemini_retry_max_attempts == expected

    @pytest.mark.parametrize(
        "bad", [0, -1, 6, 10, "0", "6", "abc", "", True, None, 1.5, "2.5"]
    )
    def test_invalid_attempts_are_rejected(self, bad):
        with pytest.raises(ValidationError):
            _settings(gemini_retry_max_attempts=bad)

    @pytest.mark.parametrize("field", _SECONDS_FIELDS)
    @pytest.mark.parametrize(("raw", "expected"), [("1.5", 1.5), (" 2 ", 2.0), (0.25, 0.25)])
    def test_valid_seconds_are_accepted(self, field, raw, expected):
        assert getattr(_settings(**{field: raw}), field) == expected

    @pytest.mark.parametrize("field", _SECONDS_FIELDS)
    @pytest.mark.parametrize(
        "bad",
        ["abc", "", "   ", "nan", "inf", "-inf", True, False, None, 0, 0.0, -1, float("nan"), float("inf")],
    )
    def test_invalid_seconds_are_rejected(self, field, bad):
        with pytest.raises(ValidationError):
            _settings(**{field: bad})


class TestConcurrencyAndRetryDefaults:
    def test_defaults_match_the_documented_values(self):
        settings = _settings()

        assert settings.gemini_embedding_max_concurrency == 4
        assert settings.gemini_generation_max_concurrency == 2
        assert settings.rag_provider_concurrency_wait_seconds == 2.0
        assert settings.ingestion_provider_concurrency_wait_seconds == 20.0
        assert settings.gemini_retry_max_attempts == 2
        assert settings.gemini_retry_initial_delay_seconds == 0.25
        assert settings.gemini_retry_window_seconds == 3.0

    def test_wait_budgets_stay_below_the_rag_deadline(self):
        settings = _settings()

        assert settings.rag_provider_concurrency_wait_seconds < (
            settings.rag_request_deadline_seconds
        )
        assert settings.ingestion_provider_concurrency_wait_seconds < (
            settings.rag_request_deadline_seconds
        )

    def test_retry_window_plus_sdk_timeout_stays_below_the_deadline(self):
        settings = _settings()

        assert (
            settings.gemini_retry_window_seconds
            + settings.gemini_generation_timeout_seconds
            < settings.rag_request_deadline_seconds
        )
        assert (
            settings.gemini_retry_window_seconds
            + settings.gemini_embedding_timeout_seconds
            < settings.rag_request_deadline_seconds
        )

    def test_retry_policy_maps_from_settings(self):
        from app.core.provider_retry import RetryPolicy

        settings = _settings(
            gemini_retry_max_attempts=3,
            gemini_retry_initial_delay_seconds=0.5,
            gemini_retry_window_seconds=2.5,
        )
        policy = RetryPolicy.from_settings(settings)

        assert policy.max_attempts == 3
        assert policy.initial_delay_seconds == 0.5
        assert policy.retry_window_seconds == 2.5

    def test_provider_limiters_map_from_settings(self):
        from app.core.provider_concurrency import ProviderLimiters

        settings = _settings(
            gemini_embedding_max_concurrency=3,
            gemini_generation_max_concurrency=1,
            rag_provider_concurrency_wait_seconds=1.5,
            ingestion_provider_concurrency_wait_seconds=12.0,
        )
        limiters = ProviderLimiters.from_settings(settings)

        assert limiters.embedding_limit == 3
        assert limiters.generation_limit == 1
        assert limiters.rag_wait_seconds == 1.5
        assert limiters.ingestion_wait_seconds == 12.0
        assert limiters.pending_cap["embedding"] == 6
        assert limiters.pending_cap["generation"] == 2
"""Unit tests for provider configuration validation and defaults."""

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def _settings(**overrides) -> Settings:
    base = {
        "database_url": "postgresql://postgres:postgres@127.0.0.1:54322/postgres",
        "supabase_url": "http://127.0.0.1:54321",
        "supabase_service_role_key": "test-key",
    }
    base.update(overrides)
    return Settings(**base)


_TIMEOUT_FIELDS = (
    "gemini_embedding_timeout_seconds",
    "gemini_generation_timeout_seconds",
    "rag_request_deadline_seconds",
)

_CONCURRENCY_FIELDS = (
    "gemini_embedding_max_concurrency",
    "gemini_generation_max_concurrency",
)

_INTEGER_FIELDS = (
    *_CONCURRENCY_FIELDS,
    "gemini_retry_max_attempts",
)

_SECONDS_FIELDS = (
    "rag_provider_concurrency_wait_seconds",
    "ingestion_provider_concurrency_wait_seconds",
    "gemini_retry_initial_delay_seconds",
    "gemini_retry_window_seconds",
)


class TestGenerationModelConfiguration:
    """Phase 6.3: the RAG generation model is configurable, with a default."""

    _REQUIRED = {
        "database_url": "postgresql://postgres:postgres@127.0.0.1:54322/postgres",
        "supabase_url": "http://127.0.0.1:54321",
        "supabase_service_role_key": "test-key",
    }

    def test_default_is_gemini_3_5_flash_lite(self, monkeypatch):
        # Ignore both the environment and the developer's local .env so
        # this test observes the Python default itself.
        monkeypatch.delenv("GEMINI_GENERATION_MODEL", raising=False)

        settings = Settings(_env_file=None, **self._REQUIRED)

        assert settings.gemini_generation_model == "gemini-3.5-flash-lite"

    def test_environment_variable_overrides_the_default(self, monkeypatch):
        from app.core.config import get_settings

        monkeypatch.setenv("GEMINI_GENERATION_MODEL", "gemini-9.9-other")
        get_settings.cache_clear()

        try:
            settings = get_settings()
            assert settings.gemini_generation_model == "gemini-9.9-other"
        finally:
            # Never leak the cached test settings into other tests.
            get_settings.cache_clear()

    def test_explicit_value_overrides_the_default(self):
        settings = Settings(
            _env_file=None,
            gemini_generation_model="gemini-9.9-other",
            **self._REQUIRED,
        )

        assert settings.gemini_generation_model == "gemini-9.9-other"


class TestTimeoutFieldCoercion:
    @pytest.mark.parametrize("field", _TIMEOUT_FIELDS)
    def test_numeric_environment_string_is_coerced(self, field):
        assert getattr(_settings(**{field: "12.5"}), field) == 12.5

    @pytest.mark.parametrize("field", _TIMEOUT_FIELDS)
    def test_padded_numeric_string_is_coerced(self, field):
        assert getattr(_settings(**{field: " 8 "}), field) == 8.0

    @pytest.mark.parametrize("field", _TIMEOUT_FIELDS)
    def test_plain_float_is_accepted(self, field):
        assert getattr(_settings(**{field: 3.5}), field) == 3.5

    @pytest.mark.parametrize(
        "field",
        _TIMEOUT_FIELDS,
    )
    @pytest.mark.parametrize(
        "bad",
        [
            "abc",
            "",
            "   ",
            "nan",
            "inf",
            "-inf",
            True,
            False,
            None,
            0,
            -1,
            float("nan"),
            float("inf"),
        ],
    )
    def test_invalid_values_are_rejected(self, field, bad):
        with pytest.raises(ValidationError):
            _settings(**{field: bad})


class TestTimeoutDefaults:
    def test_defaults_match_expected_values(self):
        settings = _settings()

        assert settings.gemini_embedding_timeout_seconds == 10.0
        assert settings.gemini_generation_timeout_seconds == 30.0
        assert settings.rag_request_deadline_seconds == 60.0

    def test_deadline_exceeds_provider_timeout_sum(self):
        settings = _settings()

        assert settings.rag_request_deadline_seconds > (
            settings.gemini_embedding_timeout_seconds
            + settings.gemini_generation_timeout_seconds
        )


class TestConcurrencyFieldCoercion:
    @pytest.mark.parametrize("field", _CONCURRENCY_FIELDS)
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1", 1),
            ("4", 4),
            (" 2 ", 2),
            ("64", 64),
            (1, 1),
            (3, 3),
            (5.0, 5),
            (64.0, 64),
        ],
    )
    def test_valid_values_are_accepted(self, field, raw, expected):
        assert getattr(_settings(**{field: raw}), field) == expected

    @pytest.mark.parametrize("field", _CONCURRENCY_FIELDS)
    @pytest.mark.parametrize(
        "bad",
        [
            0,
            -1,
            "0",
            "-2",
            "",
            "abc",
            True,
            False,
            None,
            1.5,
            "1.5",
            float("inf"),
            float("nan"),
            65,
            66,
            100,
            1000,
            "65",
            "100",
            "1000",
        ],
    )
    def test_invalid_limits_are_rejected(self, field, bad):
        with pytest.raises(ValidationError):
            _settings(**{field: bad})

    @pytest.mark.parametrize("field", _CONCURRENCY_FIELDS)
    def test_maximum_concurrency_is_64(self, field):
        assert getattr(_settings(**{field: 64}), field) == 64

        with pytest.raises(ValidationError):
            _settings(**{field: 65})


class TestRetryFieldCoercion:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1", 1),
            ("5", 5),
            (2, 2),
            (3.0, 3),
        ],
    )
    def test_valid_attempts_are_accepted(self, raw, expected):
        assert _settings(gemini_retry_max_attempts=raw).gemini_retry_max_attempts == expected

    @pytest.mark.parametrize(
        "bad",
        [0, -1, 6, 10, "0", "6", "abc", "", True, None, 1.5, "2.5"],
    )
    def test_invalid_attempts_are_rejected(self, bad):
        with pytest.raises(ValidationError):
            _settings(gemini_retry_max_attempts=bad)

    @pytest.mark.parametrize("field", _SECONDS_FIELDS)
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1.5", 1.5),
            (" 2 ", 2.0),
            (0.25, 0.25),
        ],
    )
    def test_valid_seconds_are_accepted(self, field, raw, expected):
        assert getattr(_settings(**{field: raw}), field) == expected

    @pytest.mark.parametrize("field", _SECONDS_FIELDS)
    @pytest.mark.parametrize(
        "bad",
        [
            "abc",
            "",
            "   ",
            "nan",
            "inf",
            "-inf",
            True,
            False,
            None,
            0,
            0.0,
            -1,
            float("nan"),
            float("inf"),
        ],
    )
    def test_invalid_seconds_are_rejected(self, field, bad):
        with pytest.raises(ValidationError):
            _settings(**{field: bad})


class TestConcurrencyAndRetryDefaults:
    def test_defaults_match_expected_values(self):
        settings = _settings()

        assert settings.gemini_embedding_max_concurrency == 4
        assert settings.gemini_generation_max_concurrency == 2
        assert settings.rag_provider_concurrency_wait_seconds == 2.0
        assert settings.ingestion_provider_concurrency_wait_seconds == 20.0
        assert settings.gemini_retry_max_attempts == 2
        assert settings.gemini_retry_initial_delay_seconds == 0.25
        assert settings.gemini_retry_window_seconds == 3.0

    def test_wait_budgets_stay_below_the_rag_deadline(self):
        settings = _settings()

        assert settings.rag_provider_concurrency_wait_seconds < (
            settings.rag_request_deadline_seconds
        )
        assert settings.ingestion_provider_concurrency_wait_seconds < (
            settings.rag_request_deadline_seconds
        )

    def test_retry_window_plus_sdk_timeout_stays_below_the_deadline(self):
        settings = _settings()

        assert (
            settings.gemini_retry_window_seconds
            + settings.gemini_generation_timeout_seconds
            < settings.rag_request_deadline_seconds
        )
        assert (
            settings.gemini_retry_window_seconds
            + settings.gemini_embedding_timeout_seconds
            < settings.rag_request_deadline_seconds
        )

    def test_retry_policy_maps_from_settings(self):
        from app.core.provider_retry import RetryPolicy

        settings = _settings(
            gemini_retry_max_attempts=3,
            gemini_retry_initial_delay_seconds=0.5,
            gemini_retry_window_seconds=2.5,
        )
        policy = RetryPolicy.from_settings(settings)

        assert policy.max_attempts == 3
        assert policy.initial_delay_seconds == 0.5
        assert policy.retry_window_seconds == 2.5

    def test_provider_limiters_map_from_settings(self):
        from app.core.provider_concurrency import ProviderLimiters

        settings = _settings(
            gemini_embedding_max_concurrency=3,
            gemini_generation_max_concurrency=1,
            rag_provider_concurrency_wait_seconds=1.5,
            ingestion_provider_concurrency_wait_seconds=12.0,
        )
        limiters = ProviderLimiters.from_settings(settings)

        assert limiters.embedding_limit == 3
        assert limiters.generation_limit == 1
        assert limiters.rag_wait_seconds == 1.5
        assert limiters.ingestion_wait_seconds == 12.0
        assert limiters.pending_cap["embedding"] == 6
        assert limiters.pending_cap["generation"] == 2