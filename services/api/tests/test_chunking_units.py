"""Unit tests for the Phase 5.4 text chunking function.

These tests do not require the local Supabase database, Storage,
FastAPI requests, or Gemini credentials: they only exercise
app.services.chunking.chunk_text directly.
"""

import pytest

from app.services.chunking import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_OVERLAP,
    _estimate_token_count,
    chunk_text,
)


def _filler_word(index: int) -> str:
    """A deterministic 4-character word, estimated as one token."""
    return f"w{index:03d}"


def _uniform_text(word_count: int) -> str:
    """Text made of `word_count` 4-character (~1 token) words."""
    return " ".join(_filler_word(i) for i in range(word_count))


def _max_words_within_tokens(tokens: int) -> int:
    """Largest uniform-word count whose joined estimate fits `tokens`.

    k words of 4 characters join to 4k + (k - 1) = 5k - 1 characters,
    so the estimate is ceil((5k - 1) / 4); solving for the largest k
    with that estimate <= `tokens` gives (4 * tokens + 1) // 5.
    """
    return (4 * tokens + 1) // 5


def _chunks_to_words(chunks: list[str]) -> list[list[str]]:
    return [chunk.split() for chunk in chunks]


def _shared_overlap_words(previous: list[str], current: list[str]) -> int:
    """Largest k such that previous ends with k words current starts with."""
    limit = min(len(previous), len(current))

    for k in range(limit, 0, -1):
        if previous[-k:] == current[:k]:
            return k

    return 0


def _reconstruct_words(chunks: list[str]) -> list[str]:
    """Rebuild the source word order from overlapping chunks."""
    word_lists = _chunks_to_words(chunks)
    result = list(word_lists[0])

    for current in word_lists[1:]:
        overlap = _shared_overlap_words(result, current)
        result.extend(current[overlap:])

    return result


class TestChunkShortInput:
    def test_short_input_produces_single_chunk(self):
        text = "ShopPilot return policy: items are refundable in 30 days"

        result = chunk_text(text)

        assert result == [text]

    def test_input_at_chunk_limit_produces_single_chunk(self):
        # The largest uniform text that still measures within the
        # chunk budget must not be split.
        text = _uniform_text(_max_words_within_tokens(DEFAULT_CHUNK_SIZE))

        result = chunk_text(text)

        assert len(result) == 1
        assert result[0] == text

    def test_input_below_chunk_limit_produces_single_chunk(self):
        text = _uniform_text(_max_words_within_tokens(DEFAULT_CHUNK_SIZE) - 1)

        result = chunk_text(text)

        assert len(result) == 1
        assert result[0] == text


class TestChunkLongInput:
    def test_input_above_chunk_limit_produces_multiple_chunks(self):
        n_words = _max_words_within_tokens(DEFAULT_CHUNK_SIZE) + 1

        result = chunk_text(_uniform_text(n_words))

        assert len(result) > 1

    def test_much_longer_input_spreads_across_many_chunks(self):
        n_words = _max_words_within_tokens(DEFAULT_CHUNK_SIZE) * 3 + 200

        result = chunk_text(_uniform_text(n_words))

        assert len(result) >= 3

    def test_emitted_chunks_stay_within_chunk_limit(self):
        # Sizing applies to the chunk text itself, so every non-
        # oversized chunk must estimate within the budget.
        n_words = _max_words_within_tokens(DEFAULT_CHUNK_SIZE) * 2 + 500

        for chunk in chunk_text(_uniform_text(n_words)):

            assert _estimate_token_count(chunk) <= DEFAULT_CHUNK_SIZE
            assert len(chunk.split()) <= _max_words_within_tokens(
                DEFAULT_CHUNK_SIZE
            )


