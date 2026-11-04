"""Unit tests for the Phase 6.3 generation adapter and citation validation.

These tests do not require the local Supabase database, Storage,
FastAPI requests, Gemini credentials, or network access: every
generation request goes through a fake Interactions API that records
requests and replays canned responses. No live Gemini call is made.
"""

import asyncio
import threading
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from app.core.provider_retry import ProviderCallAborted, RetryPolicy
from app.services import generation as generation_module
from app.services.generation import (
    Citation,
    GenerationError,
    GeminiGenerationProvider,
    RagAnswer,
    create_gemini_generation_provider,
    generate_rag_answer,
    validate_citations,
)
from app.services.rag_prompt import (
    INSUFFICIENT_EVIDENCE_MESSAGE,
    SYSTEM_INSTRUCTION,
    SourceReference,
    build_rag_prompt,
)


# Sentinel distinguishing "timeout not passed" from an explicit None in
# the fake interactions API, so tests can assert the Phase 6.6 rule that
# the keyword is omitted entirely when no timeout is configured.
_UNSET = object()


_PROMPT_QUERY = "How do I return an order?"

_PROMPT_CHUNKS = [
    {
        "id": "chunk-1",
        "document_id": "doc-1",
        "title": "Return Policy",
        "visibility": "public",
        "chunk_index": 0,
        "content": "Returns are accepted within 30 days.",
        "distance": 0.05,
    },
    {
        "id": "chunk-2",
        "document_id": "doc-2",
        "title": "Shipping Handbook",
        "visibility": "internal",
        "chunk_index": 2,
        "content": "Standard shipping takes 3-5 business days.",
        "distance": 0.12,
    },
]


def _prompt() -> "object":
    return build_rag_prompt(_PROMPT_QUERY, _PROMPT_CHUNKS)


def _completion(output_text: str, status: str = "completed") -> SimpleNamespace:
    """A response shaped like the SDK Interaction object."""
    return SimpleNamespace(status=status, output_text=output_text, steps=[])


def _default_response_factory(request):
    return _completion("Returns are accepted within 30 days. [1]")


class FakeInteractionsApi:
    """Records interactions.create requests and replays canned behavior."""

    def __init__(self, *, response_factory=_default_response_factory, error=None):
        self.requests = []
        self.response_factory = response_factory
        self.error = error

    def create(self, *, model, input, system_instruction, store, timeout=_UNSET):
        request = {
            "model": model,
            "input": input,
            "system_instruction": system_instruction,
            "store": store,
        }

        if timeout is not _UNSET:
            request["timeout"] = timeout

        self.requests.append(request)

        if self.error is not None:
            raise self.error

        return self.response_factory(request)


def _make_provider(api, *, model="test-generation-model"):
    return GeminiGenerationProvider(
        SimpleNamespace(interactions=api), model=model
    )


def _input_texts(request) -> list[str]:
    return [content.text for content in request["input"]]


# ---------------------------------------------------------------------------
# Provider basics
# ---------------------------------------------------------------------------


class TestProviderContract:
    def test_successful_generation_returns_output_text(self):
        api = FakeInteractionsApi(
            response_factory=lambda request: _completion("The answer text. [1]")
        )
        provider = _make_provider(api)

        assert provider.generate(_prompt()) == "The answer text. [1]"

    def test_non_ragprompt_inputs_fail_with_type_error(self):
        provider = _make_provider(FakeInteractionsApi())

        with pytest.raises(TypeError):
            provider.generate({"question": "not a RagPrompt"})

    def test_prompt_without_sources_fails_before_any_api_call(self):
        empty_prompt = build_rag_prompt(_PROMPT_QUERY, [])
        api = FakeInteractionsApi()
        provider = _make_provider(api)

        with pytest.raises(GenerationError, match="no retrieved sources"):
            provider.generate(empty_prompt)

        assert api.requests == []


# ---------------------------------------------------------------------------
# Request shape
# ---------------------------------------------------------------------------


