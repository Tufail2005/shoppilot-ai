"""Unit tests for the Phase 5.5 embedding provider.

These tests do not require the local Supabase database, Storage,
FastAPI requests, Gemini credentials, or network access: they exercise
app.services.embeddings against a fake client that records requests and
replays canned responses.
"""

import math
import threading
from types import SimpleNamespace

import httpx
import pytest
from google.genai import errors as legacy_errors
from google.genai import types
from pydantic import SecretStr

from app.core.provider_retry import ProviderCallAborted, RetryPolicy
from app.services import embeddings as embeddings_module
from app.services.embeddings import (
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL,
    EmbeddingError,
    GeminiEmbeddingProvider,
    create_gemini_provider,
)


def _distinct_vector(index: int) -> list[float]:
    """A deterministic 768-float vector unique to `index`."""
    return [float(index) + position * 0.001 for position in range(EMBEDDING_DIMENSIONS)]


def _make_vectors_response(vectors) -> SimpleNamespace:
    """Build a response shaped like the SDK EmbedContentResponse."""
    return SimpleNamespace(
        embeddings=[SimpleNamespace(values=list(vector)) for vector in vectors]
    )


def _default_response_factory(request):
    return _make_vectors_response(
        [_distinct_vector(i) for i in range(len(request["contents"]))]
    )


class FakeModelsApi:
    """Records embed_content requests and replays canned behavior."""

    def __init__(self, *, response_factory=_default_response_factory, error=None):
        self.requests = []
        self.response_factory = response_factory
        self.error = error

    def embed_content(self, *, model, contents, config):
        request = {"model": model, "contents": contents, "config": config}
        self.requests.append(request)

        if self.error is not None:
            raise self.error

        return self.response_factory(request)


def _make_provider(api) -> GeminiEmbeddingProvider:
    return GeminiEmbeddingProvider(SimpleNamespace(models=api))


class SequencedModelsApi(FakeModelsApi):
    """Fails with the given exceptions in order, then succeeds."""

    def __init__(self, errors):
        super().__init__()
        self.pending_errors = list(errors)

    def embed_content(self, *, model, contents, config):
        if self.pending_errors:
            error = self.pending_errors.pop(0)
            self.requests.append(
                {"model": model, "contents": contents, "config": config}
            )
            raise error

        return super().embed_content(
            model=model,
            contents=contents,
            config=config,
        )


def _request_texts(request) -> list[str]:
    return [content.parts[0].text for content in request["contents"]]


class TestEmbedContractBasics:
    def test_empty_input_returns_empty_list_without_api_call(self):
        api = FakeModelsApi()

        provider = _make_provider(api)

        assert provider.embed_documents([]) == []
        assert provider.embed_query([]) == []
        assert api.requests == []

    def test_single_input_produces_one_768_dimensional_vector(self):
        api = FakeModelsApi()
        provider = _make_provider(api)

        result = provider.embed_documents(["returns are accepted within 30 days"])

        assert len(result) == 1
        vector = result[0]

        assert len(vector) == EMBEDDING_DIMENSIONS == 768
        assert all(isinstance(value, float) for value in vector)
        assert all(math.isfinite(value) for value in vector)

    def test_multiple_inputs_preserve_order_with_distinct_vectors(self):
        api = FakeModelsApi()
        provider = _make_provider(api)
        texts = ["first document text", "second document text", "third document text"]

        result = provider.embed_documents(texts)

        assert len(result) == len(texts)

        for index, vector in enumerate(result):
            assert vector[0] == float(index)

        assert len({tuple(vector) for vector in result}) == len(result)

    def test_embed_query_single_text_returns_one_vector(self):
        api = FakeModelsApi()
        provider = _make_provider(api)

        result = provider.embed_query(["where is my refund"])

        assert len(result) == 1
        assert all(isinstance(value, float) for value in result[0])


class TestPromptFormatting:
    def test_documents_use_document_structure_prefix(self):
        api = FakeModelsApi()
        provider = _make_provider(api)

        provider.embed_documents(["return window is 30 days", "no cash refunds"])

        assert len(api.requests) == 1
        assert _request_texts(api.requests[0]) == [
            "title: none | text: return window is 30 days",
            "title: none | text: no cash refunds",
        ]

    def test_query_uses_search_result_task_prefix(self):
        api = FakeModelsApi()
        provider = _make_provider(api)

        provider.embed_query(["how do I return an item"])

        assert len(api.requests) == 1
        assert _request_texts(api.requests[0]) == [
            "task: search result | query: how do I return an item"
        ]