class TestChunkOverlap:
    def test_consecutive_chunks_share_default_overlap_words(self):
        n_words = _max_words_within_tokens(DEFAULT_CHUNK_SIZE) * 2 + 1
        word_lists = _chunks_to_words(
            chunk_text(_uniform_text(n_words))
        )

        assert len(word_lists) >= 3

        for previous, current in zip(word_lists, word_lists[1:]):
            shared = _shared_overlap_words(previous, current)

            # The shared tail carries at least `overlap` estimated
            # tokens (windows here are large enough to cover it), and
            # dropping the last word was the only way it stayed below.
            assert shared >= 1
            assert previous[-shared:] == current[:shared]

            if shared > 1:
                assert _estimate_token_count(
                    " ".join(current[:shared])
                ) >= DEFAULT_OVERLAP
                assert _estimate_token_count(
                    " ".join(current[: shared - 1])
                ) < DEFAULT_OVERLAP

    def test_overlap_hits_target_tokens_exactly_for_uniform_words(self):
        # With 4-character words the carried tail is exactly the
        # largest word count within the overlap budget. Small chunk
        # parameters keep every window inside the 4-character word
        # range; overlap must stay below chunk_size.
        word_lists = _chunks_to_words(
            chunk_text(_uniform_text(400), chunk_size=100, overlap=20)
        )
        expected_words = _max_words_within_tokens(20)

        assert len(word_lists) > 1

        for previous, current in zip(word_lists, word_lists[1:]):
            shared = _shared_overlap_words(previous, current)

            assert shared == expected_words
            assert previous[-shared:] == current[:shared]

    def test_overlap_words_come_from_source_text(self):
        # The shared words must be real, consecutive source words, not
        # fabricated facsimiles. Filler words embed their index, so an
        # exact substring hit proves they were lifted from the source.
        text = _uniform_text(_max_words_within_tokens(DEFAULT_CHUNK_SIZE) * 2 + 1)
        word_lists = _chunks_to_words(chunk_text(text))

        for previous, current in zip(word_lists, word_lists[1:]):
            shared = _shared_overlap_words(previous, current)

            assert shared >= 1
            assert " ".join(current[:shared]) in text


class TestChunkOrderingAndCoverage:
    def test_chunks_preserve_source_word_order_and_coverage(self):
        n_words = _max_words_within_tokens(DEFAULT_CHUNK_SIZE) * 2 + 137
        text = _uniform_text(n_words)

        result = chunk_text(text)

        assert _reconstruct_words(result) == text.split()

    def test_overlapping_chunks_reconstruct_irregular_text(self):
        # Mixed word lengths change the per-word character counts while
        # the ordering and coverage property must still hold.
        words = [f"word-{i}" + "x" * (i % 7) for i in range(3000)]
        text = " ".join(words)

        result = chunk_text(text)

        assert len(result) > 1
        assert _reconstruct_words(result) == words


class TestEmptyAndWhitespaceInput:
    def test_empty_string_returns_no_chunks(self):
        assert chunk_text("") == []

    def test_whitespace_only_string_returns_no_chunks(self):
        assert chunk_text("   ") == []
        assert chunk_text(" \n\t\r ") == []


class TestInvalidParameters:
    def test_zero_chunk_size_raises(self):
        with pytest.raises(ValueError, match="chunk_size"):
            chunk_text("some text", chunk_size=0)

    def test_negative_chunk_size_raises(self):
        with pytest.raises(ValueError, match="chunk_size"):
            chunk_text("some text", chunk_size=-5)

    def test_negative_overlap_raises(self):
        with pytest.raises(ValueError, match="overlap"):
            chunk_text("some text", overlap=-1)

    def test_overlap_equal_to_chunk_size_raises(self):
        with pytest.raises(ValueError, match="overlap"):
            chunk_text("some text", chunk_size=100, overlap=100)

    def test_overlap_greater_than_chunk_size_raises(self):
        with pytest.raises(ValueError, match="overlap"):
            chunk_text("some text", chunk_size=100, overlap=DEFAULT_CHUNK_SIZE)

    def test_non_string_input_raises_type_error(self):
        with pytest.raises(TypeError, match="string"):
            chunk_text(None)  # type: ignore[arg-type]


class TestBoundaryRobustness:
    def test_repeated_identical_words_terminate_without_empty_chunks(self):
        n_words = _max_words_within_tokens(DEFAULT_CHUNK_SIZE) * 3
        text = " ".join(["same"] * n_words)

        result = chunk_text(text)

        assert len(result) > 1
        assert all(chunk.split() for chunk in result)

    def test_single_oversized_word_becomes_its_own_chunk(self):
        giant = "z" * 100_000  # ~25,000 estimated tokens

        result = chunk_text(giant + " tail words follow")

        assert result == [giant, "tail words follow"]

    def test_oversized_word_run_between_normal_windows_terminates(self):
        n_words = _max_words_within_tokens(DEFAULT_CHUNK_SIZE)
        text = (
            _uniform_text(n_words)
            + " " + " ".join(["z"] * 50_000)
            + " " + _uniform_text(n_words)
        )

        result = chunk_text(text)

        assert len(result) > 1
        assert all(chunk.strip() == chunk for chunk in result)
        assert all(chunk.split() for chunk in result)

    def test_repeated_calls_are_deterministic(self):
        n_words = _max_words_within_tokens(DEFAULT_CHUNK_SIZE) * 2 + 11
        text = _uniform_text(n_words)

        assert chunk_text(text) == chunk_text(text)


class TestContractDefaults:
    def test_default_parameters_match_documented_targets(self):
        assert DEFAULT_CHUNK_SIZE == 800
        assert DEFAULT_OVERLAP == 100