class TestRequestShape:
    def test_request_passes_configured_model(self):
        api = FakeInteractionsApi()
        _make_provider(api, model="configured-model").generate(_prompt())

        assert api.requests[0]["model"] == "configured-model"

    def test_request_passes_system_instruction_from_rag_prompt(self):
        api = FakeInteractionsApi()
        prompt = _prompt()
        _make_provider(api).generate(prompt)

        request = api.requests[0]

        assert request["system_instruction"] == prompt.system_instruction
        assert request["system_instruction"] == SYSTEM_INSTRUCTION

    def test_request_passes_question_and_sources_as_distinct_inputs(self):
        api = FakeInteractionsApi()
        prompt = _prompt()
        _make_provider(api).generate(prompt)

        texts = _input_texts(api.requests[0])

        # question and sources arrive as separate, order-stable inputs;
        # neither is embedded inside the other.
        assert texts == [prompt.question, prompt.sources_text]
        assert prompt.question in texts
        assert prompt.sources_text in texts

    def test_request_never_stores_state(self):
        api = FakeInteractionsApi()
        _make_provider(api).generate(_prompt())

        assert api.requests[0]["store"] is False


# ---------------------------------------------------------------------------
# Untrusted response handling
# ---------------------------------------------------------------------------


class TestUntrustedResponse:
    def test_none_response_fails(self):
        api = FakeInteractionsApi(response_factory=lambda request: None)
        provider = _make_provider(api)

        with pytest.raises(GenerationError, match="response is empty"):
            provider.generate(_prompt())

    @pytest.mark.parametrize(
        "status",
        [
            "failed",
            "cancelled",
            "incomplete",
            "budget_exceeded",
            "requires_action",
            "in_progress",
            "queued",
        ],
    )
    def test_non_completed_status_fails(self, status):
        api = FakeInteractionsApi(
            response_factory=lambda request: _completion("text", status=status)
        )
        provider = _make_provider(api)

        with pytest.raises(
            GenerationError, match="did not complete successfully"
        ):
            provider.generate(_prompt())

    def test_missing_output_text_fails(self):
        api = FakeInteractionsApi(
            response_factory=lambda request: SimpleNamespace(
                status="completed", output_text=None, steps=[]
            )
        )
        provider = _make_provider(api)

        with pytest.raises(GenerationError, match="no usable output text"):
            provider.generate(_prompt())

    @pytest.mark.parametrize("empty_text", ["", "   \n\t "])
    def test_empty_or_whitespace_output_text_fails(self, empty_text):
        api = FakeInteractionsApi(
            response_factory=lambda request: _completion(empty_text)
        )
        provider = _make_provider(api)

        with pytest.raises(GenerationError, match="no usable output text"):
            provider.generate(_prompt())

    def test_non_string_output_text_fails(self):
        api = FakeInteractionsApi(
            response_factory=lambda request: SimpleNamespace(
                status="completed", output_text=12345, steps=[]
            )
        )
        provider = _make_provider(api)

        with pytest.raises(GenerationError, match="no usable output text"):
            provider.generate(_prompt())

    def test_provider_exceptions_are_chained_into_generation_error(self):
        original = RuntimeError("simulated provider outage")
        api = FakeInteractionsApi(error=original)
        provider = _make_provider(api)

        with pytest.raises(GenerationError, match="could not be generated") as cruciated:
            provider.generate(_prompt())

        assert cruciated.value.__cause__ is original

    def test_error_message_never_carries_provider_exception_text(self):
        original = RuntimeError("secret internal endpoint details")
        api = FakeInteractionsApi(error=original)
        provider = _make_provider(api)

        with pytest.raises(GenerationError) as raised:
            provider.generate(_prompt())

        assert "secret internal endpoint details" not in str(raised.value)

    def test_provider_never_exposes_the_api_key(self):
        api = FakeInteractionsApi()
        provider = _make_provider(api)
        provider.generate(_prompt())

        serialized = str(api.requests[0])

        assert "api_key" not in serialized


# ---------------------------------------------------------------------------
# Provider timeouts (Phase 6.6)
# ---------------------------------------------------------------------------