class TestSdkRequestContract:
    def test_model_and_output_dimensionality_are_passed_to_the_sdk(self):
        api = FakeModelsApi()
        _make_provider(api).embed_query(["anything"])

        request = api.requests[0]

        assert request["model"] == EMBEDDING_MODEL == "gemini-embedding-2"
        assert request["config"].output_dimensionality == EMBEDDING_DIMENSIONS == 768

    def test_each_input_is_an_independent_content_object(self):
        api = FakeModelsApi()
        _make_provider(api).embed_documents(["alpha", "beta"])

        contents = api.requests[0]["contents"]

        # Raw strings would make gemini-embedding-2 aggregate the whole
        # batch into a single embedding: every input must be its own
        # Content object holding exactly one text part.
        assert all(isinstance(content, types.Content) for content in contents)
        assert all(len(content.parts) == 1 for content in contents)
        assert all(
            isinstance(part.text, str)
            for content in contents
            for part in content.parts
        )
        assert not any(isinstance(content, str) for content in contents)


class TestInputValidation:
    @pytest.mark.parametrize("bad_input", [
        None,
        42,
        "a bare string would iterate characters",
        b"raw bytes",
        ["valid", 123],
        ["a", None],
        ["a", ["nested list"]],
        ["a", True],
        ["a", b"bytes"],
    ])
    def test_non_string_inputs_raise_type_error_without_api_call(self, bad_input):
        api = FakeModelsApi()
        provider = _make_provider(api)

        with pytest.raises(TypeError, match="string"):
            provider.embed_documents(bad_input)

        with pytest.raises(TypeError, match="string"):
            provider.embed_query(bad_input)

        assert api.requests == []

    @pytest.mark.parametrize("bad_texts", [
        [""],
        ["   "],
        [" \n\t\r "],
        ["valid", ""],
    ])
    def test_empty_or_whitespace_only_texts_raise_value_error(self, bad_texts):
        api = FakeModelsApi()
        provider = _make_provider(api)

        with pytest.raises(ValueError, match="empty"):
            provider.embed_documents(bad_texts)

        with pytest.raises(ValueError, match="empty"):
            provider.embed_query(bad_texts)

        assert api.requests == []


class TestProviderErrors:
    def test_api_failure_raises_embedding_error_with_original_exception_chained(self):
        original = RuntimeError("SDK exploded")
        api = FakeModelsApi(error=original)
        provider = _make_provider(api)

        with pytest.raises(EmbeddingError, match="embedding request failed"):
            provider.embed_documents(["text"])

        with pytest.raises(
            EmbeddingError, match="embedding request failed"
        ) as exc_info:
            provider.embed_query(["text"])

        assert isinstance(exc_info.value.__cause__, RuntimeError)

    def test_client_that_is_not_configured_for_requests_fails_clearly(self):
        class BrokenModels:
            models = None  # every request itself fails with AttributeError

        provider = GeminiEmbeddingProvider(BrokenModels())

        with pytest.raises(EmbeddingError):
            provider.embed_documents(["text"])


