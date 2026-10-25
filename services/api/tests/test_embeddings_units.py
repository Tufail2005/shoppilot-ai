"""Unit tests for the Phase 5.5 embedding provider.

These tests do not require the local Supabase database, Storage,
FastAPI requests, Gemini credentials, or network access: they exercise
app.services.embeddings against a fake client that records requests and
replays canned responses.
"""

import math
from types import SimpleNamespace

import pytest
from google.genai import types
from pydantic import SecretStr

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


def _settings_with(key):
    return SimpleNamespace(gemini_api_key=key)


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
