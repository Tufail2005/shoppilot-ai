"""Phase 6.5 live RAG acceptance run (opt-in).

Roadmap requirement for this phase:

    6.5  Test 10 questions: verify citations exist, verify internal
    docs do NOT appear for customer role
    6.6  Test low-evidence: honest "not enough information" response

The two requirements run as one acceptance matrix (10 questions, 17
``<question, role>`` cases) against the REAL local Supabase database
and the REAL Gemini providers:

- the requests run through the real ``POST /rag/query`` endpoint with
  real role resolution (``require_role`` over ``public.profiles``),
- query embeddings are produced by the real Gemini embedding API,
- answers are produced by the real Gemini generation API through a
  recorder that WRAPS the real provider (an overridden dependency,
  never a fake answer source),
- retrieval is wrapped the same way (it delegates to the real service
  and records the retrieved chunks) so the evidence covers what
  reached the model, not only what the model chose to cite.

Read-only knowledge base (approved Phase 6.5 decision): the run uses
the two SEEDED documents and never uploads, updates, or deletes
anything. No Storage dependency is installed.

Opt-in and prerequisites (this module makes real Gemini API calls and
is therefore deliberately gated):

- ``RAG_ACCEPTANCE=1`` — otherwise the module skips;
- local Supabase stack running (``tests/conftest.py`` gates the DB);
- ``GEMINI_API_KEY`` configured — a missing/unusable or unresponsive
  provider skips the whole module ("live run pending, never faked",
  the Phase 5.10 precedent);
- the seeded knowledge base must contain exactly the two expected
  ``ready`` documents, each with exactly one 768-dimension chunk:
    * document(s) missing                         -> skip,
    * other ready documents present (baseline)    -> skip,
    * wrong status/visibility/chunk count/dims    -> FAIL (a
      configuration mismatch is never skipped).

Once the acceptance cases begin, a provider/pipeline failure (any
non-200 response mapped by Phase 6.4) FAILS the run and is reported as
a pipeline failure, separately from answer-quality failures. A
provider outage is never reported as an answer defect, and vice versa.

Generation HTTP-timeout hardening (test-only): the Phase 6.3 factory
creates its Gemini client without an HTTP timeout, so a provider stall
hangs the request — and with it the whole pytest process — forever.
The acceptance run validates the production configuration through the
real factory, but then constructs the SAME
``GeminiGenerationProvider`` adapter with a bounded HTTP client
(``_GENERATION_HTTP_TIMEOUT_MS``) so a stalled generation becomes a pipeline
failure instead of an indefinite hang. The adapter, prompt building,
citation validation, and settings are the production path unchanged;
only the client timeout is hardened here. Embeddings keep the real,
unoverridden dependency (they have been reliable throughout; a stall
there remains accepted residual risk).

Recorded evaluation dimensions (per case, separately):

- ``answer_correct``: expected facts present (tolerant normalized
  matching), or the EXACT ``INSUFFICIENT_EVIDENCE_MESSAGE`` for
  refusals (strict, per the approved design),
- ``citation_present``: answer cases need citations derived from the
  ``[n]`` markers in the answer; refusal cases need none; response
  body shape is enforced here,
- ``citation_document``: cited documents are a subset of the role's
  visible documents, include the expected source document, and every
  cited ``chunk_id`` maps to a chunk that actually reached the prompt,
- ``grounded_in_cited_chunk``: at least one CITED chunk's stored
  content actually contains the expected facts — a citation marker
  alone proves the numbering syntax, not factual grounding,
- ``visibility``: customer cases must never see internal documents in
  retrieval, prompt, citations, or answer prose (internal-only
  sentinel tokens), even though the internal chunk is the closest
  retrieval match for the internal-only questions (proved live by the
  control test); staff cases must reach the internal document for
  internal questions.

Hygiene:

- only synthetic acceptance data is printed (model answers, document
  titles, distances) — never stored chunk content or storage paths;
- the module teardown clears every dependency override and the
  retrieval patch it installed, so later test modules stay unaffected;
- pre-existing documents are never modified.
"""

