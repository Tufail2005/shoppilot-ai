"""Unit tests for the Phase 6.3 generation adapter and citation validation.

These tests do not require the local Supabase database, Storage,
FastAPI requests, Gemini credentials, or network access: every
generation request goes through a fake Interactions API that records
requests and replays canned responses. No live Gemini call is made.
"""

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

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

    def create(self, *, model, input, system_instruction, store):
        request = {
            "model": model,
            "input": input,
            "system_instruction": system_instruction,
            "store": store,
        }
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

    def generate(self, prompt):
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

    def embed_query(self, texts):
        self.requests.append(list(texts))
        return list(self._vectors)

    def embed_documents(self, texts):
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

        async def fake_retrieve(pool, provider, *, query, user_role):
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
        async def fake_retrieve(pool, provider, *, query, user_role):
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
        async def fake_retrieve(pool, provider, *, query, user_role):
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
        async def failing_retrieve(pool, provider, *, query, user_role):
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
        async def fake_retrieve(pool, provider, *, query, user_role):
            return _PROMPT_CHUNKS

        monkeypatch.setattr(
            generation_module.retrieval_service, "retrieve", fake_retrieve
        )

        class FailingProvider:
            def generate(self, prompt):
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


def _settings_with(key, model="gemini-3.8-flash"):
    return SimpleNamespace(gemini_api_key=key, gemini_generation_model=model)


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
