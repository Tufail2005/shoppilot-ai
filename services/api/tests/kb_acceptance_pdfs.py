"""Deterministic in-memory sample PDFs for the Phase 5.10 acceptance run.

The roadmap defines Phase 5.10 as "Test with 2 PDFs: one public (return
policy), one internal (ops handbook)". These builders produce both
documents with realistic, fully synthetic knowledge-base content and
never write to disk: everything lives in memory, so no existing file
can be overwritten and no binary fixture is added to the repository.

The PDFs are built with the raw PDF 1.4 primitives already established
by ``tests.test_documents_units._build_pdf`` (reuse, not reimplementation).

Constraints respected by every page text:

- ASCII only, and never ``(`` ``)`` ``\\`` ``%`` — the content stream
  syntax embeds the text inside bare parentheses, so unescaped
  characters would corrupt the PDF.
- Each document embeds one unique marker token so later checks can
  attribute chunk content to exactly one document.
- Importing this module never touches the database, Storage, or the
  network.

Self-validation contract: the import-time assertions below prove the
generated PDFs are usable BEFORE any test relies on them — extractable
text present, markers recovered through the real extraction, enough
text to produce multiple chunks, and markers that survive chunking.
"""

from app.services.chunking import chunk_text
from app.services.documents import extract_text
from tests.test_documents_units import _build_pdf


KNOWLEDGE_BASE_ACCEPTANCE_MARKERS = ("XQ71", "ZM584")


def _compose_page_text(product_key: str, marker: str, paragraphs: list[str]) -> str:
    """Compose one page's text: an intro, paragraphs, and a structural tail."""

    intro = (
        f"Section reference {product_key}. This document is a synthetic "
        f"sample for the acceptance run of the knowledge-base lifecycle. "
    )

    body = " ".join(paragraphs)

    tail = " ".join(
        f"Reminder {position}: store current operating knowledge in the "
        "knowledge base with the correct visibility level."
        for position in range(1, 9)
    )

    return f"{intro}Marker {marker} identifies this sample. {body} {tail}"


_PUBLIC_PARAGRAPHS = [
    (
        "Purchases in this store include a 30 day return window counted "
        "from the delivery date. Returned items must be unused, unwashed, "
        "and complete with their original packaging and labels."
    ),
    (
        "Refunds are issued to the original payment method within 5 "
        "business days after the return is approved at the returns desk. "
        "Shipping fees are refunded only when the item arrived damaged or "
        "the wrong item was sent."
    ),
    (
        "Exchanges for a different size or color of the same item are "
        "shipped at no additional cost once the exchange request is "
        "approved. Price differences are charged or refunded at the "
        "current list price."
    ),
    (
        "Final sale items, gift cards, opened personal care products, and "
        "custom engraved goods cannot be returned. Warranty claims follow "
        "the warranty booklet that ships with the item."
    ),
]


_INTERNAL_PARAGRAPHS = [
    (
        "Tier one support answers customer tickets within 8 business "
        "hours. Tickets inactive for 10 or more calendar days are closed "
        "with a note inviting the customer to reopen them."
    ),
    (
        "Refund approvals above the published threshold require "
        "escalation to the support manager on duty. Under the threshold, "
        "tier one agents may approve refunds directly and record the "
        "decision in the ticket."
    ),
    (
        "Verify the requester with the account email and one recent order "
        "number before releasing any order specific information. Requests "
        "without verifiable identity are politely refused."
    ),
    (
        "Internal procedures must remain internal at all times. They are "
        "never quoted to customers, and questions about this handbook go "
        "to the knowledge administrator."
    ),
]


# Marker tokens are deliberately unique, ASCII, and absent from the
# other document's text so attribution is unambiguous.
_PUBLIC_MARKER = "XQ71"
_INTERNAL_MARKER = "ZM584"

_PUBLIC_PAGE_TEXTS = [
    _compose_page_text("PUBLIC-RETURN-POLICY", _PUBLIC_MARKER, _PUBLIC_PARAGRAPHS)
] * 5

_INTERNAL_PAGE_TEXTS = [
    _compose_page_text("INTERNAL-OPS-HANDBOOK", _INTERNAL_MARKER, _INTERNAL_PARAGRAPHS)
] * 5


PUBLIC_RETURN_POLICY_PDF_BYTES = _build_pdf(_PUBLIC_PAGE_TEXTS)
INTERNAL_OPERATIONS_HANDBOOK_PDF_BYTES = _build_pdf(_INTERNAL_PAGE_TEXTS)

# The endpoint derives the document title from the client filename.
PUBLIC_RETURN_POLICY_FILENAME = "Public Return Policy.pdf"
INTERNAL_OPERATIONS_HANDBOOK_FILENAME = "Internal Operations Handbook.pdf"


def _self_validate() -> None:
    """Prove both PDFs are usable before any test relies on them."""

    for label, pdf_bytes, marker, other_marker in (
        (
            "public",
            PUBLIC_RETURN_POLICY_PDF_BYTES,
            _PUBLIC_MARKER,
            _INTERNAL_MARKER,
        ),
        (
            "internal",
            INTERNAL_OPERATIONS_HANDBOOK_PDF_BYTES,
            _INTERNAL_MARKER,
            _PUBLIC_MARKER,
        ),
    ):
        extracted = extract_text(pdf_bytes)

        assert marker in extracted, f"{label} marker lost during extraction"
        assert other_marker not in extracted, (
            f"{label} text must not embed the other document's marker"
        )

        chunks = chunk_text(extracted)

        assert len(chunks) >= 2, (
            f"{label} PDF produced {len(chunks)} chunk(s); "
            "the acceptance run requires multiple chunks per document"
        )
        assert marker in " ".join(chunks), (
            f"{label} marker lost during chunking"
        )


_self_validate()
del _self_validate