import asyncio
import os
import re

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.auth import get_current_user
from app.core.config import get_settings
from app.main import app
from app.services import rag as rag_service
from app.services import retrieval as retrieval_service
from app.services.embeddings import (
    EMBEDDING_DIMENSIONS,
    EmbeddingError,
    create_gemini_provider,
)
from app.services.generation import (
    GenerationError,
    create_gemini_generation_provider,
)
from app.services.rag_prompt import (
    INSUFFICIENT_EVIDENCE_MESSAGE,
    build_rag_prompt,
)
from tests.conftest import TEST_USERS, _override_get_current_user
from tests.rag_acceptance_matrix import (
    CUSTOMER,
    CUSTOMER_FORBIDDEN_TOKENS,
    INTERNAL,
    INTERNAL_DOCUMENT_TITLE,
    PUBLIC,
    PUBLIC_DOCUMENT_TITLE,
    QUESTION_MATRIX,
    AcceptanceCase,
    acceptance_cases,
    matches_fact_groups,
    normalize,
)

# Per-case recorded results: case_id -> evaluation dict. Populated by
# the parametrized case test; consumed by the final report test.
RESULTS: dict[str, dict] = {}

_DIMENSIONS = (
    "answer_correct",
    "citation_present",
    "citation_document",
    "grounded_in_cited_chunk",
    "visibility",
)

# The expected seeded baseline documents, title -> visibility.
_EXPECTED_DOCUMENTS = (
    (PUBLIC_DOCUMENT_TITLE, PUBLIC),
    (INTERNAL_DOCUMENT_TITLE, INTERNAL),
)

_OPT_IN_ENV = "RAG_ACCEPTANCE"
_EMBED_PROBE_TIMEOUT_S = 60.0
_GENERATION_PROBE_TIMEOUT_S = 90.0
# Test-only hardening (see module docstring): the production factory
# leaves the HTTP client unbounded; the acceptance run bounds it so a
# provider stall fails a case instead of hanging the pytest process.
# Unit note: the client-level ``HttpOptions.timeout`` is milliseconds.
_GENERATION_HTTP_TIMEOUT_MS = 60_000

# One synthetic chunk so the availability probe can exercise the real
# generation adapter (a prompt without sources is refused by design).
_PROBE_CHUNK = {
    "id": "00000000-0000-4000-8000-000000000001",
    "document_id": "00000000-0000-4000-8000-000000000002",
    "title": "Phase 6.5 synthetic probe chunk",
    "visibility": "public",
    "chunk_index": 0,
    "content": "Synthetic availability probe. Reply with the single word OK.",
    "distance": 0.0,
}


# ---------------------------------------------------------------------------
# Environment fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def phase_6_5_opt_in():
    """Skip the whole module unless the live run is explicitly requested."""

    if os.environ.get(_OPT_IN_ENV) != "1":
        pytest.skip(
            "Phase 6.5 acceptance is opt-in and makes live Gemini API "
            f"calls: set {_OPT_IN_ENV}=1 to run it."
        )


def _connect() -> psycopg.Connection:
    return psycopg.Connection.connect(
        os.environ["DATABASE_URL"], connect_timeout=5
    )


@pytest.fixture(scope="module")
def kb_documents(client: TestClient) -> dict[str, dict]:
    """Read-only preflight over the two seeded knowledge-base documents.

    Missing documents and an unexpected baseline skip the module
    precisely; wrong status, visibility, chunk count, empty content,
    or embedding dimensions are configuration mismatches and FAIL.
    """

    return _preflight_documents()