class TestResponseValidation:
    def test_empty_response_fails(self):
        api = FakeModelsApi(response_factory=lambda request: None)
        provider = _make_provider(api)

        with pytest.raises(EmbeddingError, match="response is empty"):
            provider.embed_documents(["text"])

    def test_response_without_embeddings_fails(self):
        api = FakeModelsApi(
            response_factory=lambda request: SimpleNamespace(embeddings=None)
        )
        provider = _make_provider(api)

        with pytest.raises(EmbeddingError, match="no embeddings"):
            provider.embed_documents(["text"])

    def test_non_list_embeddings_fail(self):
        api = FakeModelsApi(response_factory=lambda request: SimpleNamespace(embeddings=1))
        provider = _make_provider(api)

        with pytest.raises(EmbeddingError, match="not a list of embeddings"):
            provider.embed_documents(["text"])

    def test_missing_embeddings_fail(self):
        # One embedding for two inputs also covers the aggregated-batch
        # response guard: gemini-embedding-2 would return exactly one
        # embedding if inputs were not separate Content objects.
        api = FakeModelsApi(
            response_factory=lambda request: _make_vectors_response([[0.5] * 768])
        )
        provider = _make_provider(api)

        with pytest.raises(EmbeddingError, match="count"):
            provider.embed_documents(["one", "two"])

    def test_extra_embeddings_fail(self):
        api = FakeModelsApi(
            response_factory=lambda request: _make_vectors_response(
                [_distinct_vector(0), _distinct_vector(1)]
            )
        )
        provider = _make_provider(api)

        with pytest.raises(EmbeddingError, match="count"):
            provider.embed_documents(["only one input"])

    @pytest.mark.parametrize("bad_length", [0, 1, 767, 769, 3072])
    def test_wrong_vector_dimensions_fail(self, bad_length):
        api = FakeModelsApi(
            response_factory=lambda request: _make_vectors_response(
                [[0.5] * bad_length]
            )
        )
        provider = _make_provider(api)

        with pytest.raises(EmbeddingError, match="dimensions"):
            provider.embed_documents(["text"])

    @pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_values_fail(self, bad_value):
        vector = [0.5] * EMBEDDING_DIMENSIONS
        vector[10] = bad_value
        api = FakeModelsApi(response_factory=lambda request: _make_vectors_response([vector]))
        provider = _make_provider(api)

        with pytest.raises(EmbeddingError, match="non-finite"):
            provider.embed_documents(["text"])

    @pytest.mark.parametrize("bad_value", [True, False, "0.5", None, [0.5], complex(1, 1)])
    def test_non_numeric_values_fail_including_bools(self, bad_value):
        vector = [0.5] * EMBEDDING_DIMENSIONS
        vector[25] = bad_value
        api = FakeModelsApi(response_factory=lambda request: _make_vectors_response([vector]))
        provider = _make_provider(api)

        with pytest.raises(EmbeddingError, match="non-numeric"):
            provider.embed_documents(["text"])


def _settings_with(key, timeout_seconds=10.0):
    return SimpleNamespace(
        gemini_api_key=key,
        gemini_embedding_timeout_seconds=timeout_seconds,
        gemini_retry_max_attempts=2,
        gemini_retry_initial_delay_seconds=0.25,
        gemini_retry_window_seconds=3.0,
    )


class TestProviderTimeouts:
    """Phase 6.6: per-request SDK HTTP timeout wiring and sanitization."""

    def test_configured_timeout_is_sent_as_milliseconds(self):
        api = FakeModelsApi()
        provider = GeminiEmbeddingProvider(
            SimpleNamespace(models=api), timeout_seconds=2.5
        )

        provider.embed_query(["text"])

        http_options = api.requests[0]["config"].http_options

        assert http_options is not None
        assert http_options.timeout == 2500

    def test_no_http_options_when_timeout_is_none(self):
        api = FakeModelsApi()

        _make_provider(api).embed_query(["text"])

        assert api.requests[0]["config"].http_options is None

    def test_sub_millisecond_timeout_never_becomes_zero(self):
        api = FakeModelsApi()
        provider = GeminiEmbeddingProvider(
            SimpleNamespace(models=api), timeout_seconds=0.0001
        )

        provider.embed_query(["text"])

        assert api.requests[0]["config"].http_options.timeout == 1

    def test_timeout_failure_raises_sanitized_embedding_error(self):
        original = httpx.ReadTimeout("stalled at https://internal.example/v1")
        api = FakeModelsApi(error=original)
        provider = _make_provider(api)

        with pytest.raises(EmbeddingError, match="timed out") as raised:
            provider.embed_query(["text"])

        assert raised.value.__cause__ is original
        assert "internal.example" not in str(raised.value)

    def test_timeout_wrapped_by_the_sdk_is_still_translated(self):
        """A timeout reached through the SDK wrapper keeps its stage.

        The SDK reports a stalled call as ``APITimeoutError`` with the
        original ``httpx.ReadTimeout`` as ``__cause__``; both links must
        classify as a timeout.
        """
        compat_errors = pytest.importorskip(
            "google.genai._gaos.lib.compat_errors"
        )
        request = httpx.Request("POST", "https://example.invalid/v1")
        original = compat_errors.APITimeoutError(request)
        api = FakeModelsApi(error=original)
        provider = _make_provider(api)

        with pytest.raises(EmbeddingError, match="timed out") as raised:
            provider.embed_query(["text"])

        assert raised.value.__cause__ is original

    @pytest.mark.parametrize("bad_timeout", [0, -1, float("nan"), float("inf")])
    def test_invalid_constructor_timeout_is_rejected(self, bad_timeout):
        with pytest.raises(ValueError):
            GeminiEmbeddingProvider(
                SimpleNamespace(models=FakeModelsApi()),
                timeout_seconds=bad_timeout,
            )


