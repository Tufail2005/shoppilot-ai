"""Answer generation and citation validation for the Phase 6.3 RAG service.

Phase 6.3 of the roadmap: turn an existing Phase 6.2 :class:`~app.services.rag_prompt.RagPrompt`
into the grounded answer plus the numbered citation list, using the same
Google GenAI SDK that the Phase 5.5 embedding provider already uses.

Deliberately out of scope for Phase 6.3 (owned by later phases):

- ``POST /rag/query`` and any HTTP router (Phase 6.4),
- conversation history, memory, or any persistence (Phases 7/9),
- SSE streaming (Phase 7.5), LangGraph, and agent tools (Phases 9/10),
- prompt-injection filtering and comprehensive output guardrails
  (Phase 14), including strict citation-text enforcement — see the
  "Invalid citation behavior" section below,
- similarity thresholds and relevance calibration (Phase 15).

Stateless generation
--------------------

Every generation call passes ``store=False`` to the Interactions API
(re-verified against the installed ``google-genai`` SDK, version >=2.33),
so the conversation state never lives server-side at the provider: no
Phase 6.3 request depends on, creates, or implies a stored conversation.
A Phase 6.3 answer is produced solely from the one built prompt. A
conversation/memory layer is deliberately NOT added here (Phase 7/9).

Response contract (Interactions API, not ``generate_content``)
--------------------------------------------------------------

The adapter calls ``client.interactions.create(...)`` and treats the
whole response as untrusted, mirroring the embedding provider's
validation posture:

- the interaction must have status ``"completed"``; the
  ``failed | cancelled | incomplete | budget_exceeded | requires_action
  | in_progress | queued`` cases all fail the generation, and
- the aggregated ``output_text`` convenience value must be a non-empty,
  non-whitespace string.

A missing, non-completed, or empty response raises
:class:`GenerationError`. Nothing is returned masquerading as success:
an empty or blocked model response never becomes an empty or fabricated
answer.

Invalid citation behavior (explicitly chosen safe behavior)
----------------------------------------------------------

``validate_citations`` recognizes only the Phase 6.2 numbered citation
syntax (``[1]``, ``[2]``, ...) and maps each number exclusively to the
sources present in the prompt's ``sources`` tuple. A reference the
model invents — out of range (``[7]`` when five sources exist), zero
(``[0]``), or beyond the retrieved set — is EXCLUDED from the returned
citation list: an unknown reference can never produce a citation object
for a chunk that was not retrieved. The answer text itself is returned
unchanged in Phase 6.3: this step validates citations structurally
(they point at real retrieved chunks) but does NOT police or rewrite
the prose. KNOWN LIMITATION for Phase 14.7: a model may emit prose or
invalid citations that slide past this structural check; Phase 14.7
owns broader output validation.

Also note the explicit distinction recorded here: a citation list built
from ``[n]`` markers proves only that the model USED the numbering
syntax, never that a cited claim is factually supported by the cited
chunk. Grounding quality is Phase 15 evaluation territory.

Empty retrieval
---------------

The :func:`generate_rag_answer` orchestrator short-circuits BEFORE any
generation call when retrieval returns no chunks: the answer is exactly
``INSUFFICIENT_EVIDENCE_MESSAGE`` and the citation list is empty. No
similarity threshold is applied (Phase 15 owns calibration).

Other conventions
-----------------

- Keep prompt construction in ``app/services/rag_prompt.py`` and chunk
  retrieval in ``app/services/retrieval.py``; this module consumes the
  structured :class:`~app.services.rag_prompt.RagPrompt` (it never
  re-numbers sources or rebuilds prompt text).
- Sync adapter like the other Phase 5/6 services; the orchestrator
  offloads the network call with ``asyncio.to_thread`` so the FastAPI
  event loop is never blocked.
- The client is injectable and created lazily by the module factory;
  importing this module never creates a client or requires
  credentials.
- This module never logs the API key, the query, source content, or
  generated model text — exception types, stages, and counts only.
"""

import asyncio
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from app.services import retrieval as retrieval_service
from app.services.embeddings import EmbeddingProvider
from app.services.rag_prompt import (
    INSUFFICIENT_EVIDENCE_MESSAGE,
    RagPrompt,
    SourceReference,
    build_rag_prompt,
)
from app.core.config import get_settings
from google.genai.interactions import TextContent
from psycopg_pool import AsyncConnectionPool


logger = logging.getLogger(__name__)


class GenerationError(Exception):
    """Raised when RAG answer generation cannot produce a usable result.

    Covers: missing Gemini credentials or unusable configuration,
    failures of the underlying generation call (the original exception
    is preserved through chaining), non-completed interaction statuses,
    and missing or empty output text. ``stage`` identifies the failing
    step, room for Phase 6.4's sanitized HTTP mapping; the message is
    always a static string and never contains the query, source text,
    generated text, or key material.
    """

    def __init__(self, message: str, *, stage: str):
        super().__init__(message)
        self.stage = stage