def _preflight_documents() -> dict[str, dict]:
    titles = [title for title, _ in _EXPECTED_DOCUMENTS]

    with _connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT d.id::text, d.title, d.visibility, d.status"
            " FROM public.documents d"
            " WHERE d.title = ANY(%s)",
            (titles,),
        )
        document_rows = cursor.fetchall()

        cursor.execute(
            "SELECT d.id::text, c.id::text, c.chunk_index,"
            "       c.embedding IS NOT NULL, vector_dims(c.embedding),"
            "       length(c.content), c.content"
            " FROM public.documents d"
            " JOIN public.document_chunks c ON c.document_id = d.id"
            " WHERE d.title = ANY(%s)"
            " ORDER BY d.id, c.chunk_index",
            (titles,),
        )
        chunk_rows = cursor.fetchall()

        cursor.execute(
            "SELECT title FROM public.documents WHERE status = 'ready'"
        )
        ready_titles = [row[0] for row in cursor.fetchall()]

    documents_by_title = {
        row[1]: {
            "id": row[0],
            "title": row[1],
            "visibility": row[2],
            "status": row[3],
        }
        for row in document_rows
    }

    missing = [
        title for title, _ in _EXPECTED_DOCUMENTS if title not in documents_by_title
    ]
    if missing:
        pytest.skip(
            "Phase 6.5 requires the two seeded knowledge-base documents; "
            f"missing: {missing}. Seed them via POST /admin/documents "
            f"({PUBLIC_DOCUMENT_TITLE!r} public, uploaded by admin; "
            f"{INTERNAL_DOCUMENT_TITLE!r} internal, uploaded by staff) "
            "and rerun the acceptance."
        )

    owned_titles = {title for title, _ in _EXPECTED_DOCUMENTS}
    extras = sorted(
        title for title in ready_titles if title not in owned_titles
    )
    if extras:
        pytest.skip(
            "Phase 6.5 expects a knowledge base baseline of exactly the two "
            f"seeded documents; unexpected ready document(s) present: {extras}"
        )

    chunks_by_document: dict[str, list[dict]] = {}
    for row in chunk_rows:
        chunks_by_document.setdefault(row[0], []).append(
            {
                "chunk_id": row[1],
                "chunk_index": row[2],
                "has_embedding": row[3],
                "dimensions": row[4],
                "content_length": row[5],
                "content": row[6],
            }
        )

    documents: dict[str, dict] = {}

    for title, visibility in _EXPECTED_DOCUMENTS:
        row = documents_by_title[title]
        chunks = chunks_by_document.get(row["id"], [])

        if row["status"] != "ready":
            pytest.fail(
                f"Phase 6.5 preflight: document {title!r} has status "
                f"{row['status']!r}; expected 'ready'"
            )

        if row["visibility"] != visibility:
            pytest.fail(
                f"Phase 6.5 preflight: document {title!r} has visibility "
                f"{row['visibility']!r}; expected {visibility!r}"
            )

        if len(chunks) != 1:
            pytest.fail(
                f"Phase 6.5 preflight: document {title!r} has "
                f"{len(chunks)} chunk(s); the matrix baseline expects "
                "exactly 1"
            )

        chunk = chunks[0]

        if not chunk["has_embedding"] or chunk["dimensions"] != EMBEDDING_DIMENSIONS:
            pytest.fail(
                f"Phase 6.5 preflight: chunk of {title!r} has embedding "
                f"present={chunk['has_embedding']}, dimensions="
                f"{chunk['dimensions']}; expected {EMBEDDING_DIMENSIONS}"
            )

        if not chunk["content_length"]:
            pytest.fail(
                f"Phase 6.5 preflight: chunk of {title!r} has empty content"
            )

        documents[visibility] = {
            "id": row["id"],
            "title": title,
            "visibility": visibility,
            "status": row["status"],
            "chunk_id": chunk["chunk_id"],
            "content": chunk["content"],
        }

    return documents


def _bounded_generation_provider():
    """The production generation adapter with a bounded HTTP client.

    Configuration is validated through the REAL factory first (missing
    key/model problems surface exactly as production would); the
    returned provider then uses a client with
    ``_GENERATION_HTTP_TIMEOUT_MS`` so a stalled provider fails the
    case rather than hanging the process.
    """

    # Production configuration validation (unchanged factory).
    create_gemini_generation_provider()

    from google import genai

    from app.services.generation import GeminiGenerationProvider

    settings = get_settings()
    key = settings.gemini_api_key
    api_key = key.get_secret_value() if key is not None else ""

    client = genai.Client(
        api_key=api_key,
        http_options={
            "timeout": _GENERATION_HTTP_TIMEOUT_MS,
            # Test-only: a stalled request must fail once at the timeout,
            # not multiply through the SDK's internal retries.
            "retry_options": {"attempts": 1},
        },
    )
    return GeminiGenerationProvider(
        client, model=settings.gemini_generation_model
    )


async def _run_probes(embedding_provider, generation_provider) -> None:
    await asyncio.wait_for(
        asyncio.to_thread(
            embedding_provider.embed_query,
            ["Phase 6.5 availability probe"],
        ),
        _EMBED_PROBE_TIMEOUT_S,
    )

    prompt = build_rag_prompt(
        "Availability probe: reply with the single word OK.",
        [dict(_PROBE_CHUNK)],
    )
    await asyncio.wait_for(
        asyncio.to_thread(generation_provider.generate, prompt),
        _GENERATION_PROBE_TIMEOUT_S,
    )


