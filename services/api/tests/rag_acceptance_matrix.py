"""Phase 6.5 question matrix: 10 questions, eligible roles, expectations.

Roadmap Phase 6.5: "test 10 questions — verify citations exist, verify
internal docs do NOT appear for the customer role", merged with 6.6
(low-evidence honesty) as one acceptance run.

This module is pure data plus two small deterministic helpers
(normalization and fact matching). It never touches the database,
Storage, the network, or application services at import time.

The matrix is built against the two documents seeded in the local
knowledge base ("ShopPilot_Public_Returns_Policy_Test", public, and
"ShopPilot_Internal_Support_Handbook_Test", internal — each exactly one
chunk). Expected facts were verified against the actual stored chunk
content; ``test_rag_acceptance.py`` re-verifies every fact against the
live database content before any generation call runs, so the matrix
can never silently drift away from the documents it tests.

Roles
-----

- ``customer``          -> public documents only (Phase 6.1 role map),
- ``support_agent``/``admin`` -> public + internal.

Expectations
------------

- ``answer``: the response must state the expected facts (tolerant
  normalized matching), carry at least one citation, and cite a chunk
  whose stored content actually contains the expected facts
  (grounding — a citation marker alone proves nothing).
- ``refusal``: the response must be EXACTLY
  ``INSUFFICIENT_EVIDENCE_MESSAGE`` (strict, per the approved Phase 6.5
  design) with no citations.

Visibility guards: for every customer-role case, a fixed set of
internal-only sentinel tokens must never appear in the answer text or
in the rendered sources text, and no internal document may appear in
retrieval, prompt, or citations — even though the internal chunk is the
closest retrieval match for the internal-only questions (verified live
in the control test).
"""

import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Documents (titles exactly as seeded in the local knowledge base)
# ---------------------------------------------------------------------------

PUBLIC_DOCUMENT_TITLE = "ShopPilot_Public_Returns_Policy_Test"
INTERNAL_DOCUMENT_TITLE = "ShopPilot_Internal_Support_Handbook_Test"

PUBLIC = "public"
INTERNAL = "internal"

CUSTOMER = "customer"
SUPPORT_AGENT = "support_agent"
ADMIN = "admin"

# Internal-only sentence fragments. Verified by the acceptance self-check
# to appear in the internal document and in NO public document, so any
# occurrence in a customer-facing answer is a hard visibility violation.
CUSTOMER_FORBIDDEN_TOKENS = (
    "usd 50",
    "order ownership",
    "authenticated",
    "automated decision",
    "human review",
    "internal support",
)

# ---------------------------------------------------------------------------
# Text normalization and fact matching
# ---------------------------------------------------------------------------

_DASH_TRANSLATION = str.maketrans(
    {
        # The ingestion pipeline replaced an en dash with U+FFFD in the
        # "3–7 business days" sentence; treat it (and real dashes) as "-".
        "\ufffd": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
        "\u00a0": " ",
    }
)


def normalize(text: object) -> str:
    """Lowercase, translate dash variants, and collapse whitespace.

    Also removes spaces around hyphens so "3 - 7 business days" matches
    the stored "3<dash>7 business days" fact.
    """

    if not isinstance(text, str):
        return ""

    lowered = text.lower().translate(_DASH_TRANSLATION)
    collapsed = " ".join(lowered.split())
    return re.sub(r"\s*-\s*", "-", collapsed)


def matches_fact_groups(normalized_text: str, fact_groups) -> bool:
    """True when every fact group has at least one alternative present.

    `normalized_text` must already be normalized (``normalize``); each
    alternative is normalized on the fly, so matrix entries stay readable.
    """

    if not isinstance(normalized_text, str) or not fact_groups:
        return False

    return all(
        any(normalize(alternative) in normalized_text for alternative in group)
        for group in fact_groups
    )


# ---------------------------------------------------------------------------
# Expectations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Expectation:
    """What one role's response must satisfy for one question.

    ``kind`` is ``"answer"`` (grounded, cited) or ``"refusal"`` (the
    exact insufficient-evidence message, no citations). ``document`` is
    the expected source document visibility for answers (``None`` for
    refusals, which must cite nothing). ``fact_groups`` is a tuple of
    alternative-tuples: every group needs at least one matching
    alternative in the answer, and — for the grounding check — in the
    content of at least one cited chunk.
    """

    kind: str
    document: str | None = None
    fact_groups: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class MatrixQuestion:
    """One of the ten Phase 6.5 questions with its eligible roles.

    ``customer`` is the expectation for the customer role (``None`` to
    skip that role for this question); ``staff`` is the shared
    expectation for ``support_agent`` and ``admin`` (``None`` to skip
    both staff roles). Asking each question only of its eligible roles
    keeps the live run at 17 generations instead of 30.
    """

    id: str
    text: str
    customer: Expectation | None
    staff: Expectation | None


