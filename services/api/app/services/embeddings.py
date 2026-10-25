"""Text embedding generation for the knowledge-base ingestion pipeline.

Phase 5.5 of the roadmap: a provider-neutral embedding interface with
Gemini as its only implementation, so Phase 5.6 ingestion (and later
Phase 6 retrieval) can generate embeddings through a single tested
service instead of calling the SDK directly.

Model and dimensions contract
-----------------------------

The provider uses ``gemini-embedding-2`` and requests exactly
``EMBEDDING_DIMENSIONS`` (768) output dimensions, matching the
``document_chunks.embedding vector(768)`` column from the Phase 2
migrations. Vectors are never truncated or padded to "fix" a response
shape: a successful result is one 768-finite-float vector per input, in
input order, or nothing (an ``EmbeddingError``).

Retrieval formatting conventions
--------------------------------

``gemini-embedding-2`` does not support a ``task_type`` parameter;
instead, the documented approach is to put the task instruction in the
prompt itself, with different prefixes for the asymmetric retrieval use
case (document embedding vs. query embedding):

- documents (``embed_documents``): ``title: none | text: {content}``
- queries (``embed_query``):      ``task: search result | query: {content}``

The formatting lives inside this Gemini adapter. The neutral
``EmbeddingProvider`` interface carries the distinction as two methods
so ingestion and retrieval share the same formats; Phase 6 must embed
queries through ``embed_query`` to stay compatible with vectors
produced by ``embed_documents``.

``gemini-embedding-2`` input structure and batching
----------------------------------------------------

Unlike ``gemini-embedding-001``, this model aggregates multiple raw
inputs passed directly in ``contents`` into ONE combined embedding.
Therefore every input is wrapped in its own independent
``types.Content`` object, which the API documents as producing one
separate embedding per input. The response count is validated against
the input count so an accidental aggregated single-embedding response
can never be returned as success.

The provider currently issues ONE API call per supplied batch of
texts and performs no automatic sub-batching. Gemini embedding
requests are limited to 8,192 input tokens per text (chunks of ~800
estimated tokens fit comfortably), but the total batch size is not
capped here. Production batch sizing and retry/backoff policy belong
to the Phase 5.6 ingestion pipeline, not to this service.

Other conventions
-----------------

- Synchronous, like the other Phase 5 services (``extract_text``,
  ``chunk_text``); no database, Storage, router, or ingestion logic.
- The Gemini client is constructed lazily by ``create_gemini_provider``
  and is injectable, so unit tests run offline with a fake client.
  Importing this module never creates a client or requires
  credentials; a missing ``GEMINI_API_KEY`` only fails when the
  provider factory is asked for a usable provider.
- Input validation is explicit: empty input returns ``[]`` without an
  API call, non-string items raise ``TypeError``, and empty or
  whitespace-only texts raise ``ValueError`` (meaningless inputs are
  never sent to the API).
- This module never logs the API key, input document text, query
  text, or embedding values.
"""

import logging
import math
from collections.abc import Sequence
from typing import Protocol

from google.genai import types

from app.core.config import get_settings


logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "gemini-embedding-2"

# Matches the document_chunks.embedding vector(768) column.
EMBEDDING_DIMENSIONS = 768

# The vector values the API returns are floats; ints are accepted if a
# (fake) response contains them, but bools are rejected explicitly
# because bool is a subclass of int in Python.
_ALLOWED_VECTOR_VALUE_TYPES = (int, float)


class EmbeddingError(Exception):
    """Raised when embedding generation cannot produce usable vectors.

    Covers: missing Gemini credentials, failures of the underlying
    embedding API call (the original exception is preserved through
    exception chaining), and invalid API responses (missing embeddings,
    wrong response counts, wrong dimensionality, non-numeric or
    non-finite values, accidental aggregated responses).
    """