class TestProviderFactory:
    def test_missing_credentials_fail_with_clear_error(self, monkeypatch):
        monkeypatch.setattr(embeddings_module, "get_settings", lambda: _settings_with(None))

        with pytest.raises(EmbeddingError, match="GEMINI_API_KEY"):
            create_gemini_provider()

    def test_empty_credentials_fail_with_clear_error(self, monkeypatch):
        monkeypatch.setattr(
            embeddings_module, "get_settings", lambda: _settings_with(SecretStr(""))
        )

        with pytest.raises(EmbeddingError, match="GEMINI_API_KEY"):
            create_gemini_provider()

    def test_configured_credentials_build_a_gemini_provider(self, monkeypatch):
        from google import genai

        created = {}

        def fake_client_factory(*, api_key):
            created["api_key"] = api_key
            return SimpleNamespace(models="fake-models")

        monkeypatch.setattr(embeddings_module, "get_settings",
                            lambda: _settings_with(SecretStr("test-key")))
        monkeypatch.setattr(genai, "Client", fake_client_factory)

        provider = create_gemini_provider()

        assert isinstance(provider, GeminiEmbeddingProvider)
        assert provider._client.models == "fake-models"
        assert created["api_key"] == "test-key"

    def test_factory_injects_the_configured_timeout(self, monkeypatch):
        from google import genai

        api = FakeModelsApi()

        def fake_client_factory(*, api_key):
            return SimpleNamespace(models=api)

        monkeypatch.setattr(
            embeddings_module,
            "get_settings",
            lambda: _settings_with(SecretStr("test-key"), timeout_seconds=4.0),
        )
        monkeypatch.setattr(genai, "Client", fake_client_factory)

        provider = create_gemini_provider()
        provider.embed_query(["anything"])

        assert api.requests[0]["config"].http_options.timeout == 4000

    def test_factory_injects_the_configured_retry_policy(self, monkeypatch):
        from google import genai

        created = {}

        class RecordingProvider(GeminiEmbeddingProvider):
            def __init__(self, client, *, retry_policy=None, **kwargs):
                created["retry_policy"] = retry_policy
                super().__init__(client, retry_policy=retry_policy, **kwargs)

        monkeypatch.setattr(
            embeddings_module,
            "GeminiEmbeddingProvider",
            RecordingProvider,
        )
        monkeypatch.setattr(
            embeddings_module,
            "get_settings",
            lambda: _settings_with(SecretStr("test-key")),
        )
        monkeypatch.setattr(
            genai,
            "Client",
            lambda *, api_key: SimpleNamespace(models=FakeModelsApi()),
        )

        create_gemini_provider()

        assert created["retry_policy"] == RetryPolicy(
            max_attempts=2,
            initial_delay_seconds=0.25,
            retry_window_seconds=3.0,
        )


# ---------------------------------------------------------------------------
# Bounded retries (Phase 6.7)
# ---------------------------------------------------------------------------


def _retrying_provider(api, **overrides) -> GeminiEmbeddingProvider:
    policy = {
        "max_attempts": 3,
        "initial_delay_seconds": 0.001,
        "retry_window_seconds": 3.0,
    }
    policy.update(overrides)

    return GeminiEmbeddingProvider(
        SimpleNamespace(models=api),
        retry_policy=RetryPolicy(**policy),
    )


def _transient_error():
    return httpx.ConnectError(
        "boom", request=httpx.Request("POST", "https://example.invalid")
    )