class TestProviderTimeouts:
    def test_configured_timeout_is_passed_in_seconds(self):
        api = FakeInteractionsApi()
        provider = GeminiGenerationProvider(
            SimpleNamespace(interactions=api),
            model="test-generation-model",
            timeout_seconds=15.0,
        )

        provider.generate(_prompt())

        assert api.requests[0]["timeout"] == 15.0

    def test_timeout_keyword_is_omitted_when_unset(self):
        api = FakeInteractionsApi()

        _make_provider(api).generate(_prompt())

        assert "timeout" not in api.requests[0]

    def test_timeout_failure_raises_generation_error_with_timeout_stage(self):
        original = httpx.ReadTimeout("stalled at https://internal.example/v1")
        api = FakeInteractionsApi(error=original)
        provider = _make_provider(api)

        with pytest.raises(GenerationError) as raised:
            provider.generate(_prompt())

        assert raised.value.stage == "generation_timeout"
        assert raised.value.__cause__ is original
        assert "internal.example" not in str(raised.value)

    def test_real_sdk_timeout_wrapper_maps_to_timeout_stage(self):
        """The SDK's own client-timeout class is translated, not generic.

        The SDK raises ``APITimeoutError`` (not a ``TimeoutError`` and
        not an ``httpx`` class) around the underlying
        ``httpx.ReadTimeout``; this is what a stalled production call
        actually produces.
        """
        compat_errors = pytest.importorskip(
            "google.genai._gaos.lib.compat_errors"
        )
        request = httpx.Request("POST", "https://example.invalid/v1")
        original = compat_errors.APITimeoutError(request)
        api = FakeInteractionsApi(error=original)
        provider = _make_provider(api)

        with pytest.raises(GenerationError) as raised:
            provider.generate(_prompt())

        assert raised.value.stage == "generation_timeout"
        assert raised.value.__cause__ is original

    def test_non_timeout_provider_error_keeps_request_stage(self):
        api = FakeInteractionsApi(error=RuntimeError("simulated outage"))
        provider = _make_provider(api)

        with pytest.raises(GenerationError) as raised:
            provider.generate(_prompt())

        assert raised.value.stage == "generation_request"

    @pytest.mark.parametrize("bad_timeout", [0, -1, float("nan"), float("inf")])
    def test_invalid_constructor_timeout_is_rejected(self, bad_timeout):
        with pytest.raises(ValueError):
            GeminiGenerationProvider(
                SimpleNamespace(interactions=FakeInteractionsApi()),
                model="test-generation-model",
                timeout_seconds=bad_timeout,
            )


# ---------------------------------------------------------------------------
# Citation validation
# ---------------------------------------------------------------------------


class TestCitationValidation:
    def test_valid_references_map_to_correct_source_references(self):
        prompt = _prompt()

        citations = validate_citations("Details [2] and [1].", prompt.sources)

        assert [(c.number, c.chunk_id, c.document_id) for c in citations] == [
            (2, "chunk-2", "doc-2"),
            (1, "chunk-1", "doc-1"),
        ]

    def test_citation_fields_are_copied_from_source_reference(self):
        prompt = _prompt()
        reference = prompt.sources[0]

        (citation,) = validate_citations("[1]", prompt.sources)

        assert isinstance(citation, Citation)
        assert citation.number == reference.number
        assert citation.chunk_id == reference.chunk_id
        assert citation.document_id == reference.document_id
        assert citation.document_title == reference.document_title
        assert citation.chunk_index == reference.chunk_index

    def test_repeated_citations_deduplicate_in_first_appearance_order(self):
        prompt = _prompt()

        citations = validate_citations("[2] then [1] then [2].", prompt.sources)

        assert [c.number for c in citations] == [2, 1]

    @pytest.mark.parametrize(
        "answer_text",
        [
            "[7]",
            "[0]",
            "[99]",
            "A sentence with no reference markers.",
            "A sentence mentioning reference [7] inside prose.",
        ],
    )
    def test_invalid_or_unknown_references_are_excluded(self, answer_text):
        prompt = build_rag_prompt(_PROMPT_QUERY, _PROMPT_CHUNKS)

        citations = validate_citations(answer_text, prompt.sources)

        assert citations == ()

    def test_mixed_valid_and_invalid_references_keep_only_valid_ones(self):
        prompt = _prompt()

        citations = validate_citations(
            "Claim [3] and [1] and [42].", prompt.sources
        )

        assert [c.number for c in citations] == [1]

    def test_unknown_reference_never_fabricates_a_citation_object(self):
        only_one_chunk = build_rag_prompt(_PROMPT_QUERY, _PROMPT_CHUNKS[:1])

        citations = validate_citations(
            "Answering [1] but the model also claims [2].", only_one_chunk.sources
        )

        assert [c.number for c in citations] == [1]
        assert all(
            c.chunk_id == "chunk-1" for c in citations
        )

    def test_empty_sources_produce_no_citations(self):
        citations = validate_citations("[1] [2]", ())

        assert citations == ()

    def test_non_string_answer_fails_with_type_error(self):
        prompt = _prompt()

        with pytest.raises(TypeError):
            validate_citations(None, prompt.sources)

    def test_known_limitation_citations_prove_syntax_not_grounding(self):
        # Deliberate documentation-style test: a well-formed citation
        # list says nothing about factual support; Phase 14.7/15 own
        # deeper checks. A text full of citations validates cleanly.
        prompt = _prompt()

        citations = validate_citations(
            "[1] [2] unsupported claim [1] [2].", prompt.sources
        )

        assert [c.number for c in citations] == [1, 2]


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class StubGenerationProvider:
    """Records generate requests and returns a canned answer."""

    def __init__(self, answer="Generated answer. [1]"):
        self.requests = []
        self._answer = answer

    def generate(self, prompt, *, cancel_event=None):
        self.requests.append(prompt)
        return self._answer