class EmbeddingProvider(Protocol):
    """Intent-agnostic embedding interface.

    The task distinction lives in the two methods rather than in a
    task parameter: document text and retrieval queries receive
    different treatment downstream (prompt formatting, retrieval
    asymmetry), and every implementation needs both sides to stay
    compatible with each other.

    Both methods accept a sequence of strings and return exactly one
    768-dimensional vector per input, preserving input order.
    """

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed document texts for retrieval (asymmetric "document" side)."""
        ...

    def embed_query(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed retrieval query texts (asymmetric "query" side)."""
        ...


def _format_document_text(content: str) -> str:
    """Format a document text with the documented retrieval prefix.

    Chunks have no standalone title, so the title field is ``none``
    per the documented document structure.
    """
    return f"title: none | text: {content}"


def _format_query_text(content: str) -> str:
    """Format a retrieval query with the documented task prefix."""
    return f"task: search result | query: {content}"


def _validated_texts(texts: Sequence[str], *, method: str) -> list[str]:
    """Validate the input contract shared by both provider methods.

    Returns the inputs as a plain list of strings. Raises
    ``TypeError`` when ``texts`` is not a sequence of strings (a bare
    string is rejected to avoid silently iterating its characters) and
    ``ValueError`` for empty or whitespace-only individual texts.
    Validation never calls the embedding API, so callers can rely on
    input errors being raised without any request being made.
    """
    if isinstance(texts, (str, bytes)) or not isinstance(texts, Sequence):
        raise TypeError(
            f"embed_{method} expects a sequence of strings, "
            f"got {type(texts).__name__}"
        )

    validated: list[str] = []

    for index, text in enumerate(texts):
        if not isinstance(text, str):
            raise TypeError(
                f"embed_{method} input item at index {index} is not a "
                f"string, got {type(text).__name__}"
            )

        if not text.strip():
            raise ValueError(
                f"embed_{method} input item at index {index} is empty "
                "or whitespace-only; empty texts are not embedded"
            )

        validated.append(text)

    return validated


