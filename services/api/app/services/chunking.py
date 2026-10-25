"""Deterministic text chunking for the knowledge-base ingestion pipeline.

Phase 5.4 of the roadmap: split extracted text into ordered chunks of
approximately 800 tokens with approximately 100 tokens of overlap
between consecutive chunks.

Token sizes are measured with a local characters-per-token heuristic
instead of a real tokenizer. English prose averages roughly one token
per four characters, so chunk sizes are deterministic and stable, but
not identical to the token counts a Gemini model would report, and
text written in non-Latin scripts typically packs fewer characters
per token. This keeps chunking offline and unit-testable without a
large tokenizer dependency; the approximation is acceptable because
the target sizes are approximate by design.

The estimate applies to the chunk text itself: the joining separator
spaces count toward a chunk's budget exactly like any other character.
Chunk boundaries therefore fall between whitespace-delimited words,
and every emitted chunk stays within the chunk-size budget unless it
is a single word that alone exceeds the limit.

All non-whitespace text survives across boundaries: the words of the
input appear in strictly increasing order across the chunks, with
consecutive chunks sharing the overlap words. Whitespace inside a
chunk is normalized to single spaces, so chunk contents preserve text
but not the original line wrapping.

Empty or whitespace-only input yields no chunks: extraction
(`extract_text`) already rejects textless documents, so there is
nothing to embed downstream.

This module never calls Gemini or any other external service.
"""

import math

# Characters-per-token heuristic for English prose. One token ≈ four
# characters; a 4-character word is estimated as a single token.
_CHARS_PER_TOKEN = 4

DEFAULT_CHUNK_SIZE = 800
DEFAULT_OVERLAP = 100


def _token_estimate_for_chars(char_count: int) -> int:
    """Estimate the token count for a character count."""
    if char_count <= 0:
        return 0

    return max(1, math.ceil(char_count / _CHARS_PER_TOKEN))


def _estimate_token_count(text: str) -> int:
    """Estimate the token count of a text fragment.

    Uses the module's characters-per-token heuristic, counting every
    character including whitespace. The result is deterministic and
    never requires a tokenizer model or network access. See the module
    docstring for the documented limitations of this approximation.
    """
    return _token_estimate_for_chars(len(text))


def chunk_text(
    text: str,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
) -> list[str]:
    """Split extracted text into ordered chunks of ~`chunk_size` tokens.

    Accepts extracted document text and returns chunks in original
    document order. Consecutive chunks share an overlap of
    approximately `overlap` estimated tokens: the trailing words of a
    chunk are the leading words of the next chunk, which lets retrieval
    match content that spans a boundary. Because the next window starts
    at the overlap tail, windows either cover the whole input or
    strictly advance, so the splitter terminates for every input.

    Sizes are measured by applying the characters-per-token heuristic
    to the chunk text itself, separators included (see the module
    docstring). A boundary snaps to the last word that keeps the
    joined chunk within `chunk_size` estimated tokens, so chunk sizes
    are approximate by design. A single word whose own estimated size
    exceeds `chunk_size` is emitted as an oversized single-word chunk
    rather than split.

    Empty or whitespace-only input returns an empty list.

    The input string is never mutated (strings are immutable) and no
    external API is called.

    Raises:
        TypeError: If `text` is not a string.
        ValueError: If `chunk_size < 1`, `overlap < 0`, or
            `overlap >= chunk_size` (windows would never advance).
    """
    if not isinstance(text, str):
        raise TypeError(
            f"Expected text as a string, got {type(text).__name__}"
        )

    if chunk_size < 1:
        raise ValueError(f"chunk_size must be at least 1, got {chunk_size}")

    if overlap < 0:
        raise ValueError(f"overlap cannot be negative, got {overlap}")

    # overlap >= chunk_size would keep the next window stuck on the
    # same word(s) forever, so it is rejected up front.
    if overlap >= chunk_size:
        raise ValueError(
            f"overlap ({overlap}) must be smaller than chunk_size "
            f"({chunk_size})"
        )

    words = text.split()

    if not words:
        return []

    word_count = len(words)
    chunks: list[str] = []
    start = 0

    while start < word_count:
        # Accumulate joined characters — words plus their single-space
        # separators — so the budget bounds the emitted chunk text.
        joined_chars = len(words[start])
        end = start + 1

        while end < word_count:
            candidate = joined_chars + 1 + len(words[end])

            if _token_estimate_for_chars(candidate) > chunk_size:
                break

            joined_chars = candidate
            end += 1

        chunks.append(" ".join(words[start:end]))

        if end >= word_count:
            break

        # Carry the trailing words of the window — the smallest tail
        # that reaches approximately `overlap` estimated tokens — into
        # the next window so consecutive chunks share that overlap.
        next_start = end
        index = end - 1
        carried_chars = len(words[index])

        while index > start and _token_estimate_for_chars(carried_chars) < overlap:
            index -= 1
            carried_chars += 1 + len(words[index])
            next_start = index

        # next_start always lands in [start + 1, end] and the loop
        # breaks once the last word is consumed, so every iteration
        # either finishes the input or moves start forward.
        start = next_start

    return chunks