class GenerationProvider(Protocol):
    """Provider-neutral generation interface for the RAG pipeline.

    Receives the fully built Phase 6.2 :class:`RagPrompt` (the adapter
    never constructs or mutates prompt text) and returns the generated
    answer text. Implementations are synchronous, like the phase 5.5
    embedding provider; async callers offload them to a worker thread.
    """

    def generate(self, prompt: RagPrompt) -> str:
        """Generate the answer text for one fully built prompt."""
        ...


class GeminiGenerationProvider:
    """Gemini implementation of :class:`GenerationProvider`.

    The SDK client is supplied by the caller (or created by
    ``create_gemini_generation_provider``) and only needs to expose
    ``interactions.create(...)``, so unit tests can substitute a fake
    client without any network access or credentials.
    """

    def __init__(self, client, *, model: str):
        self._client = client
        self._model = model

    def generate(self, prompt: RagPrompt) -> str:
        """Generate one answer from the Phase 6.2 prompt, statelessly."""
        if not isinstance(prompt, RagPrompt):
            raise TypeError(
                "generate expects a RagPrompt built by build_rag_prompt"
            )

        # A prompt without sources must never reach the API: empty
        # retrieval has its own deterministic answer, decided by
        # generate_rag_answer before this point.
        if not prompt.sources:
            raise GenerationError(
                "The RAG prompt carries no retrieved sources; empty "
                "retrieval must be answered without a generation call",
                stage="no_sources",
            )

        contents = [
            TextContent(text=prompt.question),
            TextContent(text=prompt.sources_text),
        ]

        try:
            response = self._client.interactions.create(
                model=self._model,
                input=contents,
                system_instruction=prompt.system_instruction,
                store=False,
            )
        except Exception as exc:
            logger.error(
                "Gemini generation request failed: %s",
                type(exc).__name__,
            )

            raise GenerationError(
                "The RAG answer could not be generated",
                stage="generation_request",
            ) from exc

        return self._extract_answer_text(response)

    def _extract_answer_text(self, response) -> str:
        """Validate the Interactions response and extract the answer text.

        The response is untrusted: a missing response, a non-
        ``completed`` status, or a missing/empty ``output_text`` fails
        the generation with :class:`GenerationError` instead of
        returning partial or fabricated output.
        """
        if response is None:
            raise GenerationError(
                "The generation response is empty",
                stage="empty_response",
            )

        status = getattr(response, "status", None)

        if status != "completed":
            raise GenerationError(
                "The generation interaction did not complete successfully",
                stage="unsuccessful_response",
            )

        text = getattr(response, "output_text", None)

        if not isinstance(text, str) or not text.strip():
            raise GenerationError(
                "The generation response contains no usable output text",
                stage="empty_output",
            )

        return text


@dataclass(frozen=True)
class Citation:
    """One validated numbered reference to a retrieved source.

    All values are copied unchanged from the matching
    :class:`~app.services.rag_prompt.SourceReference`, so a citation
    always points at the exact database chunk the retrieval returned.
    """

    number: int
    chunk_id: object
    document_id: object
    document_title: str
    chunk_index: object


def _validated_answer_text(text: object) -> str:
    if not isinstance(text, str):
        raise TypeError("The generated answer must be a string.")

    return text


def _validated_sources(sources: object) -> tuple[SourceReference, ...]:
    if isinstance(sources, (str, bytes)) or not isinstance(sources, Sequence):
        raise TypeError("The prompt sources must be a sequence of SourceReferences.")

    for source in sources:
        if not isinstance(source, SourceReference):
            raise TypeError(
                "Each prompt source must be a SourceReference."
            )

    return tuple(sources)


_CITATION_PATTERN = re.compile(r"\[(\d+)\]")


