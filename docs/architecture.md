# ShopPilot AI Architecture

## Product

ShopPilot AI is an AI customer-support platform for an e-commerce business.

It supports:

- customer support chat
- knowledge-base question answering
- product lookup
- order lookup
- ticket creation
- refund workflows
- human approval for sensitive actions
- employee/admin operations
- audit logging
- evaluation and analytics

## Core Stack

Frontend:
- Next.js
- TypeScript
- App Router
- Tailwind CSS

Backend:
- FastAPI
- Python
- Pydantic
- psycopg 3
- psycopg_pool
- pytest
- uv

Database / platform:
- Supabase
- PostgreSQL
- Supabase Auth
- Supabase Storage
- pgvector

AI:
- Gemini
- LangGraph

## High-Level Architecture

Browser
    │
    ▼
Next.js
    │
    │ HTTP/SSE
    ▼
FastAPI
    │
    ├── routers
    │
    ├── services
    │
    ├── repositories
    │
    └── PostgreSQL
            │
            └── Supabase PostgreSQL

Supabase also provides:

- Auth
- Storage
- pgvector

## Frontend

apps/web/

Use React Server Components by default.

Client Components are used only for:

- browser APIs
- event handlers
- client state
- effects
- interactive UI

API calls are centralized under:

apps/web/lib/

Docker:

- browser-facing API URL: http://localhost:8000
- server-side container API URL: http://api:8000

## Backend

services/api/

Architecture:

routers
  ↓
services
  ↓
repositories
  ↓
psycopg / PostgreSQL
  ↓
Supabase PostgreSQL

Keep business logic out of routers.

## Database

supabase/migrations/

is the source of truth for schema.

supabase/seed.sql

contains development seed data.

Core domain:

profiles
products
orders
order_items
tickets

Knowledge:

documents
document_chunks

Agent:

conversations
messages
tool_runs
escalations
feedback
audit_logs

pgvector is used for document embeddings.

## Security

Authentication:
Supabase Auth

Authorization:
RBAC + resource ownership + RLS

Roles:

customer
support_agent
admin

Never trust client-supplied:

- user_id
- customer_id
- role
- order_id

Authorization must be enforced by the backend.

RLS must reinforce application-level authorization.

service_role credentials are backend-only.

## AI Architecture

Before the agent:

retrieval → permitted context → Gemini → answer + citations

Agent architecture:

intent_detection
       ↓
router
       ↓
 ┌─────┼─────┬──────┬──────┐
 ▼     ▼     ▼      ▼      ▼
RAG product order ticket refund
                         │
                         ▼
                 deterministic logic
                         │
                    HITL if required

The LLM must not make deterministic business decisions.

Tool calls require:

- validated parameters
- authorization
- ownership checks
- logging

Retrieved documents are untrusted data, not instructions.

## Observability (Phase 6.8)

All observability is in-process, dependency-free, and best-effort: a
metrics failure can never change application behavior. Source of
truth: `app/core/observability.py`.

Logging and correlation
-----------------------

- `CorrelationMiddleware` assigns one server-generated `X-Request-ID`
  per HTTP request and exposes it to every dependency and service
  coroutine via a context variable (pure ASGI middleware, so one
  task and a guaranteed `finally` reset).
- `LOG_FORMAT` (default `text`) selects the logging surface. `json`
  switches every record to structured JSON with an explicit
  structured-field allowlist (request id, event, exception type and
  stage, level, logger name, time). Application logs never carry query
  text, retrieved or generated content, provider messages, URLs, or
  key material — exception types and stages only.

Metrics registry (`ProviderMetrics`)
------------------------------------

- Per-process, in-memory; recording is best-effort (validation and
  runtime failures are swallowed into per-series `recording_failures`
  counts — never raised, never persisted).
- Label vocabularies are bounded at the schema: unknown series, wrong
  label keys, missing labels, or out-of-vocabulary values are rejected
  at recording time, so cardinality cannot grow.
- `snapshot()` is fail-fast: an internal copy failure surfaces as an
  error, never as an empty, misleading snapshot.
- NO database persistence; NO cross-process aggregation.

The 16 registered metric series
-------------------------------

Counters (11):

- `provider_admissions_total`
  `{operation, resource, admission_outcome}`
- `provider_caller_outcomes_total` `{operation, caller_outcome}`
- `provider_worker_outcomes_total`
  `{operation, resource, worker_outcome}`
- `provider_retry_calls_total` `{operation}`
- `provider_retry_attempts_total` `{operation, kind}`
  (only failed attempts that actually trigger a retry)
- `provider_retry_success_total` `{operation, attempts}`
  (only calls recovered after at least one retry)