# Fact groups verified against the stored chunk content by the
# acceptance self-check (test_matrix_matches_seeded_knowledge_base).
_Q1_FACT_GROUPS = (("30 calendar days", "thirty calendar days", "30 days"),)
_Q2_FACT_GROUPS = (
    ("not refundable", "non-refundable", "only if", "only when", "unless"),
    ("damaged", "wrong item", "incorrect item"),
)
_Q3_FACT_GROUPS = (
    ("final sale",),
    (
        "not eligible",
        "not returnable",
        "cannot be returned",
        "can't be returned",
        "cannot",
        "no,",
    ),
)
_Q4_FACT_GROUPS = (
    ("3-7 business days", "3 to 7 business days", "three to seven business days"),
)
_Q5_FACT_GROUPS = (("7 calendar days", "seven calendar days"),)
_Q6_FACT_GROUPS = (
    ("shipped",),
    ("return process", "returns process", "request a return", "return the item"),
)
_Q7_FACT_GROUPS = (("usd 50", "$50", " 50"), ("human review", "review"))
_Q8_FACT_GROUPS = (("verify", "authenticated"), ("order ownership",))
_Q9_FACT_GROUPS = (
    ("escalat",),
    ("unusual", "conflicting", "high-value", "high value", "fraud", "policy exception"),
)


QUESTION_MATRIX = (
    MatrixQuestion(
        id="Q1",
        text="How many days does a customer have to request a return?",
        customer=Expectation("answer", PUBLIC, _Q1_FACT_GROUPS),
        staff=None,
    ),
    MatrixQuestion(
        id="Q2",
        text="When are shipping charges refundable?",
        customer=Expectation("answer", PUBLIC, _Q2_FACT_GROUPS),
        staff=None,
    ),
    MatrixQuestion(
        id="Q3",
        text="Can a final-sale item be returned?",
        customer=Expectation("answer", PUBLIC, _Q3_FACT_GROUPS),
        staff=None,
    ),
    MatrixQuestion(
        id="Q4",
        text="What is the standard delivery estimate?",
        customer=Expectation("answer", PUBLIC, _Q4_FACT_GROUPS),
        staff=None,
    ),
    MatrixQuestion(
        id="Q5",
        text="How soon must a damaged or incorrect item be reported?",
        customer=Expectation("answer", PUBLIC, _Q5_FACT_GROUPS),
        staff=None,
    ),
    MatrixQuestion(
        id="Q6",
        text="Can I cancel an order after it has been shipped?",
        customer=Expectation("answer", PUBLIC, _Q6_FACT_GROUPS),
        staff=None,
    ),
    # Internal-only about the handbook: the internal chunk is the
    # closest retrieval match (verified live), so a customer answer
    # must be the exact refusal with no internal facts leaked.
    MatrixQuestion(
        id="Q7",
        text="Which refund amount requires human review in this test document?",
        customer=Expectation("refusal"),
        staff=Expectation("answer", INTERNAL, _Q7_FACT_GROUPS),
    ),
    MatrixQuestion(
        id="Q8",
        text="What should support staff verify before discussing a refund?",
        customer=Expectation("refusal"),
        staff=Expectation("answer", INTERNAL, _Q8_FACT_GROUPS),
    ),
    MatrixQuestion(
        id="Q9",
        text="Which cases should be escalated?",
        customer=None,
        staff=Expectation("answer", INTERNAL, _Q9_FACT_GROUPS),
    ),
    # Unanswerable from BOTH documents (roadmap 6.6, merged into 6.5):
    # warranty is mentioned only as "follow the booklet that ships with
    # the item" in other sample content — no duration exists in either
    # document, so any assertion is fabrication. Strict exact refusal
    # for every role.
    MatrixQuestion(
        id="Q10",
        text="What is the warranty period for products?",
        customer=Expectation("refusal"),
        staff=Expectation("refusal"),
    ),
)


@dataclass(frozen=True)
class AcceptanceCase:
    """One <question, role> run of the matrix."""

    case_id: str
    question_id: str
    question: str
    role: str
    expectation: Expectation


def acceptance_cases() -> tuple[AcceptanceCase, ...]:
    """Expand the matrix into the 17 concrete live-run cases."""

    cases: list[AcceptanceCase] = []

    for question in QUESTION_MATRIX:
        if question.customer is not None:
            cases.append(
                AcceptanceCase(
                    f"{question.id}-{CUSTOMER}",
                    question.id,
                    question.text,
                    CUSTOMER,
                    question.customer,
                )
            )

        for staff_role in (SUPPORT_AGENT, ADMIN):
            if question.staff is not None:
                cases.append(
                    AcceptanceCase(
                        f"{question.id}-{staff_role}",
                        question.id,
                        question.text,
                        staff_role,
                        question.staff,
                    )
                )

    return tuple(cases)