class StubEmbeddingProvider:
    def __init__(self, vectors=None):
        self.requests = []
        self._vectors = (
            vectors
            if vectors is not None
            else [[0.1 * i for i in range(768)]]
        )

    def embed_query(self, texts, *, cancel_event=None):
        self.requests.append(list(texts))
        return list(self._vectors)

    def embed_documents(self, texts, *, cancel_event=None):
        raise AssertionError("unit test must not embed documents")


class RecordingRetrievalPool:
    """AsyncConnectionPool stand-in whose retrieve() result is patched."""

    def __init__(self):
        self.connections_used = 0

    def connection(self):
        self.connections_used += 1

        raise AssertionError(
            "orchestrator unit tests patch retrieval.retrieve; the pool "
            "must not open a real connection path"
        )


class TestGenerateRagAnswer:
    def test_full_generation_returns_answer_and_validated_citations(
        self, monkeypatch
    ):
        chunks = build_rag_prompt(_PROMPT_QUERY, _PROMPT_CHUNKS).sources
        chunk_dicts = _PROMPT_CHUNKS

        async def fake_retrieve(
            pool, provider, *, query, user_role, limiters=None, deadline_at=None
        ):
            assert user_role == "customer"
            return chunk_dicts

        monkeypatch.setattr(
            generation_module.retrieval_service, "retrieve", fake_retrieve
        )
        generator = StubGenerationProvider("Returns take 30 days. [2]")

        result = asyncio.run(
            generate_rag_answer(
                RecordingRetrievalPool(),
                StubEmbeddingProvider(),
                generator,
                query=_PROMPT_QUERY,
                user_role="customer",
            )
        )

        assert isinstance(result, RagAnswer)
        assert result.answer == "Returns take 30 days. [2]"
        assert [c.number for c in result.citations] == [2]

        # The generation received the prompt built from those chunks.
        assert len(generator.requests) == 1
        assert generator.requests[0].question == _PROMPT_QUERY
        assert len(generator.requests[0].sources) == len(chunk_dicts)

    def test_empty_retrieval_returns_exact_message_without_generation_call(
        self, monkeypatch
    ):
        async def fake_retrieve(
            pool, provider, *, query, user_role, limiters=None, deadline_at=None
        ):
            return []

        monkeypatch.setattr(
            generation_module.retrieval_service, "retrieve", fake_retrieve
        )
        generator = StubGenerationProvider()

        result = asyncio.run(
            generate_rag_answer(
                RecordingRetrievalPool(),
                StubEmbeddingProvider(),
                generator,
                query=_PROMPT_QUERY,
                user_role="customer",
            )
        )

        assert result.answer == INSUFFICIENT_EVIDENCE_MESSAGE

        assert result.citations == ()
        assert generator.requests == []

    def test_empty_retrieval_message_wording_is_exact(self, monkeypatch):
        async def fake_retrieve(
            pool, provider, *, query, user_role, limiters=None, deadline_at=None
        ):
            return []

        monkeypatch.setattr(
            generation_module.retrieval_service, "retrieve", fake_retrieve
        )

        result = asyncio.run(
            generate_rag_answer(
                RecordingRetrievalPool(),
                StubEmbeddingProvider(),
                StubGenerationProvider(),
                query=_PROMPT_QUERY,
                user_role="customer",
            )
        )

        assert result.answer == "I don't have enough information."

    def test_retrieval_errors_propagate_unchanged(self, monkeypatch):
        async def failing_retrieve(
            pool, provider, *, query, user_role, limiters=None, deadline_at=None
        ):
            raise GenerationError(
                "The permitted chunks could not be retrieved",
                stage="database_query",
            )

        monkeypatch.setattr(
            generation_module.retrieval_service, "retrieve", failing_retrieve
        )
        generator = StubGenerationProvider()

        with pytest.raises(GenerationError):
            asyncio.run(
                generate_rag_answer(
                    RecordingRetrievalPool(),
                    StubEmbeddingProvider(),
                    generator,
                    query=_PROMPT_QUERY,
                    user_role="customer",
                )
            )

        assert generator.requests == []

    def test_generation_errors_propagate_unchanged(self, monkeypatch):
        async def fake_retrieve(
            pool, provider, *, query, user_role, limiters=None, deadline_at=None
        ):
            return _PROMPT_CHUNKS

        monkeypatch.setattr(
            generation_module.retrieval_service, "retrieve", fake_retrieve
        )

        class FailingProvider:
            def generate(self, prompt, *, cancel_event=None):
                raise GenerationError(
                    "The RAG answer could not be generated",
                    stage="generation_request",
                )

        with pytest.raises(GenerationError):
            asyncio.run(
                generate_rag_answer(
                    RecordingRetrievalPool(),
                    StubEmbeddingProvider(),
                    FailingProvider(),
                    query=_PROMPT_QUERY,
                    user_role="customer",
                )
            )