def validate_citations(
    answer_text: str,
    sources: Sequence[SourceReference],
) -> tuple[Citation, ...]:
    """Map ``[n]`` citations in `answer_text` to the retrieved sources.

    Scans the generated text for the Phase 6.2 numbered citation syntax
    and resolves each number AGAINST `sources` — the authoritative
    number-to-chunk mapping built by ``build_rag_prompt``. Rules:

    - only sources actually present in `sources` can produce a
      citation; every ``SourceReference`` field is copied unchanged,
    - repeated citations are deduplicated, keeping the
      FIRST-APPEARANCE order of the scan,
    - out-of-range numbers (``0``, beyond the retrieved count) are
      excluded without error, so a model-invented reference can never
      produce a citation object for a chunk that was not retrieved, and
    - invalid references never alter the answer text (Phase 14.7 owns
      stricter output enforcement).

    The function is pure and offline.
    """
    text = _validated_answer_text(answer_text)
    validated_sources = _validated_sources(sources)

    by_number = {source.number: source for source in validated_sources}

    citations: list[Citation] = []
    seen: set[int] = set()

    for match in _CITATION_PATTERN.finditer(text):
        number = int(match.group(1))

        if number in seen:
            continue

        source = by_number.get(number)

        if source is None:
            # Unknown reference: excluded, never fabricated.
            continue

        seen.add(number)
        citations.append(
            Citation(
                number=source.number,
                chunk_id=source.chunk_id,
                document_id=source.document_id,
                document_title=source.document_title,
                chunk_index=source.chunk_index,
            )
        )

    return tuple(citations)


@dataclass(frozen=True)
class RagAnswer:
    """A complete Phase 6.3 RAG answer.

    ``answer`` is the generated text (or the exact insufficient-
    evidence wording when retrieval found nothing); ``citations`` are
    the validated numbered references into the retrieved sources.
    """

    answer: str
    citations: tuple[Citation, ...]


async def generate_rag_answer(
    pool: AsyncConnectionPool,
    embedding_provider: EmbeddingProvider,
    generation_provider: GenerationProvider,
    *,
    query: str,
    user_role: object,
) -> RagAnswer:
    """Generate a grounded answer with citations for one user question.

    Orchestrates the existing Phase 6.1 retrieval, Phase 6.2 prompt
    construction, and this module's generation + citation validation —
    the single reusable seam Phase 6.4's route will call. Empty
    retrieval NEVER calls the model: the answer is exactly
    ``INSUFFICIENT_EVIDENCE_MESSAGE`` with no citations.

    `user_role` must come from trusted server-side profile data (the
    Phase 3 ``require_role`` dependency), never from the request; it is
    consumed by the Phase 6.1 role map.

    Error mapping: ``EmbeddingError``, ``RetrievalError``,
    ``GenerationError``, and the prompt builder's ``TypeError``/
    ``ValueError`` propagate unchanged for Phase 6.4 to sanitize;
    provider/database failure text is never echoed to callers.
    """
    chunks = await retrieval_service.retrieve(
        pool,
        embedding_provider,
        query=query,
        user_role=user_role,
    )

    prompt = build_rag_prompt(query, chunks)

    if not prompt.sources:
        # Empty retrieval: decide without the model. Low relevance is a
        # known Phase 15 calibration concern, not a threshold.
        logger.info(
            "RAG retrieval returned no chunks; generation skipped."
        )

        return RagAnswer(
            answer=INSUFFICIENT_EVIDENCE_MESSAGE,
            citations=(),
        )

    # The provider is synchronous and makes a network call: offload to
    # a worker thread exactly like the Phase 5.6/6.1 embedding calls.
    answer_text = await asyncio.to_thread(
        generation_provider.generate,
        prompt,
    )

    citations = validate_citations(answer_text, prompt.sources)

    logger.info(
        "RAG generation produced %d citation(s).",
        len(citations),
    )

    return RagAnswer(answer=answer_text, citations=citations)


def create_gemini_generation_provider() -> GeminiGenerationProvider:
    """Create a :class:`GeminiGenerationProvider` from configuration.

    The API key comes from ``Settings.gemini_api_key`` (the
    ``GEMINI_API_KEY`` environment variable or ``services/api/.env``)
    and the model from ``Settings.gemini_generation_model``. The key
    and model are never hardcoded here and never logged.

    Both the client import and the credential check happen inside this
    factory, so importing this module never requires credentials and
    never creates a live client. Calling this without a configured
    ``GEMINI_API_KEY`` raises :class:`GenerationError` with a clear
    message instead of a cryptic SDK authentication error later.
    """
    settings = get_settings()
    api_key = settings.gemini_api_key
    key = api_key.get_secret_value() if api_key is not None else ""

    if not key:
        raise GenerationError(
            "GEMINI_API_KEY is not configured; set it in the environment "
            "or services/api/.env before generating answers",
            stage="configuration",
        )

    model = settings.gemini_generation_model

    if not isinstance(model, str) or not model.strip():
        raise GenerationError(
            "The configured generation model is unusable",
            stage="configuration",
        )

    # Deferred import: constructing the client is the only thing that
    # requires the key to be usable.
    from google import genai

    try:
        client = genai.Client(api_key=key)
    except Exception as exc:
        raise GenerationError(
            "Gemini client could not be created with the configured "
            "credentials",
            stage="configuration",
        ) from exc

    return GeminiGenerationProvider(client, model=model)