def _probe_providers(embedding_provider, generation_provider) -> None:
    """One bounded live probe of both real providers.

    A provider that is missing, unusable, or unresponsive skips the
    module ("live run pending, never faked"). Once the acceptance
    cases begin, provider failures fail the run instead.
    """

    try:
        asyncio.run(_run_probes(embedding_provider, generation_provider))
    except asyncio.TimeoutError:
        pytest.skip(
            "Phase 6.5 skipped: the generation provider did not respond "
            f"within {_GENERATION_PROBE_TIMEOUT_S:.0f}s (the configured "
            "model may be unavailable; rerun later or with an explicit "
            "GEMINI_GENERATION_MODEL override)"
        )
    except (EmbeddingError, GenerationError) as exc:
        pytest.skip(
            "Phase 6.5 skipped: provider unavailable "
            f"({type(exc).__name__}, stage={getattr(exc, 'stage', '-')})"
        )


class _RecordingGenerationProvider:
    """Wraps the REAL generation provider and records every prompt.

    The wrapped provider is the production adapter — the override only
    adds evidence capture, never an answer source.
    """

    def __init__(self, provider):
        self._provider = provider
        self.prompts: list = []

    def generate(self, prompt):
        self.prompts.append(prompt)
        return self._provider.generate(prompt)


class _CaseRecord:
    """Everything captured for one completed acceptance case."""

    def __init__(self, case: AcceptanceCase, model: str):
        self.case = case
        self.model = model
        self.status_code: int | None = None
        self.body: object = None
        self.prompt = None
        self.retrieval_chunks: list[dict] | None = None


class _CaseRunner:
    """Runs each matrix case once through the real endpoint."""

    def __init__(self, http: TestClient, documents: dict[str, dict], recorder, retrieval_log, model: str):
        self.http = http
        self.documents = documents
        self.recorder = recorder
        self.retrieval_log = retrieval_log
        self.model = model
        self.records: dict[str, _CaseRecord] = {}

    def record(self, case: AcceptanceCase) -> _CaseRecord:
        existing = self.records.get(case.case_id)
        if existing is not None:
            return existing

        record = _CaseRecord(case, self.model)
        prompt_index = len(self.recorder.prompts)
        retrieval_index = len(self.retrieval_log)

        app.dependency_overrides[get_current_user] = _override_get_current_user(
            TEST_USERS[case.role]
        )
        response = self.http.post("/rag/query", json={"query": case.question})

        record.status_code = response.status_code
        try:
            record.body = response.json()
        except ValueError:
            record.body = None

        if len(self.recorder.prompts) > prompt_index:
            record.prompt = self.recorder.prompts[prompt_index]

        if len(self.retrieval_log) > retrieval_index:
            record.retrieval_chunks = list(
                self.retrieval_log[retrieval_index]["chunks"]
            )

        self.records[case.case_id] = record
        return record


@pytest.fixture(scope="module")
def runner(client: TestClient, kb_documents: dict[str, dict]):
    """Real providers + evidence recorders for the whole module.

    The prompt recording provider delegates to the real generation
    adapter, and the retrieval wrapper delegates to the real
    ``retrieve()`` through the real embed/SQL path — the overrides
    only capture evidence. Teardown clears every override and the
    retrieval patch, so later test modules never observe leftovers.
    """

    settings = get_settings()

    try:
        embedding_provider = create_gemini_provider()
    except EmbeddingError as exc:
        pytest.skip(
            f"Phase 6.5 skipped: embedding provider unusable "
            f"({type(exc).__name__})"
        )

    try:
        generation_provider = _bounded_generation_provider()
    except GenerationError as exc:
        pytest.skip(
            "Phase 6.5 skipped: generation provider unusable "
            f"(stage={exc.stage})"
        )

    _probe_providers(embedding_provider, generation_provider)

    recorder = _RecordingGenerationProvider(generation_provider)
    app.dependency_overrides[rag_service.get_generation_provider] = (
        lambda: recorder
    )

    patcher = pytest.MonkeyPatch()
    original_retrieve = retrieval_service.retrieve
    retrieval_log: list[dict] = []

    async def recording_retrieve(*args, **kwargs):
        chunks = await original_retrieve(*args, **kwargs)
        retrieval_log.append(
            {
                "query": kwargs.get("query"),
                "user_role": kwargs.get("user_role"),
                "chunks": list(chunks),
            }
        )
        return chunks

    patcher.setattr(retrieval_service, "retrieve", recording_retrieve)

    try:
        yield _CaseRunner(
            client,
            kb_documents,
            recorder,
            retrieval_log,
            settings.gemini_generation_model,
        )
    finally:
        patcher.undo()
        app.dependency_overrides.clear()