# ---------------------------------------------------------------------------
# Factory / configuration
# ---------------------------------------------------------------------------


def _settings_with(key, model="gemini-3.8-flash", timeout_seconds=30.0):
    return SimpleNamespace(
        gemini_api_key=key,
        gemini_generation_model=model,
        gemini_generation_timeout_seconds=timeout_seconds,
        gemini_retry_max_attempts=2,
        gemini_retry_initial_delay_seconds=0.25,
        gemini_retry_window_seconds=3.0,
    )


class TestProviderFactory:
    def test_missing_credentials_fail_with_clear_error(self, monkeypatch):
        monkeypatch.setattr(
            generation_module, "get_settings", lambda: _settings_with(None)
        )

        with pytest.raises(GenerationError, match="GEMINI_API_KEY"):
            create_gemini_generation_provider()

    def test_empty_credentials_fail_with_clear_error(self, monkeypatch):
        monkeypatch.setattr(
            generation_module,
            "get_settings",
            lambda: _settings_with(SecretStr("")),
        )

        with pytest.raises(GenerationError, match="GEMINI_API_KEY"):
            create_gemini_generation_provider()

    def test_configured_credentials_build_a_gemini_provider(self, monkeypatch):
        from google import genai

        created = {}

        def fake_client_factory(*, api_key):
            created["api_key"] = api_key
            return SimpleNamespace(interactions="fake-interactions")

        monkeypatch.setattr(
            generation_module,
            "get_settings",
            lambda: _settings_with(SecretStr("test-key"), model="fake-model"),
        )
        monkeypatch.setattr(genai, "Client", fake_client_factory)

        provider = create_gemini_generation_provider()

        assert isinstance(provider, GeminiGenerationProvider)
        assert provider._client.interactions == "fake-interactions"
        assert created["api_key"] == "test-key"
        assert provider._model == "fake-model"

    def test_missing_generation_model_fails(self, monkeypatch):
        monkeypatch.setattr(
            generation_module,
            "get_settings",
            lambda: _settings_with(SecretStr("test-key"), model=None),
        )

        with pytest.raises(GenerationError, match="generation model"):
            create_gemini_generation_provider()

    def test_configured_model_reaches_the_interactions_request(self, monkeypatch):
        from google import genai

        api = FakeInteractionsApi()

        monkeypatch.setattr(
            generation_module,
            "get_settings",
            lambda: _settings_with(
                SecretStr("test-key"), model="gemini-configured-model"
            ),
        )
        monkeypatch.setattr(
            genai,
            "Client",
            lambda *, api_key: SimpleNamespace(interactions=api),
        )

        provider = create_gemini_generation_provider()
        provider.generate(_prompt())

        assert provider._model == "gemini-configured-model"
        assert api.requests[0]["model"] == "gemini-configured-model"

    def test_default_settings_model_reaches_the_interactions_request(
        self, monkeypatch
    ):
        from google import genai

        from app.core.config import Settings

        # The Python default must survive the whole path to the request,
        # so ignore any local override in the environment or .env file.
        monkeypatch.delenv("GEMINI_GENERATION_MODEL", raising=False)
        api = FakeInteractionsApi()
        settings = Settings(
            _env_file=None,
            database_url="postgresql://postgres:postgres@127.0.0.1:54322/postgres",
            supabase_url="http://127.0.0.1:54321",
            supabase_service_role_key="test-key",
            gemini_api_key="test-key",
        )

        monkeypatch.setattr(
            generation_module, "get_settings", lambda: settings
        )
        monkeypatch.setattr(
            genai,
            "Client",
            lambda *, api_key: SimpleNamespace(interactions=api),
        )

        provider = create_gemini_generation_provider()
        provider.generate(_prompt())

        assert api.requests[0]["model"] == "gemini-3.5-flash-lite"

    def test_factory_injects_the_configured_timeout(self, monkeypatch):
        from google import genai

        api = FakeInteractionsApi()

        def fake_client_factory(*, api_key):
            return SimpleNamespace(interactions=api)

        monkeypatch.setattr(
            generation_module,
            "get_settings",
            lambda: _settings_with(
                SecretStr("test-key"),
                model="fake-model",
                timeout_seconds=7.5,
            ),
        )
        monkeypatch.setattr(genai, "Client", fake_client_factory)

        provider = create_gemini_generation_provider()
        provider.generate(_prompt())

        assert api.requests[0]["timeout"] == 7.5