class GeminiEmbeddingProvider:
    """Gemini implementation of :class:`EmbeddingProvider`.

    The SDK client is supplied by the caller (or created by the
    module-level factory) and only needs to expose
    ``models.embed_content(...)``, so unit tests can substitute a fake
    client without any network access or credentials.
    """

    def __init__(
        self,
        client,
        *,
        model: str = EMBEDDING_MODEL,
        dimensions: int = EMBEDDING_DIMENSIONS,
    ):
        self._client = client
        self._model = model
        self._dimensions = dimensions

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed document texts with the document-side prompt format."""
        formatted = (
            _format_document_text(text)
            for text in _validated_texts(texts, method="documents")
        )
        return self._embed(formatted)

    def embed_query(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed query texts with the query-side prompt format."""
        formatted = (
            _format_query_text(text)
            for text in _validated_texts(texts, method="query")
        )
        return self._embed(formatted)

    def _embed(self, formatted_texts) -> list[list[float]]:
        formatted = list(formatted_texts)

        # An empty batch must never reach the API.
        if not formatted:
            return []

        # One independent Content object per input: passing raw strings
        # or multiple bare parts would make gemini-embedding-2 return a
        # single aggregated embedding for the whole batch.
        contents = [
            types.Content(parts=[types.Part.from_text(text=text)])
            for text in formatted
        ]

        try:
            response = self._client.models.embed_content(
                model=self._model,
                contents=contents,
                config=types.EmbedContentConfig(
                    output_dimensionality=self._dimensions
                ),
            )
        except Exception as exc:
            logger.error(
                "Gemini embedding request failed: %s: %s",
                type(exc).__name__,
                exc,
            )

            raise EmbeddingError(
                f"Gemini embedding request failed for {len(contents)} text(s)"
            ) from exc

        return self._extract_vectors(response, expected_count=len(contents))

    def _extract_vectors(self, response, *, expected_count: int) -> list[list[float]]:
        """Validate the API response shape and return plain float vectors.

        The whole response is treated as untrusted: any structural
        problem (no embeddings, a different number of embeddings than
        inputs, wrong vector length, or values that are not finite
        numbers) fails the entire batch with ``EmbeddingError`` instead
        of returning partial, truncated, or padded results.
        """
        if response is None:
            raise EmbeddingError(
                "Gemini embedding response is empty; "
                f"expected {expected_count} embedding(s)"
            )

        embeddings = getattr(response, "embeddings", None)

        if embeddings is None:
            raise EmbeddingError(
                "Gemini embedding response contains no embeddings; "
                f"expected {expected_count} embedding(s)"
            )

        try:
            actual_count = len(embeddings)
        except TypeError as exc:
            raise EmbeddingError(
                "Gemini embedding response is not a list of embeddings"
            ) from exc

        if actual_count != expected_count:
            raise EmbeddingError(
                "Gemini embedding response count does not match the "
                f"input count: expected {expected_count}, got {actual_count} "
                "(this also guards against the model returning one "
                "aggregated embedding for the whole batch)"
            )

        vectors: list[list[float]] = []

        for index, embedding in enumerate(embeddings):
            vectors.append(
                self._validated_vector_values(embedding, index)
            )

        return vectors

    def _validated_vector_values(self, embedding, index: int) -> list[float]:
        """Validate one embedding as exactly N finite numbers.

        Booleans are rejected even though Python treats ``bool`` as a
        subclass of ``int``: embedding components are floats, and
        silently accepting ``True``/``False`` would hide a malformed
        response.
        """
        values = getattr(embedding, "values", None)

        if values is None:
            raise EmbeddingError(
                f"Gemini embedding {index} contains no values"
            )

        try:
            actual_length = len(values)
        except TypeError as exc:
            raise EmbeddingError(
                f"Gemini embedding {index} values are not a vector"
            ) from exc

        if actual_length != self._dimensions:
            raise EmbeddingError(
                f"Gemini embedding {index} has {actual_length} dimensions, "
                f"expected {self._dimensions}"
            )

        validated: list[float] = []

        for position, value in enumerate(values):
            if isinstance(value, bool) or not isinstance(
                value, _ALLOWED_VECTOR_VALUE_TYPES
            ):
                raise EmbeddingError(
                    f"Gemini embedding {index} contains a non-numeric "
                    f"value at position {position}"
                )

            numeric = float(value)

            if not math.isfinite(numeric):
                raise EmbeddingError(
                    f"Gemini embedding {index} contains a non-finite "
                    f"value at position {position}"
                )

            validated.append(numeric)

        return validated


def create_gemini_provider() -> GeminiEmbeddingProvider:
    """Create a :class:`GeminiEmbeddingProvider` using configured credentials.

    The API key comes from ``Settings.gemini_api_key`` (the
    ``GEMINI_API_KEY`` environment variable or ``services/api/.env``).
    The key is never hardcoded here and never logged.

    Both the client import and the credential check happen inside this
    factory, so importing the module never requires credentials and
    never creates a live client. Calling this without a configured
    ``GEMINI_API_KEY`` raises :class:`EmbeddingError` with a clear
    message instead of a cryptic SDK authentication error later.
    """
    settings = get_settings()
    api_key = settings.gemini_api_key
    key = api_key.get_secret_value() if api_key is not None else ""

    if not key:
        raise EmbeddingError(
            "GEMINI_API_KEY is not configured; set it in the environment "
            "or services/api/.env before embedding text"
        )

    # Deferred import: constructing the client is the only thing that
    # requires the key to be usable.
    from google import genai

    try:
        client = genai.Client(api_key=key)
    except Exception as exc:
        raise EmbeddingError(
            "Gemini client could not be created with the configured "
            "credentials"
        ) from exc

    return GeminiEmbeddingProvider(client)