CASES: tuple[AcceptanceCase, ...] = acceptance_cases()
CASE_BY_ID: dict[str, AcceptanceCase] = {
    case.case_id: case for case in CASES
}


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _evaluate(
    case: AcceptanceCase,
    record: _CaseRecord,
    documents: dict[str, dict],
) -> dict:
    """Compute the five recordable dimensions plus diagnostics."""

    evaluation = {dimension: None for dimension in _DIMENSIONS}
    evaluation.update(
        pipeline_ok=True,
        answer="",
        cited_titles=[],
        retrieval_titles=[],
        leak_tokens=[],
    )

    if record.status_code != 200:
        evaluation["pipeline_ok"] = False
        return evaluation

    body = record.body if isinstance(record.body, dict) else {}
    answer = body.get("answer", "")
    citations = body.get("citations", [])
    if not isinstance(citations, list):
        citations = []

    evaluation["answer"] = str(answer)
    evaluation["cited_titles"] = [
        citation.get("document_title", "")
        for citation in citations
        if isinstance(citation, dict)
    ]
    evaluation["retrieval_titles"] = [
        str(chunk.get("title", "")) for chunk in (record.retrieval_chunks or [])
    ]

    expectation = case.expectation
    public_id = documents[PUBLIC]["id"]
    internal_id = documents[INTERNAL]["id"]
    visible_ids = (
        {public_id}
        if case.role == CUSTOMER
        else {public_id, internal_id}
    )

    prompt_sources = record.prompt.sources if record.prompt is not None else ()
    prompt_chunk_ids = {str(source.chunk_id) for source in prompt_sources}
    prompt_source_contents = {
        str(source.chunk_id): source.content for source in prompt_sources
    }

    shape_ok = set(body) == {"answer", "citations"} and all(
        isinstance(citation, dict)
        and set(citation)
        == {"number", "chunk_id", "document_id", "document_title", "chunk_index"}
        for citation in citations
    )

    # 1. answer correctness (tolerant facts; exact refusal wording)
    if expectation.kind == "refusal":
        evaluation["answer_correct"] = (
            str(answer).strip() == INSUFFICIENT_EVIDENCE_MESSAGE
        )
    else:
        evaluation["answer_correct"] = matches_fact_groups(
            normalize(str(answer)),
            expectation.fact_groups,
        )

    # 2. citation presence (body shape folded in; failure details name it)
    if expectation.kind == "answer":
        evaluation["citation_present"] = (
            shape_ok
            and bool(citations)
            and re.search(r"\[\d+\]", str(answer)) is not None
        )
    else:
        evaluation["citation_present"] = shape_ok and citations == []

    # 3. citation document identity
    if expectation.kind == "answer":
        cited_ids = {
            str(citation.get("document_id"))
            for citation in citations
            if isinstance(citation, dict)
        }
        citations_reach_retrieved_chunks = (
            bool(prompt_chunk_ids)
            and all(
                str(citation.get("chunk_id")) in prompt_chunk_ids
                for citation in citations
                if isinstance(citation, dict)
            )
        )
        evaluation["citation_document"] = (
            documents[expectation.document]["id"] in cited_ids
            and cited_ids.issubset(visible_ids)
            and citations_reach_retrieved_chunks
        )

    # 4. grounding: at least one CITED chunk contains the expected facts
    if expectation.kind == "answer":
        evaluation["grounded_in_cited_chunk"] = bool(citations) and any(
            matches_fact_groups(
                normalize(prompt_source_contents.get(str(citation.get("chunk_id")), "")),
                expectation.fact_groups,
            )
            for citation in citations
            if isinstance(citation, dict)
        )

    # 5. visibility
    retrieval_chunks = record.retrieval_chunks or []
    sources_text = (
        record.prompt.sources_text if record.prompt is not None else ""
    )

    if case.role == CUSTOMER:
        normalized_answer = normalize(str(answer))
        leak_tokens = [
            token
            for token in CUSTOMER_FORBIDDEN_TOKENS
            if token in normalized_answer or token in normalize(sources_text)
        ]
        evaluation["leak_tokens"] = leak_tokens
        evaluation["visibility"] = bool(
            record.retrieval_chunks is not None
            and record.prompt is not None
            and all(
                str(chunk.get("document_id")) != internal_id
                for chunk in retrieval_chunks
            )
            and all(
                chunk.get("visibility") == PUBLIC for chunk in retrieval_chunks
            )
            and all(
                str(source.document_id) != internal_id
                for source in prompt_sources
            )
            and all(
                str(citation.get("document_id")) != internal_id
                for citation in citations
                if isinstance(citation, dict)
            )
            and not leak_tokens
        )
    elif expectation.document == INTERNAL:
        evaluation["visibility"] = bool(
            record.retrieval_chunks is not None
            and record.prompt is not None
            and any(
                str(chunk.get("document_id")) == internal_id
                for chunk in retrieval_chunks
            )
            and any(
                str(source.document_id) == internal_id
                for source in prompt_sources
            )
        )
    else:
        evaluation["visibility"] = bool(
            record.retrieval_chunks is not None
            and record.prompt is not None
        )

    return evaluation