# ---------------------------------------------------------------------------
# Prompt integration
# ---------------------------------------------------------------------------


class TestRagPromptIntegration:
    def test_generator_does_not_rebuild_or_renumber_prompt_sources(self):
        prompt = _prompt()
        api = FakeInteractionsApi()
        provider = _make_provider(api)

        provider.generate(prompt)

        texts = _input_texts(api.requests[0])
        sources_text = texts[1]

        # Builder-assigned reference numbers are part of the sources
        # exactly as built; the adapter never renumbers.
        assert "[1]" in sources_text
        assert "[2]" in sources_text

    def test_source_references_are_plain_values_for_citation_mapping(self):
        prompt = _prompt()

        for position, source in enumerate(prompt.sources, start=1):
            assert isinstance(source, SourceReference)
            assert source.number == position
            assert source.citation == f"[{position}]"


# ---------------------------------------------------------------------------
# Bounded retries (Phase 6.7)
# ---------------------------------------------------------------------------


class SequencedInteractionsApi(FakeInteractionsApi):
    """Fails with the given exceptions in order, then succeeds."""

    def __init__(self, errors):
        super().__init__()
        self.pending_errors = list(errors)

    def create(
        self, *, model, input, system_instruction, store, timeout=_UNSET
    ):
        if self.pending_errors:
            error = self.pending_errors.pop(0)
            request = {
                "model": model,
                "input": input,
                "system_instruction": system_instruction,
                "store": store,
            }

            if timeout is not _UNSET:
                request["timeout"] = timeout

            self.requests.append(request)
            raise error

        return super().create(
            model=model,
            input=input,
            system_instruction=system_instruction,
            store=store,
            timeout=timeout,
        )


def _retrying_generation_provider(api, **overrides):
    policy = {
        "max_attempts": 3,
        "initial_delay_seconds": 0.001,
        "retry_window_seconds": 3.0,
    }
    policy.update(overrides)

    return GeminiGenerationProvider(
        SimpleNamespace(interactions=api),
        model="test-generation-model",
        retry_policy=RetryPolicy(**policy),
    )


