"""Shared fakes for the Phase 5.6 ingestion tests.

Used by ``test_ingestion_units.py`` (fully offline) and
``test_ingestion_integration.py`` (real database; Storage and the
embedding provider stay faked). These doubles deliberately implement
the same external surface as the real components: the Supabase
``Client.from_(bucket)`` chain with a synchronous ``download`` and the
``EmbeddingProvider`` protocol with a synchronous ``embed_documents``.
"""

from collections.abc import Sequence

from app.services.embeddings import EmbeddingError
from app.storage import DOCUMENTS_BUCKET


class FakeStorageState:
    """In-memory object store backing :class:`FakeStorageClient`."""

    def __init__(self, *, fail_download: bool = False):
        self.fail_download = fail_download
        # path -> stored PDF bytes
        self.objects: dict[str, bytes] = {}
        self.downloads: list[str] = []


class FakeStorageBucket:
    """Fake `documents` bucket supporting synchronous downloads."""

    def __init__(self, state: FakeStorageState):
        self._state = state

    def download(self, path: str) -> bytes:
        self._state.downloads.append(path)

        if self._state.fail_download:
            raise RuntimeError("simulated storage outage")

        try:
            return self._state.objects[path]
        except KeyError as exc:
            raise RuntimeError("simulated missing Storage object") from exc


class FakeStorageClient:
    """Fake service-role Storage client, like ``supabase.Client``."""

    def __init__(self, state: FakeStorageState):
        self._state = state

    def from_(self, bucket: str) -> FakeStorageBucket:
        assert bucket == DOCUMENTS_BUCKET
        return FakeStorageBucket(self._state)


class FakeEmbeddingProvider:
    """Deterministic, misbehavior-configurable embedding double.

    Implements the :class:`EmbeddingProvider` interface used by
    ingestion. Deterministic vectors make order alignment assertable:
    value 0 of every vector encodes ``batch_index * 1000 +
    position_in_batch + offset``, and every other value equals
    ``offset``.

    Misbehavior knobs (unit-test only; the real Gemini provider already
    validates its own responses, so these simulate a provider that —
    like a hypothetical reimplementation — can return invalid shapes):

    - ``fail_batches``: batch indices whose call raises
      ``EmbeddingError``.
    - ``wrong_count_batch``: batch index returning one extra vector.
    - ``wrong_dim_batch``: batch index whose first vector is one value
      short of ``dimensions``.
    - ``bad_value_batch``: ``(batch_index, value)`` replacing the first
      value of the batch's first vector (e.g. ``True``, ``"text"``,
      ``float("inf")``).
    - ``raw_override_batch``: ``(batch_index, replacement)`` returning
      ``replacement`` as the batch's whole vector list, to simulate
      structurally invalid responses (non-sequence "vectors", etc.).
    """

    def __init__(
        self,
        *,
        dimensions: int = 768,
        offset: float = 0.0,
        fail_batches: Sequence[int] = (),
        wrong_count_batch: int | None = None,
        wrong_dim_batch: int | None = None,
        bad_value_batch: tuple[int, object] | None = None,
        raw_override_batch: tuple[int, object] | None = None,
    ):
        self.dimensions = dimensions
        self.offset = offset
        self.fail_batches = set(fail_batches)
        self.wrong_count_batch = wrong_count_batch
        self.wrong_dim_batch = wrong_dim_batch
        self.bad_value_batch = bad_value_batch
        self.raw_override_batch = raw_override_batch

        # calls[i] is the i-th batch of texts sent to the provider.
        self.calls: list[list[str]] = []
        self._invocations = 0

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        batch_index = self._invocations
        self._invocations += 1

        self.calls.append(list(texts))

        if batch_index in self.fail_batches:
            raise EmbeddingError("simulated embedding provider outage")

        if (
            self.wrong_count_batch is not None
            and batch_index == self.wrong_count_batch
        ):
            return [[self.offset] * self.dimensions for _ in range(len(texts) + 1)]

        vectors = [
            self._deterministic_vector(batch_index, position)
            for position in range(len(texts))
        ]

        if self.wrong_dim_batch is not None and batch_index == self.wrong_dim_batch:
            vectors[0] = vectors[0][:-1]

        if self.bad_value_batch is not None:
            bad_batch, bad_value = self.bad_value_batch

            if batch_index == bad_batch:
                vectors[0] = [bad_value] + vectors[0][1:]

        if self.raw_override_batch is not None:
            bad_batch, replacement = self.raw_override_batch

            if batch_index == bad_batch:
                return replacement

        return vectors

    def _deterministic_vector(
        self,
        batch_index: int,
        position: int,
    ) -> list[float]:
        encoded = float(batch_index * 1000 + position) + self.offset

        return [encoded] + [self.offset] * (self.dimensions - 1)

    def embed_query(self, texts: Sequence[str]) -> list[list[float]]:
        raise AssertionError("ingestion must only call embed_documents")