def _case_failure_message(
    record: _CaseRecord,
    evaluation: dict,
    failed_dimensions: list[str],
) -> str:
    lines = [
        "Phase 6.5 case "
        f"{record.case.case_id!r} failed dimension(s): "
        f"{', '.join(failed_dimensions)}",
        f"  role={record.case.role} expectation={record.case.expectation.kind}",
        f"  HTTP {record.status_code}",
        f"  answer: {evaluation['answer']!r}",
        f"  cited documents: {evaluation['cited_titles']}",
        f"  retrieved documents: {evaluation['retrieval_titles']}",
        f"  leak tokens: {evaluation['leak_tokens']}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_matrix_matches_seeded_knowledge_base(kb_documents: dict[str, dict]):
    """Prove the matrix aligns with the real documents BEFORE running.

    Every expected fact group must exist in the expected document's
    stored content, and every internal-only sentinel must be internal
    (never in the public document), so the leak guard is meaningful.
    """

    public_content = normalize(kb_documents[PUBLIC]["content"])
    internal_content = normalize(kb_documents[INTERNAL]["content"])

    for token in CUSTOMER_FORBIDDEN_TOKENS:
        assert token in internal_content, (
            f"matrix drift: internal sentinel {token!r} was not found in "
            "the internal document; the customer leak guard would be "
            "meaningless"
        )
        assert token not in public_content, (
            f"matrix drift: sentinel {token!r} also appears in the public "
            "document; it cannot be treated as internal-only"
        )

    for question in QUESTION_MATRIX:
        for role_label, expectation in (
            ("customer", question.customer),
            ("staff", question.staff),
        ):
            if expectation is None or expectation.kind == "refusal":
                continue

            content = normalize(kb_documents[expectation.document]["content"])

            for group_index, group in enumerate(
                expectation.fact_groups, start=1
            ):
                matched = [
                    alternative
                    for alternative in group
                    if normalize(alternative) in content
                ]
                assert matched, (
                    f"matrix drift: {question.id} ({role_label}) fact group "
                    f"{group_index} has no alternative present in the "
                    f"{expectation.document} document; alternatives="
                    f"{group!r}"
                )


@pytest.mark.parametrize(
    "case", CASES, ids=[case.case_id for case in CASES]
)
def test_phase_6_5_acceptance_case(case: AcceptanceCase, runner: _CaseRunner):
    """Run one matrix case and assert its five dimensions separately."""

    record = runner.record(case)
    evaluation = _evaluate(case, record, runner.documents)
    RESULTS[case.case_id] = evaluation

    if not evaluation["pipeline_ok"]:
        body = record.body if isinstance(record.body, dict) else {}
        pytest.fail(
            "Phase 6.5 pipeline/provider failure (not an answer-quality "
            f"defect) in case {case.case_id!r}: HTTP {record.status_code}, "
            f"detail={body.get('detail')!r}",
            pytrace=False,
        )

    failed = [
        dimension for dimension in _DIMENSIONS if evaluation[dimension] is False
    ]
    assert not failed, _case_failure_message(record, evaluation, failed)


def test_internal_nearest_chunk_stays_hidden_from_customer(
    runner: _CaseRunner,
):
    """The Phase 6.5 core control, proven with recorded distances.

    For Q7 the internal chunk is the CLOSEST retrieval match. The
    staff run must retrieve it at rank 1 while the customer run must
    never retrieve it — the visibility filter decides what each role
    sees, not relevance.
    """

    customer_record = runner.record(CASE_BY_ID["Q7-customer"])
    agent_record = runner.record(CASE_BY_ID["Q7-support_agent"])

    customer_chunks = customer_record.retrieval_chunks or []
    agent_chunks = agent_record.retrieval_chunks or []

    assert customer_record.prompt is not None and agent_record.prompt is not None, (
        "the control test requires recorded retrieval results for both "
        "roles; the acceptance case requests must have produced them"
    )

    internal_id = runner.documents[INTERNAL]["id"]

    assert agent_chunks, "staff retrieval must return chunks for Q7"
    assert str(agent_chunks[0]["document_id"]) == internal_id, (
        "control precondition drifted: the internal chunk is no longer "
        "the closest retrieval match for Q7; revalidate the control "
        "question against the seeded documents"
    )
    internal_distance = agent_chunks[0]["distance"]

    assert customer_chunks, "customer retrieval must return the public chunk"
    assert all(
        str(chunk["document_id"]) != internal_id
        for chunk in customer_chunks
    ), (
        "VISIBILITY VIOLATION: the internal document reached customer "
        "retrieval for Q7"
    )

    customer_best_distance = min(
        chunk["distance"] for chunk in customer_chunks
    )
    assert internal_distance < customer_best_distance, (
        "control precondition drifted: the hidden internal chunk "
        f"(distance {internal_distance}) is not closer than the best "
        f"visible chunk (distance {customer_best_distance})"
    )

    print(
        "Phase 6.5 control (Q7): internal chunk was the closest match "
        f"(distance {internal_distance:.4f}) yet stayed hidden from the "
        f"customer, whose best visible chunk has distance "
        f"{customer_best_distance:.4f}."
    )


def test_zz_phase_6_5_matrix_report(runner: _CaseRunner):
    """Record-completeness gate plus the separately dimensioned report."""

    recorded_ids = set(RESULTS)
    missing = sorted({case.case_id for case in CASES} - recorded_ids)
    assert not missing, (
        f"Phase 6.5 report: cases never evaluated: {missing}"
    )

    violations = sorted(
        case_id
        for case_id, evaluation in RESULTS.items()
        if evaluation.get("visibility") is False
    )
    assert not violations, (
        f"Phase 6.5 VISIBILITY VIOLATIONS across the run: {violations}"
    )

    quality_failures = sorted(
        f"{case_id}:{dimension}"
        for case_id, evaluation in RESULTS.items()
        for dimension in _DIMENSIONS
        if dimension != "visibility" and evaluation.get(dimension) is False
    )
    assert not quality_failures, (
        f"Phase 6.5 answer-quality failures across the run: "
        f"{quality_failures}"
    )

    def cell(value):
        if value is True:
            return "ok"
        if value is False:
            return "FAIL"
        if value is None:
            return "n/a"
        return str(value)

    print(
        f"\nPhase 6.5 acceptance matrix (generation model: {runner.model})"
    )
    header = (
        f"{'case':<22}{'role':<15}{'expect':<9}{'pipe':<6}{'answer':<8}"
        f"{'cit.':<6}{'cdoc':<6}{'ground':<8}{'visib':<7}"
    )
    print(header)
    print("-" * len(header))

    for case in CASES:
        evaluation = RESULTS[case.case_id]
        print(
            f"{case.case_id:<22}{case.role:<15}{case.expectation.kind:<9}"
            f"{cell(evaluation.get('pipeline_ok')):<6}"
            f"{cell(evaluation.get('answer_correct')):<8}"
            f"{cell(evaluation.get('citation_present')):<6}"
            f"{cell(evaluation.get('citation_document')):<6}"
            f"{cell(evaluation.get('grounded_in_cited_chunk')):<8}"
            f"{cell(evaluation.get('visibility')):<7}"
        )
        print(f"    answer: {evaluation.get('answer', '')}")
        print(
            f"    cited: {evaluation.get('cited_titles')} | "
            f"retrieved: {evaluation.get('retrieval_titles')} | "
            f"leak tokens: {evaluation.get('leak_tokens')}"
        )

    print(
        f"Phase 6.5: {len(CASES)} cases recorded, "
        "0 visibility violations across the run."
    )