def _transient_generation_error():
    return httpx.ConnectError(
        "boom", request=httpx.Request("POST", "https://example.invalid")
    )


class TestBoundedRetries:
    def test_transient_failure_is_retried_then_succeeds(self):
        api = SequencedInteractionsApi([_transient_generation_error()])
        provider = _retrying_generation_provider(api)

        answer = provider.generate(_prompt())

        assert len(api.requests) == 2
        assert answer

    def test_exhausted_retries_keep_the_generation_request_stage(self):
        api = FakeInteractionsApi(error=_transient_generation_error())
        provider = _retrying_generation_provider(api, max_attempts=2)

        with pytest.raises(GenerationError) as raised:
            provider.generate(_prompt())

        assert raised.value.stage == "generation_request"
        assert len(api.requests) == 2

    def test_timeout_is_never_retried(self):
        compat_errors = pytest.importorskip(
            "google.genai._gaos.lib.compat_errors"
        )
        timeout = compat_errors.APITimeoutError(
            httpx.Request("POST", "https://example.invalid")
        )
        api = SequencedInteractionsApi([timeout])
        provider = _retrying_generation_provider(api)

        with pytest.raises(GenerationError) as raised:
            provider.generate(_prompt())

        assert raised.value.stage == "generation_timeout"
        assert len(api.requests) == 1

    def test_rate_limited_failure_reports_stage_and_hint(self):
        compat_errors = pytest.importorskip(
            "google.genai._gaos.lib.compat_errors"
        )
        response = httpx.Response(
            429,
            headers={"Retry-After": "9"},
            request=httpx.Request("POST", "https://example.invalid"),
        )
        api = FakeInteractionsApi(
            error=compat_errors.RateLimitError(
                "simulated", response=response, body=None
            )
        )
        provider = _retrying_generation_provider(api, max_attempts=1)

        with pytest.raises(GenerationError) as raised:
            provider.generate(_prompt())

        assert raised.value.stage == "rate_limited"
        assert raised.value.retry_after_seconds == 9.0
        assert len(api.requests) == 1

    def test_cancelled_before_the_first_attempt_makes_no_call(self):
        cancel_event = threading.Event()
        cancel_event.set()
        api = FakeInteractionsApi()
        provider = _retrying_generation_provider(api)

        with pytest.raises(ProviderCallAborted):
            provider.generate(_prompt(), cancel_event=cancel_event)

        assert api.requests == []

    def test_cancellation_during_backoff_propagates_unwrapped(self):
        cancel_event = threading.Event()
        api = FakeInteractionsApi(error=_transient_generation_error())
        original = api.create

        def cancelling(**kwargs):
            cancel_event.set()
            return original(**kwargs)

        api.create = cancelling
        provider = _retrying_generation_provider(
            api, initial_delay_seconds=0.3
        )

        with pytest.raises(ProviderCallAborted):
            provider.generate(_prompt(), cancel_event=cancel_event)

        assert len(api.requests) == 1

    def test_default_direct_provider_makes_exactly_one_attempt(self):
        api = FakeInteractionsApi(error=_transient_generation_error())
        provider = _make_provider(api)

        with pytest.raises(GenerationError):
            provider.generate(_prompt())

        assert len(api.requests) == 1

    def test_factory_injects_the_configured_retry_policy(self, monkeypatch):
        from google import genai

        created = {}

        class RecordingProvider(GeminiGenerationProvider):
            def __init__(self, client, *, retry_policy=None, **kwargs):
                created["retry_policy"] = retry_policy
                super().__init__(client, retry_policy=retry_policy, **kwargs)

        monkeypatch.setattr(
            generation_module, "GeminiGenerationProvider", RecordingProvider
        )
        monkeypatch.setattr(
            generation_module,
            "get_settings",
            lambda: _settings_with(SecretStr("test-key")),
        )
        monkeypatch.setattr(
            genai,
            "Client",
            lambda *, api_key: SimpleNamespace(
                interactions=FakeInteractionsApi()
            ),
        )

        create_gemini_generation_provider()

        assert created["retry_policy"] == RetryPolicy(
            max_attempts=2,
            initial_delay_seconds=0.25,
            retry_window_seconds=3.0,
        )