class TestBoundedRetries:
    def test_transient_failure_is_retried_then_succeeds(self):
        api = SequencedModelsApi([_transient_error()])
        provider = _retrying_provider(api)

        vectors = provider.embed_query(["text"])

        assert len(api.requests) == 2
        assert len(vectors) == 1

    def test_exhausted_retries_report_provider_unavailable(self):
        api = FakeModelsApi(error=_transient_error())
        provider = _retrying_provider(api, max_attempts=2)

        with pytest.raises(EmbeddingError) as raised:
            provider.embed_query(["text"])

        assert raised.value.stage == "provider_unavailable"
        assert len(api.requests) == 2
        assert isinstance(raised.value.__cause__, httpx.ConnectError)

    def test_timeout_is_never_retried(self):
        timeout = httpx.ReadTimeout(
            "slow", request=httpx.Request("POST", "https://example.invalid")
        )
        api = SequencedModelsApi([timeout])
        provider = _retrying_provider(api)

        with pytest.raises(EmbeddingError) as raised:
            provider.embed_query(["text"])

        assert raised.value.stage == "embedding_timeout"
        assert len(api.requests) == 1

    def test_rate_limited_failure_reports_stage_and_hint(self):
        response = httpx.Response(
            429,
            headers={"Retry-After": "5"},
            request=httpx.Request("POST", "https://example.invalid"),
        )
        api = FakeModelsApi(
            error=legacy_errors.APIError(429, {"error": "rate"}, response)
        )
        provider = _retrying_provider(api, max_attempts=1)

        with pytest.raises(EmbeddingError) as raised:
            provider.embed_query(["text"])

        assert raised.value.stage == "rate_limited"
        assert raised.value.retry_after_seconds == 5.0
        assert len(api.requests) == 1

    def test_fatal_failure_keeps_the_request_stage(self):
        api = FakeModelsApi(
            error=legacy_errors.APIError(401, {"error": "denied"})
        )
        provider = _retrying_provider(api)

        with pytest.raises(EmbeddingError) as raised:
            provider.embed_query(["text"])

        assert raised.value.stage == "provider_request"
        assert len(api.requests) == 1

    def test_cancelled_before_the_first_attempt_makes_no_call(self):
        cancel_event = threading.Event()
        cancel_event.set()
        api = FakeModelsApi()
        provider = _retrying_provider(api)

        with pytest.raises(ProviderCallAborted):
            provider.embed_query(["text"], cancel_event=cancel_event)

        assert api.requests == []

    def test_cancellation_during_backoff_propagates_unwrapped(self):
        cancel_event = threading.Event()
        api = FakeModelsApi(error=_transient_error())
        original = api.embed_content

        def cancelling(**kwargs):
            cancel_event.set()
            return original(**kwargs)

        api.embed_content = cancelling
        provider = _retrying_provider(api, initial_delay_seconds=0.3)

        with pytest.raises(ProviderCallAborted):
            provider.embed_query(["text"], cancel_event=cancel_event)

        assert len(api.requests) == 1

    def test_default_direct_provider_makes_exactly_one_attempt(self):
        api = FakeModelsApi(error=_transient_error())
        provider = _make_provider(api)

        with pytest.raises(EmbeddingError):
            provider.embed_query(["text"])

        assert len(api.requests) == 1


# ---------------------------------------------------------------------------
# Phase 6.8 retry-metrics operation labels
# ---------------------------------------------------------------------------


@pytest.fixture
def retry_metrics(monkeypatch):
    """A fresh ProviderMetrics registry wired into the retry module.

    The module-level singleton is never touched, so other tests (and
    the production call sites) remain unaffected.
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


class TestRetryOperationLabels:
    """The two embedding paths record distinct, approved operations."""

    def test_embed_query_records_the_query_embedding_operation(
        self, retry_metrics
    ):
        api = SequencedModelsApi([_transient_error()])
        provider = _retrying_provider(api)

        provider.embed_query(["text"])

        assert _operation_count(
            retry_metrics,
            "provider_retry_calls_total",
            operation="query_embedding",
        ) == 1
        assert _operation_count(
            retry_metrics,
            "provider_retry_attempts_total",
            operation="query_embedding",
            kind="transient",
        ) == 1
        assert _operation_count(
            retry_metrics,
            "provider_retry_success_total",
            operation="query_embedding",
            attempts="2",
        ) == 1

    def test_embed_documents_records_the_ingestion_embedding_operation(
        self, retry_metrics
    ):
        api = SequencedModelsApi([_transient_error()])
        provider = _retrying_provider(api)

        provider.embed_documents(["text"])

        assert _operation_count(
            retry_metrics,
            "provider_retry_calls_total",
            operation="ingestion_embedding",
        ) == 1
        assert _operation_count(
            retry_metrics,
            "provider_retry_attempts_total",
            operation="ingestion_embedding",
            kind="transient",
        ) == 1
        assert _operation_count(
            retry_metrics,
            "provider_retry_success_total",
            operation="ingestion_embedding",
            attempts="2",
        ) == 1
        # The sibling operation is never recorded by this path.
        assert _operation_count(
            retry_metrics,
            "provider_retry_calls_total",
            operation="query_embedding",
        ) is None