- `provider_retry_exhausted_total` `{operation, kind, attempts}`
- `rag_requests_total` `{outcome}`
- `ingestion_pipelines_total` `{ingestion_outcome}`
- `ingestion_failure_stages_total` `{stage}`
- `document_lifecycle_outcomes_total`
  `{document_lifecycle_outcome}`

Durations (5, seconds; cumulative bucket boundaries
`0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0` plus a derived
final `+Inf` bucket that always equals the count):

- `provider_admission_wait_seconds` `{operation, resource}`
- `provider_call_seconds` `{operation, caller_outcome}`
- `provider_worker_seconds` `{operation, resource, worker_outcome}`
- `rag_request_seconds` `{outcome}`
- `ingestion_pipeline_seconds` `{ingestion_outcome}`

Label vocabularies
------------------

- `operation`: `query_embedding`, `rag_generation`,
  `ingestion_embedding`, `unattributed`
- `resource`: `embedding`, `generation`, `ingestion_gate`
- `admission_outcome`: `acquired`, `saturated`, `caller_cancelled`,
  `shutdown`, `unexpected` (both timed-out and pending-cap refusals
  are `saturated`; shutdown is always distinct)
- `caller_outcome` (coarse buckets only): `succeeded`,
  `provider_failure`, `caller_cancelled`, `saturated`, `shutdown`,
  `unexpected`
- `worker_outcome`: `succeeded`, `timeout`, `rate_limited`,
  `transient`, `fatal`, `unexpected`, `worker_aborted`, `shutdown`
- retry `kind`/failure families: `timeout`, `rate_limited`,
  `transient`, `fatal`; `attempts`: `1`, `2`, `3`, `4`, `5+`
- `outcome` / `ingestion_outcome`: `succeeded`, `failed`,
  `caller_cancelled`
- `stage`: the seven `ingest_document` failure stages
  (`storage_download`, `text_extraction`, `chunking`,
  `embedding_validation`, `embedding_saturated`,
  `embedding_generation`, `database_insertion`) — never a
  cancellation lifecycle status
- `document_lifecycle_outcome`: `ready`, `failed`, `conflict`,
  `cancelled`, `unexpected`

Measurement boundaries
----------------------

- A provider call is measured across the WHOLE `run_bounded()` call
  (admission + worker), from the caller's entry to the delivered
  result or exception. Admission observations are per-permit: one
  `provider_admissions_total` row AND one admission-wait duration per
  resource the call needs, including instant refusals (wait ≈ 0) and
  instant shutdown refusals.
- A worker observation is taken in the worker task's done callback —
  registered AFTER permit release and BEFORE completion settling —
  so metrics never alter permit accounting, cancellation, or late
  outcome semantics. Worker outcomes use the retry layer's outcome
  vocabulary; a discarded result after a caller abort is
  `worker_aborted`, and only a process shutdown maps to `shutdown`.
  Worker metrics are attributed to the admission's PRIMARY resource
  (the first resource of the call: `ingestion_gate` for ingestion
  batches, `embedding` for query embeddings, `generation` for
  generation calls).
- A RAG request (`rag_requests_total`, `rag_request_seconds`) is the
  WHOLE `answer_query()` envelope: one terminal outcome per request —
  `succeeded` for a returned answer, `failed` for every
  non-cancellation failure (sanitized HTTP errors included),
  `caller_cancelled` for request cancellation.
- An ingestion pipeline (`ingestion_pipelines_total`,
  `ingestion_pipeline_seconds`) is the WHOLE `ingest_document()`
  envelope; a failed pipeline also records its stage (unless the
  exception carries no stage at all).
- A document lifecycle row is a counter only (no duration), recorded
  exactly once at each terminal point of
  `_ingest_registered_document`: `ready`, `failed`, `conflict` (a
  newer terminal status won), `unexpected` (status persistence
  failed), or `cancelled` (never duplicated by the detached
  cancellation marker).

Admin endpoint
--------------

- `GET /admin/metrics` — admin-only through the trusted
  `require_role("admin")` dependency. Returns the aggregate snapshot
  ONLY: bounded label values, counts, sums, extremes, and histogram
  buckets. There are no user IDs, document IDs, request IDs,
  queries/prompts, or provider error messages in the registry, so none
  can appear in the response. Snapshot construction is fail-fast.

Limitations
-----------

- Metrics are per-process only. The API is deployed with a single
  Uvicorn worker; with multiple workers each keeps its own registry
  and there is no aggregation, no exposition format conversion, and no
  persistence.
- Instrumentation targets the local Gemini provider flows (embedding
  and generation). Live-provider behavior of the metrics under real
  Gemini calls has not been exercised in CI; unit tests use fakes.

## Docker

Docker Compose runs:

- web
- api

Supabase local development is provided by the Supabase CLI.

Do not introduce a second PostgreSQL container unless explicitly required.
